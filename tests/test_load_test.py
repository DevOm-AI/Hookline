from datetime import UTC, datetime, timedelta

import pytest

from scripts.load_test import (
    TARGET_P95_MS,
    StepResult,
    latencies_ms,
    parse_k6_summary,
    percentile,
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
    ],
)
def test_step_misses(k6: dict, delivered: int, latencies: list[float]):
    assert not StepResult.measure(k6, delivered, latencies).passed
