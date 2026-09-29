import logging
import uuid

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models import Delivery, DeliveryStatus
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


def release_stuck_deliveries(db: Session) -> list[uuid.UUID]:
    """Set in_progress deliveries whose lock has expired back to pending. The caller commits.

    A delivery stays in_progress for good if its worker died mid-send, or if its task never
    reached a worker. Once locked_until passes, no worker owns it any more. Its
    next_attempt_at is already in the past (that's why it was claimed), so the scheduler
    claims it again on its next tick. The attempt that was cut short was never recorded,
    so attempt_count stays as it is.

    One UPDATE, so concurrent sweepers are safe: a row one of them has just released no
    longer matches for the other.
    """
    return list(
        db.scalars(
            update(Delivery)
            .where(
                Delivery.status == DeliveryStatus.IN_PROGRESS,
                # The database clock, like the scheduler that set the lock.
                Delivery.locked_until < func.now(),
            )
            .values(status=DeliveryStatus.PENDING, locked_until=None)
            .returning(Delivery.id)
        )
    )


@celery_app.task(name="hookline.sweep_stuck_deliveries")
def sweep_stuck_deliveries() -> int:
    """Beat job: hand deliveries abandoned by a dead worker back to the scheduler."""
    with SessionLocal() as db:
        released = release_stuck_deliveries(db)
        db.commit()

    if released:
        logger.warning(
            "Released %d stuck deliveries back to pending: %s",
            len(released),
            ", ".join(str(delivery_id) for delivery_id in released),
        )
    return len(released)
