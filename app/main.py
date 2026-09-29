import logging

from fastapi import FastAPI

from app.api.endpoints import router as endpoints_router
from app.api.health import router as health_router
from app.core.config import get_settings

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    if settings.api_key_hash is None:
        logger.warning("API_KEY_HASH is not set: every authenticated route will return 401.")

    app = FastAPI(title="Hookline", debug=settings.debug)
    app.include_router(health_router)
    app.include_router(endpoints_router)
    return app


app = create_app()
