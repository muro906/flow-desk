"""Each test proves one design decision rather than that the framework works."""

import asyncio

from sqlalchemy import func, select

from app import queue
from app.llm import PermanentLLMError, TransientLLMError
from app.models import Classification, DeadLetter, Ticket, TicketStatus
from app.retry import backoff
from tests.conftest import FakeLLM, post_ticket


async def count(session_factory, model) -> int:
    async with session_factory() as session:
        return await session.scalar(select(func.count()).select_from(model))


async def get_ticket(session_factory, ticket_id) -> Ticket:
    async with session_factory() as session:
        return await session.get(Ticket, ticket_id)


async def test_idempotent_repeat(client, redis, session_factory):
    """Five deliveries of one external_id make one ticket and one job."""
    responses = await asyncio.gather(*(post_ticket(client) for _ in range(5)))

    assert {r.status_code for r in responses} == {202}
    assert sorted(r.json()["duplicate"] for r in responses) == [False, True, True, True, True]
    assert len({r.json()["ticket_id"] for r in responses}) == 1
    assert await count(session_factory, Ticket) == 1
    assert await queue.depth(redis) == 1


async def test_retry_then_succeed(client, make_worker, session_factory):
    """Transient 503s are retried; the third attempt succeeds."""
    llm = FakeLLM(errors=[TransientLLMError("503"), TransientLLMError("503")])
    ticket_id = (await post_ticket(client)).json()["ticket_id"]

    assert await make_worker(llm).process_next(block_seconds=1)

    ticket = await get_ticket(session_factory, ticket_id)
    assert ticket.attempts == 3
    assert ticket.status == TicketStatus.routed
    assert await count(session_factory, Classification) == 1


async def test_exhausted_retries_dead_letter(client, make_worker, session_factory, settings):
    """A provider that always fails ends in dead_letters, visibly, and can be replayed."""
    llm = FakeLLM(errors=[TransientLLMError("500")] * 10)
    ticket_id = (await post_ticket(client)).json()["ticket_id"]

    await make_worker(llm).process_next(block_seconds=1)

    ticket = await get_ticket(session_factory, ticket_id)
    assert ticket.status == TicketStatus.failed
    assert ticket.attempts == settings.max_attempts
    dead = (await client.get("/dead-letters")).json()
    assert [d["ticket_id"] for d in dead] == [ticket_id]

    replay = await client.post(f"/dead-letters/{dead[0]['id']}/replay")
    assert replay.status_code == 202
    assert (await get_ticket(session_factory, ticket_id)).status == TicketStatus.queued
    assert await count(session_factory, DeadLetter) == 0


async def test_low_confidence_routes_to_review(client, make_worker):
    """Below the threshold the flow escalates; a reviewer's approval routes the ticket."""
    ticket_id = (await post_ticket(client)).json()["ticket_id"]
    await make_worker(FakeLLM(confidence=0.4)).process_next(block_seconds=1)

    ticket = (await client.get(f"/tickets/{ticket_id}")).json()
    assert ticket["status"] == "awaiting_review"
    assert ticket["classification"]["routed_to"] == "billing-team"  # suggestion for the reviewer

    approved = await client.post(
        f"/tickets/{ticket_id}/approve", json={"routed_to": "customer-success"}
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "routed"
    assert approved.json()["classification"]["routed_to"] == "customer-success"
    assert (await client.post(f"/tickets/{ticket_id}/reject")).status_code == 409


async def test_permanent_error_is_not_retried(client, make_worker, session_factory):
    """A 400 would fail identically every time, so it is dead-lettered on the first attempt."""
    llm = FakeLLM(errors=[PermanentLLMError("400: bad request")])
    ticket_id = (await post_ticket(client)).json()["ticket_id"]

    await make_worker(llm).process_next(block_seconds=1)

    ticket = await get_ticket(session_factory, ticket_id)
    assert (ticket.status, ticket.attempts, llm.calls) == (TicketStatus.failed, 1, 1)


async def test_crashed_worker_job_is_recovered(client, redis, make_worker, session_factory):
    """BLMOVE leaves a dead worker's job in its processing list; the reaper returns it."""
    ticket_id = (await post_ticket(client)).json()["ticket_id"]
    assert await queue.claim(redis, "crashed-worker", block_seconds=1) == ticket_id
    assert await queue.depth(redis) == 0  # claimed and never acked; no heartbeat exists

    assert await queue.reap(redis) == 1
    await make_worker(FakeLLM()).process_next(block_seconds=1)

    assert (await get_ticket(session_factory, ticket_id)).status == TicketStatus.routed


async def test_full_queue_sheds_load(client, settings, monkeypatch):
    """Past QUEUE_MAX_DEPTH the API answers 503 with Retry-After instead of queueing."""
    monkeypatch.setattr(settings, "queue_max_depth", 1)

    assert (await post_ticket(client, "a")).status_code == 202
    rejected = await post_ticket(client, "b")

    assert rejected.status_code == 503
    assert int(rejected.headers["Retry-After"]) > 0


async def test_cache_hit_skips_llm_until_prompt_version_changes(
    client, make_worker, session_factory, settings, monkeypatch
):
    """Identical tickets reuse a decision; bumping PROMPT_VERSION invalidates it."""
    llm = FakeLLM()
    worker = make_worker(llm)
    await post_ticket(client, "a", body="Refund   please")
    await post_ticket(client, "b", body="refund please")
    await worker.process_next(block_seconds=1)
    await worker.process_next(block_seconds=1)
    assert llm.calls == 1

    monkeypatch.setattr(settings, "prompt_version", "v2")
    await post_ticket(client, "c", body="refund please")
    await worker.process_next(block_seconds=1)
    assert llm.calls == 2

    async with session_factory() as session:
        decided = list(await session.scalars(select(Classification.decided_by).order_by("id")))
    assert decided == ["llm", "cache", "llm"]


def test_backoff_uses_full_jitter():
    """Delays spread across [0, ceiling] instead of clustering at the ceiling."""
    delays = [backoff(3, base=0.5, cap=30.0) for _ in range(1000)]
    assert all(0 <= d <= 4.0 for d in delays)
    assert min(delays) < 0.5 and max(delays) > 3.5
    assert all(backoff(20, base=0.5, cap=30.0) <= 30.0 for _ in range(100))
