import json
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

import httpx2
from sqlalchemy import func, select, update
from sqlalchemy.orm import joinedload

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.signing import SIGNATURE_HEADER, sign
from app.core.url_safety import (
    UnresolvableHostError,
    UnsafeURLError,
    resolve_public_addresses,
)
from app.models import Delivery, DeliveryAttempt, DeliveryStatus
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10.0
# Enough of a failed response's body to debug it, without trusting its size.
MAX_ERROR_BODY_BYTES = 1024
USER_AGENT = "Hookline/0.1"

# Wait before the next try, after each failed attempt. A failure after the last wait is final:
# 5 attempts in all, spread over about 36 minutes.
RETRY_DELAYS = (
    timedelta(seconds=10),
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=30),
)
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1
# ±20%, so a burst of deliveries that failed together doesn't retry in the same second.
JITTER = 0.2

# Tests swap in an httpx2.MockTransport; None means real network connections.
_transport: httpx2.BaseTransport | None = None


@dataclass(frozen=True)
class Outgoing:
    """Everything needed to send, read up front so no transaction stays open during the POST."""

    url: str
    # Attempts made before this one.
    attempt_count: int
    # repr=False keeps the secret out of logs and tracebacks.
    secret: str = field(repr=False)
    event_id: uuid.UUID
    event_type: str
    body: bytes


