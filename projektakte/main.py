"""HTTP application."""
from __future__ import annotations

import hmac
import logging
import sqlite3
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, db
from .ai.services import AIServices, resolve_settings
from .auth import COOKIE, PUBLIC_PREFIXES, Auth, is_public
from .background import BackgroundIndexer
from .config import Settings, load_settings
from .startup import check_data_dir, log_startup
from .websecurity import UNSAFE_METHODS, SecurityHeadersMiddleware, is_cross_site

log = logging.getLogger("projektakte.main")

ERROR_TITLES = {401: "Anmeldung nötig", 403: "Kein Zugriff", 404: "Nicht gefunden",
                429: "Zu viele Versuche", 503: "Vorübergehend nicht verfügbar"}
READY_DB_TIMEOUT = 2.0  # seconds; the readiness probe must answer quickly


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


def _age_seconds(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        when = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - when).total_seconds()))


def _pending_vectors(db_path: Path) -> int:
    """Sections without an embedding; raises sqlite3.Error if the database is not usable."""
    uri = Path(db_path).resolve().as_uri() + "?mode=rw"  # never create a missing database here
    with closing(sqlite3.connect(uri, uri=True, timeout=READY_DB_TIMEOUT)) as conn:
        conn.execute(f"PRAGMA busy_timeout = {int(READY_DB_TIMEOUT * 1000)}")
        return conn.execute("SELECT COUNT(*) FROM chunks WHERE embedding IS NULL").fetchone()[0]


def readiness(app: FastAPI, *, indexer_expected: bool = True) -> tuple[dict, bool]:
    """Data-poor readiness for monitoring and the Docker HEALTHCHECK: database usable, indexer
    thread alive (both required), age of the last successful indexing run and pending vectors
    (``degraded`` if stale or failed). No content, no paths, no error texts."""
    settings = app.state.settings
    try:
        pending: int | None = _pending_vectors(settings.db_path)
        database = "ok"
    except sqlite3.Error:
        pending, database = None, "error"
    st = app.state.indexer.status()
    indexer = ("ok" if st.get("alive") else "stopped") if indexer_expected else "off"
    last_error = bool(st.get("last_error"))
    # "last_success" if the indexer records it; otherwise the last run, if it did not fail
    last_ok = st.get("last_success") or (None if last_error else st.get("last_finished"))
    age = _age_seconds(last_ok)
    interval = settings.index_interval
    stale = bool(interval > 0 and st.get("runs", 0) > 0
                 and (age is None or age > max(3 * interval, 3600)))
    ready = database == "ok" and indexer != "stopped"
    status = "error" if not ready else "degraded" if (stale or last_error) else "ok"
    return {"status": status,
            "checks": {"database": database, "indexer": indexer},
            "indexing": {"running": bool(st.get("running")), "runs": st.get("runs", 0),
                         "last_success_age_s": age, "last_run_failed": last_error, "stale": stale,
                         "pending_vectors": pending}}, ready


def wants_json(request: Request) -> bool:
    return (request.url.path.startswith(("/api/", "/health"))
            or "text/html" not in request.headers.get("accept", ""))


def error_response(request: Request, status: int, detail: str, headers: dict | None = None):
    """JSON for the API and other clients, a German HTML page for the browser."""
    if wants_json(request):
        return JSONResponse({"detail": detail}, status_code=status, headers=headers)
    from .web import templates

    return templates.TemplateResponse(request, "error.html",
                                      {"title": ERROR_TITLES.get(status, "Fehler"), "message": detail},
                                      status_code=status, headers=headers)


def create_app(settings: Settings | None = None, *, background: bool = True) -> FastAPI:
    """Builds the application. Raises ``startup.ConfigError`` (German message) for an unusable or
    unsafe configuration – nothing is started in that case."""
    settings = settings or load_settings()
    check_data_dir(settings.data_dir, settings.db_path)
    auth = Auth.from_settings(settings)
    db.init_db(settings.db_path)
    settings = resolve_settings(settings)
    ai = AIServices.from_settings(settings)
    indexer = BackgroundIndexer(settings, ai)
    log_startup(settings, auth)

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
    app.state.auth = auth

    @app.middleware("http")
    async def access_control(request: Request, call_next):
        """Rejects cross-site state changes; everything except the public paths needs a login."""
        request.state.user = None
        path = request.url.path
        if request.method in UNSAFE_METHODS and is_cross_site(request, settings.public_url):
            log.warning("Anfrage einer fremden Seite abgelehnt: %s %s (Sec-Fetch-Site: %s)", request.method,
                        path, request.headers.get("sec-fetch-site", "-"))
            return error_response(request, 403, "Anfrage von einer fremden Seite abgelehnt.")
        if auth.disabled or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)
        user = auth.read_cookie(request.cookies.get(COOKIE))
        if is_public(path):
            request.state.user = user
            return await call_next(request)
        authorization = request.headers.get("authorization", "")
        if not user and path.startswith("/api/integration/") and settings.integration_token:
            if hmac.compare_digest(authorization.encode(), f"Bearer {settings.integration_token}".encode()):
                user = "plmail-integration"
        if not user and authorization[:6].lower() == "basic " and path.startswith(("/api/", "/health/")):
            client = request.client.host if request.client else ""
            result = await auth.authenticate_basic(authorization, client)
            if result.ok:
                user = result.user
            elif result.status in ("throttled", "busy"):
                detail = ("Zu viele Fehlversuche – bitte später erneut versuchen." if result.status == "throttled"
                          else "Zu viele gleichzeitige Anmeldungen – bitte gleich erneut versuchen.")
                return JSONResponse({"detail": detail}, status_code=429 if result.status == "throttled" else 503,
                                    headers={"Retry-After": str(result.retry_after)})
        if user:
            request.state.user = user
            return await call_next(request)
        if request.headers.get("HX-Request") == "true":
            return JSONResponse({"detail": "Anmeldung erforderlich"}, status_code=401,
                                headers={"HX-Redirect": "/login"})
        if wants_json(request):
            return JSONResponse({"detail": "Anmeldung erforderlich"}, status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="Projektakte"'})
        target = path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target)}", status_code=303)

    # outermost: the headers are also set on redirects, 401/403 answers and static files
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/health")
    def health() -> dict:
        # must stay fast and independent of the AI services (used by monitoring)
        return {"status": "ok", "version": __version__, "env": settings.app_env}

    @app.get("/health/ready")
    def health_ready() -> JSONResponse:
        # public and data-poor: 200 when ready (also "degraded"), 503 when not (Docker HEALTHCHECK)
        data, ready = readiness(app, indexer_expected=background)
        return JSONResponse(data, status_code=200 if ready else 503)

    @app.get("/health/deps")
    def health_deps() -> dict:
        return health_deps_data(app)

    @app.get("/api/version")
    def version() -> dict:
        return {"version": __version__}

    from .api import router as api_router
    from .auth_web import router as auth_router
    from .integration import router as integration_router
    from .web import router as web_router

    app.include_router(auth_router)  # /login, /logout – before the web router
    app.include_router(api_router)
    app.include_router(integration_router)
    app.include_router(web_router)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return error_response(request, exc.status_code, exc.detail, headers=exc.headers)

    return app


def __getattr__(name: str):
    # ``uvicorn projektakte.main:app`` keeps working, but importing this module has no side effects
    if name == "app":
        global app
        app = create_app()
        return app
    raise AttributeError(name)
