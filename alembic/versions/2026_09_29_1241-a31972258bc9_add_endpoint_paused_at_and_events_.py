"""add endpoint paused_at and events created_at index

Revision ID: a31972258bc9
Revises: 31a051b5ad68
Create Date: 2026-09-29 12:41:31.232045

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a31972258bc9"
down_revision: str | Sequence[str] | None = "31a051b5ad68"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("endpoints", sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True))
    # Endpoints paused automatically already know when. For ones paused by hand before this
    # column existed there's no record, so recovering them needs an explicit `since`.
    op.execute(
        "UPDATE endpoints SET paused_at = auto_paused_at "
        "WHERE NOT is_active AND auto_paused_at IS NOT NULL"
    )
    op.create_index("ix_events_created_at_id", "events", ["created_at", "id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_events_created_at_id", table_name="events")
    op.drop_column("endpoints", "paused_at")
