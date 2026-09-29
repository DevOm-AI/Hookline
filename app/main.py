from fastapi import FastAPI

from app.api.health import router as health_router
from app.core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Hookline", debug=settings.debug)
    app.include_router(health_router)
    return app


app = create_app()
