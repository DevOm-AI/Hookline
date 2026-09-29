import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.security import hash_api_key
from app.main import app
from app.models import DeliveryStatus, Endpoint
from tests.conftest import TEST_API_KEY
from tests.test_scheduler import add_delivery

NEW_ENDPOINT = {"url": "https://example.com/hook", "event_types": ["order.shipped"]}

ROUTES = [
    ("POST", "/endpoints"),
    ("GET", "/endpoints"),
    ("GET", f"/endpoints/{uuid.uuid4()}"),
    ("PATCH", f"/endpoints/{uuid.uuid4()}"),
    ("DELETE", f"/endpoints/{uuid.uuid4()}"),
    ("GET", "/endpoints/stats"),
    ("GET", "/events"),
]


def create(client: TestClient, **overrides) -> dict:
    response = client.post("/endpoints", json=NEW_ENDPOINT | overrides)
    assert response.status_code == 201, response.text
    return response.json()


# --- auth ---


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_routes_reject_missing_api_key(client: TestClient, method: str, path: str):
    del client.headers["Authorization"]

    response = client.request(method, path)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "header", ["Bearer wrong-key", "Basic aGs6dGVzdA==", "hk_test_key", "Bearer "]
)
def test_routes_reject_bad_api_key(client: TestClient, header: str):
    response = client.get("/endpoints", headers={"Authorization": header})

    assert response.status_code == 401


def test_routes_fail_closed_without_configured_key(client: TestClient):
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, api_key_hash=None)

    assert client.get("/endpoints").status_code == 401


def test_health_stays_public(client: TestClient):
    del client.headers["Authorization"]

    assert client.get("/health").status_code != 401


# --- create ---


def test_create_returns_secret_once(client: TestClient, db: Session):
    body = create(client)

    assert body["secret"].startswith("whsec_")
    assert body["url"] == "https://example.com/hook"
    assert body["event_types"] == ["order.shipped"]
    assert body["is_active"] is True
    assert body["created_at"]
    assert db.get(Endpoint, uuid.UUID(body["id"])).secret == body["secret"]

    listed = client.get("/endpoints").json()
    fetched = client.get(f"/endpoints/{body['id']}").json()
    assert "secret" not in listed[0]
    assert "secret" not in fetched


def test_each_endpoint_gets_its_own_secret(client: TestClient):
    assert create(client)["secret"] != create(client)["secret"]


def test_create_normalizes_event_types(client: TestClient):
    body = create(client, event_types=[" order.shipped ", "order.paid", "order.shipped"])

    assert body["event_types"] == ["order.shipped", "order.paid"]


@pytest.mark.parametrize(
    "payload",
    [
        {"url": "https://example.com/hook", "event_types": []},
        {"url": "https://example.com/hook", "event_types": ["  "]},
        {"url": "https://example.com/hook"},
        {"url": "not a url", "event_types": ["a"]},
        {"url": "ftp://example.com/hook", "event_types": ["a"]},
        {"event_types": ["a"]},
        # The secret is always generated, never chosen by the caller.
        {"url": "https://example.com/hook", "event_types": ["a"], "secret": "mine"},
    ],
)
def test_create_rejects_invalid_input(client: TestClient, payload: dict):
    assert client.post("/endpoints", json=payload).status_code == 422


@pytest.mark.parametrize(
    "url", ["http://localhost:9000/hook", "http://10.0.0.5/hook", "http://169.254.169.254/"]
)
def test_create_rejects_internal_urls(client: TestClient, url: str):
    response = client.post("/endpoints", json=NEW_ENDPOINT | {"url": url})

    assert response.status_code == 422
    assert response.json() == {"detail": "URL resolves to a private or internal address"}
    assert client.get("/endpoints").json() == []


def test_create_allows_localhost_only_in_debug(client: TestClient):
    debug = Settings(_env_file=None, api_key_hash=hash_api_key(TEST_API_KEY), debug=True)
    app.dependency_overrides[get_settings] = lambda: debug

    assert create(client, url="http://localhost:9000/hook")["url"] == "http://localhost:9000/hook"
    response = client.post("/endpoints", json=NEW_ENDPOINT | {"url": "http://10.0.0.5/hook"})
    assert response.status_code == 422


# --- read ---


def test_list_returns_all_endpoints(client: TestClient):
    ids = {create(client)["id"], create(client)["id"]}

    response = client.get("/endpoints")

    assert response.status_code == 200
    assert {endpoint["id"] for endpoint in response.json()} == ids


