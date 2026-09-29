import json
import re
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx2
import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core import signing
from app.core.config import Settings
from app.core.signing import verify_signature
from app.models import Delivery, DeliveryAttempt, DeliveryStatus, Endpoint, Event
from app.workers import delivery as worker
from app.workers.celery_app import celery_app
from app.workers.delivery import (
    MAX_ATTEMPTS,
    MAX_ERROR_BODY_BYTES,
    RETRY_DELAYS,
    Outgoing,
    deliver,
    retry_delay,
)
from app.workers.scheduler import LOCK_DURATION, claim_due_deliveries
from app.workers.sweeper import release_stuck_deliveries

PAYLOAD = {"order_id": 42, "note": "café"}
SECRET = "whsec_x"

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
        url=url, secret=SECRET, event_types=["order.shipped"], is_active=endpoint_active
    )
    event = Event(type="order.shipped", payload=PAYLOAD, idempotency_key=str(uuid.uuid4()))
    db.add_all([endpoint, event])
    db.flush()
    delivery = Delivery(event_id=event.id, endpoint_id=endpoint.id, status=status)
    if status == DeliveryStatus.IN_PROGRESS:
        delivery.locked_until = db.scalar(select(func.now())) + LOCK_DURATION
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


def test_signs_the_exact_bytes_sent_with_the_endpoint_secret(db: Session, receiver: Receiver):
    run(db, add_delivery(db))

    [request] = receiver.requests
    header = request.headers["Hookline-Signature"]
    assert re.fullmatch(r"t=\d+,v1=[0-9a-f]{64}", header)
    assert verify_signature(SECRET, request.content, header)
    assert not verify_signature("whsec_other", request.content, header)


def test_every_try_is_signed_with_the_time_it_was_sent(
    db: Session, receiver: Receiver, monkeypatch: pytest.MonkeyPatch
):
    clock = iter([1_727_600_000, 1_727_600_060])
    monkeypatch.setattr(signing.time, "time", lambda: next(clock))
    delivery = add_delivery(db)

    run(db, delivery)
    db.execute(update(Delivery).values(status=DeliveryStatus.IN_PROGRESS))
    run(db, delivery)

    headers = [r.headers["Hookline-Signature"] for r in receiver.requests]
    assert [h.split(",")[0] for h in headers] == ["t=1727600000", "t=1727600060"]


def test_secret_stays_out_of_logs():
    outgoing = Outgoing(
        url="https://example.com/hook",
        attempt_count=0,
        locked_until=None,
        secret="whsec_do_not_log",
        event_id=uuid.uuid4(),
        event_type="order.shipped",
        body=b"{}",
    )

    assert "whsec_do_not_log" not in repr(outgoing)


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
    # One attempt, not two: the addresses are one host, and trying again later may work.
    assert delivery.status == DeliveryStatus.PENDING
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
    assert delivery.attempt_count == 1


@pytest.mark.parametrize("status_code", [301, 400, 401, 404, 410, 422])
def test_other_non_2xx_is_dead_straight_away(db: Session, receiver: Receiver, status_code: int):
    """The request itself is wrong: sending it again would get the same answer."""
    receiver.handler = lambda request: httpx2.Response(status_code, text="receiver says no")
    delivery = add_delivery(db)

    run(db, delivery)

    assert delivery.status == DeliveryStatus.DEAD
    assert delivery.attempt_count == 1
    assert delivery.locked_until is None
    [attempt] = attempts_for(db, delivery)
    assert attempt.status_code == status_code
    assert attempt.error == f"HTTP {status_code}: receiver says no"


@pytest.mark.parametrize("status_code", [429, 500, 502, 503, 504])
def test_5xx_and_429_are_retried(db: Session, receiver: Receiver, status_code: int):
    receiver.handler = lambda request: httpx2.Response(status_code, text="receiver says no")
    delivery = add_delivery(db)

    run(db, delivery)

    assert delivery.status == DeliveryStatus.PENDING
    assert delivery.attempt_count == 1
    assert delivery.locked_until is None
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

    assert delivery.status == DeliveryStatus.PENDING
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


def test_host_that_no_longer_resolves_is_retried(
    db: Session, receiver: Receiver, dns: dict[str, list[str]]
):
    """A DNS outage can fix itself, unlike an internal address."""
    delivery = add_delivery(db, url="https://gone.example.com/hook")

    run(db, delivery)

    assert receiver.requests == []
    assert delivery.status == DeliveryStatus.PENDING
    assert attempts_for(db, delivery)[0].error == "URL host could not be resolved"


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


