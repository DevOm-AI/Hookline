import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, func, select, text
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session

from app.api import events as events_api
from app.models import Delivery, DeliveryStatus, Endpoint, Event
from tests.test_deliveries_api import add_attempt, dead_delivery, now

EVENT = {"type": "order.shipped", "payload": {"order_id": 42}}


def post_event(client: TestClient, key: str | None = None, **overrides):
    headers = {"Idempotency-Key": key or str(uuid.uuid4())}
    return client.post("/events", json=EVENT | overrides, headers=headers)


def add_endpoint(db: Session, event_types: list[str], is_active: bool = True) -> Endpoint:
    endpoint = Endpoint(
        url="https://example.com/hook",
        secret="whsec_x",
        event_types=event_types,
        is_active=is_active,
    )
    db.add(endpoint)
    db.flush()
    return endpoint


def deliveries_for(db: Session, event_id: str) -> list[Delivery]:
    return list(db.scalars(select(Delivery).where(Delivery.event_id == uuid.UUID(event_id))))


def count_events(db: Session, key: str) -> int:
    return db.scalar(select(func.count()).select_from(Event).where(Event.idempotency_key == key))


def test_requires_api_key(client: TestClient):
    del client.headers["Authorization"]

    assert post_event(client).status_code == 401


def test_accepts_event_with_202(client: TestClient, db: Session):
    response = post_event(client, key="key-1")

    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"id", "type", "created_at"}
    event = db.get(Event, uuid.UUID(body["id"]))
    assert event.payload == {"order_id": 42}
    assert event.idempotency_key == "key-1"


def test_creates_one_pending_delivery_per_matching_active_endpoint(client: TestClient, db: Session):
    first = add_endpoint(db, ["order.shipped"])
    second = add_endpoint(db, ["order.paid", "order.shipped"])
    add_endpoint(db, ["order.shipped"], is_active=False)  # paused
    add_endpoint(db, ["order.paid"])  # not subscribed

    event_id = post_event(client).json()["id"]

    deliveries = deliveries_for(db, event_id)
    assert {d.endpoint_id for d in deliveries} == {first.id, second.id}
    assert len({d.id for d in deliveries}) == 2
    for delivery in deliveries:
        assert delivery.status == DeliveryStatus.PENDING
        assert delivery.attempt_count == 0
        assert delivery.next_attempt_at is not None
        assert delivery.locked_until is None


def test_event_without_subscribers_is_still_stored(client: TestClient, db: Session):
    add_endpoint(db, ["order.paid"])

    response = post_event(client)

    assert response.status_code == 202
    assert db.get(Event, uuid.UUID(response.json()["id"])) is not None
    assert deliveries_for(db, response.json()["id"]) == []


def test_same_idempotency_key_returns_original_and_creates_nothing(client: TestClient, db: Session):
    add_endpoint(db, ["order.shipped"])
    first = post_event(client, key="same-key")

    # Even a different body: the key decides, the original event wins.
    second = post_event(client, key="same-key", payload={"order_id": 99})

    assert second.status_code == 202
    assert second.json() == first.json()
    assert second.headers["Idempotent-Replayed"] == "true"
    assert "Idempotent-Replayed" not in first.headers
    assert count_events(db, "same-key") == 1
    assert len(deliveries_for(db, first.json()["id"])) == 1
    assert db.get(Event, uuid.UUID(first.json()["id"])).payload == {"order_id": 42}


def wait_for_insert_blocked_on_a_lock(engine: Engine, timeout: float = 5.0) -> None:
    """Return once another connection's INSERT INTO events is waiting on a row lock."""
    # Autocommit: pg_stat_activity is read once per transaction and cached until it ends.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            waiting = connection.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                    " AND query ILIKE 'INSERT INTO events%'"
                )
            )
            if waiting:
                return
            time.sleep(0.01)
    raise AssertionError("the second request's insert never waited on the first one")


