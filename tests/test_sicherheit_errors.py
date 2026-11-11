"""live-demo: unknown URLs show the German error page in the browser (JSON for the API), the
favicon exists, and original files get a CSP that lets the browser's own viewers work."""
import secrets
import struct
import zlib

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from projektakte import db
from projektakte.auth import hash_password
from projektakte.config import load_settings
from projektakte.main import create_app
from projektakte.websecurity import (CONTENT_SECURITY_POLICY, FILE_CONTENT_SECURITY_POLICY, SecurityHeadersMiddleware,
                                     file_policy)

PW = "Brandschutz-2025!"
HTML = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
PNG = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
       + b"\x00" * 4)


@pytest.fixture()
def app(tmp_path):
    projects = tmp_path / "projekte"
    (projects / "BS-2024-0001").mkdir(parents=True)
    s = load_settings({"DATA_DIR": str(tmp_path / "data"), "APP_ENV": "test", "SECRET_KEY": secrets.token_urlsafe(48),
                       "AUTH_USERS": "anna:" + hash_password(PW, iterations=1000), "PROJECTS_DIR": str(projects)})
    application = create_app(s, background=False)
    files = {"plan.png": PNG, "notiz.txt": "Zeile 1\nZeile 2\n".encode(), "konzept.pdf": b"%PDF-1.4\n%%EOF\n"}
    with db.session(s.db_path) as conn:
        for i, (name, data) in enumerate(files.items(), start=1):
            path = projects / "BS-2024-0001" / name
            path.write_bytes(data)
            conn.execute("INSERT INTO documents (id, path, project, rel_path, filename, ext) VALUES (?, ?, ?, ?, ?, ?)",
                         (i, str(path), None, f"BS-2024-0001/{name}", name, path.suffix))
        conn.commit()
    return application


@pytest.fixture()
def client(app):
    c = TestClient(app)
    assert c.post("/login", data={"username": "anna", "password": PW}, follow_redirects=False).status_code == 303
    return c


# -- unknown URLs ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/gibtsnicht", "/projekte/BS-2024-0001/gibtsnicht", "/static/../gibtsnicht.html"])
def test_unknown_url_shows_german_page_in_the_browser(client, path):
    r = client.get(path, headers=HTML)
    assert r.status_code == 404 and r.headers["content-type"].startswith("text/html")
    assert "Diese Seite gibt es nicht" in r.text and "Zur Projektliste" in r.text and "Not Found" not in r.text
    assert r.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


def test_unknown_url_stays_json_for_api_and_scripts(client):
    assert client.get("/api/gibtsnicht", headers=HTML).json() == {"detail": "Not Found"}
    r = client.get("/gibtsnicht")  # curl: no text/html in Accept
    assert r.status_code == 404 and r.json() == {"detail": "Not Found"}


