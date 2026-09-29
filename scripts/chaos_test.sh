#!/usr/bin/env bash
# The chaos test: send 10,000 events while a random worker is killed (SIGKILL, no graceful
# shutdown) every 20 seconds, with the receiver failing 20% of requests. At the end, every
# event Hookline accepted must have reached the receiver. scripts/chaos.py does the sending,
# waiting and comparing; this script runs the workers and kills them.
#
#   scripts/chaos_test.sh                        # the full test, about 5 minutes of sending
#   EVENTS=1000 scripts/chaos_test.sh            # a quicker one
#
# Needs ALLOWED_INTERNAL_HOSTS=receiver:9000 in .env (see .env.example). Leaves the stack
# running with one worker again. Exits 1 if any event was lost.
set -euo pipefail
cd "$(dirname "$0")/.."

WORKERS=${WORKERS:-3}
KILL_EVERY=${KILL_EVERY:-20}
# How long a killed worker stays down before it is started again.
DOWN_FOR=${DOWN_FOR:-5}
EVENTS=${EVENTS:-10000}
RATE=${RATE:-50}
FAIL_PERCENT=${FAIL_PERCENT:-20}

kill_log=$(mktemp)
killer_pid=

finish() {
  if [ -n "$killer_pid" ]; then
    kill "$killer_pid" 2>/dev/null || true
    wait "$killer_pid" 2>/dev/null || true
  fi
  echo "Restarting any stopped workers, then back to one..."
  docker compose up -d --scale "worker=$WORKERS" worker >/dev/null 2>&1 || true
  docker compose up -d --scale worker=1 worker >/dev/null 2>&1 || true
  rm -f "$kill_log"
}
trap finish EXIT

wait_healthy() {
  local url=$1
  for _ in $(seq 60); do
    curl -fsS "$url" >/dev/null 2>&1 && return 0
    sleep 1
  done
  echo "$url never became healthy" >&2
  exit 1
}

kill_random_workers() {
  while true; do
    sleep "$KILL_EVERY"
    # Running workers only: one still down from the last kill isn't picked again.
    victim=$(docker compose ps -q worker | shuf -n 1)
    [ -n "$victim" ] || continue
    name=$(docker inspect -f '{{.Name}}' "$victim")
    docker kill "$victim" >/dev/null
    echo "$(date -u +%FT%TZ) killed ${name#/}" | tee -a "$kill_log"
    sleep "$DOWN_FOR"
    docker start "$victim" >/dev/null
  done
}

echo "Starting the stack with $WORKERS workers and the mock receiver..."
docker compose up -d --scale "worker=$WORKERS" api worker beat receiver
wait_healthy http://localhost:8000/health
wait_healthy http://localhost:9000/health

kill_random_workers &
killer_pid=$!

uv run python -m scripts.chaos \
  --events "$EVENTS" --rate "$RATE" --fail-percent "$FAIL_PERCENT" --kill-log "$kill_log"
