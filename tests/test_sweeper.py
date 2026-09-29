from datetime import timedelta

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.models import Delivery, DeliveryStatus
from app.workers import sweeper
from app.workers.celery_app import celery_app
from app.workers.scheduler import claim_due_deliveries
from app.workers.sweeper import release_stuck_deliveries, sweep_stuck_deliveries
from tests.test_scheduler import add_delivery, add_endpoint


def lock(db: Session, delivery: Delivery, expires_in: timedelta) -> None:
    """Set the delivery's lock to expire `expires_in` from now (negative = already expired)."""
    db.execute(
        update(Delivery)
        .where(Delivery.id == delivery.id)
        .values(locked_until=func.now() + expires_in)
    )
    db.refresh(delivery)


def stuck_delivery(db: Session, expired_for: timedelta = timedelta(seconds=1)) -> Delivery:
    """An in_progress delivery whose worker died: its lock ran out `expired_for` ago."""
    delivery = add_delivery(
        db, add_endpoint(db), due_in=timedelta(minutes=-2), status=DeliveryStatus.IN_PROGRESS
    )
    lock(db, delivery, -expired_for)
    return delivery


def test_releases_deliveries_whose_lock_expired(db: Session):
    delivery = stuck_delivery(db)
    due_at = delivery.next_attempt_at

    assert release_stuck_deliveries(db) == [delivery.id]

    db.refresh(delivery)
    assert delivery.status == DeliveryStatus.PENDING
    assert delivery.locked_until is None
    # Still due now, and the cut-short attempt was never recorded, so it isn't counted.
    assert delivery.next_attempt_at == due_at
    assert delivery.attempt_count == 0


def test_returns_nothing_when_nothing_is_stuck(db: Session):
    assert release_stuck_deliveries(db) == []


def test_leaves_deliveries_whose_lock_is_still_held(db: Session):
    delivery = stuck_delivery(db)
    lock(db, delivery, timedelta(seconds=30))

    assert release_stuck_deliveries(db) == []
    db.refresh(delivery)
    assert delivery.status == DeliveryStatus.IN_PROGRESS


@pytest.mark.parametrize(
    "status", [DeliveryStatus.PENDING, DeliveryStatus.SUCCEEDED, DeliveryStatus.DEAD]
)
def test_leaves_deliveries_that_are_not_in_progress(db: Session, status: DeliveryStatus):
    delivery = add_delivery(db, add_endpoint(db), due_in=timedelta(minutes=-2), status=status)
    lock(db, delivery, timedelta(seconds=-1))

    assert release_stuck_deliveries(db) == []
    db.refresh(delivery)
    assert delivery.status == status


def test_released_delivery_is_claimed_again(db: Session):
    endpoint = add_endpoint(db)
    delivery = add_delivery(db, endpoint, due_in=timedelta(minutes=-1))
    assert claim_due_deliveries(db) == [delivery.id]
    # The worker died: nothing records a result, and the lock runs out.
    assert claim_due_deliveries(db) == []
    lock(db, delivery, timedelta(seconds=-1))

    release_stuck_deliveries(db)

    assert claim_due_deliveries(db) == [delivery.id]


def test_task_commits_the_release(db: Session, monkeypatch: pytest.MonkeyPatch):
    first = stuck_delivery(db)
    second = stuck_delivery(db, expired_for=timedelta(minutes=5))

    commits: list[str] = []
    # Sessions on the test connection: commit releases a savepoint, the test still rolls back.
    make_session = sessionmaker(bind=db.connection(), join_transaction_mode="create_savepoint")

    def session_local() -> Session:
        session = make_session()
        event.listen(session, "after_commit", lambda _: commits.append("commit"))
        return session

    monkeypatch.setattr(sweeper, "SessionLocal", session_local)

    assert sweep_stuck_deliveries() == 2
    assert commits == ["commit"]
    statuses = db.scalars(select(Delivery.status).where(Delivery.id.in_([first.id, second.id])))
    assert set(statuses) == {DeliveryStatus.PENDING}


def test_task_is_on_the_beat_schedule():
    entry = celery_app.conf.beat_schedule["sweep-stuck-deliveries"]

    assert entry["task"] == sweep_stuck_deliveries.name
    assert entry["schedule"] == 30.0
