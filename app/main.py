from fastapi import FastAPI

from app.core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    return FastAPI(title="Hookline", debug=settings.debug)


app = create_app()
