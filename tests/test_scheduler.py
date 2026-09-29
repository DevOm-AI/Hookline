import uuid
from datetime import timedelta

import pytest
from sqlalchemy import Engine, delete, event, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.models import Delivery, DeliveryStatus, Endpoint, Event
from app.workers import scheduler
from app.workers.celery_app import celery_app
from app.workers.scheduler import LOCK_DURATION, claim_due_deliveries, schedule_due_deliveries


def add_endpoint(db: Session, is_active: bool = True) -> Endpoint:
    endpoint = Endpoint(
        url="https://example.com/hook",
        secret="whsec_x",
        event_types=["order.shipped"],
        is_active=is_active,
    )
    db.add(endpoint)
    db.flush()
    return endpoint


def add_delivery(
    db: Session,
    endpoint: Endpoint,
    due_in: timedelta = timedelta(0),
    status: DeliveryStatus = DeliveryStatus.PENDING,
) -> Delivery:
    """A delivery of a fresh event, due `due_in` from now (negative = overdue)."""
    evt = Event(type="order.shipped", payload={}, idempotency_key=str(uuid.uuid4()))
    db.add(evt)
    db.flush()
    delivery = Delivery(
        event_id=evt.id,
        endpoint_id=endpoint.id,
        status=status,
        next_attempt_at=db.scalar(select(func.now())) + due_in,
    )
    db.add(delivery)
    db.flush()
    return delivery


def test_claims_due_pending_deliveries(db: Session):
    endpoint = add_endpoint(db)
    delivery = add_delivery(db, endpoint)

    assert claim_due_deliveries(db) == [delivery.id]

    db.refresh(delivery)
    assert delivery.status == DeliveryStatus.IN_PROGRESS
    assert delivery.locked_until == db.scalar(select(func.now())) + LOCK_DURATION
    # Claiming isn't an attempt: the worker counts attempts when it sends.
    assert delivery.attempt_count == 0


def test_returns_nothing_when_no_work(db: Session):
    assert claim_due_deliveries(db) == []


def test_skips_deliveries_not_due_yet(db: Session):
    endpoint = add_endpoint(db)
    later = add_delivery(db, endpoint, due_in=timedelta(minutes=5))

    assert claim_due_deliveries(db) == []
    db.refresh(later)
    assert later.status == DeliveryStatus.PENDING
    assert later.locked_until is None


@pytest.mark.parametrize(
    "status", [DeliveryStatus.IN_PROGRESS, DeliveryStatus.SUCCEEDED, DeliveryStatus.DEAD]
)
def test_skips_deliveries_that_are_not_pending(db: Session, status: DeliveryStatus):
    add_delivery(db, add_endpoint(db), due_in=timedelta(minutes=-1), status=status)

    assert claim_due_deliveries(db) == []


def test_leaves_paused_endpoints_deliveries_pending(db: Session):
    paused = add_delivery(db, add_endpoint(db, is_active=False))

    assert claim_due_deliveries(db) == []
    db.refresh(paused)
    assert paused.status == DeliveryStatus.PENDING


def test_claims_oldest_first_up_to_the_limit(db: Session):
    endpoint = add_endpoint(db)
    newest = add_delivery(db, endpoint, due_in=timedelta(seconds=-1))
    oldest = add_delivery(db, endpoint, due_in=timedelta(minutes=-10))
    middle = add_delivery(db, endpoint, due_in=timedelta(minutes=-5))

    assert set(claim_due_deliveries(db, limit=2)) == {oldest.id, middle.id}
    db.refresh(newest)
    assert newest.status == DeliveryStatus.PENDING


def test_task_commits_the_claim_before_enqueueing(db: Session, monkeypatch: pytest.MonkeyPatch):
    endpoint = add_endpoint(db)
    first = add_delivery(db, endpoint, due_in=timedelta(minutes=-1))
    second = add_delivery(db, endpoint)

    steps: list[str] = []
    # Sessions on the test connection: commit releases a savepoint, the test still rolls back.
    make_session = sessionmaker(bind=db.connection(), join_transaction_mode="create_savepoint")

    def session_local() -> Session:
        session = make_session()
        event.listen(session, "after_commit", lambda _: steps.append("commit"))
        return session

    monkeypatch.setattr(scheduler, "SessionLocal", session_local)
    monkeypatch.setattr(scheduler.deliver, "delay", lambda delivery_id: steps.append(delivery_id))

    assert schedule_due_deliveries() == 2
    assert steps[0] == "commit"
    assert set(steps[1:]) == {str(first.id), str(second.id)}


def test_task_is_on_the_beat_schedule():
    entry = celery_app.conf.beat_schedule["schedule-due-deliveries"]

    assert entry["task"] == schedule_due_deliveries.name
    assert entry["schedule"] == 1.0


def test_concurrent_schedulers_never_claim_the_same_delivery(engine: Engine):
    """Two schedulers in separate transactions: the second skips what the first has locked."""
    with Session(engine) as setup:
        endpoint = add_endpoint(setup)
        deliveries = [add_delivery(setup, endpoint, due_in=timedelta(minutes=-n)) for n in range(4)]
        # Read before commit, which expires the objects.
        endpoint_id = endpoint.id
        ids = [d.id for d in deliveries]
        event_ids = [d.event_id for d in deliveries]
        setup.commit()

    try:
        with Session(engine) as first, Session(engine) as second:
            claimed_by_first = claim_due_deliveries(first, limit=2)
            # Fail fast instead of hanging if SKIP LOCKED ever stops skipping.
            second.execute(text("SET LOCAL lock_timeout = '2s'"))
            claimed_by_second = claim_due_deliveries(second)
            first.commit()
            second.commit()

        assert len(claimed_by_first) == 2
        assert set(claimed_by_first).isdisjoint(claimed_by_second)
        assert set(claimed_by_first) | set(claimed_by_second) == set(ids)
    finally:
        # These rows were really committed, so remove them for the tests that follow.
        with Session(engine) as cleanup:
            # Deliveries go with their endpoint and events (ON DELETE CASCADE).
            cleanup.execute(delete(Event).where(Event.id.in_(event_ids)))
            cleanup.execute(delete(Endpoint).where(Endpoint.id == endpoint_id))
            cleanup.commit()
