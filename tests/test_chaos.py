from datetime import UTC, datetime, timedelta

from scripts.chaos import ChaosResult

T0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def record(after_ms: int, delivered: int = 1) -> dict:
    first_seen = T0 + timedelta(milliseconds=after_ms)
    return {"first_seen_at": first_seen.isoformat(), "attempts": delivered, "delivered": delivered}


def test_every_event_arrived():
    created = {"a": T0, "b": T0, "c": T0 + timedelta(seconds=1)}
    received = {"a": record(100), "b": record(300), "c": record(1200)}

    result = ChaosResult.measure(created, received)

    assert (result.sent, result.delivered, result.lost, result.duplicates) == (3, 3, 0, 0)
    assert (result.p50_ms, result.p95_ms) == (200.0, 300.0)
    # The last accepted at T0 + 1 s, the last first attempt at T0 + 1.2 s.
    assert result.all_attempted_after_s == 0.2


def test_an_event_that_never_arrived_is_lost():
    result = ChaosResult.measure({"a": T0, "b": T0}, {"a": record(100)})

    assert (result.delivered, result.lost) == (1, 1)
    assert result.all_attempted_after_s is None


def test_an_event_that_only_ever_failed_is_lost():
    """The receiver saw it, but answered 500 every time: not delivered."""
    result = ChaosResult.measure({"a": T0}, {"a": record(100, delivered=0)})

    assert result.lost == 1


def test_duplicates_count_extra_2xx_answers():
    result = ChaosResult.measure({"a": T0, "b": T0}, {"a": record(100, 3), "b": record(100)})

    assert (result.delivered, result.lost, result.duplicates) == (2, 0, 2)


def test_other_events_at_the_receiver_are_ignored():
    result = ChaosResult.measure({"a": T0}, {"a": record(100), "someone-else": record(5)})

    assert (result.sent, result.delivered, result.duplicates) == (1, 1, 0)


def test_nothing_sent():
    result = ChaosResult.measure({}, {})

    assert (result.sent, result.lost, result.p95_ms, result.all_attempted_after_s) == (
        0,
        0,
        None,
        None,
    )
