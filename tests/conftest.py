import os
import socket
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.core.security import hash_api_key
from app.main import app

ROOT = Path(__file__).resolve().parent.parent

# Separate from DATABASE_URL so tests never touch the dev database.
# The default is the docker compose Postgres as seen from the host.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://hookline:hookline@localhost:5433/hookline_test",
)


def alembic_config(connection: Connection) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    # Picked up by alembic/env.py instead of DATABASE_URL.
    config.attributes["connection"] = connection
    return config


def _create_database_if_missing(url: str) -> None:
    db_url = make_url(url)
    # SKIP LOCKED, ON CONFLICT, JSONB and arrays are what these tests check; SQLite has none.
    if db_url.get_backend_name() != "postgresql":
        raise RuntimeError(f"TEST_DATABASE_URL must be a Postgres URL, got {db_url.drivername!r}")
    # Guard: migrations get rolled back and forth here, so never point this at a real database.
    if not db_url.database or not db_url.database.endswith("_test"):
        raise RuntimeError(
            f"TEST_DATABASE_URL must name a *_test database, got {db_url.database!r}"
        )

    admin = create_engine(db_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            exists = connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": db_url.database},
            )
            if not exists:
                connection.execute(text(f'CREATE DATABASE "{db_url.database}"'))
    except OperationalError as exc:
        raise RuntimeError(
            f"Can't reach the test Postgres at {db_url.render_as_string(hide_password=True)}. "
            "Start it with `docker compose up -d postgres` or set TEST_DATABASE_URL."
        ) from exc
    finally:
        admin.dispose()


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """Test database migrated to head once per run: the schema always comes from migrations."""
    _create_database_if_missing(TEST_DATABASE_URL)
    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as connection:
        command.upgrade(alembic_config(connection), "head")
    yield engine
    engine.dispose()


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    """A session whose changes are rolled back after the test, so tests never see each other."""
    with engine.connect() as connection:
        transaction = connection.begin()
        session = Session(bind=connection, join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            session.close()
            transaction.rollback()


TEST_API_KEY = "hk_test_key"


def _api_client(get_test_db: Callable[[], Iterator[Session]]) -> Iterator[TestClient]:
    """API client sending a valid API key, with sessions from get_test_db."""
    settings = Settings(_env_file=None, api_key_hash=hash_api_key(TEST_API_KEY))
    app.dependency_overrides[get_db] = get_test_db
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        yield TestClient(app, headers={"Authorization": f"Bearer {TEST_API_KEY}"})
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def client(db: Session) -> Iterator[TestClient]:
    """API client using the rolled-back test session."""

    def get_test_db() -> Iterator[Session]:
        # Like get_db closing its session: whatever a request didn't commit is discarded.
        try:
            yield db
        finally:
            db.rollback()

    yield from _api_client(get_test_db)


@pytest.fixture
def committed_client(engine: Engine) -> Iterator[TestClient]:
    """API client whose requests each get their own session and really commit.

    For tests where requests run at the same time and must see each other's transactions.
    Nothing is rolled back: the test deletes what it created.
    """
    make_session = sessionmaker(bind=engine)

    def get_test_db() -> Iterator[Session]:
        with make_session() as session:
            yield session

    yield from _api_client(get_test_db)


@pytest.fixture(autouse=True)
def dns(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Fake DNS so no test depends on the network. Add names to the returned dict.

    Unknown names fail to resolve; IP literals (including forms like 2130706433) still
    resolve, through getaddrinfo in numeric-only mode.
    """
    records = {"example.com": ["93.184.215.14"], "localhost": ["127.0.0.1", "::1"]}
    real_getaddrinfo = socket.getaddrinfo
    # psycopg resolves the database host through socket.getaddrinfo too; leave it real.
    database_host = make_url(TEST_DATABASE_URL).host

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host == database_host:
            return real_getaddrinfo(host, port, *args, **kwargs)
        if host in records:
            return [
                (
                    socket.AF_INET6 if ":" in ip else socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    (ip, port),
                )
                for ip in records[host]
            ]
        return real_getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    return records


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test that needs Postgres, so `pytest -m "not integration"` runs without it."""
    for item in items:
        if "engine" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.integration)
