# Hookline

![CI](https://github.com/DevOm-AI/Hookline/actions/workflows/ci.yml/badge.svg)

A webhook delivery service that doesn't lose events.

> Work in progress. The full README (architecture, design decisions, chaos test results) comes later.

## Run locally

Requires Docker and [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env
docker compose up --build
```

API docs: http://localhost:8000/docs
Health check: http://localhost:8000/health

Migrations run automatically: the `migrate` service applies `alembic upgrade head`
before the api, worker and beat start.

## API key

Every route except `/health` needs `Authorization: Bearer <api key>`. Hookline stores only
the key's SHA-256 hash, in `API_KEY_HASH`. The `.env.example` hash is for the local-only
key `hk_local_dev_key`. For any deployed environment, generate a new pair:

```bash
uv run python -m app.core.security
```

## Endpoints

Endpoint URLs must resolve only to public addresses. Private, loopback, link-local
(including `169.254.169.254`) and other internal addresses are rejected with 422, so
Hookline can't be pointed at internal services. With `DEBUG=true`, `localhost` is allowed
for local testing.

```bash
# Register an endpoint. The response includes its signing secret, shown only this once.
curl -X POST localhost:8000/endpoints \
  -H "Authorization: Bearer hk_local_dev_key" -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/hook", "event_types": ["order.shipped"]}'

curl localhost:8000/endpoints -H "Authorization: Bearer hk_local_dev_key"

# Pause (or resume with true)
curl -X PATCH localhost:8000/endpoints/<id> \
  -H "Authorization: Bearer hk_local_dev_key" -H "Content-Type: application/json" \
  -d '{"is_active": false}'

curl -X DELETE localhost:8000/endpoints/<id> -H "Authorization: Bearer hk_local_dev_key"
```

## Events

```bash
# 202 Accepted: the event and one pending delivery per subscribed, active endpoint are saved
# together. Resending the same Idempotency-Key returns the original event and creates nothing.
curl -X POST localhost:8000/events \
  -H "Authorization: Bearer hk_local_dev_key" -H "Content-Type: application/json" \
  -H "Idempotency-Key: order-42-shipped" \
  -d '{"type": "order.shipped", "payload": {"order_id": 42}}'
```

## Delivery

Every second, the `beat` service triggers a job that claims up to 100 due deliveries
(`pending`, `next_attempt_at` passed, endpoint active) with `FOR UPDATE SKIP LOCKED`.
It marks them `in_progress` for 60 seconds, commits, and only then queues them for the
workers. Several schedulers can run at once without claiming the same delivery. Deliveries
for a paused endpoint wait as `pending` until it's resumed.

A worker then POSTs the event's JSON payload to the endpoint (10-second timeout, redirects
not followed) with these headers:

| Header | Value |
| --- | --- |
| `Hookline-Event-Id` | The event's id. Dedupe on it: see [At-least-once delivery](#at-least-once-delivery). |
| `Hookline-Event-Type` | The event's type, e.g. `order.shipped` |
| `Hookline-Signature` | `t=<unix time>,v1=<hex HMAC-SHA256>`, see [Verifying signatures](#verifying-signatures) |

Every try is logged in `delivery_attempts` with its status code, time taken and error. A
2xx marks the delivery `succeeded`.

### Retries

A timeout, connection error, DNS failure, 5xx or 429 is retried with backoff. The delivery
goes back to `pending` with a later `next_attempt_at`, and the scheduler picks it up again
then:

| Attempt | 1 | 2 | 3 | 4 | 5 |
| --- | --- | --- | --- | --- | --- |
| Wait before the next try | 10 s | 1 min | 5 min | 30 min | `dead` |

Each wait gets ±20% random jitter, so deliveries that failed together don't all retry in the
same second. Any other response (3xx, other 4xx) means the request itself is wrong, so the
delivery is marked `dead` straight away, as is an endpoint that now resolves to an internal
address. Each retry is signed again with its own timestamp.

The endpoint's host is resolved and checked again at send time, and the worker connects to
exactly the address it checked. A name that has since started pointing at an internal
address (DNS rebinding) is blocked.

Celery tasks are acknowledged only after they finish (`acks_late`) and requeued if a
worker process dies mid-task.

### Recovering stuck deliveries

If a worker dies mid-send, or a queued task is lost, its delivery would stay `in_progress`
forever. Every 30 seconds a sweeper job sets `in_progress` deliveries whose 60-second lock
has expired back to `pending`, and the scheduler claims them again on its next tick.
Postgres, not Redis, is what guarantees the work gets done.

A worker saves its result only if it still holds the delivery: the row must still be
`in_progress` with the same `locked_until` its claim set (every claim sets a new one). If a
slow request outlived the lock and the delivery has since been released, claimed by another
worker or settled, that update changes 0 rows and the stale result is dropped, so it never
overwrites the current owner's. A lock that expired with nobody taking over still counts.

### Graceful shutdown

`docker stop` sends SIGTERM, which starts Celery's warm shutdown: the worker finishes the
requests it is sending, puts the messages it had prefetched back on the queue, takes no new
work, and exits. The worker's `stop_grace_period` is 60 seconds, as long as a delivery lock.
Docker's default of 10 seconds before SIGKILL could cut short a send that is still within
its timeouts (connecting, writing and reading each get 10 seconds).

A worker killed outright (`kill -9`, out of memory, a lost machine) loses nothing either. Its
delivery stays `in_progress` until the lock runs out, then the sweeper releases it and it is
sent again: up to 60 seconds for the lock plus up to 30 until the next sweep. To try it, kill
the worker while it is sending, then start it again:

```bash
docker compose kill -s SIGKILL worker
docker compose start worker
```

## At-least-once delivery

Hookline delivers every event **at least once**, not exactly once. Your receiver can get the
same event more than once:

- A worker dies after your receiver got the request but before Hookline saved the result.
  The sweeper hands the delivery back and it is sent again.
- Your receiver handles the request but answers after the 10-second timeout, or the response
  is lost on the way back. Hookline sees a failure and retries.
- A worker holds a delivery past its 60-second lock (a backed-up queue, say), and the sweeper
  gives it to another worker.

No webhook sender can avoid this. Hookline can't know whether a request it never got an
answer to was processed, and resending is the only way not to lose the event. Stripe,
GitHub and Shopify webhooks work the same way.

**Dedupe on `Hookline-Event-Id`.** It is the same on every retry and resend of an event (the
signature is not: each try is signed with its own timestamp). Record the ids you have
processed in the same transaction as the work itself, so a crash rolls back both:

```python
# processed_events.event_id is the primary key.
with db.begin():
    first_time = db.execute(
        text("INSERT INTO processed_events (event_id) VALUES (:id) ON CONFLICT DO NOTHING"),
        {"id": request.headers["Hookline-Event-Id"]},
    ).rowcount
    if first_time:
        handle(event)
```

Answer a duplicate with 2xx too, or Hookline keeps retrying it. The id belongs to the
event, so an event sent to several of your endpoints has the same id at each; if one
receiver serves several endpoints, dedupe per endpoint. Keep responses under 10 seconds:
queue slow work and answer right away.

## Verifying signatures

Each request is signed with the endpoint's secret (the `whsec_...` value returned when the
endpoint was created):

```
Hookline-Signature: t=1727600000,v1=5f2c...
v1 = hex(HMAC_SHA256(secret, f"{t}.{raw_body}"))
```

`t` is the time of that try, so every retry carries a fresh signature. Recompute the HMAC
over the **raw** request body (parsing and re-serialising the JSON changes the bytes), and
reject requests whose `t` is more than 5 minutes away from your clock. That stops a captured
request from being replayed later. Copy this into your receiver (Python standard library
only):

<!-- verify_signature: tests/test_signing.py runs this exact code -->
```python
import hashlib
import hmac
import time


def verify_signature(secret: str, body: bytes, header: str, tolerance: int = 300) -> bool:
    """Check a Hookline-Signature header against the raw request body."""
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
    if abs(time.time() - timestamp) > tolerance:
        return False
    message = f"{timestamp}.".encode() + body
    expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, signature) for signature in signatures)
```

For example, in FastAPI:

```python
@app.post("/hook")
async def hook(request: Request):
    body = await request.body()
    if not verify_signature(WEBHOOK_SECRET, body, request.headers.get("Hookline-Signature", "")):
        raise HTTPException(status_code=401)
    event = json.loads(body)
    ...
```

## Migrations

Run Alembic inside a container, where `DATABASE_URL` points at the `postgres` service:

```bash
# After changing models in app/models/
docker compose run --rm api alembic revision --autogenerate -m "describe the change"
docker compose run --rm api alembic upgrade head
docker compose run --rm api alembic current
```

New migration files land in `alembic/versions/`. Format and review them before committing:

```bash
uv run ruff format alembic/versions && uv run ruff check alembic/versions
```

## Tests and lint

Tests run against a real Postgres, in a separate `hookline_test` database that is
created and migrated automatically. Your dev database is never touched.

```bash
uv sync
docker compose up -d postgres
uv run ruff check .
uv run pytest
```

By default tests connect to the compose Postgres at `localhost:5433`. To use a different
server, set `TEST_DATABASE_URL` in your shell (it is not read from `.env`); the database
name must end in `_test`.
