# Hookline

A webhook delivery service that doesn't lose events.

> Work in progress. The full README (architecture, design decisions, chaos test results) comes later.

## Run locally

Requires Docker and [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env
docker compose up --build
```

API docs: http://localhost:8000/docs

## Tests and lint

```bash
uv sync
uv run ruff check .
uv run pytest
```
