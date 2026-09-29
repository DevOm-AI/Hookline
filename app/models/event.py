from typing import TYPE_CHECKING, Any

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAt, UUIDPk

if TYPE_CHECKING:
    from app.models.delivery import Delivery


class Event(Base):
    __tablename__ = "events"

    id: Mapped[UUIDPk]
    type: Mapped[str] = mapped_column(String(255))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # The unique constraint is what makes a repeated Idempotency-Key a no-op.
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[CreatedAt]

    deliveries: Mapped[list["Delivery"]] = relationship(
        back_populates="event", passive_deletes=True
    )
