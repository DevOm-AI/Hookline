import logging
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, status
from redis import Redis
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.redis import get_redis

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
def health(
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> dict[str, Any]:
    """Check Postgres and Redis. Public on purpose: load balancers and Docker probe it."""
    checks = {
        "database": _check("database", lambda: db.execute(text("SELECT 1"))),
        "redis": _check("redis", redis.ping),
    }
    healthy = all(result == "ok" for result in checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if healthy else "error", "checks": checks}


def _check(name: str, probe: Callable[[], object]) -> str:
    try:
        probe()
    except Exception as exc:
        # Details go to the log only, so hostnames and credentials never reach the response.
        logger.warning("Health check failed for %s: %s", name, exc)
        return "error"
    return "ok"
