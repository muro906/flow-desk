import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import queue
from app.config import get_settings
from app.db import get_session
from app.models import Ticket, TicketStatus
from app.schemas import TicketIn, TicketOut, WebhookAccepted


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    yield
    await app.state.redis.aclose()


app = FastAPI(
    title="Flowdesk", description="LLM ticket triage with human review.", lifespan=lifespan
)


def get_redis(request: Request) -> Redis:
    return request.app.state.redis


Session = Annotated[AsyncSession, Depends(get_session)]
RedisDep = Annotated[Redis, Depends(get_redis)]


def verify_secret(x_webhook_secret: Annotated[str, Header()]) -> None:
    # Constant-time comparison so the secret can't be recovered by timing responses.
    if not hmac.compare_digest(x_webhook_secret, get_settings().webhook_secret):
        raise HTTPException(status_code=401, detail="invalid webhook secret")


@app.post(
    "/webhooks/ticket",
    status_code=202,
    tags=["webhooks"],
    dependencies=[Depends(verify_secret)],
)
async def receive_ticket(
    payload: TicketIn, session: Session, redis: RedisDep, response: Response
) -> WebhookAccepted:
    # ON CONFLICT rather than check-then-insert: two concurrent deliveries of the same
    # ticket would both pass the check; only one can win the unique constraint.
    ticket_id = await session.scalar(
        insert(Ticket)
        .values(**payload.model_dump(), status=TicketStatus.queued)
        .on_conflict_do_nothing(index_elements=[Ticket.external_id])
        .returning(Ticket.id)
    )
    if ticket_id is None:
        existing = await session.scalar(
            select(Ticket).where(Ticket.external_id == payload.external_id)
        )
        return _accepted(response, existing.id, existing.status, duplicate=True)

    # Commit before enqueueing: a fast worker must never pop an id it can't see yet.
    await session.commit()
    await queue.enqueue(redis, ticket_id)
    return _accepted(response, ticket_id, TicketStatus.queued, duplicate=False)


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


def _accepted(
    response: Response, ticket_id: int, status: TicketStatus, duplicate: bool
) -> WebhookAccepted:
    location = f"/tickets/{ticket_id}"
    response.headers["Location"] = location
    return WebhookAccepted(
        ticket_id=ticket_id, status=status, location=location, duplicate=duplicate
    )


async def _load(session: AsyncSession, ticket_id: int) -> Ticket:
    ticket = await session.get(Ticket, ticket_id, populate_existing=True)
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    return ticket
