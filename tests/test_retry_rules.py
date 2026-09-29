"""The retry rule and backoff schedule on their own, without a database or a receiver.

test_delivery_worker.py checks the same rules end to end, for a sample of status codes.
"""

from datetime import timedelta

import pytest

from app.workers import delivery as worker
from app.workers.delivery import (
    MAX_ATTEMPTS,
    RETRY_DELAYS,
    AttemptResult,
    _is_retryable_status,
    retry_delay,
)


def outcome(status_code: int) -> str:
    """What the worker does after a response with this status code."""
    result = AttemptResult(status_code, 0, None, retryable=_is_retryable_status(status_code))
    if result.succeeded:
        return "succeeded"
    return "retry" if result.retryable else "dead"


def test_every_status_code_has_the_documented_outcome():
    for status_code in range(100, 600):
        if 200 <= status_code < 300:
            expected = "succeeded"
        elif status_code == 429 or status_code >= 500:
            expected = "retry"
        else:
            # 1xx, 3xx and every other 4xx: resending the same request gets the same answer.
            expected = "dead"
        assert outcome(status_code) == expected, status_code


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (200, "succeeded"),
        (204, "succeeded"),
        (299, "succeeded"),
        (301, "dead"),
        (304, "dead"),
        (400, "dead"),
        (401, "dead"),
        (403, "dead"),
        (404, "dead"),
        (408, "dead"),
        (410, "dead"),
        (428, "dead"),
        (429, "retry"),
        (430, "dead"),
        (500, "retry"),
        (502, "retry"),
        (503, "retry"),
        (504, "retry"),
        (599, "retry"),
    ],
)
def test_status_codes_near_the_boundaries(status_code: int, expected: str):
    assert outcome(status_code) == expected


def test_no_status_code_means_no_success():
    """A timeout or connection error has no response at all."""
    assert not AttemptResult(None, 0, "Timed out after 10s", retryable=True).succeeded


def test_backoff_schedule():
    assert RETRY_DELAYS == (
        timedelta(seconds=10),
        timedelta(minutes=1),
        timedelta(minutes=5),
        timedelta(minutes=30),
    )
    # One try, four retries, then dead.
    assert MAX_ATTEMPTS == 5


@pytest.mark.parametrize(
    ("attempt", "wait"),
    [
        (1, timedelta(seconds=10)),
        (2, timedelta(minutes=1)),
        (3, timedelta(minutes=5)),
        (4, timedelta(minutes=30)),
    ],
)
def test_retry_delay_without_jitter(monkeypatch: pytest.MonkeyPatch, attempt: int, wait: timedelta):
    monkeypatch.setattr(worker.random, "uniform", lambda low, high: 1.0)

    assert retry_delay(attempt) == wait


def test_retry_delay_asks_for_20_percent_jitter(monkeypatch: pytest.MonkeyPatch):
    bounds: list[tuple[float, float]] = []

    def uniform(low: float, high: float) -> float:
        bounds.append((low, high))
        return high

    monkeypatch.setattr(worker.random, "uniform", uniform)

    assert retry_delay(1) == timedelta(seconds=12)
    assert bounds == [(0.8, 1.2)]
