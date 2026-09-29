import logging
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import DeliveryStatus, Endpoint
from app.workers import auto_pause
from app.workers.auto_pause import (
    FAILING_FOR,
    pause_failing_endpoints,
    pause_failing_endpoints_task,
)
from app.workers.celery_app import celery_app
from app.workers.scheduler import claim_due_deliveries
from tests.test_scheduler import add_delivery, add_endpoint


def now(db: Session):
    return db.scalar(select(func.now()))


def failing_endpoint(
    db: Session, failing_for: timedelta | None, is_active: bool = True
) -> Endpoint:
    endpoint = add_endpoint(db, is_active=is_active)
    endpoint.failing_since = None if failing_for is None else now(db) - failing_for
    db.flush()
    return endpoint


def test_pauses_endpoint_that_failed_everything_for_24_hours(db: Session):
    endpoint = failing_endpoint(db, timedelta(hours=25))

    [(endpoint_id, url, failing_since)] = pause_failing_endpoints(db)

    assert (endpoint_id, url) == (endpoint.id, endpoint.url)
    db.refresh(endpoint)
    assert endpoint.is_active is False
    assert endpoint.auto_paused_at == now(db)
    # Kept, so it shows when the trouble began.
    assert endpoint.failing_since == failing_since == now(db) - timedelta(hours=25)


def test_pauses_at_exactly_24_hours(db: Session):
    failing_endpoint(db, FAILING_FOR)

    assert len(pause_failing_endpoints(db)) == 1


@pytest.mark.parametrize("failing_for", [None, timedelta(hours=23, minutes=59)])
def test_leaves_healthy_and_recently_failing_endpoints(db: Session, failing_for):
    endpoint = failing_endpoint(db, failing_for)

    assert pause_failing_endpoints(db) == []
    db.refresh(endpoint)
    assert endpoint.is_active is True
    assert endpoint.auto_paused_at is None


def test_leaves_endpoints_already_paused_by_hand(db: Session):
    endpoint = failing_endpoint(db, timedelta(days=3), is_active=False)

    assert pause_failing_endpoints(db) == []
    db.refresh(endpoint)
    assert endpoint.auto_paused_at is None


def test_paused_endpoint_stops_sending_and_resume_sends_what_waited(
    client: TestClient, db: Session
):
    endpoint = failing_endpoint(db, timedelta(hours=30))
    retrying = add_delivery(db, endpoint, due_in=timedelta(minutes=-1))
    pause_failing_endpoints(db)
    db.commit()

    assert claim_due_deliveries(db) == []
    db.refresh(retrying)
    assert retrying.status == DeliveryStatus.PENDING

    body = client.patch(f"/endpoints/{endpoint.id}", json={"is_active": True}).json()

    assert body["is_active"] is True
    # A fresh 24 hours: the old streak would otherwise pause it again on the next run.
    assert (body["failing_since"], body["auto_paused_at"]) == (None, None)
    assert pause_failing_endpoints(db) == []
    assert claim_due_deliveries(db) == [retrying.id]


def test_endpoint_api_shows_why_it_was_paused(client: TestClient, db: Session):
    endpoint = failing_endpoint(db, timedelta(hours=25))
    pause_failing_endpoints(db)
    db.commit()

    body = client.get(f"/endpoints/{endpoint.id}").json()

    assert body["is_active"] is False
    assert body["auto_paused_at"] is not None
    assert body["failing_since"] is not None


def test_task_commits_and_logs_each_paused_endpoint(
    db: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    endpoint = failing_endpoint(db, timedelta(hours=25))
    failing_endpoint(db, timedelta(hours=1))
    monkeypatch.setattr(
        auto_pause,
        "SessionLocal",
        sessionmaker(bind=db.connection(), join_transaction_mode="create_savepoint"),
    )

    with caplog.at_level(logging.WARNING, logger=auto_pause.__name__):
        assert pause_failing_endpoints_task() == 1

    db.refresh(endpoint)
    assert endpoint.is_active is False
    assert str(endpoint.id) in caplog.text
    assert "every attempt has failed since" in caplog.text


def test_task_is_on_the_beat_schedule():
    entry = celery_app.conf.beat_schedule["pause-failing-endpoints"]

    assert entry["task"] == pause_failing_endpoints_task.name
    assert entry["schedule"] == 60.0
