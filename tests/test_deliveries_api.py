import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Delivery, DeliveryAttempt, DeliveryStatus, Endpoint
from app.workers.scheduler import claim_due_deliveries
from tests.test_scheduler import add_delivery, add_endpoint

ROUTES = [
    ("GET", "/deliveries"),
    ("POST", f"/deliveries/{uuid.uuid4()}/replay"),
    ("POST", f"/endpoints/{uuid.uuid4()}/replay-dead"),
]


def now(db: Session):
    # The test's transaction time: every server-side now() in this test returns it.
    return db.scalar(select(func.now()))


def add_attempt(
    db: Session,
    delivery: Delivery,
    ago: timedelta,
    status_code: int | None = 503,
    error: str | None = "HTTP 503: down",
) -> DeliveryAttempt:
    # created_at set by hand: now() is fixed within a test, so it wouldn't order attempts.
    attempt = DeliveryAttempt(
        delivery_id=delivery.id,
        status_code=status_code,
        response_ms=12,
        error=error,
        created_at=now(db) - ago,
    )
    db.add(attempt)
    db.flush()
    return attempt


def dead_delivery(db: Session, endpoint: Endpoint | None = None) -> Delivery:
    """A delivery that failed all 5 attempts, the last with a timeout."""
    delivery = add_delivery(
        db, endpoint or add_endpoint(db), due_in=timedelta(minutes=-40), status=DeliveryStatus.DEAD
    )
    delivery.attempt_count = 5
    for minutes_ago in (40, 30, 25, 1):
        add_attempt(db, delivery, timedelta(minutes=minutes_ago))
    add_attempt(db, delivery, timedelta(0), status_code=None, error="Timed out after 10s")
    return delivery


def status_of(db: Session, delivery: Delivery) -> DeliveryStatus:
    db.refresh(delivery)
    return delivery.status


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_routes_require_api_key(client: TestClient, method: str, path: str):
    del client.headers["Authorization"]

    assert client.request(method, path).status_code == 401


# --- list ---


def test_dead_letter_list_shows_last_error(client: TestClient, db: Session):
    delivery = dead_delivery(db)
    add_delivery(db, add_endpoint(db))  # pending, not listed

    response = client.get("/deliveries", params={"status": "dead"})

    assert response.status_code == 200
    [body] = response.json()
    assert body["id"] == str(delivery.id)
    assert body["event_id"] == str(delivery.event_id)
    assert body["endpoint_id"] == str(delivery.endpoint_id)
    assert body["status"] == "dead"
    assert body["attempt_count"] == 5
    assert body["last_status_code"] is None
    assert body["last_error"] == "Timed out after 10s"
    assert body["last_attempt_at"] is not None


def test_last_attempt_is_the_newest(client: TestClient, db: Session):
    delivery = add_delivery(db, add_endpoint(db), status=DeliveryStatus.DEAD)
    add_attempt(db, delivery, timedelta(minutes=1), status_code=410, error="HTTP 410: gone")
    add_attempt(db, delivery, timedelta(minutes=5), status_code=500, error="HTTP 500")

    [body] = client.get("/deliveries").json()

    assert (body["last_status_code"], body["last_error"]) == (410, "HTTP 410: gone")


def test_untried_delivery_has_no_last_attempt(client: TestClient, db: Session):
    add_delivery(db, add_endpoint(db))

    [body] = client.get("/deliveries").json()

    assert body["status"] == "pending"
    assert body["last_status_code"] is None
    assert body["last_error"] is None
    assert body["last_attempt_at"] is None


def test_list_without_status_returns_every_delivery(client: TestClient, db: Session):
    endpoint = add_endpoint(db)
    ids = {str(add_delivery(db, endpoint, status=status).id) for status in DeliveryStatus}

    assert {body["id"] for body in client.get("/deliveries").json()} == ids


def test_list_filters_by_endpoint(client: TestClient, db: Session):
    endpoint = add_endpoint(db)
    delivery = dead_delivery(db, endpoint)
    dead_delivery(db)

    response = client.get("/deliveries", params={"status": "dead", "endpoint_id": endpoint.id})

    assert [body["id"] for body in response.json()] == [str(delivery.id)]


def test_list_is_newest_first_and_limited(client: TestClient, db: Session):
    endpoint = add_endpoint(db)
    deliveries = [add_delivery(db, endpoint) for _ in range(3)]
    for age, delivery in enumerate(deliveries):
        delivery.created_at = now(db) - timedelta(minutes=age)
    db.flush()

    response = client.get("/deliveries", params={"limit": 2})

    assert [body["id"] for body in response.json()] == [str(d.id) for d in deliveries[:2]]


