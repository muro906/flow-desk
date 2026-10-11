import hashlib

from redis.asyncio import Redis

from app.config import Settings
from app.llm import Decision


class DecisionCache:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings

    def key(self, subject: str | None, body: str) -> str:
        normalised = " ".join(f"{subject or ''}\n{body}".split()).casefold()
        digest = hashlib.sha256(normalised.encode()).hexdigest()
        # Prompt version and model are part of the key: without them, changing the
        # prompt would keep serving decisions the old prompt made, and routing would
        # drift with no visible cause.
        return f"flowdesk:cache:{self._settings.prompt_version}:{self._settings.llm_model}:{digest}"

    async def get(self, subject: str | None, body: str) -> Decision | None:
        raw = await self._redis.get(self.key(subject, body))
        return Decision.model_validate_json(raw) if raw else None

    async def put(self, subject: str | None, body: str, decision: Decision) -> None:
        await self._redis.set(
            self.key(subject, body),
            decision.model_dump_json(),
            ex=self._settings.cache_ttl_seconds,
        )
