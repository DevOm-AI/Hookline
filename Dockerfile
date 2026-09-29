# One image for the api, worker and beat services.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /uvx /bin/

# The venv lives outside /app so the dev bind mount in docker-compose doesn't hide it.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Install dependencies first so this layer is cached until the lockfile changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY app ./app
COPY alembic.ini ./
COPY alembic ./alembic

RUN useradd --create-home --uid 1000 hookline
USER hookline

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
