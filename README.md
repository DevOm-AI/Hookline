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
