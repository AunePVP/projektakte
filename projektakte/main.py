"""HTTP application."""
from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from pathlib import Path

from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, db
from .ai.services import AIServices, resolve_settings
from .auth import COOKIE, Auth, is_public
from .background import BackgroundIndexer
from .config import Settings, load_settings


def health_deps_data(app: FastAPI) -> dict:
    """State of the AI services (and plMail, if configured) – used by /health/deps and /status."""
    settings = app.state.settings
    result = app.state.ai.check()
    result["ai_mode"] = settings.ai_mode
    if settings.plmail_enabled:
        from .indexer import plmail_client

        client = plmail_client(settings)
        client.timeout = settings.health_timeout
        result["plmail"] = client.probe()
    return result


def create_app(settings: Settings | None = None, *, background: bool = True) -> FastAPI:
    settings = resolve_settings(settings or load_settings())
    ai = AIServices.from_settings(settings)
    indexer = BackgroundIndexer(settings, ai)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # start-up must stay fast: indexing (and demo data generation) runs in the background
        if background:
            indexer.start()
        yield
        indexer.stop()

    # no Swagger UI/ReDoc: they load scripts from a CDN; the schema stays at /openapi.json
    app = FastAPI(title="Projektakte", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.ai = ai
    app.state.indexer = indexer
    app.state.auth = auth = Auth.from_settings(settings)
    db.init_db(settings.db_path)

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        """Everything except /health, /login and /static needs a logged-in user."""
        request.state.user = None
        path = request.url.path
        if auth.disabled or is_public(path):
            if not auth.disabled:
                request.state.user = auth.read_cookie(request.cookies.get(COOKIE))
            return await call_next(request)
        user = auth.read_cookie(request.cookies.get(COOKIE))
        if not user and path.startswith("/api/integration/") and settings.integration_token:
            given = request.headers.get("authorization", "")
            if hmac.compare_digest(given.encode(), f"Bearer {settings.integration_token}".encode()):
                user = "plmail-integration"
        basic = request.headers.get("authorization", "").lower().startswith("basic ")
        if not user and basic and path.startswith(("/api/", "/health/")):
            client = request.client.host if request.client else ""
            user = await run_in_threadpool(auth.basic_user, request.headers["authorization"], client)
        if user:
            request.state.user = user
            return await call_next(request)
        if request.headers.get("HX-Request") == "true":
            return JSONResponse({"detail": "Anmeldung erforderlich"}, status_code=401,
                                headers={"HX-Redirect": "/login"})
        if path.startswith(("/api/", "/health")) or "text/html" not in request.headers.get("accept", ""):
            return JSONResponse({"detail": "Anmeldung erforderlich"}, status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="Projektakte"'})
        target = path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target)}", status_code=303)

    @app.get("/health")
    def health() -> dict:
        # must stay fast and independent of the AI services (used by monitoring)
        return {"status": "ok", "version": __version__, "env": settings.app_env}

    @app.get("/health/deps")
    def health_deps() -> dict:
        return health_deps_data(app)

    @app.get("/api/version")
    def version() -> dict:
        return {"version": __version__}

    from .api import router as api_router
    from .integration import router as integration_router
    from .web import router as web_router, templates

    app.include_router(api_router)
    app.include_router(integration_router)
    app.include_router(web_router)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        # JSON for the API, a German HTML page for the browser
        if request.url.path.startswith(("/api/", "/health")) or "text/html" not in request.headers.get("accept", ""):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        title = {404: "Nicht gefunden", 401: "Anmeldung nötig", 403: "Kein Zugriff"}.get(exc.status_code, "Fehler")
        return templates.TemplateResponse(request, "error.html", {"title": title, "message": exc.detail},
                                          status_code=exc.status_code)

    return app


def __getattr__(name: str):
    # ``uvicorn projektakte.main:app`` keeps working, but importing this module has no side effects
    if name == "app":
        global app
        app = create_app()
        return app
    raise AttributeError(name)
