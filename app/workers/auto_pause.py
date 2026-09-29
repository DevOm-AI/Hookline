import logging
from datetime import timedelta

from sqlalchemy import Row, func, update
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models import Endpoint
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

# How long an endpoint may fail every attempt before it's paused.
FAILING_FOR = timedelta(hours=24)


def pause_failing_endpoints(db: Session, failing_for: timedelta = FAILING_FOR) -> list[Row]:
    """Pause active endpoints whose every attempt has failed for `failing_for`. The caller commits.

    failing_since is set by the first failed attempt after a success and cleared by the next
    success (app/workers/delivery.py), so it is at least `failing_for` old only if nothing
    has got through since. Once paused, the scheduler stops claiming the endpoint's
    deliveries and they wait as pending; resuming it (PATCH is_active=true) sends them.
    """
    return list(
        db.execute(
            update(Endpoint)
            .where(
                Endpoint.is_active,
                # The database clock, like the worker that set failing_since.
                Endpoint.failing_since <= func.now() - failing_for,
            )
            .values(is_active=False, auto_paused_at=func.now())
            .returning(Endpoint.id, Endpoint.url, Endpoint.failing_since)
        )
    )


@celery_app.task(name="hookline.pause_failing_endpoints")
def pause_failing_endpoints_task() -> int:
    """Beat job: stop sending to endpoints that have failed everything for 24 hours."""
    with SessionLocal() as db:
        paused = pause_failing_endpoints(db)
        db.commit()

    for endpoint_id, url, failing_since in paused:
        logger.warning(
            "Paused endpoint %s (%s): every attempt has failed since %s",
            endpoint_id,
            url,
            failing_since.isoformat(),
        )
    return len(paused)
