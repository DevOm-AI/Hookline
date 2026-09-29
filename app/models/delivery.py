import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import Enum, ForeignKey, Index, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAt, UUIDPk

if TYPE_CHECKING:
    from app.models.endpoint import Endpoint
    from app.models.event import Event


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    DEAD = "dead"


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (
        # One delivery per event per endpoint, so fan-out can never duplicate.
        UniqueConstraint("event_id", "endpoint_id"),
        # The scheduler, the sweeper and the dead-letter list all filter on status first.
        Index("ix_deliveries_status_next_attempt_at", "status", "next_attempt_at"),
        Index("ix_deliveries_endpoint_id_status", "endpoint_id", "status"),
    )

    id: Mapped[UUIDPk]
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    endpoint_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("endpoints.id", ondelete="CASCADE"))
    # VARCHAR + CHECK instead of a Postgres ENUM, which is painful to change in migrations.
    status: Mapped[DeliveryStatus] = mapped_column(
        Enum(
            DeliveryStatus,
            name="delivery_status",
            native_enum=False,
            create_constraint=True,
            length=20,
            values_callable=lambda statuses: [status.value for status in statuses],
        ),
        default=DeliveryStatus.PENDING,
        server_default=DeliveryStatus.PENDING.value,
    )
    attempt_count: Mapped[int] = mapped_column(default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(server_default=func.now())
    locked_until: Mapped[datetime | None]
    created_at: Mapped[CreatedAt]

    event: Mapped["Event"] = relationship(back_populates="deliveries")
    endpoint: Mapped["Endpoint"] = relationship()
    attempts: Mapped[list["DeliveryAttempt"]] = relationship(
        back_populates="delivery",
        passive_deletes=True,
        order_by="DeliveryAttempt.created_at",
    )


class DeliveryAttempt(Base):
    __tablename__ = "delivery_attempts"
    __table_args__ = (
        Index("ix_delivery_attempts_delivery_id_created_at", "delivery_id", "created_at"),
    )

    id: Mapped[UUIDPk]
    delivery_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("deliveries.id", ondelete="CASCADE"))
    # None when there was no response (timeout, connection error).
    status_code: Mapped[int | None]
    response_ms: Mapped[int]
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[CreatedAt]

    delivery: Mapped["Delivery"] = relationship(back_populates="attempts")
