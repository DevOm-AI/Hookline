import json
import threading
import uuid
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx2
import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.models import Delivery, DeliveryAttempt, DeliveryStatus, Endpoint, Event
from app.workers import delivery as worker
from app.workers.celery_app import celery_app
from app.workers.delivery import MAX_ERROR_BODY_BYTES, deliver

PAYLOAD = {"order_id": 42, "note": "café"}

Handler = Callable[[httpx2.Request], httpx2.Response]


@pytest.fixture(autouse=True)
def worker_db(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker's sessions share the test connection, so everything still rolls back."""
    monkeypatch.setattr(
        worker,
        "SessionLocal",
        sessionmaker(
            bind=db.connection(), join_transaction_mode="create_savepoint", expire_on_commit=False
        ),
    )


@pytest.fixture(autouse=True)
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    # Not the local .env, whose DEBUG=true would allow loopback receivers.
    settings = Settings(_env_file=None)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    return settings


class Receiver:
    """A fake receiver: records every request and answers with `handler`."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.handler: Handler = lambda request: httpx2.Response(200)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        request.read()
        self.requests.append(request)
        return self.handler(request)


@pytest.fixture
def receiver(monkeypatch: pytest.MonkeyPatch) -> Receiver:
    receiver = Receiver()
    monkeypatch.setattr(worker, "_transport", httpx2.MockTransport(receiver))
    return receiver


def add_delivery(
    db: Session,
    url: str = "https://example.com/hook",
    status: DeliveryStatus = DeliveryStatus.IN_PROGRESS,
    endpoint_active: bool = True,
) -> Delivery:
    """A delivery as the scheduler leaves it: claimed (in_progress) and locked."""
    endpoint = Endpoint(
        url=url, secret="whsec_x", event_types=["order.shipped"], is_active=endpoint_active
    )
    event = Event(type="order.shipped", payload=PAYLOAD, idempotency_key=str(uuid.uuid4()))
    db.add_all([endpoint, event])
    db.flush()
    delivery = Delivery(event_id=event.id, endpoint_id=endpoint.id, status=status)
    db.add(delivery)
    db.flush()
    db.refresh(delivery)
    return delivery


def attempts_for(db: Session, delivery: Delivery) -> list[DeliveryAttempt]:
    return list(
        db.scalars(select(DeliveryAttempt).where(DeliveryAttempt.delivery_id == delivery.id))
    )


def run(db: Session, delivery: Delivery) -> None:
    deliver(str(delivery.id))
    db.expire_all()


def test_success_marks_delivery_succeeded_and_logs_attempt(db: Session, receiver: Receiver):
    delivery = add_delivery(db)

    run(db, delivery)

    assert delivery.status == DeliveryStatus.SUCCEEDED
    assert delivery.attempt_count == 1
    assert delivery.locked_until is None
    [attempt] = attempts_for(db, delivery)
    assert attempt.status_code == 200
    assert attempt.error is None
    assert attempt.response_ms >= 0


def test_posts_the_json_payload_with_event_headers(db: Session, receiver: Receiver):
    delivery = add_delivery(db)

    run(db, delivery)

    [request] = receiver.requests
    assert request.method == "POST"
    assert request.url.path == "/hook"
    assert json.loads(request.content) == PAYLOAD
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["Hookline-Event-Id"] == str(delivery.event_id)
    assert request.headers["Hookline-Event-Type"] == "order.shipped"


def test_connects_to_the_checked_address_under_the_original_name(db: Session, receiver: Receiver):
    run(db, add_delivery(db, url="https://example.com:8443/hook?x=1"))

    [request] = receiver.requests
    # The IP checked against the SSRF rules, not a second DNS lookup.
    assert request.url.host == "93.184.215.14"
    assert request.url.query == b"x=1"
    assert request.headers["Host"] == "example.com:8443"
    assert request.extensions["sni_hostname"] == "example.com"


def test_falls_back_to_the_next_address_when_one_is_unreachable(
    db: Session, receiver: Receiver, dns: dict[str, list[str]]
):
    """E.g. a host with an IPv6 address the worker's network can't reach."""
    dns["example.com"] = ["2606:2800:21f:cb07:6820:80da:af6b:8b2c", "93.184.215.14"]

    def ipv4_only(request: httpx2.Request) -> httpx2.Response:
        if request.url.host != "93.184.215.14":
            raise httpx2.ConnectError("network unreachable")
        return httpx2.Response(200)

    receiver.handler = ipv4_only
    delivery = add_delivery(db)

    run(db, delivery)

    assert [r.url.host for r in receiver.requests] == [
        "2606:2800:21f:cb07:6820:80da:af6b:8b2c",
        "93.184.215.14",
    ]
    assert delivery.status == DeliveryStatus.SUCCEEDED
    assert len(attempts_for(db, delivery)) == 1


def test_fails_when_every_address_is_unreachable(
    db: Session, receiver: Receiver, dns: dict[str, list[str]]
):
    dns["example.com"] = ["93.184.215.14", "93.184.215.15"]

    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError(f"refused by {request.url.host}")

    receiver.handler = refuse
    delivery = add_delivery(db)

    run(db, delivery)

    assert len(receiver.requests) == 2
    assert delivery.status == DeliveryStatus.DEAD
    [attempt] = attempts_for(db, delivery)
    assert attempt.status_code is None
    assert attempt.error == "ConnectError: refused by 93.184.215.15"


def test_a_response_from_one_address_is_final(
    db: Session, receiver: Receiver, dns: dict[str, list[str]]
):
    """Only connection failures move on to the next address, never an answer or a timeout."""
    dns["example.com"] = ["93.184.215.14", "93.184.215.15"]
    receiver.handler = lambda request: httpx2.Response(503)
    delivery = add_delivery(db)

    run(db, delivery)

    assert len(receiver.requests) == 1
    assert delivery.status == DeliveryStatus.DEAD


@pytest.mark.parametrize("status_code", [400, 404, 429, 500, 503])
def test_non_2xx_marks_delivery_dead_with_the_response(
    db: Session, receiver: Receiver, status_code: int
):
    receiver.handler = lambda request: httpx2.Response(status_code, text="receiver says no")
    delivery = add_delivery(db)

    run(db, delivery)

    assert delivery.status == DeliveryStatus.DEAD
    assert delivery.attempt_count == 1
    [attempt] = attempts_for(db, delivery)
    assert attempt.status_code == status_code
    assert attempt.error == f"HTTP {status_code}: receiver says no"


def test_error_body_is_capped(db: Session, receiver: Receiver):
    receiver.handler = lambda request: httpx2.Response(500, content=b"x" * 50_000)
    delivery = add_delivery(db)

    run(db, delivery)

    [attempt] = attempts_for(db, delivery)
    assert attempt.error == "HTTP 500: " + "x" * MAX_ERROR_BODY_BYTES


def test_redirects_are_not_followed(db: Session, receiver: Receiver):
    receiver.handler = lambda request: httpx2.Response(
        302, headers={"Location": "http://169.254.169.254/"}
    )
    delivery = add_delivery(db)

    run(db, delivery)

    assert len(receiver.requests) == 1
    assert delivery.status == DeliveryStatus.DEAD
    assert attempts_for(db, delivery)[0].status_code == 302


@pytest.mark.parametrize(
    ("exc", "error"),
    [
        (httpx2.ReadTimeout("slow"), "Timed out after 10s"),
        (httpx2.ConnectError("refused"), "ConnectError: refused"),
    ],
)
def test_network_failure_is_logged_without_status_code(
    db: Session, receiver: Receiver, exc: Exception, error: str
):
    def fail(request: httpx2.Request) -> httpx2.Response:
        raise exc

    receiver.handler = fail
    delivery = add_delivery(db)

    run(db, delivery)

    assert delivery.status == DeliveryStatus.DEAD
    [attempt] = attempts_for(db, delivery)
    assert attempt.status_code is None
    assert attempt.error == error


def test_host_that_now_resolves_internally_is_blocked(
    db: Session, receiver: Receiver, dns: dict[str, list[str]]
):
    """DNS rebinding: public at registration, internal at send time."""
    delivery = add_delivery(db)
    dns["example.com"] = ["10.0.0.5"]

    run(db, delivery)

    assert receiver.requests == []
    assert delivery.status == DeliveryStatus.DEAD
    [attempt] = attempts_for(db, delivery)
    assert attempt.status_code is None
    assert attempt.error.startswith("Blocked:")


@pytest.mark.parametrize(
    "status", [DeliveryStatus.PENDING, DeliveryStatus.SUCCEEDED, DeliveryStatus.DEAD]
)
def test_skips_delivery_that_is_not_in_progress(
    db: Session, receiver: Receiver, status: DeliveryStatus
):
    """E.g. a task message redelivered after the delivery was already settled."""
    delivery = add_delivery(db, status=status)

    run(db, delivery)

    assert receiver.requests == []
    assert delivery.status == status
    assert delivery.attempt_count == 0
    assert attempts_for(db, delivery) == []


def test_missing_delivery_is_a_no_op(receiver: Receiver):
    deliver(str(uuid.uuid4()))

    assert receiver.requests == []


def test_paused_endpoint_hands_delivery_back_to_pending(db: Session, receiver: Receiver):
    delivery = add_delivery(db, endpoint_active=False)

    run(db, delivery)

    assert receiver.requests == []
    assert delivery.status == DeliveryStatus.PENDING
    assert delivery.locked_until is None
    assert delivery.attempt_count == 0


def test_delivery_deleted_during_the_request_is_not_recorded(db: Session, receiver: Receiver):
    delivery = add_delivery(db)
    delivery_id, endpoint_id = delivery.id, delivery.endpoint_id

    def delete_endpoint_mid_request(request: httpx2.Request) -> httpx2.Response:
        db.execute(delete(Endpoint).where(Endpoint.id == endpoint_id))
        return httpx2.Response(200)

    receiver.handler = delete_endpoint_mid_request

    run(db, delivery)

    assert db.scalars(select(Delivery).where(Delivery.id == delivery_id)).all() == []
    assert db.scalars(select(DeliveryAttempt)).all() == []


def test_tasks_are_acked_only_after_they_finish():
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1


@pytest.fixture
def local_receiver() -> Iterator[tuple[int, list[dict]]]:
    """A real HTTP server on 127.0.0.1, answering 204 and recording what it got."""
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append({"path": self.path, "headers": dict(self.headers), "body": body})
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], received
    finally:
        server.shutdown()
        server.server_close()


def test_delivers_over_a_real_connection(
    db: Session, settings: Settings, local_receiver: tuple[int, list[dict]]
):
    """No mock transport: the pinned-address request really goes over a socket."""
    port, received = local_receiver
    settings.debug = True  # loopback receivers are allowed only with DEBUG
    delivery = add_delivery(db, url=f"http://localhost:{port}/hook")

    run(db, delivery)

    assert delivery.status == DeliveryStatus.SUCCEEDED
    assert attempts_for(db, delivery)[0].status_code == 204
    [request] = received
    assert request["path"] == "/hook"
    assert request["headers"]["Host"] == f"localhost:{port}"
    assert request["headers"]["Hookline-Event-Id"] == str(delivery.event_id)
    assert json.loads(request["body"]) == PAYLOAD


def test_loopback_receiver_is_blocked_without_debug(
    db: Session, local_receiver: tuple[int, list[dict]]
):
    port, received = local_receiver
    delivery = add_delivery(db, url=f"http://localhost:{port}/hook")

    run(db, delivery)

    assert received == []
    assert delivery.status == DeliveryStatus.DEAD
