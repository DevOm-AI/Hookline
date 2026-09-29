from datetime import UTC, datetime, timedelta

import httpx2
import pytest

from scripts.load_test import (
    TARGET_P95_MS,
    StepResult,
    arrivals,
    clean_up,
    latencies_ms,
    parse_k6_summary,
    percentile,
    step_event_types,
)

T0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
K6 = {"rate": 50, "accepted": 3, "requests": 3, "dropped": 0, "post_p95_ms": 12.5}


@pytest.mark.parametrize(("p", "expected"), [(50, 50), (95, 95), (99, 99), (100, 100), (1, 1)])
def test_percentile_is_nearest_rank(p: float, expected: float):
    assert percentile(list(range(100, 0, -1)), p) == expected


def test_percentile_of_a_few_values():
    assert percentile([300.0, 100.0, 200.0], 95) == 300.0
    assert percentile([300.0, 100.0, 200.0], 50) == 200.0
    assert percentile([7.0], 95) == 7.0


def test_percentile_needs_values():
    with pytest.raises(ValueError):
        percentile([], 95)


def test_latency_runs_from_accepted_to_first_attempt():
    created = {"a": T0, "b": T0, "never-arrived": T0}
    first_seen = {"a": T0 + timedelta(milliseconds=250), "b": T0 + timedelta(seconds=1.5)}

    assert sorted(latencies_ms(created, first_seen)) == [250.0, 1500.0]


def test_latency_ignores_events_from_other_runs():
    assert latencies_ms({"a": T0}, {"a": T0, "other": T0}) == [0.0]


def test_parses_the_tagged_k6_summary():
    stdout = 'some k6 output\nHOOKLINE_K6_SUMMARY {"rate": 50, "accepted": 3}\n'

    assert parse_k6_summary(stdout) == {"rate": 50, "accepted": 3}


def test_missing_k6_summary_is_an_error():
    with pytest.raises(ValueError):
        parse_k6_summary("k6 crashed\n")


def test_step_passes_when_everything_arrived_fast_enough():
    result = StepResult.measure(K6, delivered=3, latencies=[100.0, 200.0, 900.0])

    assert (result.p50_ms, result.p95_ms, result.max_ms) == (200.0, 900.0, 900.0)
    assert result.passed


@pytest.mark.parametrize(
    ("k6", "delivered", "latencies"),
    [
        (K6, 3, [100.0, 200.0, TARGET_P95_MS]),  # p95 at the target is a miss
        (K6 | {"dropped": 4}, 3, [100.0, 200.0, 300.0]),  # the rate wasn't held
        (K6, 2, [100.0, 200.0]),  # an event never arrived
        (K6 | {"accepted": 0}, 0, []),  # nothing was accepted
        (K6 | {"requests": 4}, 3, [100.0, 200.0, 300.0]),  # one request wasn't accepted
    ],
)
def test_step_misses(k6: dict, delivered: int, latencies: list[float]):
    assert not StepResult.measure(k6, delivered, latencies).passed


def test_arrivals_count_only_this_steps_delivered_events():
    received = {
        "mine": {"first_seen_at": "2026-09-29T12:00:00.250000+00:00", "delivered": 1},
        "mine-failed-so-far": {"first_seen_at": "2026-09-29T12:00:00+00:00", "delivered": 0},
        # A late delivery from the step before, after the receiver was reset.
        "earlier-step": {"first_seen_at": "2026-09-29T12:00:00+00:00", "delivered": 1},
    }

    arrived = arrivals(received, {"mine", "mine-failed-so-far", "not-yet"})

    assert arrived == {"mine": T0 + timedelta(milliseconds=250)}


def test_every_step_gets_its_own_event_type():
    types = step_event_types("abc", [50, 50, 100])

    assert types == ["loadtest.abc.s1.r50", "loadtest.abc.s2.r50", "loadtest.abc.s3.r100"]


def client(status_code: int, calls: list[str]) -> httpx2.Client:
    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(f"{request.method} {request.url.path}")
        return httpx2.Response(status_code)

    return httpx2.Client(base_url="http://test", transport=httpx2.MockTransport(handler))


def test_clean_up_reports_nothing_when_both_succeed():
    calls: list[str] = []

    assert clean_up(client(204, calls), client(204, calls), "ep1") == []
    assert calls == ["DELETE /endpoints/ep1", "DELETE /received"]


def test_clean_up_reports_a_failure_and_still_tries_the_rest():
    calls: list[str] = []

    failures = clean_up(client(500, calls), client(204, calls), "ep1")

    assert calls == ["DELETE /endpoints/ep1", "DELETE /received"]
    assert len(failures) == 1
    assert failures[0].startswith("Couldn't delete the test endpoint")
