from datetime import datetime

from sqlalchemy import CheckConstraint, Text, true
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAt, UUIDPk


class Endpoint(Base):
    __tablename__ = "endpoints"
    __table_args__ = (
        CheckConstraint("cardinality(event_types) > 0", name="event_types_not_empty"),
    )

    id: Mapped[UUIDPk]
    url: Mapped[str] = mapped_column(Text)
    # Stored readable, not hashed: it's needed to sign every request.
    secret: Mapped[str] = mapped_column(Text)
    event_types: Mapped[list[str]] = mapped_column(ARRAY(Text))
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())
    # When its current run of failed attempts began; None once an attempt succeeds. After
    # 24 hours of nothing but failures the endpoint is paused (app/workers/auto_pause.py).
    failing_since: Mapped[datetime | None]
    # Set when that pause was automatic; resuming clears it.
    auto_paused_at: Mapped[datetime | None]
    created_at: Mapped[CreatedAt]
