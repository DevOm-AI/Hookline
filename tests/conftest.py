import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

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
