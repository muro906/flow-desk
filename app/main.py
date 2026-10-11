import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app import queue
from app.config import get_settings
from app.db import get_session
from app.models import DeadLetter, Ticket, TicketStatus
from app.processor import replay_dead_letter
from app.schemas import DeadLetterOut, Queued, TicketIn, TicketOut, WebhookAccepted

RETRY_AFTER_SECONDS = 30


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
    responses={503: {"description": "Queue full; retry after the Retry-After header"}},
)
async def receive_ticket(
    payload: TicketIn, session: Session, redis: RedisDep, response: Response
) -> WebhookAccepted:
    # Shed load rather than queue without bound: senders retry on 503, so the backlog
    # waits upstream instead of growing Redis memory and every ticket's latency.
    if await queue.depth(redis) >= get_settings().queue_max_depth:
        raise HTTPException(
            status_code=503,
            detail="queue full, retry later",
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        )

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


@app.get("/dead-letters", tags=["dead letters"])
async def list_dead_letters(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[DeadLetterOut]:
    query = select(DeadLetter).order_by(DeadLetter.created_at.desc()).limit(limit).offset(offset)
    return list(await session.scalars(query))


@app.post("/dead-letters/{dead_letter_id}/replay", status_code=202, tags=["dead letters"])
async def replay(
    dead_letter_id: int, session: Session, redis: RedisDep, response: Response
) -> Queued:
    try:
        ticket_id = await replay_dead_letter(session, redis, dead_letter_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="dead letter not found") from None
    response.headers["Location"] = f"/tickets/{ticket_id}"
    return Queued(ticket_id=ticket_id, status=TicketStatus.queued, location=f"/tickets/{ticket_id}")


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz", tags=["ops"])
async def readyz(session: Session, redis: RedisDep) -> JSONResponse:
    try:
        await session.execute(text("SELECT 1"))
        depth = await queue.depth(redis)
    except (SQLAlchemyError, RedisError, OSError) as exc:
        return JSONResponse({"status": "unavailable", "error": str(exc)}, status_code=503)
    return JSONResponse({"status": "ready", "queue_depth": depth})


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
