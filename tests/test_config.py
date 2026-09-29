import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
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
    [("", []), ("receiver", ["receiver"]), (" Receiver , mock ,", ["receiver", "mock"])],
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
