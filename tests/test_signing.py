import hashlib
import hmac
import re
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from app.core.signing import sign, verify_signature

ROOT = Path(__file__).resolve().parent.parent
SECRET = "whsec_test_secret"
BODY = b'{"order_id":42}'
NOW = 1_727_600_000

Verifier = Callable[..., bool]


def readme_verify_signature() -> Verifier:
    """The verify_signature receivers copy from the README, exactly as written there."""
    readme = (ROOT / "README.md").read_text()
    match = re.search(r"<!-- verify_signature:.*?-->\n```python\n(.*?)```", readme, re.DOTALL)
    assert match, "README verify_signature snippet not found"
    namespace: dict = {}
    exec(match.group(1), namespace)
    return namespace["verify_signature"]


def app_verify_signature(secret: str, body: bytes, header: str, tolerance: int = 300) -> bool:
    return verify_signature(secret, body, header, tolerance)


@pytest.fixture(params=[app_verify_signature, readme_verify_signature()], ids=["app", "readme"])
def verify(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Verifier:
    """Every test runs against both copies, so the README can't drift from the worker."""
    monkeypatch.setattr(time, "time", lambda: NOW)
    return request.param


def test_signature_matches_the_documented_formula():
    expected = hmac.new(SECRET.encode(), b"1727600000." + BODY, hashlib.sha256).hexdigest()

    assert sign(SECRET, BODY, timestamp=NOW) == f"t=1727600000,v1={expected}"


def test_sign_uses_the_current_time(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(time, "time", lambda: NOW + 0.9)

    assert sign(SECRET, BODY).startswith("t=1727600000,v1=")


def test_accepts_a_valid_signature(verify: Verifier):
    assert verify(SECRET, BODY, sign(SECRET, BODY, timestamp=NOW))


@pytest.mark.parametrize("age", [-300, -1, 1, 299, 300])
def test_accepts_signatures_within_five_minutes(verify: Verifier, age: int):
    assert verify(SECRET, BODY, sign(SECRET, BODY, timestamp=NOW - age))


@pytest.mark.parametrize("age", [301, 3600, -301])
def test_rejects_old_or_future_signatures(verify: Verifier, age: int):
    """A replayed request is older than 5 minutes; a far-future t is forged or skewed."""
    assert not verify(SECRET, BODY, sign(SECRET, BODY, timestamp=NOW - age))


def test_rejects_a_changed_body(verify: Verifier):
    header = sign(SECRET, BODY, timestamp=NOW)

    assert not verify(SECRET, b'{"order_id":43}', header)
    # Same JSON, different bytes: receivers must verify the raw body.
    assert not verify(SECRET, b'{"order_id": 42}', header)


def test_rejects_the_wrong_secret(verify: Verifier):
    assert not verify("whsec_other", BODY, sign(SECRET, BODY, timestamp=NOW))


def test_rejects_a_replayed_signature_with_a_fresh_timestamp(verify: Verifier):
    """t is inside the HMAC, so an attacker can't just bump it on a captured request."""
    old = sign(SECRET, BODY, timestamp=NOW - 3600)
    v1 = old.split("v1=")[1]

    assert not verify(SECRET, BODY, f"t={NOW},v1={v1}")


def test_accepts_any_matching_v1(verify: Verifier):
    """Several v1 values let a secret be rotated without rejecting requests."""
    header = sign(SECRET, BODY, timestamp=NOW)
    v1 = header.split("v1=")[1]

    assert verify(SECRET, BODY, f"t={NOW},v1={'0' * 64},v1={v1}")
    assert verify(SECRET, BODY, f"t={NOW}, v1={v1}")


@pytest.mark.parametrize(
    "header",
    [
        "",
        "garbage",
        f"t={NOW}",
        "v1=abc",
        f"t=-{NOW},v1=abc",
        f"t=soon,v1={'0' * 64}",
        f"t={NOW},v1=",
        f"t={NOW},v0={'0' * 64}",
    ],
)
def test_rejects_malformed_headers(verify: Verifier, header: str):
    assert not verify(SECRET, BODY, header)


def test_custom_tolerance(verify: Verifier):
    header = sign(SECRET, BODY, timestamp=NOW - 30)

    assert verify(SECRET, BODY, header, tolerance=60)
    assert not verify(SECRET, BODY, header, tolerance=10)