def set_mid_request(db: Session, delivery: Delivery, **values: object) -> Handler:
    """A receiver that answers 500, after something else changed the delivery meanwhile."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        db.execute(update(Delivery).where(Delivery.id == delivery.id).values(**values))
        return httpx2.Response(500)

    return handler


def test_result_is_dropped_once_the_sweeper_released_the_delivery(db: Session, receiver: Receiver):
    """The request outlived the lock; the sweeper handed the delivery back meanwhile."""
    delivery = add_delivery(db)
    receiver.handler = set_mid_request(
        db, delivery, status=DeliveryStatus.PENDING, locked_until=None
    )

    run(db, delivery)

    assert len(receiver.requests) == 1
    assert delivery.status == DeliveryStatus.PENDING
    assert delivery.attempt_count == 0
    assert attempts_for(db, delivery) == []


def test_result_never_overwrites_a_newer_claim(db: Session, receiver: Receiver):
    """Released and claimed again by another worker: in_progress again, but not ours."""
    delivery = add_delivery(db)
    # A later claim's lock. Here it's the same transaction, so now() alone wouldn't differ.
    new_lock = delivery.locked_until + timedelta(seconds=30)
    receiver.handler = set_mid_request(db, delivery, locked_until=new_lock)

    run(db, delivery)

    assert delivery.status == DeliveryStatus.IN_PROGRESS
    assert delivery.locked_until == new_lock
    assert delivery.attempt_count == 0
    assert attempts_for(db, delivery) == []


def test_result_never_overwrites_one_saved_by_another_worker(db: Session, receiver: Receiver):
    delivery = add_delivery(db)
    receiver.handler = set_mid_request(
        db, delivery, status=DeliveryStatus.SUCCEEDED, attempt_count=2, locked_until=None
    )

    run(db, delivery)

    # The 500 this worker got doesn't turn the success back into a retry.
    assert delivery.status == DeliveryStatus.SUCCEEDED
    assert delivery.attempt_count == 2
    assert attempts_for(db, delivery) == []


def test_result_is_saved_when_the_lock_expired_but_nobody_took_over(
    db: Session, receiver: Receiver
):
    delivery = add_delivery(db)
    db.execute(
        update(Delivery)
        .where(Delivery.id == delivery.id)
        .values(locked_until=func.now() - timedelta(seconds=1))
    )

    run(db, delivery)

    assert delivery.status == DeliveryStatus.SUCCEEDED
    assert delivery.attempt_count == 1
    assert len(attempts_for(db, delivery)) == 1


def test_tasks_are_acked_only_after_they_finish():
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1


class WorkerKilled(Exception):
    pass


def test_crash_after_the_receiver_got_it_means_it_is_sent_again(
    db: Session, receiver: Receiver, monkeypatch: pytest.MonkeyPatch
):
    """At least once: the receiver answered 2xx, but the worker died before saving that."""
    delivery = add_delivery(db)

    def killed(*args: object) -> None:
        raise WorkerKilled

    with monkeypatch.context() as crash:
        crash.setattr(worker, "_record", killed)
        with pytest.raises(WorkerKilled):
            run(db, delivery)

    # Nothing was saved. Once the lock runs out, the sweeper hands it back to the scheduler.
    db.execute(
        update(Delivery)
        .where(Delivery.id == delivery.id)
        .values(locked_until=func.now() - timedelta(seconds=1))
    )
    assert release_stuck_deliveries(db) == [delivery.id]
    assert claim_due_deliveries(db) == [delivery.id]
    run(db, delivery)

    # The receiver got it twice, with the same id to dedupe on.
    assert [r.headers["Hookline-Event-Id"] for r in receiver.requests] == [
        str(delivery.event_id)
    ] * 2
    assert delivery.status == DeliveryStatus.SUCCEEDED
    # Only the attempt that was saved counts.
    assert delivery.attempt_count == 1
    assert len(attempts_for(db, delivery)) == 1


class LocalReceiver:
    """A real HTTP server on 127.0.0.1. Answers `fail_with` in order, then 204 for good."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.received: list[dict] = []
        self.fail_with: list[int] = []


