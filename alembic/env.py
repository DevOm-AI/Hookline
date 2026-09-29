from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, create_engine, pool

from app.core.config import get_settings
from app.models import Base

config = context.config

if config.config_file_name is not None:
    # Keep loggers created before this (app, pytest) working when tests run migrations.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Single source for the URL: the DATABASE_URL setting, never alembic.ini.
database_url = get_settings().database_url
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it (alembic upgrade --sql)."""
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Tests pass their own connection so migrations run against the test database.
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_migrations(connection)
        return

    connectable = create_engine(database_url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        _run_migrations(connection)


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
