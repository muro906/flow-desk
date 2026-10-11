import asyncio
from dataclasses import dataclass
from datetime import timedelta

from redis.asyncio import Redis
from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import queue
from app.config import Settings
from app.db import SessionLocal
from app.llm import ROUTES, Decision, LLMClient, PermanentLLMError, TransientLLMError
from app.models import Classification, DeadLetter, Ticket, TicketStatus
from app.retry import backoff

ACTIVE = (TicketStatus.queued, TicketStatus.processing)
STALE_AFTER = timedelta(minutes=15)


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
        decision = await _classify_with_retries(session, ticket, deps)
        if decision is None:
            return

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
            last_error=None,
        )


async def _classify_with_retries(
    session: AsyncSession, ticket: Ticket, deps: Deps
) -> Decision | None:
    """Return the decision, or None once the ticket has been dead-lettered."""
    settings = deps.settings
    while True:
        ticket.attempts += 1
        await session.commit()
        try:
            return await deps.llm.classify(ticket.subject, ticket.body)
        except PermanentLLMError as exc:
            # Retrying would spend the whole budget on a request that cannot succeed.
            await _dead_letter(session, ticket, str(exc))
            return None
        except TransientLLMError as exc:
            if ticket.attempts >= settings.max_attempts:
                await _dead_letter(session, ticket, str(exc))
                return None
            ticket.last_error = str(exc)
            await session.commit()
            # The job stays in this worker's processing list while it waits, so a
            # crash here is recovered by the reaper rather than lost.
            await asyncio.sleep(
                backoff(ticket.attempts, settings.retry_base_delay, settings.retry_max_delay)
            )


async def _dead_letter(session: AsyncSession, ticket: Ticket, error: str) -> None:
    session.add(DeadLetter(ticket_id=ticket.id, attempts=ticket.attempts, error=error))
    await _finish(session, ticket.id, TicketStatus.failed, last_error=error)


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


async def requeue_stale(redis: Redis) -> int:
    """Re-enqueue tickets that have sat untouched in an active state for too long.

    Covers the gap between the webhook's database commit and its Redis push: if the
    API dies in between, the ticket exists but no job does. Processing is idempotent,
    so re-enqueueing a ticket that was merely slow costs one wasted claim.
    """
    async with SessionLocal() as session:
        ids = list(
            await session.scalars(
                update(Ticket)
                .where(
                    Ticket.status.in_(ACTIVE),
                    Ticket.updated_at < func.now() - STALE_AFTER,
                )
                .values(updated_at=func.now())
                .returning(Ticket.id)
            )
        )
        await session.commit()
    for ticket_id in ids:
        await queue.enqueue(redis, ticket_id)
    return len(ids)


async def replay_dead_letter(session: AsyncSession, redis: Redis, dead_letter_id: int) -> int:
    dead_letter = await session.get(DeadLetter, dead_letter_id)
    if dead_letter is None:
        raise LookupError(dead_letter_id)
    ticket_id = dead_letter.ticket_id
    await session.execute(
        update(Ticket)
        .where(Ticket.id == ticket_id)
        .values(status=TicketStatus.queued, attempts=0, last_error=None)
    )
    await session.delete(dead_letter)
    await session.commit()
    await queue.enqueue(redis, ticket_id)
    return ticket_id
