"""Login and logout pages (form login with the signed session cookie, see auth.py)."""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .auth import COOKIE, DEVICE_COOKIE, Auth, LoginResult, safe_next

log = logging.getLogger("projektakte.auth")

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def client_host(request: Request) -> str:
    """Client address – behind a trusted reverse proxy already the real client (uvicorn
    proxy_headers + FORWARDED_ALLOW_IPS)."""
    return request.client.host if request.client else ""


def wait_text(seconds: int) -> str:
    if seconds >= 120:
        return f"{-(-seconds // 60)} Minuten"
    return "1 Sekunde" if seconds == 1 else f"{seconds} Sekunden"


def failure_message(result: LoginResult) -> tuple[int, str]:
    """HTTP status and German message for a failed login attempt."""
    if result.status == "busy":
        return 503, "Der Server prüft gerade sehr viele Anmeldungen – bitte in einigen Sekunden erneut versuchen."
    if result.status == "throttled":
        return 429, f"Zu viele Fehlversuche – bitte in {wait_text(result.retry_after)} erneut versuchen."
    message = "Benutzername oder Passwort falsch."
    if result.retry_after:
        message += f" Nächster Versuch in {wait_text(result.retry_after)} möglich."
    return 401, message


def _insecure_transport(request: Request, auth: Auth) -> bool:
    """Secure cookies are dropped by the browser on plain HTTP (except on localhost)."""
    return auth.cookie_secure and request.url.scheme != "https" and request.url.hostname not in LOOPBACK_HOSTS


def login_page(request: Request, *, next_: str, error: str | None = None, status: int = 200,
               retry_after: int = 0, logged_out: str = "", username: str = "") -> HTMLResponse:
    auth: Auth = request.app.state.auth
    context = {"next": safe_next(next_), "error": error, "no_users": not auth.users,
               "insecure": _insecure_transport(request, auth), "logged_out": logged_out,
               "username": username[:128]}
    headers = {"Retry-After": str(retry_after)} if retry_after and status in (429, 503) else None
    return templates.TemplateResponse(request, "login.html", context, status_code=status, headers=headers)


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):  # noqa: A002
    auth: Auth = request.app.state.auth
    if request.state.user or auth.disabled:
        return RedirectResponse(safe_next(next), status_code=303)
    return login_page(request, next_=next, logged_out=request.query_params.get("abgemeldet", ""))


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, username: str = Form(""), password: str = Form(""),
                       next: str = Form("/")):  # noqa: A002
    auth: Auth = request.app.state.auth
    target = safe_next(next)
    if auth.disabled:
        return RedirectResponse(target, status_code=303)
    user = username.strip()
    device = request.cookies.get(DEVICE_COOKIE)
    result = await auth.authenticate(user, password, client_host(request), device=device)
    if not result.ok:
        status, message = failure_message(result)
        return login_page(request, next_=next, error=message, status=status, retry_after=result.retry_after,
                          username=user)
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(COOKIE, auth.make_cookie(user), max_age=auth.session_seconds, httponly=True,
                    samesite="lax", secure=auth.cookie_secure, path="/")
    if not auth.device_id(device, user):
        # only sent to /login: lets this browser log in even while others fail for the same user
        resp.set_cookie(DEVICE_COOKIE, auth.make_device_cookie(user), max_age=auth.device_seconds,
                        httponly=True, samesite="strict", secure=auth.cookie_secure, path="/login")
    log.info("Angemeldet: %s", user)
    return resp


@router.post("/logout")
def logout(request: Request):
    """Ends every session of the user server-side (all devices), then removes the cookie."""
    auth: Auth = request.app.state.auth
    user = getattr(request.state, "user", None)
    outcome = "1"
    if not auth.disabled and user in auth.users:
        try:
            auth.logout(user)
        except sqlite3.Error as exc:  # the cookie is removed anyway; say that copies stay valid
            log.error("Abmelden von %s auf dem Server fehlgeschlagen: %s", user, exc)
            outcome = "teilweise"
    resp = RedirectResponse(f"/login?abgemeldet={outcome}", status_code=303)
    resp.delete_cookie(COOKIE, path="/", httponly=True, samesite="lax", secure=auth.cookie_secure)
    return resp
