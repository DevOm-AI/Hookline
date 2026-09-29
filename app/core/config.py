from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """App settings, read from environment variables (and .env as a fallback)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: Literal["local", "test", "production"] = "local"
    # Loosens local-only checks, e.g. allowing localhost endpoint URLs.
    debug: bool = False
    # Endpoint hostnames allowed to resolve to private addresses, comma-separated: e.g.
    # "receiver", the docker compose mock receiver. Exact names only; only hosts you run.
    allowed_internal_hosts: Annotated[list[str], NoDecode] = []

    database_url: str = "postgresql+psycopg://hookline:hookline@localhost:5433/hookline"
    redis_url: str = "redis://localhost:6380/0"
    # Falls back to redis_url when unset.
    celery_broker_url: str | None = None

    # SHA-256 hex digest of the API key; the key itself is never stored.
    # Generate a pair with `uv run python -m app.core.security`. Unset = every API call is 401.
    api_key_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("allowed_internal_hosts", mode="before")
    @classmethod
    def _split_hosts(cls, value: object) -> object:
        if isinstance(value, str):
            return [host.strip().lower() for host in value.split(",") if host.strip()]
        return value

    @property
    def broker_url(self) -> str:
        return self.celery_broker_url or self.redis_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
