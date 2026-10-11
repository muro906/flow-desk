from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # No defaults: a missing secret or connection string should stop the process
    # at startup, not surface as a confusing error on the first request.
    database_url: str
    redis_url: str
    webhook_secret: str
    llm_api_key: str

    llm_model: str = "gemini-3.5-flash"
    prompt_version: str = "v1"
    confidence_threshold: float = 0.70
    max_attempts: int = 5
    retry_base_delay: float = 0.5
    retry_max_delay: float = 30.0
    queue_max_depth: int = 1000
    worker_concurrency: int = 4
    cache_ttl_seconds: int = 86400
    breaker_threshold: int = 5
    breaker_reset_seconds: float = 60.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
