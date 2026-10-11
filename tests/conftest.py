"""Tests run against real Postgres and Redis (the Compose services), using a separate
`flowdesk_test` database and Redis DB 15 so they never touch development data.
The LLM is faked at the client boundary, never at the network layer."""

import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class _HostPorts(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    postgres_port: int = 5432
    redis_port: int = 6379


_ports = _HostPorts()
os.environ.update(
    DATABASE_URL=os.environ.get(
        "TEST_DATABASE_URL",
        f"postgresql+asyncpg://flowdesk:flowdesk@localhost:{_ports.postgres_port}/flowdesk_test",
    ),
    REDIS_URL=os.environ.get("TEST_REDIS_URL", f"redis://localhost:{_ports.redis_port}/15"),
    WEBHOOK_SECRET="test-secret",
    LLM_API_KEY="test-key",
    RETRY_BASE_DELAY="0.001",
    RETRY_MAX_DELAY="0.005",
)

import httpx  # noqa: E402
import pytest  # noqa: E402
from redis.asyncio import Redis  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import SessionLocal, engine  # noqa: E402
from app.llm import Decision  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402
from app.processor import Deps  # noqa: E402
from app.worker import Worker  # noqa: E402

SECRET_HEADER = {"X-Webhook-Secret": "test-secret"}


class FakeLLM:
    """Stands in for LLMClient: scripted errors first, then a fixed decision."""

    def __init__(self, *, category="billing", confidence=0.9, errors=()):
        self.category = category
        self.confidence = confidence
        self.errors = list(errors)
        self.calls = 0

    async def classify(self, subject, body):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return Decision(
            category=self.category,
            summary="Customer was charged twice.",
            confidence=self.confidence,
        )


@pytest.fixture(scope="session", autouse=True)
async def database():
    url = make_url(os.environ["DATABASE_URL"])
    admin = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        exists = await conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": url.database}
        )
        if not exists:
            await conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    await admin.dispose()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


@pytest.fixture
async def redis():
    client = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture(autouse=True)
async def clean(redis):
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE tickets, classifications, dead_letters RESTART IDENTITY CASCADE")
        )
    await redis.flushdb()


@pytest.fixture
async def client():
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def make_worker(redis, settings):
    def build(llm, worker_id="test-worker"):
        return Worker(Deps(settings=settings, redis=redis, llm=llm), worker_id=worker_id)

    return build


@pytest.fixture
def session_factory():
    return SessionLocal


async def post_ticket(client, external_id="zendesk-1", body="I was billed twice for March."):
    return await client.post(
        "/webhooks/ticket",
        headers=SECRET_HEADER,
        json={"external_id": external_id, "body": body, "subject": "Billing", "source": "test"},
    )
