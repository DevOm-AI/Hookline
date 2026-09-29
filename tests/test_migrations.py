from alembic import command
from sqlalchemy import Engine, inspect

from tests.conftest import alembic_config


def test_migrations_downgrade_and_upgrade_cleanly(engine: Engine):
    # Postgres DDL is transactional: roll back so the shared test schema stays at head.
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            config = alembic_config(connection)
            command.downgrade(config, "base")
            assert "deliveries" not in inspect(connection).get_table_names()
            command.upgrade(config, "head")
            assert "deliveries" in inspect(connection).get_table_names()
        finally:
            transaction.rollback()


def test_models_match_migrations(engine: Engine):
    # Fails when a model changes without a new migration.
    with engine.connect() as connection:
        command.check(alembic_config(connection))


def test_scheduler_index_exists(engine: Engine):
    indexes = {
        index["name"]: index["column_names"] for index in inspect(engine).get_indexes("deliveries")
    }

    assert indexes["ix_deliveries_status_next_attempt_at"] == ["status", "next_attempt_at"]


def test_one_delivery_per_event_and_endpoint_is_enforced(engine: Engine):
    # Recovery and fan-out rely on it (ON CONFLICT DO NOTHING) to never duplicate.
    constraints = {
        c["name"]: c["column_names"] for c in inspect(engine).get_unique_constraints("deliveries")
    }

    assert constraints["uq_deliveries_event_id_endpoint_id"] == ["event_id", "endpoint_id"]


def test_recovery_index_exists(engine: Engine):
    indexes = {i["name"]: i["column_names"] for i in inspect(engine).get_indexes("events")}

    assert indexes["ix_events_created_at_id"] == ["created_at", "id"]
