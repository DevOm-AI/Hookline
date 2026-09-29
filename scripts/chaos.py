"""The chaos test's measuring half. Run scripts/chaos_test.sh, which kills workers meanwhile.

1. Registers the mock receiver, which fails `--fail-percent` of requests with a 500.
2. k6 sends `--events` events at `--rate` per second.
3. Waits `--drain-timeout` for them to arrive, as retries would on their own.
4. Settles the rest, receiver still failing: retries still waiting (the last one is 30 minutes
   out) are made due now, and dead deliveries (five failures in a row) are replayed. That
   changes when those events go out, not whether: every one of them is still in Postgres.
5. Compares the event ids the receiver got with the events Hookline accepted.

Exits 1 if any event was lost, or if the load didn't happen as asked: k6 dropped requests,
the API refused some, or fewer than --events were accepted.
"""

import argparse
import json
import math
import os
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import psycopg

from scripts.load_test import (
    RESULTS_DIR,
    ROOT,
    arrivals,
    clean_up,
    event_times,
    latencies_ms,
    percentile,
    run_k6,
)


@dataclass
class ChaosResult:
    # Accepted by the API (202): the events that must all arrive.
    sent: int
    # Of those, how many the receiver answered 2xx at least once.
    delivered: int
    # 2xx answers beyond the first for an event: at-least-once delivery at work.
    duplicates: int
    # Accepted-to-first-attempt time, over every delivered event.
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    # Seconds from the last event accepted until every event had its first attempt: the
    # recovery tail, e.g. a delivery cut off by a kill waits out its lock for the sweeper.
    all_attempted_after_s: float | None

    @property
    def lost(self) -> int:
        return self.sent - self.delivered

    @classmethod
    def measure(cls, created: dict[str, datetime], received: dict[str, dict]) -> "ChaosResult":
        mine = {event_id: received[event_id] for event_id in created if event_id in received}
        arrived = arrivals(mine, set(created))
        latencies = latencies_ms(created, arrived)
        stats = [percentile(latencies, p) for p in (50, 95, 99)] if latencies else [None] * 3
        first_attempts = [datetime.fromisoformat(r["first_seen_at"]) for r in mine.values()]
        all_attempted_after_s = None
        if created and len(first_attempts) == len(created):
            last_accepted = max(created.values())
            all_attempted_after_s = (max(first_attempts) - last_accepted).total_seconds()
        return cls(
            sent=len(created),
            delivered=len(arrived),
            duplicates=sum(record["delivered"] for record in mine.values()) - len(arrived),
            p50_ms=stats[0],
            p95_ms=stats[1],
            p99_ms=stats[2],
            all_attempted_after_s=all_attempted_after_s,
        )


def shortfalls(
    result: ChaosResult, k6: dict, events_requested: int, workers_killed: int | None
) -> list[str]:
    """Why this run didn't test what it set out to, if it didn't.

    Zero lost proves little if the load or the failures never happened: every requested event
    must have been sent and accepted, and every accepted one delivered, with at least one worker
    killed on the way. workers_killed is None when nothing was killing them (no --kill-log).
    """
    problems = []
    if workers_killed == 0:
        problems.append("No worker was killed: raise the events or lower KILL_EVERY")
    if k6["dropped"]:
        problems.append(f"k6 dropped {k6['dropped']} requests: the rate wasn't held")
    if k6["accepted"] < k6["requests"]:
        problems.append(f"The API accepted {k6['accepted']} of {k6['requests']} requests")
    if result.sent < events_requested:
        problems.append(f"Only {result.sent} of {events_requested} events were accepted")
    if result.lost:
        problems.append(f"{result.lost} events were lost")
    return problems


def delivery_states(database_url: str, endpoint_id: str) -> dict[str, int]:
    with psycopg.connect(database_url) as connection:
        rows = connection.execute(
            "SELECT status, count(*) FROM deliveries WHERE endpoint_id = %s GROUP BY status",
            (endpoint_id,),
        ).fetchall()
    return dict(rows)


def make_retries_due(database_url: str, endpoint_id: str) -> int:
    """Skip the wait of every retry still scheduled for this endpoint; return how many."""
    with psycopg.connect(database_url) as connection:
        cursor = connection.execute(
            "UPDATE deliveries SET next_attempt_at = now()"
            " WHERE endpoint_id = %s AND status = 'pending' AND next_attempt_at > now()",
            (endpoint_id,),
        )
        return cursor.rowcount