def test_unknown_url_without_login_redirects_to_login(app):
    r = TestClient(app).get("/gibtsnicht", headers=HTML, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")  # existence is not revealed


def test_wrong_method_is_german_too(client):
    r = client.put("/status", headers=HTML)
    assert r.status_code == 405 and "nicht möglich" in r.text and "Method Not Allowed" not in r.text
    assert client.put("/api/version").status_code == 405


def test_malformed_id_in_the_address(client):
    r = client.get("/dokumente/abc", headers=HTML)
    assert r.status_code == 404 and "Diese Seite gibt es nicht" in r.text
    r = client.get("/dokumente/abc")  # API clients keep FastAPI's details
    assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["path", "doc_id"]


def test_routes_own_errors_keep_their_message(client):
    r = client.get("/dokumente/999/datei", headers=HTML)
    assert r.status_code == 404 and "nicht gefunden" in r.text.lower()


# -- favicon ---------------------------------------------------------------------------------------------
def test_favicon_is_public_png(app):
    r = TestClient(app).get("/favicon.ico")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG\r\n\x1a\n") and "max-age" in r.headers["cache-control"]
    width, height = struct.unpack(">II", r.content[16:24])
    assert (width, height) == (32, 32)
    idat = r.content.index(b"IDAT")
    length = struct.unpack(">I", r.content[idat - 4:idat])[0]
    assert len(zlib.decompress(r.content[idat + 4:idat + 4 + length])) == 32 * (1 + 32 * 3)


# -- original files: CSP for the browser's own viewers ---------------------------------------------------
@pytest.mark.parametrize("doc_id,media,sandboxed", [(1, "image/png", True), (2, "text/plain", True),
                                                    (3, "application/pdf", False)])
def test_original_files_get_viewer_csp(client, doc_id, media, sandboxed):
    r = client.get(f"/dokumente/{doc_id}/datei", headers=HTML)
    assert r.status_code == 200 and r.headers["content-type"].startswith(media)
    csp = r.headers["content-security-policy"]
    assert "style-src 'unsafe-inline'" in csp and "script-src" not in csp and "default-src 'none'" in csp
    # the app's own document viewer frames them: same origin only (tests/test_dokument_betrachter.py)
    assert "frame-ancestors 'self'" in csp and "form-action 'none'" in csp
    assert csp.endswith("; sandbox") is sandboxed  # PDF viewers refuse sandboxed documents
    assert r.headers["x-frame-options"] == "SAMEORIGIN" and r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"


def test_file_policy_rules():
    assert file_policy("text/html; charset=utf-8") == CONTENT_SECURITY_POLICY  # error page under that path
    assert file_policy("text/html", 'attachment; filename="x.html"').endswith("; sandbox")  # an HTML file
    assert file_policy("application/pdf") == FILE_CONTENT_SECURITY_POLICY
    sandboxed = FILE_CONTENT_SECURITY_POLICY + "; sandbox"
    assert file_policy("image/png") == file_policy("text/plain; charset=utf-8") == sandboxed
    assert "'unsafe-inline'" not in CONTENT_SECURITY_POLICY  # the pages keep the strict policy


def test_viewer_csp_also_for_the_filename_variant():
    """…/datei/<Dateiname> (tab title and "save as" show the file name) gets the same policy;
    other paths and HTML answers keep the strict one."""
    async def png(request):
        return Response(PNG, media_type="image/png")

    async def html(request):
        return HTMLResponse("<h1>Nicht gefunden</h1>", status_code=404)

    app = Starlette(routes=[Route("/dokumente/{i}/datei/{name:path}", png), Route("/dokumente/{i}/datei", png),
                            Route("/dokumente/{i}/datei-fehlt", html), Route("/bild.png", png),
                            Route("/dokumente/{i}/fehler/datei", html)])
    c = TestClient(SecurityHeadersMiddleware(app))
    for path in ("/dokumente/7/datei", "/dokumente/7/datei/Fluchtwegplan%20EG.png", "/dokumente/7/datei/a/b.png"):
        assert c.get(path).headers["content-security-policy"] == FILE_CONTENT_SECURITY_POLICY + "; sandbox", path
    for path in ("/bild.png", "/dokumente/7/datei-fehlt", "/dokumente/7/fehler/datei"):
        assert c.get(path).headers["content-security-policy"] == CONTENT_SECURITY_POLICY, path


def test_every_file_route_gets_the_viewer_csp(app):
    """Guards the path rule against new file routes (e.g. …/datei/{filename} for the tab title)."""
    import re

    from projektakte.websecurity import FILE_PATH

    def paths(routes):
        for route in routes:
            if hasattr(route, "original_router"):  # FastAPI ≥ 0.16x keeps included routers nested
                yield from paths(route.original_router.routes)
            elif hasattr(route, "routes") and not hasattr(route, "path"):
                yield from paths(route.routes)
            elif isinstance(getattr(route, "path", None), str):
                yield route.path

    routes = [path for path in paths(app.routes) if re.search(r"/datei\b", path)]
    assert "/dokumente/{doc_id}/datei" in routes
    for route in routes:
        sample = re.sub(r"\{[^}]+\}", "Plan EG.png", re.sub(r"\{doc_id[^}]*\}", "7", route))
        assert FILE_PATH.match(sample), route
