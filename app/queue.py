"""Redis work queue with at-least-once delivery.

A claim moves a ticket id from the ready list into the worker's own processing list
(BLMOVE) and it is removed only once the work is finished (ack). A worker that dies
mid-job leaves the id behind, and the reaper returns it once the worker's heartbeat
expires. Nothing is lost, but a ticket can be processed twice, so every downstream
write must be idempotent.
"""

from redis.asyncio import Redis

READY = "flowdesk:ready"
PROCESSING = "flowdesk:processing:"
HEARTBEAT = "flowdesk:heartbeat:"
HEARTBEAT_TTL = 30
BLOCK_SECONDS = 5


async def enqueue(redis: Redis, ticket_id: int) -> None:
    await redis.lpush(READY, ticket_id)


async def depth(redis: Redis) -> int:
    return await redis.llen(READY)


async def claim(redis: Redis, worker_id: str, block_seconds: float = BLOCK_SECONDS) -> int | None:
    # Not BRPOP: that removes the job the moment it is taken, so a worker killed
    # mid-job would lose the ticket without a trace.
    raw = await redis.blmove(READY, PROCESSING + worker_id, block_seconds, "RIGHT", "LEFT")
    return int(raw) if raw is not None else None


async def ack(redis: Redis, worker_id: str, ticket_id: int) -> None:
    await redis.lrem(PROCESSING + worker_id, 1, ticket_id)


async def requeue(redis: Redis, worker_id: str, ticket_id: int) -> None:
    async with redis.pipeline(transaction=True) as pipe:
        pipe.lrem(PROCESSING + worker_id, 1, ticket_id)
        pipe.lpush(READY, ticket_id)
        await pipe.execute()


async def heartbeat(redis: Redis, worker_id: str) -> None:
    await redis.set(HEARTBEAT + worker_id, 1, ex=HEARTBEAT_TTL)


async def reap(redis: Redis) -> int:
    """Return jobs held by workers whose heartbeat has expired to the ready list."""
    moved = 0
    async for key in redis.scan_iter(match=PROCESSING + "*"):
        if await redis.exists(HEARTBEAT + key.removeprefix(PROCESSING)):
            continue
        while await redis.lmove(key, READY, "RIGHT", "LEFT") is not None:
            moved += 1
    return moved