def test_concurrent_requests_with_the_same_key_create_one_event(
    committed_client: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
):
    """The retry arrives while the first request is still in its transaction.

    Its insert waits on the first one's uncommitted key, then does nothing once that commits:
    one event and one set of deliveries, and both callers get the same event back.
    """
    key = f"race-{uuid.uuid4()}"
    with Session(engine) as setup:
        endpoint_id = add_endpoint(setup, ["order.shipped"]).id
        setup.commit()

    first_inserted = threading.Event()
    fan_out = events_api._fan_out

    def fan_out_once_the_second_request_waits(db: Session, event: Event) -> None:
        first_inserted.set()
        wait_for_insert_blocked_on_a_lock(engine)
        fan_out(db, event)

    monkeypatch.setattr(events_api, "_fan_out", fan_out_once_the_second_request_waits)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(post_event, committed_client, key)
            assert first_inserted.wait(timeout=5)
            second = pool.submit(post_event, committed_client, key)
            first, second = first.result(timeout=10), second.result(timeout=10)

        assert (first.status_code, second.status_code) == (202, 202)
        assert first.json() == second.json()
        assert "Idempotent-Replayed" not in first.headers
        assert second.headers["Idempotent-Replayed"] == "true"
        with Session(engine) as check:
            assert count_events(check, key) == 1
            assert len(deliveries_for(check, first.json()["id"])) == 1
    finally:
        # Really committed, so remove it for the tests that follow. Deliveries cascade.
        with Session(engine) as cleanup:
            cleanup.execute(delete(Event).where(Event.idempotency_key == key))
            cleanup.execute(delete(Endpoint).where(Endpoint.id == endpoint_id))
            cleanup.commit()


def test_event_is_not_saved_when_fan_out_fails(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
):
    def broken_fan_out(db: Session, event: Event) -> None:
        raise RuntimeError("crash between event and deliveries")

    monkeypatch.setattr(events_api, "_fan_out", broken_fan_out)

    with pytest.raises(RuntimeError):
        post_event(client, key="atomic-key")

    assert count_events(db, "atomic-key") == 0


@pytest.mark.parametrize("headers", [{}, {"Idempotency-Key": ""}, {"Idempotency-Key": "k" * 256}])
def test_rejects_missing_or_invalid_idempotency_key(client: TestClient, headers: dict):
    assert client.post("/events", json=EVENT, headers=headers).status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {"payload": {"a": 1}},
        {"type": "", "payload": {"a": 1}},
        {"type": "order.shipped"},
        {"type": "order.shipped", "payload": [1, 2]},
        {"type": "order.shipped", "payload": {}, "extra": True},
    ],
)
def test_rejects_invalid_body(client: TestClient, payload: dict):
    response = client.post("/events", json=payload, headers={"Idempotency-Key": "k"})

    assert response.status_code == 422


# --- delivery log ---


def get_event(client: TestClient, event_id: str) -> dict:
    response = client.get(f"/events/{event_id}")
    assert response.status_code == 200, response.text
    return response.json()


def test_get_event_shows_every_delivery_and_attempt(client: TestClient, db: Session):
    retried = add_endpoint(db, ["order.shipped"])
    retried.url = "https://example.com/retried"
    untried = add_endpoint(db, ["order.shipped"])
    untried.url = "https://example.com/untried"
    event_id = post_event(client, key="order-42-shipped").json()["id"]
    delivery = next(d for d in deliveries_for(db, event_id) if d.endpoint_id == retried.id)
    add_attempt(db, delivery, timedelta(minutes=1), status_code=None, error="Timed out after 10s")
    add_attempt(db, delivery, timedelta(0), status_code=200, error=None)
    delivery.status, delivery.attempt_count = DeliveryStatus.SUCCEEDED, 2
    db.flush()

    body = get_event(client, event_id)

    assert body["id"] == event_id
    assert body["type"] == "order.shipped"
    assert body["payload"] == {"order_id": 42}
    assert body["idempotency_key"] == "order-42-shipped"
    assert body["created_at"]
    by_url = {d["endpoint_url"]: d for d in body["deliveries"]}
    assert set(by_url) == {"https://example.com/retried", "https://example.com/untried"}

    sent = by_url["https://example.com/retried"]
    assert sent["id"] == str(delivery.id)
    assert sent["endpoint_id"] == str(retried.id)
    assert (sent["status"], sent["attempt_count"]) == ("succeeded", 2)
    # Oldest first: the story of the delivery in order.
    assert [(a["status_code"], a["error"]) for a in sent["attempts"]] == [
        (None, "Timed out after 10s"),
        (200, None),
    ]
    assert all(a["id"] and a["created_at"] and a["response_ms"] == 12 for a in sent["attempts"])

    waiting = by_url["https://example.com/untried"]
    assert (waiting["status"], waiting["attempt_count"], waiting["attempts"]) == ("pending", 0, [])
    assert waiting["next_attempt_at"]


