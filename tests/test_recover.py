import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sa_event
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.endpoints import PAUSE_RACE_MARGIN, _create_missing_deliveries
from app.models import Delivery, DeliveryStatus, Endpoint, Event
from app.workers.scheduler import claim_due_deliveries
from tests.test_events_api import add_endpoint, post_event

# Every now() in a test is the same instant (one transaction), so "before the pause" is made
# by moving created_at back by hand. Setup is committed before requests that don't commit,
# whose rollback would otherwise take it with them.


def now(db: Session):
    return db.scalar(select(func.now()))


def subscribed_endpoint(db: Session, event_types=("order.shipped",)) -> Endpoint:
    endpoint = add_endpoint(db, list(event_types))
    endpoint.created_at = now(db) - timedelta(hours=1)
    db.commit()
    return endpoint


def send(client: TestClient, db: Session, type_: str = "order.shipped", ago=None) -> Event:
    event = db.get(Event, uuid.UUID(post_event(client, type=type_).json()["id"]))
    if ago is not None:
        event.created_at = now(db) - ago
        db.commit()
    return event


def pause(client: TestClient, endpoint: Endpoint) -> dict:
    return client.patch(f"/endpoints/{endpoint.id}", json={"is_active": False}).json()


def resume(client: TestClient, endpoint: Endpoint) -> dict:
    return client.patch(f"/endpoints/{endpoint.id}", json={"is_active": True}).json()