def received_all(receiver: httpx2.Client, event_ids: set[str]) -> tuple[dict, bool]:
    received = receiver.get("/received").raise_for_status().json()
    return received, len(arrivals(received, event_ids)) == len(event_ids)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--events", type=int, default=10_000)
    parser.add_argument("--rate", type=int, default=50, help="events/s")
    parser.add_argument("--fail-percent", type=float, default=20)
    parser.add_argument("--drain-timeout", type=float, default=90, help="seconds, retries as is")
    parser.add_argument("--settle-timeout", type=float, default=600, help="seconds")
    parser.add_argument(
        "--kill-log", type=Path, help="one line per worker killed; none in it fails the run"
    )
    parser.add_argument("--api-url", default="http://localhost:8000")
    parser.add_argument("--k6-api-url", default="http://api:8000")
    parser.add_argument("--receiver-url", default="http://localhost:9000")
    parser.add_argument("--api-key", default=os.environ.get("HOOKLINE_API_KEY", "hk_local_dev_key"))
    parser.add_argument(
        "--database-url",
        default=os.environ.get(
            "LOADTEST_DATABASE_URL", "postgresql://hookline:hookline@localhost:5433/hookline"
        ),
    )
    args = parser.parse_args()
    # run_k6 reads the duration from args: long enough at this rate for --events.
    send_seconds = math.ceil(args.events / args.rate)
    args.duration = f"{send_seconds}s"

    event_type = f"chaos.{uuid.uuid4().hex[:8]}"
    api = httpx2.Client(
        base_url=args.api_url, headers={"Authorization": f"Bearer {args.api_key}"}, timeout=10
    )
    receiver = httpx2.Client(base_url=args.receiver_url, timeout=30)
    response = api.post(
        "/endpoints", json={"url": "http://receiver:9000/webhook", "event_types": [event_type]}
    )
    if response.status_code == 422:
        sys.exit(f"{response.json()['detail']}: is ALLOWED_INTERNAL_HOSTS=receiver:9000 set?")
    endpoint_id = response.raise_for_status().json()["id"]
    secret = response.json()["secret"]

    try:
        receiver.delete("/received").raise_for_status()
        receiver.patch(
            "/config", json={"secret": secret, "fail_percent": args.fail_percent, "delay_ms": 0}
        ).raise_for_status()

        print(f"Sending {args.events} events at {args.rate}/s ({args.duration})...", flush=True)
        k6 = run_k6(args, args.rate, event_type)
        created = event_times(args.database_url, event_type)
        event_ids = set(created)

        print(f"Waiting up to {args.drain_timeout:g}s for retries as scheduled...", flush=True)
        deadline = time.monotonic() + args.drain_timeout
        _, done = received_all(receiver, event_ids)
        while not done and time.monotonic() < deadline:
            time.sleep(2)
            _, done = received_all(receiver, event_ids)
        before_settling = delivery_states(args.database_url, endpoint_id)

        print(f"Settling what's left (up to {args.settle_timeout:g}s)...", flush=True)
        made_due = replayed = 0
        deadline = time.monotonic() + args.settle_timeout
        while not done and time.monotonic() < deadline:
            made_due += make_retries_due(args.database_url, endpoint_id)
            replay = api.post(f"/endpoints/{endpoint_id}/replay-dead").raise_for_status()
            replayed += replay.json()["replayed"]
            time.sleep(2)
            _, done = received_all(receiver, event_ids)

        received, _ = received_all(receiver, event_ids)
        stats = receiver.get("/stats").raise_for_status().json()
        after = delivery_states(args.database_url, endpoint_id)
    finally:
        failures = clean_up(api, receiver, endpoint_id)
        # Leave the receiver answering normally for whoever uses it next.
        try:
            receiver.patch("/config", json={"fail_percent": 0}).raise_for_status()
        except httpx2.HTTPError as exc:
            failures.append(f"Couldn't reset the receiver's failure rate: {exc}")

    result = ChaosResult.measure(created, received)
    kills = len(args.kill_log.read_text().splitlines()) if args.kill_log else None
    accepted_per_s = result.sent / send_seconds
    report = {
        "events_requested": args.events,
        "rate": args.rate,
        "fail_percent": args.fail_percent,
        "workers_killed": kills,
        "k6": k6,
        "accepted_per_s": accepted_per_s,
        "receiver_requests": stats["requests"],
        "receiver_failed_on_purpose": stats["failed"],
        "deliveries_before_settling": before_settling,
        "retries_made_due": made_due,
        "dead_replayed": replayed,
        "deliveries_at_end": after,
        **asdict(result),
        "lost": result.lost,
    }

    def ms(value: float | None) -> str:
        return "-" if value is None else f"{value:.0f} ms"

    tail = "-" if result.all_attempted_after_s is None else f"{result.all_attempted_after_s:.0f} s"

    print(
        f"""
Events sent (accepted):  {result.sent}  (k6: {k6["requests"]} requests, {k6["dropped"]} dropped)
Events delivered:        {result.delivered}
Events lost:             {result.lost}
Duplicates:              {result.duplicates}
Workers killed:          {kills if kills is not None else "-"}
Receiver 500s on purpose: {stats["failed"]} of {stats["requests"]} requests
First attempt latency:   p50 {ms(result.p50_ms)}, p95 {ms(result.p95_ms)}, p99 {ms(result.p99_ms)}
Accepted under chaos:    {accepted_per_s:.1f} events/s for {send_seconds} s
All had a first attempt: {tail} after the last was accepted
Before settling:         {before_settling}
Settled:                 {made_due} retries made due, {replayed} dead replayed"""
    )
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"chaos-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(report, indent=2, default=str))
    print(f"Saved {path.relative_to(ROOT)}")

    problems = shortfalls(result, k6, args.events, kills) + failures
    if problems:
        sys.exit("\nFAILED:\n" + "\n".join(problems))
    print("\nPASSED: every event sent was accepted and delivered.")


if __name__ == "__main__":
    main()