def test_get_unknown_endpoint_is_404(client: TestClient):
    assert client.get(f"/endpoints/{uuid.uuid4()}").status_code == 404


def test_get_with_malformed_id_is_422(client: TestClient):
    assert client.get("/endpoints/not-a-uuid").status_code == 422


# --- pause / resume ---


def test_patch_pauses_and_resumes(client: TestClient):
    endpoint_id = create(client)["id"]

    paused = client.patch(f"/endpoints/{endpoint_id}", json={"is_active": False})
    assert paused.status_code == 200
    assert paused.json()["is_active"] is False
    assert client.get(f"/endpoints/{endpoint_id}").json()["is_active"] is False

    resumed = client.patch(f"/endpoints/{endpoint_id}", json={"is_active": True})
    assert resumed.json()["is_active"] is True


@pytest.mark.parametrize(
    "payload", [{}, {"is_active": "maybe"}, {"is_active": False, "url": "https://x.io"}]
)
def test_patch_rejects_invalid_input(client: TestClient, payload: dict):
    endpoint_id = create(client)["id"]

    assert client.patch(f"/endpoints/{endpoint_id}", json=payload).status_code == 422


def test_patch_unknown_endpoint_is_404(client: TestClient):
    response = client.patch(f"/endpoints/{uuid.uuid4()}", json={"is_active": False})

    assert response.status_code == 404


# --- delete ---


def test_delete_removes_endpoint(client: TestClient):
    endpoint_id = create(client)["id"]

    response = client.delete(f"/endpoints/{endpoint_id}")

    assert response.status_code == 204
    assert response.content == b""
    assert client.get(f"/endpoints/{endpoint_id}").status_code == 404


def test_delete_unknown_endpoint_is_404(client: TestClient):
    assert client.delete(f"/endpoints/{uuid.uuid4()}").status_code == 404


# --- stats ---


def test_stats_counts_last_24_hours_and_success_rate(client: TestClient, db: Session):
    endpoint = db.get(Endpoint, uuid.UUID(create(client)["id"]))
    for status in ["succeeded"] * 3 + ["dead", "pending", "in_progress"]:
        add_delivery(db, endpoint, status=DeliveryStatus(status))
    # Older than the window: not counted.
    old = add_delivery(db, endpoint, status=DeliveryStatus.DEAD)
    old.created_at = db.scalar(select(func.now())) - timedelta(hours=25)
    db.flush()

    [stats] = client.get("/endpoints/stats").json()

    assert stats == {
        "endpoint_id": str(endpoint.id),
        "deliveries": {"succeeded": 3, "dead": 1, "pending": 1, "in_progress": 1},
        # Unfinished deliveries count for neither side.
        "success_rate": 0.75,
        # Every dead delivery, old ones included: what replay-dead would replay.
        "dead_total": 2,
    }


def test_stats_include_endpoints_without_finished_deliveries(client: TestClient, db: Session):
    idle = create(client)["id"]
    waiting = db.get(Endpoint, uuid.UUID(create(client)["id"]))
    add_delivery(db, waiting)

    stats = {s["endpoint_id"]: s for s in client.get("/endpoints/stats").json()}

    assert stats[idle] == {
        "endpoint_id": idle,
        "deliveries": {},
        "success_rate": None,
        "dead_total": 0,
    }
    assert stats[str(waiting.id)]["deliveries"] == {"pending": 1}
    assert stats[str(waiting.id)]["success_rate"] is None


def test_stats_route_is_not_taken_for_an_endpoint_id(client: TestClient):
    assert client.get("/endpoints/stats").status_code == 200


def test_stats_dead_total_is_per_endpoint(client: TestClient, db: Session):
    first = db.get(Endpoint, uuid.UUID(create(client)["id"]))
    second = db.get(Endpoint, uuid.UUID(create(client)["id"]))
    for _ in range(3):
        add_delivery(db, first, status=DeliveryStatus.DEAD)
    add_delivery(db, second, status=DeliveryStatus.DEAD)
    add_delivery(db, second, status=DeliveryStatus.SUCCEEDED)

    stats = {s["endpoint_id"]: s for s in client.get("/endpoints/stats").json()}

    assert stats[str(first.id)]["dead_total"] == 3
    assert stats[str(second.id)]["dead_total"] == 1
    assert stats[str(second.id)]["deliveries"] == {"dead": 1, "succeeded": 1}
