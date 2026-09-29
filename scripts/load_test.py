"""Load test: the highest event rate at which Hookline's p95 latency stays under a second.

For each rate, k6 (loadtest/events.js) sends events at that rate for a while, the mock
receiver answers every delivery, and this script measures each event's latency: from when
Hookline accepted it (events.created_at) to when its first delivery attempt reached the
receiver (first_seen_at). Rates run in order, and it stops at the first that misses the target.

Needs the compose stack and the mock receiver, with ALLOWED_INTERNAL_HOSTS=receiver:9000:

    docker compose up -d && docker compose up -d receiver
    uv run python scripts/load_test.py --rates 50,100,200,400 --duration 30s

Endpoints and receiver state are cleaned up afterwards; the events stay in the database.
"""

import argparse
import json
import math
import os
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import psycopg

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = ROOT / "loadtest" / "results"
SUMMARY_TAG = "HOOKLINE_K6_SUMMARY "
TARGET_P95_MS = 1000.0


def percentile(values: list[float], p: float) -> float:
    """Nearest rank: the smallest value with at least p% of the values at or below it."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    return ordered[max(math.ceil(p / 100 * len(ordered)), 1) - 1]


def latencies_ms(created: dict[str, datetime], first_seen: dict[str, datetime]) -> list[float]:
    """Accepted-to-first-attempt time of every event that reached the receiver."""
    return [
        (first_seen[event_id] - created_at).total_seconds() * 1000
        for event_id, created_at in created.items()
        if event_id in first_seen
    ]


def parse_k6_summary(stdout: str) -> dict:
    """The summary loadtest/events.js prints as one tagged JSON line."""
    for line in stdout.splitlines():
        if line.startswith(SUMMARY_TAG):
            return json.loads(line.removeprefix(SUMMARY_TAG))
    raise ValueError("k6 printed no summary")


@dataclass
class StepResult:
    rate: int
    # Accepted by the API (202).
    sent: int
    # Requests k6 couldn't start on time because the API was too slow: the rate wasn't held.
    dropped: int
    post_p95_ms: float
    # Of the events sent, how many reached the receiver before the drain timeout.
    delivered: int
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    max_ms: float | None

    @property
    def passed(self) -> bool:
        return (
            self.dropped == 0
            and self.sent > 0
            and self.delivered == self.sent
            and self.p95_ms is not None
            and self.p95_ms < TARGET_P95_MS
        )

    @classmethod
    def measure(cls, k6: dict, delivered: int, latencies: list[float]) -> "StepResult":
        stats = [percentile(latencies, p) for p in (50, 95, 99, 100)] if latencies else [None] * 4
        return cls(
            rate=k6["rate"],
            sent=int(k6["accepted"]),
            dropped=int(k6["dropped"]),
            post_p95_ms=k6["post_p95_ms"],
            delivered=delivered,
            p50_ms=stats[0],
            p95_ms=stats[1],
            p99_ms=stats[2],
            max_ms=stats[3],
        )


def run_k6(rate: int, duration: str, event_type: str, api_key: str) -> dict:
    command = ["docker", "compose", "run", "--rm", "--quiet-pull"]
    for name, value in {
        "RATE": rate,
        "DURATION": duration,
        "EVENT_TYPE": event_type,
        "API_KEY": api_key,
    }.items():
        command += ["-e", f"{name}={value}"]
    command += ["k6", "run", "--quiet", "/loadtest/events.js"]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"k6 failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
    return parse_k6_summary(result.stdout)


def wait_for_deliveries(receiver: httpx2.Client, expected: int, timeout: float) -> int:
    """Poll the receiver until `expected` events arrived or `timeout` passed; return how many."""
    deadline = time.monotonic() + timeout
    while True:
        unique = receiver.get("/stats").raise_for_status().json()["unique_events"]
        if unique >= expected or time.monotonic() >= deadline:
            return unique
        time.sleep(1)


def event_times(database_url: str, event_type: str) -> dict[str, datetime]:
    with psycopg.connect(database_url) as connection:
        rows = connection.execute(
            "SELECT id, created_at FROM events WHERE type = %s", (event_type,)
        ).fetchall()
    return {str(event_id): created_at for event_id, created_at in rows}


def first_attempts(receiver: httpx2.Client) -> dict[str, datetime]:
    received = receiver.get("/received").raise_for_status().json()
    return {
        event_id: datetime.fromisoformat(record["first_seen_at"])
        for event_id, record in received.items()
    }


def run_step(
    args: argparse.Namespace, receiver: httpx2.Client, rate: int, event_type: str
) -> StepResult:
    receiver.delete("/received").raise_for_status()
    k6 = run_k6(rate, args.duration, event_type, args.api_key)
    delivered = wait_for_deliveries(receiver, int(k6["accepted"]), args.drain_timeout)
    latencies = latencies_ms(event_times(args.database_url, event_type), first_attempts(receiver))
    return StepResult.measure(k6, delivered, latencies)


def print_results(results: list[StepResult]) -> None:
    def ms(value: float | None) -> str:
        return "-" if value is None else f"{value:.0f}"

    print(
        f"\n{'rate/s':>7} {'sent':>7} {'dropped':>8} {'delivered':>10} "
        f"{'p50 ms':>7} {'p95 ms':>7} {'p99 ms':>7} {'max ms':>7} {'POST p95':>9}"
    )
    for r in results:
        print(
            f"{r.rate:>7} {r.sent:>7} {r.dropped:>8} {r.delivered:>10} {ms(r.p50_ms):>7} "
            f"{ms(r.p95_ms):>7} {ms(r.p99_ms):>7} {ms(r.max_ms):>7} {r.post_p95_ms:>9.1f}"
            + ("" if r.passed else "   <- missed")
        )
    best = max((r.rate for r in results if r.passed), default=None)
    print(
        f"\nHighest rate with p95 under {TARGET_P95_MS:.0f} ms: "
        + (f"{best} events/s" if best else "none of the rates tried")
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--rates", default="50,100,200,400", help="events/s, in order")
    parser.add_argument("--duration", default="30s", help="how long each rate runs (k6 syntax)")
    parser.add_argument("--drain-timeout", type=float, default=120, help="seconds per rate")
    parser.add_argument("--all", action="store_true", help="run every rate, even after a miss")
    parser.add_argument("--api-url", default="http://localhost:8000")
    parser.add_argument("--receiver-url", default="http://localhost:9000")
    parser.add_argument(
        "--api-key",
        default=os.environ.get("HOOKLINE_API_KEY", "hk_local_dev_key"),
        help="default: $HOOKLINE_API_KEY, else the local dev key",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get(
            "LOADTEST_DATABASE_URL", "postgresql://hookline:hookline@localhost:5433/hookline"
        ),
        help="to read events.created_at; default: $LOADTEST_DATABASE_URL, else compose",
    )
    args = parser.parse_args()
    rates = [int(rate) for rate in args.rates.split(",")]

    run_id = uuid.uuid4().hex[:8]
    event_types = {rate: f"loadtest.{run_id}.r{rate}" for rate in rates}
    api = httpx2.Client(
        base_url=args.api_url, headers={"Authorization": f"Bearer {args.api_key}"}, timeout=10
    )
    receiver = httpx2.Client(base_url=args.receiver_url, timeout=30)

    response = api.post(
        "/endpoints",
        json={"url": "http://receiver:9000/webhook", "event_types": list(event_types.values())},
    )
    if response.status_code == 422:
        sys.exit(f"{response.json()['detail']}: is ALLOWED_INTERNAL_HOSTS=receiver:9000 set?")
    endpoint = response.raise_for_status().json()
    results: list[StepResult] = []
    try:
        receiver.patch(
            "/config", json={"secret": endpoint["secret"], "fail_percent": 0, "delay_ms": 0}
        ).raise_for_status()
        for rate in rates:
            print(f"{rate} events/s for {args.duration}...", flush=True)
            result = run_step(args, receiver, rate, event_types[rate])
            results.append(result)
            if not result.passed and not args.all:
                break
    finally:
        api.delete(f"/endpoints/{endpoint['id']}")
        receiver.delete("/received")

    print_results(results)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{run_id}.json"
    path.write_text(
        json.dumps(
            {
                "duration": args.duration,
                "target_p95_ms": TARGET_P95_MS,
                "cpus": os.cpu_count(),
                "results": [asdict(r) | {"passed": r.passed} for r in results],
            },
            indent=2,
        )
    )
    print(f"Saved {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
