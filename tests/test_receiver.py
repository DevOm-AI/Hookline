import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.signing import SIGNATURE_HEADER, sign
from receiver import main as receiver_main
from receiver.main import Behaviour, Receiver, create_app

SECRET = "whsec_receiver_test"
BODY = json.dumps({"order_id": 42}, separators=(",", ":")).encode()


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Behaviour(_env_file=None, secret=SECRET)))


def state(client: TestClient) -> Receiver:
    return client.app.state.receiver


def send(
    client: TestClient,
    event_id: str | None = None,
    secret: str = SECRET,
    body: bytes = BODY,
    signature: str | None = None,
):
    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: signature or sign(secret, body),
        "Hookline-Event-Id": event_id or str(uuid.uuid4()),
    }
    return client.post("/webhook", content=body, headers=headers)


def test_accepts_a_signed_request_and_records_the_event_id(client: TestClient):
    event_id = str(uuid.uuid4())

    response = send(client, event_id)

    assert response.status_code == 204
    received = client.get("/received").json()
    assert list(received) == [event_id]
    assert received[event_id]["attempts"] == 1
    assert received[event_id]["delivered"] == 1
    assert received[event_id]["first_seen_at"]


def test_rejects_the_wrong_secret(client: TestClient):
    assert send(client, secret="whsec_other").status_code == 401
    assert client.get("/received").json() == {}
    assert client.get("/stats").json()["rejected"] == 1


def test_rejects_a_changed_body(client: TestClient):
    signature = sign(SECRET, BODY)

    assert send(client, body=b'{"order_id":43}', signature=signature).status_code == 401


def test_rejects_an_old_signature(client: TestClient):
    """Older than five minutes: a replayed request."""
    old = sign(SECRET, BODY, timestamp=1_000_000_000)

    assert send(client, signature=old).status_code == 401


def test_rejects_a_missing_signature(client: TestClient):
    response = client.post("/webhook", content=BODY, headers={"Hookline-Event-Id": "e1"})

    assert response.status_code == 401


def test_rejects_everything_until_a_secret_is_set():
    client = TestClient(create_app(Behaviour(_env_file=None)))

    assert client.get("/config").json()["secret_set"] is False
    assert send(client).status_code == 401

    client.patch("/config", json={"secret": SECRET})

    assert send(client).status_code == 204


def test_rejects_a_signed_request_without_an_event_id(client: TestClient):
    response = client.post("/webhook", content=BODY, headers={SIGNATURE_HEADER: sign(SECRET, BODY)})

    assert response.status_code == 400
    assert client.get("/stats").json()["rejected"] == 1


def test_counts_duplicates(client: TestClient):
    """At-least-once delivery: the same event id twice is one event, one duplicate."""
    send(client, "event-1")
    send(client, "event-1")
    send(client, "event-2")

    stats = client.get("/stats").json()

    assert stats["delivered"] == 3
    assert stats["unique_events"] == 2
    assert stats["duplicates"] == 1


def test_fails_every_request_at_100_percent(client: TestClient):
    client.patch("/config", json={"fail_percent": 100})

    assert send(client, "event-1").status_code == 500

    received = client.get("/received").json()["event-1"]
    assert (received["attempts"], received["delivered"]) == (1, 0)
    stats = client.get("/stats").json()
    assert (stats["failed"], stats["unique_events"], stats["undelivered_events"]) == (1, 0, 1)


def test_a_failed_event_counts_once_it_is_retried_successfully(client: TestClient):
    client.patch("/config", json={"fail_percent": 100})
    send(client, "event-1")
    first_seen = client.get("/received").json()["event-1"]["first_seen_at"]
    client.patch("/config", json={"fail_percent": 0})

    send(client, "event-1")

    received = client.get("/received").json()["event-1"]
    # First seen stays the first attempt, the failed one.
    assert received == {"first_seen_at": first_seen, "attempts": 2, "delivered": 1}
    stats = client.get("/stats").json()
    assert (stats["unique_events"], stats["duplicates"], stats["undelivered_events"]) == (1, 0, 0)


