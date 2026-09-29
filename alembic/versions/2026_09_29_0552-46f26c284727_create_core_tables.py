"""create core tables

Revision ID: 46f26c284727
Create Date: 2026-09-29 05:52:37.710761

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "46f26c284727"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "endpoints",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("secret", sa.Text(), nullable=False),
        sa.Column("event_types", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "cardinality(event_types) > 0", name=op.f("ck_endpoints_event_types_not_empty")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_endpoints")),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("type", sa.String(length=255), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_events_idempotency_key")),
    )
    op.create_table(
        "deliveries",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("endpoint_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "in_progress",
                "succeeded",
                "dead",
                name="delivery_status",
                native_enum=False,
                # Enforced by ck_deliveries_delivery_status below; autogenerate emits it twice.
                create_constraint=False,
                length=20,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'in_progress', 'succeeded', 'dead')",
            name=op.f("ck_deliveries_delivery_status"),
        ),
        sa.ForeignKeyConstraint(
            ["endpoint_id"],
            ["endpoints.id"],
            name=op.f("fk_deliveries_endpoint_id_endpoints"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_deliveries_event_id_events"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deliveries")),
        sa.UniqueConstraint(
            "event_id", "endpoint_id", name=op.f("uq_deliveries_event_id_endpoint_id")
        ),
    )
    op.create_index(
        "ix_deliveries_endpoint_id_status", "deliveries", ["endpoint_id", "status"], unique=False
    )
    op.create_index(
        "ix_deliveries_status_next_attempt_at",
        "deliveries",
        ["status", "next_attempt_at"],
        unique=False,
    )
    op.create_table(
        "delivery_attempts",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("delivery_id", sa.Uuid(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("response_ms", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["delivery_id"],
            ["deliveries.id"],
            name=op.f("fk_delivery_attempts_delivery_id_deliveries"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_delivery_attempts")),
    )
    op.create_index(
        "ix_delivery_attempts_delivery_id_created_at",
        "delivery_attempts",
        ["delivery_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_delivery_attempts_delivery_id_created_at", table_name="delivery_attempts")
    op.drop_table("delivery_attempts")
    op.drop_index("ix_deliveries_status_next_attempt_at", table_name="deliveries")
    op.drop_index("ix_deliveries_endpoint_id_status", table_name="deliveries")
    op.drop_table("deliveries")
    op.drop_table("events")
    op.drop_table("endpoints")
