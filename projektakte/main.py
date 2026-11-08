"""HTTP application."""
from __future__ import annotations

from fastapi import FastAPI

from . import __version__
from .config import load_settings


def create_app() -> FastAPI:
    settings = load_settings()
    app = FastAPI(title="Projektakte", version=__version__)

    @app.get("/health")
    def health() -> dict:
        # must stay fast and independent of the AI services (used by monitoring)
        return {"status": "ok", "version": __version__, "env": settings.app_env}

    @app.get("/api/version")
    def version() -> dict:
        return {"version": __version__}

    return app


app = create_app()