@pytest.mark.parametrize(
    "params",
    [{"status": "failed"}, {"limit": 0}, {"limit": 501}, {"endpoint_id": "not-a-uuid"}],
)
def test_list_rejects_invalid_params(client: TestClient, params: dict):
    assert client.get("/deliveries", params=params).status_code == 422


# --- replay one ---


def test_replay_resets_dead_delivery_to_pending(client: TestClient, db: Session):
    delivery = dead_delivery(db)

    response = client.post(f"/deliveries/{delivery.id}/replay")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["attempt_count"] == 0
    # The past attempts stay in the log.
    assert body["last_error"] == "Timed out after 10s"
    db.refresh(delivery)
    assert delivery.status == DeliveryStatus.PENDING
    assert delivery.attempt_count == 0
    assert delivery.next_attempt_at == now(db)
    assert delivery.locked_until is None
    assert len(delivery.attempts) == 5


def test_replayed_delivery_is_claimed_by_the_scheduler(client: TestClient, db: Session):
    delivery = dead_delivery(db)
    assert claim_due_deliveries(db) == []

    client.post(f"/deliveries/{delivery.id}/replay")

    assert claim_due_deliveries(db) == [delivery.id]


@pytest.mark.parametrize(
    "status", [DeliveryStatus.PENDING, DeliveryStatus.IN_PROGRESS, DeliveryStatus.SUCCEEDED]
)
def test_replay_rejects_delivery_that_is_not_dead(
    client: TestClient, db: Session, status: DeliveryStatus
):
    delivery = add_delivery(db, add_endpoint(db), status=status)
    delivery.attempt_count = 2
    # Committed so the request's rollback (it changes nothing) doesn't take the setup with it.
    db.commit()

    response = client.post(f"/deliveries/{delivery.id}/replay")

    assert response.status_code == 409
    assert response.json() == {
        "detail": f"Only dead deliveries can be replayed; this one is {status}"
    }
    db.refresh(delivery)
    assert (delivery.status, delivery.attempt_count) == (status, 2)


def test_replay_twice_conflicts(client: TestClient, db: Session):
    delivery = dead_delivery(db)

    assert client.post(f"/deliveries/{delivery.id}/replay").status_code == 200
    assert client.post(f"/deliveries/{delivery.id}/replay").status_code == 409


def test_replay_unknown_delivery_is_404(client: TestClient):
    assert client.post(f"/deliveries/{uuid.uuid4()}/replay").status_code == 404


# --- replay everything dead for an endpoint ---


def test_replay_dead_replays_only_that_endpoints_dead_deliveries(client: TestClient, db: Session):
    endpoint = add_endpoint(db)
    dead = [dead_delivery(db, endpoint) for _ in range(3)]
    succeeded = add_delivery(db, endpoint, status=DeliveryStatus.SUCCEEDED)
    other_endpoints_dead = dead_delivery(db)

    response = client.post(f"/endpoints/{endpoint.id}/replay-dead")

    assert response.status_code == 200
    assert response.json() == {"replayed": 3}
    for delivery in dead:
        db.refresh(delivery)
        assert (delivery.status, delivery.attempt_count) == (DeliveryStatus.PENDING, 0)
    assert status_of(db, succeeded) == DeliveryStatus.SUCCEEDED
    assert status_of(db, other_endpoints_dead) == DeliveryStatus.DEAD


def test_replay_dead_with_nothing_dead_replays_nothing(client: TestClient, db: Session):
    endpoint = add_endpoint(db)
    add_delivery(db, endpoint)

    assert client.post(f"/endpoints/{endpoint.id}/replay-dead").json() == {"replayed": 0}


def test_replay_dead_on_paused_endpoint_waits_for_resume(client: TestClient, db: Session):
    endpoint = add_endpoint(db, is_active=False)
    delivery = dead_delivery(db, endpoint)

    assert client.post(f"/endpoints/{endpoint.id}/replay-dead").json() == {"replayed": 1}
    assert status_of(db, delivery) == DeliveryStatus.PENDING
    assert claim_due_deliveries(db) == []

    client.patch(f"/endpoints/{endpoint.id}", json={"is_active": True})
    assert claim_due_deliveries(db) == [delivery.id]


def test_replay_dead_unknown_endpoint_is_404(client: TestClient):
    assert client.post(f"/endpoints/{uuid.uuid4()}/replay-dead").status_code == 404
