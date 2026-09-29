"""add endpoint failing_since and auto_paused_at

Revision ID: 31a051b5ad68
Revises: 46f26c284727
Create Date: 2026-09-29 10:34:33.408829

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "31a051b5ad68"
down_revision: str | Sequence[str] | None = "46f26c284727"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable with no default: Postgres adds them without rewriting the table.
    op.add_column(
        "endpoints", sa.Column("failing_since", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "endpoints", sa.Column("auto_paused_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("endpoints", "auto_paused_at")
    op.drop_column("endpoints", "failing_since")
