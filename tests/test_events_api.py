import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import events as events_api
from app.models import Delivery, DeliveryStatus, Endpoint, Event

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
