"""SEC-08: security headers, no caching of protected content, rejection of cross-site state changes."""
import re
import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from projektakte.auth import hash_password
from projektakte.config import load_settings
from projektakte.main import create_app
from projektakte.websecurity import CONTENT_SECURITY_POLICY, SECURITY_HEADERS

PW = "Brandschutz-2025!"
TOKEN = "plmail_" + secrets.token_hex(16)
TEMPLATES = Path(__file__).resolve().parent.parent / "projektakte" / "templates"


@pytest.fixture()
def client(tmp_path):
    s = load_settings({"DATA_DIR": str(tmp_path), "APP_ENV": "test", "SECRET_KEY": secrets.token_urlsafe(48),
                       "AUTH_USERS": "anna:" + hash_password(PW, iterations=1000), "INTEGRATION_TOKEN": TOKEN,
                       "PUBLIC_URL": "https://projektakte.firma.de"})
    c = TestClient(create_app(s, background=False), base_url="http://projektakte.lan")
    c.post("/login", data={"username": "anna", "password": PW}, follow_redirects=False)
    return c


def assert_security_headers(r):
    for name, value in SECURITY_HEADERS.items():
        assert r.headers.get(name) == value, name


@pytest.mark.parametrize("path", ["/", "/login", "/health", "/health/ready", "/api/projects", "/api/version",
                                  "/status", "/gibt-es-nicht"])
def test_security_headers_and_no_store(client, path):
    r = client.get(path, headers={"Accept": "text/html"})
    assert_security_headers(r)
    assert r.headers["cache-control"] == "no-store"


def test_headers_on_unauthenticated_answers(tmp_path):
    s = load_settings({"DATA_DIR": str(tmp_path), "APP_ENV": "test", "AUTH_USERS": ""})
    anonymous = TestClient(create_app(s, background=False))
    for r in (anonymous.get("/", headers={"Accept": "text/html"}, follow_redirects=False),
              anonymous.get("/api/projects")):
        assert r.status_code in (303, 401)
        assert_security_headers(r)
        assert r.headers["cache-control"] == "no-store"


def test_static_files_stay_cacheable(client):
    r = client.get("/static/app.css")
    assert r.status_code == 200
    assert_security_headers(r)
    assert "no-store" not in r.headers.get("cache-control", "")


def test_csp_is_strict():
    assert "script-src 'self'" in CONTENT_SECURITY_POLICY and "unsafe-inline" not in CONTENT_SECURITY_POLICY
    assert "frame-ancestors 'none'" in CONTENT_SECURITY_POLICY and "unsafe-eval" not in CONTENT_SECURITY_POLICY


def test_templates_work_with_the_csp():
    """No inline scripts, inline styles or event handler attributes (the CSP would block them)."""
    offenders = []
    for path in TEMPLATES.glob("*.html"):
        text = re.sub(r"\{#.*?#\}", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)  # Jinja comments
        for pattern in (r"<script(?![^>]*\bsrc=)[^>]*>", r"<style\b", r"\sstyle=", r"\son[a-z]+=",
                        r"javascript:", r"hx-on"):
            if re.search(pattern, text, re.IGNORECASE):
                offenders.append(f"{path.name}: {pattern}")
    assert not offenders, offenders


# -- cross-site requests ----------------------------------------------------------------------------
@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"},  # sibling subdomain, e.g. another intranet app with an XSS
    {"Origin": "https://evil.example"},  # older browser without Sec-Fetch-Site
    {"Origin": "null"},  # sandboxed iframe / data: URL
    {"Origin": "http://projektakte.lan.evil.example"},
])
def test_cross_site_state_changes_rejected(client, headers):
    for method, path, kw in (("POST", "/status/neu-indexieren", {}), ("POST", "/logout", {}),
                             ("POST", "/api/reindex", {}), ("POST", "/api/mails/1/assign", {"json": {"action": "none"}}),
                             ("POST", "/login", {"data": {"username": "anna", "password": PW}})):
        r = client.request(method, path, headers=headers, follow_redirects=False, **kw)
        assert r.status_code == 403, (method, path, r.status_code)
        assert_security_headers(r)
    assert client.get("/api/version").status_code == 200  # still logged in: the logout was rejected


def test_cross_site_rejection_is_german_html_for_browsers(client):
    r = client.post("/status/neu-indexieren", headers={"Sec-Fetch-Site": "cross-site", "Accept": "text/html"})
    assert r.status_code == 403 and "fremden Seite" in r.text and r.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "same-origin"},
    {"Sec-Fetch-Site": "none"},
    {"Origin": "http://projektakte.lan"},
    {"Origin": "https://projektakte.firma.de"},  # PUBLIC_URL (TLS proxy in front)
    {},  # curl, scripts
])
def test_same_origin_and_non_browser_requests_pass(client, headers):
    r = client.post("/status/neu-indexieren", headers=headers, follow_redirects=False)
    assert r.status_code == 303


def test_cross_site_get_is_allowed(client):
    # links from other pages (e.g. plMail) must keep working; GET changes nothing
    r = client.get("/api/version", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 200


def test_integration_bearer_is_exempt(client):
    anonymous = TestClient(client.app)
    r = anonymous.post("/api/integration/notify", headers={"Authorization": f"Bearer {TOKEN}",
                                                            "Origin": "https://plmail.firma.de",
                                                            "Sec-Fetch-Site": "cross-site"}, json={})
    assert r.status_code != 403
    r = anonymous.post("/api/integration/notify", headers={"Origin": "https://plmail.firma.de",
                                                            "Sec-Fetch-Site": "cross-site"}, json={})
    assert r.status_code == 403  # without the token the browser rule applies
