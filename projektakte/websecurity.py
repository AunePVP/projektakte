"""HTTP hardening: security headers on every response and rejection of cross-site state changes.

* Security headers (frame protection, nosniff, referrer policy, a strict Content Security Policy –
  the templates load scripts and styles only from /static, no inline code) and
  ``Cache-Control: no-store`` for everything except /static, so mails and documents do not stay
  in the browser cache of shared PCs.
* Original files (``/dokumente/{id}/datei…``) open in the browser's own viewers (image, text, PDF),
  which style their page inline and embed the PDF: they get a policy of their own – no scripts or
  forms, and a sandbox for everything but PDFs (PDF viewers refuse sandboxed documents). The
  application's own document viewer shows them in an iframe, so they may be framed by pages of the
  same origin (``frame-ancestors 'self'``, ``X-Frame-Options: SAMEORIGIN``); every other page may
  not be framed at all. The pages themselves frame only the own origin and embed no plugins.
* Cross-site requests: a browser marks every request with ``Sec-Fetch-Site``; for older browsers
  the ``Origin`` header is compared with the own host. State-changing requests (POST, PUT, PATCH,
  DELETE) that another site – also a sibling subdomain (``same-site``) – triggered are rejected.
  This complements ``SameSite=Lax`` cookies, which do not help against sibling subdomains, and also
  covers HTTP Basic credentials the browser sends by itself. Requests without both headers come from
  non-browser clients (curl, scripts) and pass; Bearer calls of the integration API too.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CONTENT_SECURITY_POLICY = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                           "frame-src 'self'; object-src 'none'; "
                           "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
SECURITY_HEADERS: dict[str, str] = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
FILE_CONTENT_SECURITY_POLICY = ("default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
                                "object-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'")
FILE_FRAME_OPTIONS = "SAMEORIGIN"  # older browsers without frame-ancestors
FILE_PATH = re.compile(r"/dokumente/\d+/datei(?:/.*)?\Z", re.DOTALL)  # also …/datei/<Dateiname>
CACHEABLE_PREFIXES = ("/static/",)
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
SAME_ORIGIN_SITES = frozenset({"same-origin", "none"})  # "none": typed URL, bookmark
BEARER_PREFIXES = ("/api/integration/",)


class SecurityHeadersMiddleware:
    """Pure ASGI middleware (no buffering, works with streamed file responses). Headers a route set
    itself are kept."""

    def __init__(self, app: ASGIApp, *, cacheable_prefixes: tuple[str, ...] = CACHEABLE_PREFIXES):
        self.app = app
        self.cacheable_prefixes = cacheable_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cacheable = scope["path"].startswith(self.cacheable_prefixes)
        original_file = bool(FILE_PATH.match(scope["path"]))

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if original_file:
                    policy = file_policy(headers.get("content-type", ""), headers.get("content-disposition", ""))
                    headers.setdefault("Content-Security-Policy", policy)
                    if policy != CONTENT_SECURITY_POLICY:  # an original file, not an error page
                        headers.setdefault("X-Frame-Options", FILE_FRAME_OPTIONS)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
                if not cacheable:
                    headers.setdefault("Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, send_with_headers)


def file_policy(content_type: str, content_disposition: str = "") -> str:
    """CSP for an original file shown by the browser's own viewer. The application's own HTML
    (an error page under the same address – original files always carry Content-Disposition)
    keeps the normal policy; an HTML *file* is sandboxed like any other."""
    media = content_type.split(";", 1)[0].strip().lower()
    if media == "text/html" and not content_disposition:
        return CONTENT_SECURITY_POLICY
    if media == "application/pdf":
        return FILE_CONTENT_SECURITY_POLICY
    return FILE_CONTENT_SECURITY_POLICY + "; sandbox"


def _normalize_host(netloc: str, scheme: str = "") -> str:
    host = netloc.strip().lower()
    for default in ((":443",) if scheme == "https" else (":80",) if scheme == "http" else (":80", ":443")):
        if host.endswith(default):
            host = host[: -len(default)]
    return host


def _origin_host(origin: str) -> str:
    try:
        parts = urlsplit(origin)
    except ValueError:
        return ""
    return _normalize_host(parts.netloc, parts.scheme) if parts.scheme in ("http", "https") else ""


def own_hosts(request: Request, public_url: str = "") -> set[str]:
    """Hosts under which the application is reached: the Host header and PUBLIC_URL."""
    hosts = {_normalize_host(request.headers.get("host", ""))}
    if public_url:
        hosts.add(_origin_host(public_url))
    hosts.discard("")
    return hosts


def is_cross_site(request: Request, public_url: str = "") -> bool:
    """True if a browser sent this request on behalf of another site (CSRF)."""
    headers = request.headers
    if (request.url.path.startswith(BEARER_PREFIXES)
            and headers.get("authorization", "")[:7].lower() == "bearer "):
        return False  # browsers never attach a Bearer token by themselves
    site = headers.get("sec-fetch-site", "").strip().lower()
    if site:
        return site not in SAME_ORIGIN_SITES
    origin = headers.get("origin", "").strip()
    if not origin:
        return False  # no browser (or a very old one: SameSite=Lax still applies)
    return _origin_host(origin) not in own_hosts(request, public_url)  # "null" → "" → rejected
