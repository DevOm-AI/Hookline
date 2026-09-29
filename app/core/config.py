from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.core.security import hash_api_key

# The key .env.example ships with, for local use only; see the README's "API key".
LOCAL_DEV_API_KEY_HASH = hash_api_key("hk_local_dev_key")


class Settings(BaseSettings):
    """App settings, read from environment variables (and .env as a fallback)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: Literal["local", "test", "production"] = "local"
    # Loosens local-only checks, e.g. allowing localhost endpoint URLs.
    debug: bool = False
    # Endpoint hosts allowed to resolve to private addresses, comma-separated: "host:port" for
    # one port (e.g. "receiver:9000", the docker compose mock receiver), "host" for any port.
    # Exact names only, and only ones your own DNS answers for: their addresses aren't checked.
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

    @model_validator(mode="after")
    def _check_production(self) -> "Settings":
        # Refuse to start rather than run production with local-only settings.
        if self.environment != "production":
            return self
        problems = []
        if self.debug:
            problems.append("DEBUG must be false (it allows localhost endpoint URLs)")
        if self.api_key_hash is None:
            problems.append("API_KEY_HASH must be set (generate one: python -m app.core.security)")
        elif self.api_key_hash == LOCAL_DEV_API_KEY_HASH:
            problems.append("API_KEY_HASH is the public local dev key's; generate a new one")
        if self.allowed_internal_hosts:
            problems.append(
                "ALLOWED_INTERNAL_HOSTS must be empty (it lets endpoints reach private hosts)"
            )
        if problems:
            raise ValueError("Unsafe settings for ENVIRONMENT=production: " + "; ".join(problems))
        return self

    @property
    def broker_url(self) -> str:
        return self.celery_broker_url or self.redis_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