def test_fails_about_the_requested_share(client: TestClient):
    state(client).random.seed(1234)
    client.patch("/config", json={"fail_percent": 20})

    codes = [send(client).status_code for _ in range(400)]

    assert set(codes) == {204, 500}
    assert 60 <= codes.count(500) <= 100
    assert client.get("/stats").json()["failed"] == codes.count(500)


def test_waits_before_answering(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(receiver_main.asyncio, "sleep", fake_sleep)
    client.patch("/config", json={"delay_ms": 1500})

    assert send(client).status_code == 204
    assert waits == [1.5]


def test_no_wait_by_default(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(receiver_main.asyncio, "sleep", fake_sleep)

    send(client)

    assert waits == []


def test_rejected_requests_are_answered_straight_away(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """Delay and failures apply to validly signed requests only."""

    async def fail_if_called(seconds: float) -> None:
        raise AssertionError("slept for a rejected request")

    monkeypatch.setattr(receiver_main.asyncio, "sleep", fail_if_called)
    client.patch("/config", json={"delay_ms": 1000, "fail_percent": 100})

    assert send(client, secret="whsec_other").status_code == 401


def test_config_updates_only_the_fields_sent(client: TestClient):
    client.patch("/config", json={"fail_percent": 20, "delay_ms": 300})

    response = client.patch("/config", json={"delay_ms": 0, "fail_percent": None})

    assert response.json() == {"secret_set": True, "fail_percent": 20, "delay_ms": 0}
    assert client.get("/config").json() == response.json()


@pytest.mark.parametrize(
    "body", [{"fail_percent": -1}, {"fail_percent": 101}, {"delay_ms": -1}, {"secret": ""}]
)
def test_config_rejects_invalid_values(client: TestClient, body: dict):
    assert client.patch("/config", json=body).status_code == 422
    assert client.get("/config").json() == {"secret_set": True, "fail_percent": 0, "delay_ms": 0}


def test_config_never_shows_the_secret(client: TestClient):
    assert SECRET not in client.get("/config").text
    assert SECRET not in repr(state(client).behaviour)


def test_behaviour_starts_from_the_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RECEIVER_SECRET", SECRET)
    monkeypatch.setenv("RECEIVER_FAIL_PERCENT", "20")
    monkeypatch.setenv("RECEIVER_DELAY_MS", "250")

    client = TestClient(create_app())

    assert client.get("/config").json() == {"secret_set": True, "fail_percent": 20, "delay_ms": 250}


def test_reset_forgets_what_was_received_but_keeps_the_config(client: TestClient):
    client.patch("/config", json={"delay_ms": 0, "fail_percent": 0})
    send(client)
    send(client, secret="whsec_other")

    assert client.delete("/received").status_code == 204

    assert client.get("/received").json() == {}
    assert set(client.get("/stats").json().values()) == {0}
    assert client.get("/config").json()["secret_set"] is True


def test_reset_during_a_request_keeps_the_new_records_clean(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """A request waiting out delay_ms when the reset comes finishes into the old records."""

    async def reset_while_waiting(seconds: float) -> None:
        assert client.delete("/received").status_code == 204

    monkeypatch.setattr(receiver_main.asyncio, "sleep", reset_while_waiting)
    client.patch("/config", json={"delay_ms": 100, "fail_percent": 100})

    assert send(client, "event-1").status_code == 500

    assert client.get("/received").json() == {}
    assert set(client.get("/stats").json().values()) == {0}


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/config"), ("POST", "/received"), ("POST", "/stats"), ("POST", "/reset")],
)
def test_only_the_webhook_takes_post(client: TestClient, method: str, path: str):
    """Hookline only POSTs, so an endpoint aimed at any other path can't touch the records."""
    send(client, "event-1")

    assert client.request(method, path, content=BODY).status_code in {404, 405}

    assert client.get("/stats").json()["unique_events"] == 1
    assert client.get("/config").json()["secret_set"] is True


def test_health(client: TestClient):
    assert client.get("/health").json() == {"status": "ok"}
