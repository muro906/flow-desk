from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import SessionLocal
from app.llm import ROUTES, LLMClient
from app.models import Classification, Ticket, TicketStatus

ACTIVE = (TicketStatus.queued, TicketStatus.processing)


@dataclass
class Deps:
    settings: Settings
    redis: Redis
    llm: LLMClient


async def process_ticket(ticket_id: int, deps: Deps) -> None:
    async with SessionLocal() as session:
        ticket = await _claim(session, ticket_id)
        if ticket is None:
            return

        ticket.attempts += 1
        await session.commit()
        decision = await deps.llm.classify(ticket.subject, ticket.body)
        # A redelivered job may get here twice; the first classification stands.
        await session.execute(
            insert(Classification)
            .values(
                ticket_id=ticket.id,
                category=decision.category,
                summary=decision.summary,
                confidence=decision.confidence,
                routed_to=ROUTES[decision.category],
                decided_by="llm",
                prompt_version=deps.settings.prompt_version,
            )
            .on_conflict_do_nothing(index_elements=[Classification.ticket_id])
        )
        await _finish(
            session,
            ticket.id,
            TicketStatus.routed
            if decision.confidence >= deps.settings.confidence_threshold
            else TicketStatus.awaiting_review,
        )


async def _claim(session: AsyncSession, ticket_id: int) -> Ticket | None:
    # A redelivered job finds its ticket already resolved and stops here.
    ticket = await session.scalar(
        update(Ticket)
        .where(Ticket.id == ticket_id, Ticket.status.in_(ACTIVE))
        .values(status=TicketStatus.processing)
        .returning(Ticket)
    )
    await session.commit()
    return ticket


async def _finish(session: AsyncSession, ticket_id: int, status: TicketStatus, **values) -> None:
    # Guarded so a late duplicate worker can never overwrite a reviewer's decision.
    await session.execute(
        update(Ticket)
        .where(Ticket.id == ticket_id, Ticket.status.in_(ACTIVE))
        .values(status=status, **values)
    )
    await session.commit()