def test_get_event_without_subscribers_has_no_deliveries(client: TestClient):
    event_id = post_event(client).json()["id"]

    assert get_event(client, event_id)["deliveries"] == []


def test_get_event_keeps_attempts_from_before_a_replay(client: TestClient, db: Session):
    delivery = dead_delivery(db)
    client.post(f"/deliveries/{delivery.id}/replay")

    [logged] = get_event(client, str(delivery.event_id))["deliveries"]

    assert (logged["status"], logged["attempt_count"]) == ("pending", 0)
    assert len(logged["attempts"]) == 5


def test_get_event_query_count_does_not_grow_with_deliveries(
    client: TestClient, db: Session, engine: Engine
):
    def queries_to_get(endpoint_count: int) -> int:
        # Its own event type, so the other call's endpoints don't subscribe.
        event_type = f"order.{endpoint_count}"
        for _ in range(endpoint_count):
            add_endpoint(db, [event_type])
        event_id = post_event(client, type=event_type).json()["id"]
        for delivery in deliveries_for(db, event_id):
            add_attempt(db, delivery, timedelta(minutes=1))
            add_attempt(db, delivery, timedelta(0))

        statements = []

        def record(conn, cursor, statement, *args):
            statements.append(statement)

        sa_event.listen(engine, "before_cursor_execute", record)
        try:
            assert len(get_event(client, event_id)["deliveries"]) == endpoint_count
        finally:
            sa_event.remove(engine, "before_cursor_execute", record)
        return len(statements)

    assert queries_to_get(1) == queries_to_get(5)


def test_get_unknown_event_is_404(client: TestClient):
    response = client.get(f"/events/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Event not found"}


def test_get_event_with_malformed_id_is_422(client: TestClient):
    assert client.get("/events/not-a-uuid").status_code == 422


def test_get_event_requires_api_key(client: TestClient):
    del client.headers["Authorization"]

    assert client.get(f"/events/{uuid.uuid4()}").status_code == 401


# --- list ---


def test_list_events_newest_first_with_delivery_counts(client: TestClient, db: Session):
    add_endpoint(db, ["order.shipped"])
    add_endpoint(db, ["order.shipped"])
    older = post_event(client).json()["id"]
    newer = post_event(client).json()["id"]
    lonely = post_event(client, type="nobody.listens").json()["id"]
    for age, event_id in enumerate([lonely, newer, older]):
        db.get(Event, uuid.UUID(event_id)).created_at = now(db) - timedelta(minutes=age)
    dead, _ = deliveries_for(db, older)
    dead.status = DeliveryStatus.DEAD
    db.flush()

    response = client.get("/events")

    assert response.status_code == 200
    assert [(e["id"], e["deliveries"]) for e in response.json()] == [
        (lonely, {}),
        (newer, {"pending": 2}),
        (older, {"pending": 1, "dead": 1}),
    ]
    assert set(response.json()[0]) == {"id", "type", "created_at", "deliveries"}


def test_list_events_respects_limit(client: TestClient):
    for _ in range(3):
        post_event(client)

    assert len(client.get("/events", params={"limit": 2}).json()) == 2


@pytest.mark.parametrize("limit", [0, 201, "many"])
def test_list_events_rejects_bad_limit(client: TestClient, limit):
    assert client.get("/events", params={"limit": limit}).status_code == 422
