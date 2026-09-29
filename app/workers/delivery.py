import json
import logging
import time
import uuid
from dataclasses import dataclass

import httpx2
from sqlalchemy import select, update
from sqlalchemy.orm import joinedload

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.url_safety import UnsafeURLError, resolve_public_address
from app.models import Delivery, DeliveryAttempt, DeliveryStatus
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10.0
# Enough of a failed response's body to debug it, without trusting its size.
MAX_ERROR_BODY_BYTES = 1024
USER_AGENT = "Hookline/0.1"

# Tests swap in an httpx2.MockTransport; None means real network connections.
_transport: httpx2.BaseTransport | None = None


@dataclass(frozen=True)
class Outgoing:
    """Everything needed to send, read up front so no transaction stays open during the POST."""

    url: str
    event_id: uuid.UUID
    event_type: str
    body: bytes


@dataclass(frozen=True)
class AttemptResult:
    status_code: int | None
    response_ms: int
    error: str | None

    @property
    def succeeded(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300


@celery_app.task(name="hookline.deliver")
def deliver(delivery_id: str) -> None:
    """Send one claimed delivery to its endpoint and record the attempt."""
    send_delivery(uuid.UUID(delivery_id))


def send_delivery(delivery_id: uuid.UUID) -> None:
    outgoing = _load(delivery_id)
    if outgoing is None:
        return
    result = _post(outgoing)
    _record(delivery_id, result)


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
    }
    started = time.monotonic()

    def elapsed_ms() -> int:
        return round((time.monotonic() - started) * 1000)

    try:
        url, headers["Host"], extensions = _pin_address(outgoing.url)
        # No redirects: a 3xx could point anywhere, including internal addresses.
        # No trust_env: an HTTP(S)_PROXY from the environment would bypass the pinned address.
        with (
            httpx2.Client(
                transport=_transport,
                timeout=REQUEST_TIMEOUT,
                follow_redirects=False,
                trust_env=False,
            ) as client,
            client.stream(
                "POST", url, content=outgoing.body, headers=headers, extensions=extensions
            ) as response,
        ):
            response_ms = elapsed_ms()
            if 200 <= response.status_code < 300:
                return AttemptResult(response.status_code, response_ms, None)
            return AttemptResult(response.status_code, response_ms, _error_body(response))
    except UnsafeURLError as exc:
        return AttemptResult(None, elapsed_ms(), f"Blocked: {exc}")
    except httpx2.TimeoutException:
        return AttemptResult(None, elapsed_ms(), f"Timed out after {REQUEST_TIMEOUT:g}s")
    except httpx2.HTTPError as exc:
        return AttemptResult(None, elapsed_ms(), f"{type(exc).__name__}: {exc}"[:500])


def _pin_address(raw_url: str) -> tuple[httpx2.URL, str, dict[str, str]]:
    """Resolve and check the host now, then connect to exactly that address.

    Returns the URL with the host swapped for the checked IP, the Host header value and the
    request extensions (TLS SNI).

    Checking the name and letting the HTTP client resolve it again would leave a gap for DNS
    rebinding: the second lookup could return an internal address. The original host still
    goes in the Host header and in TLS SNI, so virtual hosts and certificate checks work.
    """
    url = httpx2.URL(raw_url)
    address = resolve_public_address(raw_url, allow_loopback=get_settings().debug)
    # raw_host: the ASCII (punycode) form a TLS handshake needs.
    sni = url.raw_host.decode("ascii")
    extensions = {"sni_hostname": sni} if url.scheme == "https" else {}
    return url.copy_with(host=str(address)), url.netloc.decode("ascii"), extensions


def _error_body(response: httpx2.Response) -> str:
    """The start of the response body, read no further than MAX_ERROR_BODY_BYTES."""
    received = b""
    for chunk in response.iter_bytes():
        received += chunk
        if len(received) >= MAX_ERROR_BODY_BYTES:
            break
    text = received[:MAX_ERROR_BODY_BYTES].decode("utf-8", errors="replace").strip()
    return f"HTTP {response.status_code}" + (f": {text}" if text else "")


def _record(delivery_id: uuid.UUID, result: AttemptResult) -> None:
    # Until retries exist, a failed attempt is final: the delivery goes to the dead-letter state.
    status = DeliveryStatus.SUCCEEDED if result.succeeded else DeliveryStatus.DEAD
    with SessionLocal() as db:
        updated = db.execute(
            update(Delivery)
            .where(Delivery.id == delivery_id)
            .values(
                status=status,
                attempt_count=Delivery.attempt_count + 1,
                locked_until=None,
            )
        )
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
    logger.info("Delivery %s %s (%s)", delivery_id, status, result.error or result.status_code)
