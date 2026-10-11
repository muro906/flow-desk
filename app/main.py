import hmac
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.llm import ROUTES, LLMClient
from app.models import Classification, Ticket, TicketStatus
from app.schemas import TicketIn, TicketOut

app = FastAPI(title="Flowdesk", description="LLM ticket triage with human review.")
llm = LLMClient()

Session = Annotated[AsyncSession, Depends(get_session)]


def verify_secret(x_webhook_secret: Annotated[str, Header()]) -> None:
    # Constant-time comparison so the secret can't be recovered by timing responses.
    if not hmac.compare_digest(x_webhook_secret, get_settings().webhook_secret):
        raise HTTPException(status_code=401, detail="invalid webhook secret")


@app.post("/webhooks/ticket", tags=["webhooks"], dependencies=[Depends(verify_secret)])
async def receive_ticket(payload: TicketIn, session: Session) -> TicketOut:
    # Stage 2 (naive): the LLM call happens inside the request, so the caller waits
    # for the whole round-trip and a slow provider stalls every webhook sender.
    ticket = Ticket(**payload.model_dump(), status=TicketStatus.processing, attempts=1)
    session.add(ticket)
    await session.flush()

    decision = await llm.classify(ticket.subject, ticket.body)
    settings = get_settings()
    session.add(
        Classification(
            ticket_id=ticket.id,
            category=decision.category,
            summary=decision.summary,
            confidence=decision.confidence,
            routed_to=ROUTES[decision.category],
            decided_by="llm",
            prompt_version=settings.prompt_version,
        )
    )
    ticket.status = (
        TicketStatus.routed
        if decision.confidence >= settings.confidence_threshold
        else TicketStatus.awaiting_review
    )
    await session.commit()
    return await _load(session, ticket.id)


@app.get("/tickets/{ticket_id}", tags=["tickets"])
async def get_ticket(ticket_id: int, session: Session) -> TicketOut:
    return await _load(session, ticket_id)


@app.get("/tickets", tags=["tickets"])
async def list_tickets(
    session: Session,
    status: TicketStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[TicketOut]:
    query = select(Ticket).order_by(Ticket.created_at.desc()).limit(limit).offset(offset)
    if status:
        query = query.where(Ticket.status == status)
    return list(await session.scalars(query))


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


async def _load(session: AsyncSession, ticket_id: int) -> Ticket:
    ticket = await session.get(Ticket, ticket_id, populate_existing=True)
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    return ticket
