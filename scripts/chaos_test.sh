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
# running with one worker again. Exits 1 if any event was lost, the load fell short, no
# worker was killed, or the workers couldn't be restored.
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
  local status=$?
  if [ -n "$killer_pid" ]; then
    # Its sleep (or docker command) too, found first: once the loop is gone it's reparented.
    local children
    children=$(pgrep -P "$killer_pid" || true)
    kill "$killer_pid" 2>/dev/null || true
    wait "$killer_pid" 2>/dev/null || true
    # shellcheck disable=SC2086  # one pid per word
    [ -z "$children" ] || kill $children 2>/dev/null || true
  fi
  rm -f "$kill_log"
  # Starts the one worker kept, even if it was the last one killed, and removes the rest.
  echo "Back to one worker..."
  local running
  if ! docker compose up -d --scale worker=1 worker >/dev/null \
    || ! running=$(docker compose ps -q --status running worker | wc -l) \
    || [ "$running" -ne 1 ]; then
    echo "Couldn't restore the workers (${running:-?} running): check docker compose ps" >&2
    status=1
  fi
  exit "$status"
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
  # Keep going if one docker command fails (set -e would end the loop, and the kills with it):
  # a kill is logged, and counted by scripts/chaos.py, only once docker kill succeeded.
  set +e
  while true; do
    sleep "$KILL_EVERY"
    # Running workers only: one still down from the last kill isn't picked again.
    victim=$(docker compose ps -q worker | shuf -n 1)
    [ -n "$victim" ] || continue
    name=$(docker inspect -f '{{.Name}}' "$victim")
    if docker kill "$victim" >/dev/null; then
      echo "$(date -u +%FT%TZ) killed ${name#/}" | tee -a "$kill_log"
    else
      echo "Couldn't kill ${name#/}; trying another next time" >&2
    fi
    sleep "$DOWN_FOR"
    docker start "$victim" >/dev/null || echo "Couldn't start ${name#/} again" >&2
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
