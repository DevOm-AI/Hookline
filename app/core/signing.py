"""Webhook signatures: Hookline-Signature: t=<unix time>,v1=<hex HMAC-SHA256>.

The signed message is f"{t}.{raw_body}", keyed with the endpoint's secret (the whole string,
whsec_ prefix included). Putting t inside the HMAC means a captured request can't be
replayed later with a fresh timestamp; receivers reject anything older than 5 minutes.

The README carries a standalone copy of verify_signature for receivers, and a test checks
that copy against this module.
"""

import hashlib
import hmac
import time

SIGNATURE_HEADER = "Hookline-Signature"
DEFAULT_TOLERANCE_SECONDS = 300


def compute_signature(secret: str, timestamp: int, body: bytes) -> str:
    message = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def sign(secret: str, body: bytes, timestamp: int | None = None) -> str:
    """The Hookline-Signature header value for this body, signed now unless timestamp is given."""
    if timestamp is None:
        timestamp = int(time.time())
    return f"t={timestamp},v1={compute_signature(secret, timestamp, body)}"


def verify_signature(
    secret: str,
    body: bytes,
    header: str,
    tolerance: int = DEFAULT_TOLERANCE_SECONDS,
    now: float | None = None,
) -> bool:
    """True if the header is a valid signature of body, made within `tolerance` seconds.

    body must be the raw request bytes: re-serialised JSON can differ by a single space and
    fail. Several v1 values are accepted, so a secret can be rotated without downtime.
    """
    timestamp = None
    signatures = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t" and value.isdigit():
            timestamp = int(value)
        elif key == "v1":
            signatures.append(value)
    if timestamp is None or not signatures:
        return False

    # Too old is a replay; too far ahead is a forged or badly skewed clock.
    if abs((time.time() if now is None else now) - timestamp) > tolerance:
        return False

    expected = compute_signature(secret, timestamp, body)
    # compare_digest takes the same time wherever the strings differ, so the signature
    # can't be guessed a character at a time.
    return any(hmac.compare_digest(expected, signature) for signature in signatures)
