import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.core.security import hash_api_key
from app.main import app


def test_settings_read_from_environment(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db:5432/x")
    monkeypatch.setenv("REDIS_URL", "redis://cache:6379/1")

    settings = Settings(_env_file=None)

    assert settings.environment == "test"
    assert settings.debug is True
    assert settings.database_url == "postgresql+psycopg://u:p@db:5432/x"
    assert settings.redis_url == "redis://cache:6379/1"


def test_broker_url_defaults_to_redis_url(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://cache:6379/1")
    monkeypatch.delenv("CELERY_BROKER_URL", raising=False)

    assert Settings(_env_file=None).broker_url == "redis://cache:6379/1"


def test_broker_url_can_be_overridden(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://cache:6379/1")
    monkeypatch.setenv("CELERY_BROKER_URL", "redis://broker:6379/2")

    assert Settings(_env_file=None).broker_url == "redis://broker:6379/2"


@pytest.mark.parametrize(
    ("value", "hosts"),
    [
        ("", []),
        ("receiver:9000", ["receiver:9000"]),
        (" Receiver:9000 , mock ,", ["receiver:9000", "mock"]),
    ],
)
def test_allowed_internal_hosts_are_comma_separated(monkeypatch, value: str, hosts: list[str]):
    monkeypatch.setenv("ALLOWED_INTERNAL_HOSTS", value)

    assert Settings(_env_file=None).allowed_internal_hosts == hosts


def test_no_internal_hosts_are_allowed_by_default(monkeypatch):
    monkeypatch.delenv("ALLOWED_INTERNAL_HOSTS", raising=False)

    assert Settings(_env_file=None).allowed_internal_hosts == []


def test_app_serves_docs():
    response = TestClient(app).get("/docs")

    assert response.status_code == 200


PRODUCTION_KEY_HASH = hash_api_key("hk_production_key")


def production_env(monkeypatch, **overrides: str) -> None:
    env = {
        "ENVIRONMENT": "production",
        "DEBUG": "false",
        "API_KEY_HASH": PRODUCTION_KEY_HASH,
        "ALLOWED_INTERNAL_HOSTS": "",
    }
    env.update(overrides)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def test_production_settings_are_accepted(monkeypatch):
    production_env(monkeypatch)

    settings = Settings(_env_file=None)

    assert settings.environment == "production"
    assert settings.api_key_hash == PRODUCTION_KEY_HASH


def test_production_refuses_debug(monkeypatch):
    production_env(monkeypatch, DEBUG="true")

    with pytest.raises(ValidationError, match="DEBUG must be false"):
        Settings(_env_file=None)


def test_production_refuses_missing_api_key_hash(monkeypatch):
    production_env(monkeypatch)
    monkeypatch.delenv("API_KEY_HASH")

    with pytest.raises(ValidationError, match="API_KEY_HASH must be set"):
        Settings(_env_file=None)


def test_production_refuses_local_dev_api_key_hash(monkeypatch):
    production_env(monkeypatch, API_KEY_HASH=hash_api_key("hk_local_dev_key"))

    with pytest.raises(ValidationError, match="local dev key"):
        Settings(_env_file=None)


def test_production_refuses_allowed_internal_hosts(monkeypatch):
    production_env(monkeypatch, ALLOWED_INTERNAL_HOSTS="receiver:9000")

    with pytest.raises(ValidationError, match="ALLOWED_INTERNAL_HOSTS must be empty"):
        Settings(_env_file=None)


def test_production_reports_every_problem_at_once(monkeypatch):
    production_env(monkeypatch, DEBUG="true", ALLOWED_INTERNAL_HOSTS="receiver:9000")
    monkeypatch.delenv("API_KEY_HASH")

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)

    message = str(exc_info.value)
    assert "DEBUG" in message
    assert "API_KEY_HASH" in message
    assert "ALLOWED_INTERNAL_HOSTS" in message


def test_local_settings_allow_debug_and_the_dev_key(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.setenv("API_KEY_HASH", hash_api_key("hk_local_dev_key"))
    monkeypatch.setenv("ALLOWED_INTERNAL_HOSTS", "receiver:9000")

    assert Settings(_env_file=None).debug is True
