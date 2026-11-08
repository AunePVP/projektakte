"""HTTP application."""
from __future__ import annotations

from fastapi import FastAPI

from . import __version__
from .ai.services import AIServices, resolve_settings
from .config import Settings, load_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = resolve_settings(settings or load_settings())
    app = FastAPI(title="Projektakte", version=__version__)
    app.state.settings = settings
    app.state.ai = AIServices.from_settings(settings)

    @app.get("/health")
    def health() -> dict:
        # must stay fast and independent of the AI services (used by monitoring)
        return {"status": "ok", "version": __version__, "env": settings.app_env}

    @app.get("/health/deps")
    def health_deps() -> dict:
        result = app.state.ai.check()
        result["ai_mode"] = settings.ai_mode
        return result

    @app.get("/api/version")
    def version() -> dict:
        return {"version": __version__}

    return app


app = create_app()