def recover(client: TestClient, endpoint: Endpoint, **params) -> dict:
    response = client.post(f"/endpoints/{endpoint.id}/recover", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def deliveries(db: Session, endpoint: Endpoint) -> dict[uuid.UUID, Delivery]:
    rows = db.scalars(select(Delivery).where(Delivery.endpoint_id == endpoint.id))
    return {delivery.event_id: delivery for delivery in rows}


def test_events_during_pause_create_no_deliveries(client: TestClient, db: Session):
    endpoint = subscribed_endpoint(db)
    pause(client, endpoint)

    send(client, db)

    assert deliveries(db, endpoint) == {}


def test_recover_creates_exactly_the_deliveries_missed_while_paused(
    client: TestClient, db: Session
):
    endpoint = subscribed_endpoint(db)
    before = send(client, db, ago=timedelta(minutes=30))
    pause(client, endpoint)
    during = [send(client, db) for _ in range(3)]

    body = recover(client, endpoint)

    assert body["created"] == 3
    found = deliveries(db, endpoint)
    assert set(found) == {before.id} | {event.id for event in during}
    for event in during:
        assert found[event.id].status == DeliveryStatus.PENDING
        assert found[event.id].attempt_count == 0


def test_recover_twice_creates_nothing_new(client: TestClient, db: Session):
    endpoint = subscribed_endpoint(db)
    pause(client, endpoint)
    send(client, db)
    send(client, db)

    assert recover(client, endpoint)["created"] == 2
    assert recover(client, endpoint)["created"] == 0
    assert len(deliveries(db, endpoint)) == 2


def test_recover_skips_events_of_unsubscribed_types(client: TestClient, db: Session):
    endpoint = subscribed_endpoint(db, ["order.shipped", "order.paid"])
    pause(client, endpoint)
    shipped = send(client, db, "order.shipped")
    paid = send(client, db, "order.paid")
    send(client, db, "order.refunded")

    assert recover(client, endpoint)["created"] == 2
    assert set(deliveries(db, endpoint)) == {shipped.id, paid.id}


def test_recovered_deliveries_wait_while_paused_and_go_after_resume(
    client: TestClient, db: Session
):
    endpoint = subscribed_endpoint(db)
    pause(client, endpoint)
    event = send(client, db)
    recover(client, endpoint)

    assert claim_due_deliveries(db) == []
    resume(client, endpoint)
    assert claim_due_deliveries(db) == [deliveries(db, endpoint)[event.id].id]


def test_resume_does_not_recover_but_says_from_when(client: TestClient, db: Session):
    endpoint = subscribed_endpoint(db)
    paused_at = pause(client, endpoint)["paused_at"]
    event = send(client, db)

    body = resume(client, endpoint)

    assert body["is_active"] is True
    assert body["paused_at"] is None
    assert body["recover_since"] is not None
    assert deliveries(db, endpoint) == {}
    # After resuming there's no paused_at to default to: the caller passes recover_since.
    assert client.post(f"/endpoints/{endpoint.id}/recover").status_code == 422
    assert recover(client, endpoint, since=body["recover_since"])["created"] == 1
    assert set(deliveries(db, endpoint)) == {event.id}
    assert paused_at is not None


def test_pausing_sets_paused_at_once_and_resuming_clears_it(client: TestClient, db: Session):
    endpoint = subscribed_endpoint(db)
    assert pause(client, endpoint)["paused_at"] is not None
    first = now(db) - timedelta(hours=3)
    db.get(Endpoint, endpoint.id).paused_at = first
    db.commit()

    # Pausing again mustn't move the start of the gap.
    assert pause(client, endpoint)["paused_at"] == first.isoformat().replace("+00:00", "Z")
    assert resume(client, endpoint)["paused_at"] is None


def test_resuming_an_active_endpoint_has_nothing_to_recover(client: TestClient, db: Session):
    assert resume(client, subscribed_endpoint(db))["recover_since"] is None


def test_default_window_covers_events_racing_the_pause(client: TestClient, db: Session):
    endpoint = subscribed_endpoint(db)
    pause(client, endpoint)
    # Accepted in a transaction that began just before the pause, fanned out just after it.
    racing = send(client, db, ago=PAUSE_RACE_MARGIN / 2)
    too_old = send(client, db, ago=PAUSE_RACE_MARGIN * 2)

    body = recover(client, endpoint)

    assert body["created"] == 1
    assert set(deliveries(db, endpoint)) == {racing.id}
    assert too_old.id not in deliveries(db, endpoint)


def test_default_window_never_reaches_before_the_endpoint_existed(client: TestClient, db: Session):
    endpoint = subscribed_endpoint(db)
    db.get(Endpoint, endpoint.id).created_at = now(db) - PAUSE_RACE_MARGIN / 4
    db.commit()
    pause(client, endpoint)
    send(client, db, ago=PAUSE_RACE_MARGIN / 2)

    assert recover(client, endpoint)["created"] == 0


def test_explicit_since_reaches_further_back(client: TestClient, db: Session):
    endpoint = subscribed_endpoint(db)
    pause(client, endpoint)
    older = send(client, db, ago=timedelta(minutes=45))

    body = recover(client, endpoint, since=(now(db) - timedelta(hours=1)).isoformat())

    assert body["created"] == 1
    assert set(deliveries(db, endpoint)) == {older.id}


@pytest.mark.parametrize("since", ["2026-09-29T10:00:00", "yesterday"])
def test_since_must_be_a_timestamp_with_a_zone(client: TestClient, db: Session, since: str):
    endpoint = subscribed_endpoint(db)

    assert (
        client.post(f"/endpoints/{endpoint.id}/recover", params={"since": since}).status_code == 422
    )


def test_recover_without_since_on_an_active_endpoint_is_422(client: TestClient, db: Session):
    response = client.post(f"/endpoints/{subscribed_endpoint(db).id}/recover")

    assert response.status_code == 422
    assert "pass `since`" in response.json()["detail"]


def test_recover_unknown_endpoint_is_404(client: TestClient):
    assert client.post(f"/endpoints/{uuid.uuid4()}/recover").status_code == 404


def test_recover_inserts_and_commits_in_batches(client: TestClient, db: Session):
    endpoint = subscribed_endpoint(db)
    pause(client, endpoint)
    events = [send(client, db) for _ in range(5)]
    commits = []

    def record(session):
        commits.append(session)

    sa_event.listen(db, "after_commit", record)
    try:
        created = _create_missing_deliveries(
            db, db.get(Endpoint, endpoint.id), now(db) - timedelta(minutes=1), batch_size=2
        )
    finally:
        sa_event.remove(db, "after_commit", record)

    assert created == 5
    assert len(commits) == 3  # 2 + 2 + 1
    assert set(deliveries(db, endpoint)) == {event.id for event in events}


def test_delivery_inserted_meanwhile_is_skipped_not_duplicated(client: TestClient, db: Session):
    endpoint = subscribed_endpoint(db)
    pause(client, endpoint)
    events = [send(client, db) for _ in range(3)]
    connection = db.connection()
    injected = []

    def race(conn, cursor, statement, parameters, context, executemany):
        # Right after the batch is picked, another recover inserts one of its deliveries.
        if not injected and statement.lstrip().startswith("SELECT events.created_at"):
            injected.append(True)
            conn.exec_driver_sql(
                "INSERT INTO deliveries (event_id, endpoint_id) VALUES (%(event)s, %(endpoint)s)",
                {"event": events[0].id, "endpoint": endpoint.id},
            )

    sa_event.listen(connection, "after_cursor_execute", race)
    try:
        created = _create_missing_deliveries(
            db, db.get(Endpoint, endpoint.id), now(db) - timedelta(minutes=1)
        )
    finally:
        sa_event.remove(connection, "after_cursor_execute", race)

    assert injected
    assert created == 2
    assert set(deliveries(db, endpoint)) == {event.id for event in events}
