from celery import Celery

from app.core.config import get_settings

settings = get_settings()

# No result backend: Postgres is the source of truth, Redis only wakes workers up.
celery_app = Celery("hookline", broker=settings.broker_url)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
)
