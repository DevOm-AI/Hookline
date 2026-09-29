import uuid
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models import Delivery, DeliveryStatus, Endpoint
from app.workers.celery_app import celery_app
from app.workers.delivery import deliver

BATCH_SIZE = 100
# How long a worker owns a claimed delivery. After this the sweeper may hand it to another.
LOCK_DURATION = timedelta(seconds=60)


def claim_due_deliveries(
    db: Session, limit: int = BATCH_SIZE, lock_for: timedelta = LOCK_DURATION
) -> list[uuid.UUID]:
    """Mark up to `limit` due deliveries in_progress and return their ids. The caller commits.

    SKIP LOCKED lets several schedulers run at once: rows another transaction is claiming
    are skipped instead of waited on, so no delivery is claimed twice. Times come from the
    database clock, so schedulers on different machines agree on what "due" means.
    """
    due_ids = db.scalars(
        select(Delivery.id)
        .join(Endpoint, Delivery.endpoint_id == Endpoint.id)
        # Deliveries of a paused endpoint stay pending and go out once it's resumed.
        .where(
            Delivery.status == DeliveryStatus.PENDING,
            Delivery.next_attempt_at <= func.now(),
            Endpoint.is_active,
        )
        .order_by(Delivery.next_attempt_at)
        .limit(limit)
        # Lock only the delivery rows; endpoints stay free for PATCH /endpoints.
        .with_for_update(skip_locked=True, of=Delivery)
    ).all()
    if not due_ids:
        return []

    db.execute(
        update(Delivery)
        .where(Delivery.id.in_(due_ids))
        .values(status=DeliveryStatus.IN_PROGRESS, locked_until=func.now() + lock_for)
    )
    return list(due_ids)


@celery_app.task(name="hookline.schedule_due_deliveries")
def schedule_due_deliveries() -> int:
    """Beat job: claim due deliveries, then hand each one to a worker."""
    with SessionLocal() as db:
        delivery_ids = claim_due_deliveries(db)
        # Commit before enqueueing, so a worker never loads a row that isn't claimed yet.
        # If enqueueing fails after this, the rows sit in_progress until locked_until
        # passes and the sweeper returns them to pending: Postgres, not Redis, holds the work.
        db.commit()

    for delivery_id in delivery_ids:
        deliver.delay(str(delivery_id))
    return len(delivery_ids)