@pytest.fixture
def local_receiver() -> Iterator[LocalReceiver]:
    receiver: LocalReceiver

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            receiver.received.append(
                {"path": self.path, "headers": dict(self.headers), "body": body}
            )
            self.send_response(receiver.fail_with.pop(0) if receiver.fail_with else 204)
            self.end_headers()

        def log_message(self, *args) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    receiver = LocalReceiver(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield receiver
    finally:
        server.shutdown()
        server.server_close()


def test_delivers_over_a_real_connection(
    db: Session, settings: Settings, local_receiver: LocalReceiver
):
    """No mock transport: the pinned-address request really goes over a socket."""
    port, received = local_receiver.port, local_receiver.received
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
    assert verify_signature(SECRET, request["body"], request["headers"]["Hookline-Signature"])


def test_loopback_receiver_is_blocked_without_debug(db: Session, local_receiver: LocalReceiver):
    port, received = local_receiver.port, local_receiver.received
    delivery = add_delivery(db, url=f"http://localhost:{port}/hook")

    run(db, delivery)

    assert received == []
    assert delivery.status == DeliveryStatus.DEAD


def db_now(db: Session) -> datetime:
    # Tests run in one transaction, so now() is the same instant for the test and the worker.
    return db.scalar(select(func.now()))


@pytest.mark.parametrize(
    ("previous_attempts", "wait"),
    [(0, timedelta(seconds=10)), (1, timedelta(minutes=1)), (2, timedelta(minutes=5))]
    + [(3, timedelta(minutes=30))],
)
def test_retry_waits_follow_the_backoff_table(
    db: Session,
    receiver: Receiver,
    monkeypatch: pytest.MonkeyPatch,
    previous_attempts: int,
    wait: timedelta,
):
    monkeypatch.setattr(worker.random, "uniform", lambda low, high: 1.0)
    receiver.handler = lambda request: httpx2.Response(503)
    delivery = add_delivery(db)
    delivery.attempt_count = previous_attempts
    db.flush()

    run(db, delivery)

    assert delivery.status == DeliveryStatus.PENDING
    assert delivery.attempt_count == previous_attempts + 1
    assert delivery.next_attempt_at - db_now(db) == wait


@pytest.mark.parametrize("factor", [0.8, 1.2])
def test_retry_wait_is_jittered(
    db: Session, receiver: Receiver, monkeypatch: pytest.MonkeyPatch, factor: float
):
    monkeypatch.setattr(worker.random, "uniform", lambda low, high: factor)
    receiver.handler = lambda request: httpx2.Response(503)
    delivery = add_delivery(db)

    run(db, delivery)

    assert delivery.next_attempt_at - db_now(db) == timedelta(seconds=10) * factor


@pytest.mark.parametrize("attempt", range(1, MAX_ATTEMPTS))
def test_jitter_stays_within_20_percent(attempt: int):
    base = RETRY_DELAYS[attempt - 1]
    delays = [retry_delay(attempt) for _ in range(1000)]

    assert all(base * 0.8 <= delay <= base * 1.2 for delay in delays)
    # Actually spread out, not one fixed value.
    assert len(set(delays)) > 900
    assert min(delays) < base * 0.9 and max(delays) > base * 1.1


def test_fifth_failure_is_dead(db: Session, receiver: Receiver):
    assert MAX_ATTEMPTS == 5
    receiver.handler = lambda request: httpx2.Response(503)
    delivery = add_delivery(db)
    delivery.attempt_count = MAX_ATTEMPTS - 1
    db.flush()

    run(db, delivery)

    assert delivery.status == DeliveryStatus.DEAD
    assert delivery.attempt_count == MAX_ATTEMPTS
    assert delivery.locked_until is None


def test_failing_receiver_gets_five_tries_then_dead(db: Session, receiver: Receiver):
    receiver.handler = lambda request: httpx2.Response(500)
    delivery = add_delivery(db)

    for _ in range(MAX_ATTEMPTS):
        db.execute(
            update(Delivery)
            .where(Delivery.id == delivery.id)
            .values(status=DeliveryStatus.IN_PROGRESS)
        )
        run(db, delivery)

    assert len(receiver.requests) == MAX_ATTEMPTS
    assert delivery.status == DeliveryStatus.DEAD
    assert [a.status_code for a in attempts_for(db, delivery)] == [500] * MAX_ATTEMPTS


def test_receiver_failing_twice_gets_the_event_on_the_third_try(
    db: Session, settings: Settings, local_receiver: LocalReceiver
):
    """The guide's "done when": scheduler and worker together, over a real connection."""
    settings.debug = True  # loopback receivers are allowed only with DEBUG
    local_receiver.fail_with = [503, 503]
    delivery = add_delivery(db, url=f"http://localhost:{local_receiver.port}/hook")
    delivery.status = DeliveryStatus.PENDING
    db.flush()

    for _ in range(3):
        assert claim_due_deliveries(db) == [delivery.id]
        worker.send_delivery(delivery.id)
        db.expire_all()
        if delivery.status == DeliveryStatus.PENDING:
            # Waiting for the retry: not due yet, so the scheduler leaves it alone...
            assert claim_due_deliveries(db) == []
            # ...until its time comes. Jump there instead of sleeping.
            db.execute(
                update(Delivery)
                .where(Delivery.id == delivery.id)
                .values(next_attempt_at=func.now() - timedelta(seconds=1))
            )

    assert delivery.status == DeliveryStatus.SUCCEEDED
    assert delivery.attempt_count == 3
    assert [a.status_code for a in attempts_for(db, delivery)] == [503, 503, 204]
    assert len(local_receiver.received) == 3
    final = local_receiver.received[-1]
    assert json.loads(final["body"]) == PAYLOAD
    assert verify_signature(SECRET, final["body"], final["headers"]["Hookline-Signature"])
