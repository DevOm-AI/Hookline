import logging

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="hookline.deliver")
def deliver(delivery_id: str) -> None:
    """Send one claimed delivery to its endpoint.

    Placeholder: the scheduler already claims and enqueues deliveries; sending, signing
    and retries land here next.
    """
    logger.info("Delivery %s claimed; sending is not implemented yet", delivery_id)
