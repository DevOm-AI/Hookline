import uuid
from typing import Any

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Delivery, DeliveryAttempt, DeliveryStatus, Endpoint, Event


def make_endpoint(**overrides: Any) -> Endpoint:
    fields = {
        "url": "https://example.com/hook",
        "secret": "whsec_test",
        "event_types": ["order.shipped"],
    }
    return Endpoint(**(fields | overrides))


def make_event(**overrides: Any) -> Event:
    fields = {
        "type": "order.shipped",
        "payload": {"order_id": 42},
        "idempotency_key": str(uuid.uuid4()),
    }
    return Event(**(fields | overrides))


def test_insert_applies_defaults(db: Session):
    delivery = Delivery(event=make_event(), endpoint=make_endpoint())
    delivery.attempts.append(DeliveryAttempt(status_code=500, response_ms=120, error="boom"))
    db.add(delivery)
    db.flush()

    assert delivery.status == DeliveryStatus.PENDING
    assert delivery.attempt_count == 0
    assert delivery.next_attempt_at is not None
    assert delivery.locked_until is None
    assert delivery.endpoint.is_active is True
    assert delivery.event.created_at.tzinfo is not None
    assert delivery.attempts[0].delivery_id == delivery.id


def test_server_defaults_apply_to_raw_sql_inserts(db: Session):
    # Fan-out in 1.3 may insert deliveries with plain SQL, bypassing Python defaults.
    endpoint, event = make_endpoint(), make_event()
    db.add_all([endpoint, event])
    db.flush()

    row = db.execute(
        text(
            "INSERT INTO deliveries (event_id, endpoint_id) VALUES (:event_id, :endpoint_id) "
            "RETURNING id, status, attempt_count, next_attempt_at"
        ),
        {"event_id": event.id, "endpoint_id": endpoint.id},
    ).one()

    assert row.id is not None
    assert row.status == "pending"
    assert row.attempt_count == 0
    assert row.next_attempt_at is not None


def test_duplicate_idempotency_key_is_rejected(db: Session):
    db.add(make_event(idempotency_key="same-key"))
    db.flush()

    db.add(make_event(idempotency_key="same-key"))
    with pytest.raises(IntegrityError, match="uq_events_idempotency_key"):
        db.flush()


def test_one_delivery_per_event_and_endpoint(db: Session):
    endpoint, event = make_endpoint(), make_event()
    db.add(Delivery(event=event, endpoint=endpoint))
    db.flush()

    db.add(Delivery(event=event, endpoint=endpoint))
    with pytest.raises(IntegrityError, match="uq_deliveries_event_id_endpoint_id"):
        db.flush()


def test_unknown_delivery_status_is_rejected(db: Session):
    delivery = Delivery(event=make_event(), endpoint=make_endpoint())
    db.add(delivery)
    db.flush()

    with pytest.raises(IntegrityError, match="ck_deliveries_delivery_status"):
        db.execute(
            update(Delivery.__table__).where(Delivery.id == delivery.id).values(status="bogus")
        )


def test_endpoint_needs_at_least_one_event_type(db: Session):
    db.add(make_endpoint(event_types=[]))

    with pytest.raises(IntegrityError, match="ck_endpoints_event_types_not_empty"):
        db.flush()


def test_deleting_endpoint_cascades_to_deliveries_and_attempts(db: Session):
    endpoint = make_endpoint()
    delivery = Delivery(event=make_event(), endpoint=endpoint)
    delivery.attempts.append(DeliveryAttempt(status_code=503, response_ms=80))
    db.add(delivery)
    db.flush()
    endpoint_id, delivery_id = endpoint.id, delivery.id

    db.execute(delete(Endpoint).where(Endpoint.id == endpoint_id))

    count = select(func.count())
    assert db.scalar(count.select_from(Delivery).where(Delivery.id == delivery_id)) == 0
    assert (
        db.scalar(
            count.select_from(DeliveryAttempt).where(DeliveryAttempt.delivery_id == delivery_id)
        )
        == 0
    )
