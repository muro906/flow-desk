from redis.asyncio import Redis

from app.config import Settings

FAILURES = "flowdesk:breaker:failures"
OPEN = "flowdesk:breaker:open"


class CircuitBreaker:
    """Stops workers dialling a provider that is down.

    State lives in Redis rather than in each process, so N workers share one opinion
    about the provider instead of each discovering the outage separately.
    """

    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings

    async def is_open(self) -> bool:
        return bool(await self._redis.exists(OPEN))

    async def record_success(self) -> None:
        await self._redis.delete(FAILURES)

    async def record_failure(self) -> None:
        # The count is deliberately not reset when the breaker opens. Once OPEN
        # expires, the next call is a trial (half-open): success clears the count,
        # failure takes it straight back over the threshold and reopens.
        if await self._redis.incr(FAILURES) >= self._settings.breaker_threshold:
            await self._redis.set(OPEN, 1, px=int(self._settings.breaker_reset_seconds * 1000))