@dataclass(frozen=True)
class AttemptResult:
    status_code: int | None
    response_ms: int
    error: str | None
    # Could trying again help? Yes for timeouts, connection errors, 5xx and 429; no for other
    # responses, where the request itself is wrong and resending it changes nothing.
    retryable: bool

    @property
    def succeeded(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300


def _is_retryable_status(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


@celery_app.task(name="hookline.deliver")
def deliver(delivery_id: str) -> None:
    """Send one claimed delivery to its endpoint and record the attempt."""
    send_delivery(uuid.UUID(delivery_id))


def send_delivery(delivery_id: uuid.UUID) -> None:
    outgoing = _load(delivery_id)
    if outgoing is None:
        return
    result = _post(outgoing)
    # A crash here, after the receiver got the request but before the result is saved, means
    # the sweeper hands the delivery back and it is sent again: at-least-once, not exactly-once.
    _record(delivery_id, outgoing.attempt_count + 1, result)


def _load(delivery_id: uuid.UUID) -> Outgoing | None:
    with SessionLocal() as db:
        delivery = db.scalar(
            select(Delivery)
            .where(Delivery.id == delivery_id)
            .options(joinedload(Delivery.event), joinedload(Delivery.endpoint))
        )
        if delivery is None:
            # Its endpoint or event was deleted after the claim (the rows cascade).
            logger.info("Delivery %s no longer exists; skipping", delivery_id)
            return None
        if delivery.status != DeliveryStatus.IN_PROGRESS:
            # A redelivered task message (acks_late) for a delivery that is already settled.
            logger.info(
                "Delivery %s is %s, not in_progress; skipping", delivery_id, delivery.status
            )
            return None
        if not delivery.endpoint.is_active:
            # Paused after the claim: hand it back, it goes out once the endpoint is resumed.
            delivery.status = DeliveryStatus.PENDING
            delivery.locked_until = None
            db.commit()
            logger.info("Endpoint of delivery %s is paused; back to pending", delivery_id)
            return None

        return Outgoing(
            url=delivery.endpoint.url,
            attempt_count=delivery.attempt_count,
            secret=delivery.endpoint.secret,
            event_id=delivery.event.id,
            event_type=delivery.event.type,
            body=json.dumps(delivery.event.payload, separators=(",", ":")).encode(),
        )


def _post(outgoing: Outgoing) -> AttemptResult:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        # Error bodies are read capped; uncompressed keeps that cap close to what's on the wire.
        "Accept-Encoding": "identity",
        "Hookline-Event-Id": str(outgoing.event_id),
        "Hookline-Event-Type": outgoing.event_type,
        # Signed per try with the current time, over exactly the bytes sent below.
        SIGNATURE_HEADER: sign(outgoing.secret, outgoing.body),
    }
    started = time.monotonic()

    def elapsed_ms() -> int:
        return round((time.monotonic() - started) * 1000)

    try:
        urls, headers["Host"], extensions = _pin_addresses(outgoing.url)
        # No redirects: a 3xx could point anywhere, including internal addresses.
        # No trust_env: an HTTP(S)_PROXY from the environment would bypass the pinned address.
        with httpx2.Client(
            transport=_transport,
            timeout=REQUEST_TIMEOUT,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            for index, url in enumerate(urls):
                try:
                    with client.stream(
                        "POST", url, content=outgoing.body, headers=headers, extensions=extensions
                    ) as response:
                        code, response_ms = response.status_code, elapsed_ms()
                        if 200 <= code < 300:
                            return AttemptResult(code, response_ms, None, retryable=False)
                        return AttemptResult(
                            code,
                            response_ms,
                            _error_body(response),
                            retryable=_is_retryable_status(code),
                        )
                except httpx2.ConnectError:
                    # Refused or unreachable (e.g. a host's IPv6 address on an IPv4-only
                    # network): try its next checked address, as any HTTP client would.
                    if index == len(urls) - 1:
                        raise
    except UnresolvableHostError as exc:
        # A DNS outage or a record that's being changed: worth another try.
        return AttemptResult(None, elapsed_ms(), f"{exc}", retryable=True)
    except UnsafeURLError as exc:
        # Resolves to an internal address: sending again would still be refused.
        return AttemptResult(None, elapsed_ms(), f"Blocked: {exc}", retryable=False)
    except httpx2.TimeoutException:
        error = f"Timed out after {REQUEST_TIMEOUT:g}s"
        return AttemptResult(None, elapsed_ms(), error, retryable=True)
    except httpx2.HTTPError as exc:
        # Connection refused or reset, TLS failure, broken response: network trouble.
        error = f"{type(exc).__name__}: {exc}"[:500]
        return AttemptResult(None, elapsed_ms(), error, retryable=True)
    raise AssertionError("unreachable: resolve_public_addresses never returns an empty list")


def _pin_addresses(raw_url: str) -> tuple[list[httpx2.URL], str, dict[str, str]]:
    """Resolve and check the host now, then connect only to the addresses checked.

    Returns one URL per address, with the host swapped for that IP, plus the Host header value
    and the request extensions (TLS SNI).

    Checking the name and letting the HTTP client resolve it again would leave a gap for DNS
    rebinding: the second lookup could return an internal address. The original host still
    goes in the Host header and in TLS SNI, so virtual hosts and certificate checks work.
    """
    url = httpx2.URL(raw_url)
    addresses = resolve_public_addresses(raw_url, allow_loopback=get_settings().debug)
    # raw_host: the ASCII (punycode) form a TLS handshake needs.
    sni = url.raw_host.decode("ascii")
    extensions = {"sni_hostname": sni} if url.scheme == "https" else {}
    urls = [url.copy_with(host=str(address)) for address in addresses]
    return urls, url.netloc.decode("ascii"), extensions


def _error_body(response: httpx2.Response) -> str:
    """The start of the response body, read no further than MAX_ERROR_BODY_BYTES."""
    received = b""
    for chunk in response.iter_bytes():
        received += chunk
        if len(received) >= MAX_ERROR_BODY_BYTES:
            break
    text = received[:MAX_ERROR_BODY_BYTES].decode("utf-8", errors="replace").strip()
    return f"HTTP {response.status_code}" + (f": {text}" if text else "")


def retry_delay(attempt: int) -> timedelta:
    """Wait after failed attempt number `attempt` (1-based), with ±20% random jitter."""
    return RETRY_DELAYS[attempt - 1] * random.uniform(1 - JITTER, 1 + JITTER)


def _record(delivery_id: uuid.UUID, attempt: int, result: AttemptResult) -> None:
    """Log the attempt and move the delivery on: succeeded, pending for a retry, or dead."""
    values: dict = {"attempt_count": attempt, "locked_until": None}
    if result.succeeded:
        values["status"] = DeliveryStatus.SUCCEEDED
        outcome = "succeeded"
    elif result.retryable and attempt < MAX_ATTEMPTS:
        delay = retry_delay(attempt)
        # Back to pending: the scheduler claims it again once next_attempt_at passes.
        # The database clock, like the scheduler's, so worker clock drift doesn't matter.
        values["status"] = DeliveryStatus.PENDING
        values["next_attempt_at"] = func.now() + delay
        outcome = f"retrying in {delay.total_seconds():.0f}s"
    else:
        values["status"] = DeliveryStatus.DEAD
        outcome = "dead" if result.retryable else "dead (not retryable)"

    with SessionLocal() as db:
        updated = db.execute(update(Delivery).where(Delivery.id == delivery_id).values(**values))
        if updated.rowcount == 0:
            # Deleted with its endpoint or event while the request was in flight.
            logger.info("Delivery %s was deleted during the attempt; not recorded", delivery_id)
            return
        db.add(
            DeliveryAttempt(
                delivery_id=delivery_id,
                status_code=result.status_code,
                response_ms=result.response_ms,
                error=result.error,
            )
        )
        db.commit()
    logger.info(
        "Delivery %s attempt %d/%d: %s, %s",
        delivery_id,
        attempt,
        MAX_ATTEMPTS,
        result.error or result.status_code,
        outcome,
    )
