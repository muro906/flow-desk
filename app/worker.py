"""Queue consumer. Run with `python -m app.worker`; scale by running more processes."""

import asyncio
import contextlib
import logging
import os
import signal
import socket
import uuid

from redis.asyncio import Redis

from app import queue
from app.config import get_settings
from app.llm import LLMClient
from app.processor import Deps, process_ticket, requeue_stale

log = logging.getLogger("flowdesk.worker")
SWEEP_LOCK = "flowdesk:sweep-lock"
SWEEP_INTERVAL = 60


class Worker:
    def __init__(self, deps: Deps, worker_id: str | None = None) -> None:
        self.deps = deps
        # Unique per process start, so a restarted worker never adopts a dead one's list.
        self.id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self._stopping = asyncio.Event()

    async def process_next(self, block_seconds: float = queue.BLOCK_SECONDS) -> bool:
        ticket_id = await queue.claim(self.deps.redis, self.id, block_seconds)
        if ticket_id is None:
            return False
        await self._handle(ticket_id)
        return True

    async def _handle(self, ticket_id: int) -> None:
        try:
            await process_ticket(ticket_id, self.deps)
        except Exception:
            # Unexpected failure (e.g. database down): put the job back rather than
            # dropping it, after a pause so a persistent fault doesn't spin.
            log.exception("ticket %s failed unexpectedly; requeueing", ticket_id)
            await asyncio.sleep(1)
            await queue.requeue(self.deps.redis, self.id, ticket_id)
        else:
            # Ack only after the work is done: acking on receipt would drop the
            # ticket whenever a worker dies mid-job.
            await queue.ack(self.deps.redis, self.id, ticket_id)

    async def run(self) -> None:
        await queue.heartbeat(self.deps.redis, self.id)
        maintenance = asyncio.create_task(self._maintain())
        log.info("worker %s started", self.id)
        while not self._stopping.is_set():
            await self.process_next()
        maintenance.cancel()
        log.info("worker %s stopped", self.id)

    def stop(self) -> None:
        self._stopping.set()

    async def _maintain(self) -> None:
        redis = self.deps.redis
        while True:
            await queue.heartbeat(redis, self.id)
            if moved := await queue.reap(redis):
                log.warning("reaped %d orphaned jobs", moved)
            # One sweeper per interval across all workers, or each would re-enqueue
            # the same stale tickets.
            if await redis.set(SWEEP_LOCK, self.id, nx=True, ex=SWEEP_INTERVAL):
                if swept := await requeue_stale(redis):
                    log.warning("re-enqueued %d stale tickets", swept)
            await asyncio.sleep(queue.HEARTBEAT_TTL / 3)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    # The socket timeout must outlast BLMOVE's server-side block, or every idle
    # claim surfaces as a client timeout.
    redis = Redis.from_url(
        settings.redis_url, decode_responses=True, socket_timeout=queue.BLOCK_SECONDS * 2
    )
    worker = Worker(Deps(settings=settings, redis=redis, llm=LLMClient()))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.stop)
    try:
        await worker.run()
    finally:
        with contextlib.suppress(Exception):
            await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
