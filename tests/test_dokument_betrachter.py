"""Document viewer (Dokument-Betrachter): the panel fragment per file type, page anchors from citations,
TIFF/BMP previews with their limits, the headers that let only the own origin frame original files,
login, links without JavaScript, and HTML/SVG never shown as active content."""
import io
import re
import secrets

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from projektakte import betrachter, db
from projektakte.auth import hash_password
from projektakte.config import load_settings
from projektakte.main import create_app
from projektakte.web import templates, viewer_url
from projektakte.websecurity import CONTENT_SECURITY_POLICY

PW = "Brandschutz-2025!"
HX = {"HX-Request": "true"}
PROJECT = "BS-2024-0001"


def _image(mode: str, size: tuple[int, int], fmt: str, pages: int = 1) -> bytes:
    frames = [Image.new(mode, size, 255 if mode in ("1", "L") else (200, 30 + 60 * i, 30)) for i in range(pages)]
    buf = io.BytesIO()
    extra = {"save_all": True, "append_images": frames[1:]} if pages > 1 else {}
    frames[0].save(buf, fmt, **extra)
    return buf.getvalue()


FILES = {  # id: (file name, content, chunks)
    1: ("Brandschutzkonzept.pdf", b"%PDF-1.4\n%%EOF\n", [(3, "Abschnitt 4 Rettungswege")]),
    2: ("Foto Treppenhaus.jpg", None, []),
    3: ("Bestandsplan EG.tif", None, [(1, "Plan Erdgeschoss")]),
    4: ("Stellungnahme.eml", b"From: a@b.example\r\nSubject: Test\r\n\r\nText\r\n",
        [(None, "## Stellungnahme\n\nDie Feuerwehrzufahrt ist frei zu halten."), (None, "Zweiter Abschnitt")]),
    5: ("Erläuterung.docx", b"PK\x03\x04docx", [(None, "Erläuterungsbericht zum Brandschutz")]),
    6: ("Protokoll.odt", b"PK\x03\x04odt", [(None, "Protokoll der Begehung")]),
    7: ("Übersicht.html", b"<script>alert(1)</script><p>Hallo</p>", [(None, "Hallo")]),
    8: ("Symbol.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', []),
    9: ("Lageplan.bmp", None, []),
}


@pytest.fixture()
def app(tmp_path):
    projects = tmp_path / "projekte"
    folder = projects / PROJECT / "Pläne"
    folder.mkdir(parents=True)
    s = load_settings({"DATA_DIR": str(tmp_path / "data"), "APP_ENV": "test", "SECRET_KEY": secrets.token_urlsafe(48),
                       "AUTH_USERS": "anna:" + hash_password(PW, iterations=1000), "PROJECTS_DIR": str(projects)})
    application = create_app(s, background=False)
    content = dict(FILES)
    images = {2: _image("RGB", (64, 48), "JPEG"), 3: _image("1", (80, 120), "TIFF", pages=3),
              9: _image("RGB", (40, 30), "BMP")}
    with db.session(s.db_path) as conn:
        conn.execute("INSERT INTO projects (number, name, path) VALUES (?, 'Testprojekt', ?)",
                     (PROJECT, str(projects / PROJECT)))
        for doc_id, (name, data, chunks) in content.items():
            path = folder / name
            path.write_bytes(images.get(doc_id, data))
            conn.execute("INSERT INTO documents (id, path, project, rel_path, filename, ext, size) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (doc_id, str(path), PROJECT, f"{PROJECT}/Pläne/{name}", name, path.suffix.lower(),
                          path.stat().st_size))
            for ord_, (page, text) in enumerate(chunks):
                conn.execute("INSERT INTO chunks (id, doc_id, project, ord, page, text) VALUES (?, ?, ?, ?, ?, ?)",
                             (doc_id * 100 + ord_, doc_id, PROJECT, ord_, page, text))
        mid = conn.execute(
            "INSERT INTO mails(source, source_key, subject, sender, sender_name, date, body, preview, project, "
            "assign_status, has_attachments) VALUES('jmap', 'jmap:7:bt', 'Plan folgt', 'a@b.example', 'Anna', "
            "'2025-03-03T08:00:00+00:00', 'Anbei.', 'Anbei.', ?, 'assigned', 1)", (PROJECT,)).lastrowid
        conn.execute("INSERT INTO documents (id, path, project, mail_id, rel_path, filename, ext, size, status, "
                     "message, cache_path) VALUES (20, ?, ?, ?, 'Plan.pdf', 'Plan.pdf', '.pdf', 10, 'pending', "
                     "'Anhang ausstehend: plMail hat ihn noch nicht geliefert.', '')", (f"mail:{mid}:0:Plan.pdf",
                                                                                     PROJECT, mid))
        conn.execute("INSERT INTO documents (id, path, project, rel_path, filename, ext, size) VALUES "
                     "(21, ?, ?, ?, 'Weg.pdf', '.pdf', 10)", (str(folder / "Weg.pdf"), PROJECT, f"{PROJECT}/Pläne/Weg.pdf"))
        conn.commit()
    return application


@pytest.fixture()
def client(app):
    c = TestClient(app)
    assert c.post("/login", data={"username": "anna", "password": PW}, follow_redirects=False).status_code == 303
    return c


def panel(client, doc_id: int, **params) -> str:
    r = client.get(f"/dokumente/{doc_id}/ansicht", params=params, headers=HX)
    assert r.status_code == 200, r.text
    assert "<html" not in r.text  # a fragment for the panel, not a page
    return r.text


# -- panel per type ------------------------------------------------------------------------------------------
def test_pdf_opens_in_a_frame_of_the_file_route(client):
    html = panel(client, 1)
    assert '<iframe class="bt-frame" src="/dokumente/1/datei/Brandschutzkonzept.pdf"' in html
    assert "Brandschutzkonzept.pdf" in html and f"{PROJECT}/<wbr>Pläne" in html  # file name and folder
    assert 'data-bt-schliessen' in html and "In neuem Fenster öffnen" in html and 'target="_blank"' in html
    assert 'href="/dokumente/1">Textansicht</a>' in html


def test_citation_with_a_page_opens_the_pdf_at_that_page(client):
    html = panel(client, 1, seite=3, c=100)
    assert 'src="/dokumente/1/datei/Brandschutzkonzept.pdf#page=3"' in html and "Seite 3" in html
    assert 'href="/dokumente/1#c100">Textansicht</a>' in html
    assert viewer_url(1, 3, 100) == "/dokumente/1/ansicht?seite=3&c=100" and viewer_url(5) == "/dokumente/5/ansicht"


def test_source_and_hit_links_carry_the_viewer_address():
    """Answers and search hits: the href stays the document page (without JavaScript), data-betrachter opens
    the panel at the cited page and section."""
    tpl = templates.env.from_string('{% from "_macros.html" import source_list, search_hit with context %}'
                                    '{{ source_list(items) }}{{ search_hit(h, "Rettungsweg") }}')
    item = {"n": 1, "kind": "document", "doc_id": 1, "chunk_id": 100, "page": 3, "label": "Konzept",
            "snippet": "Rettungsweg", "text": "Rettungsweg", "project": None}
    hit = {"kind": "document", "doc_id": 4, "chunk_id": 400, "page": None, "label": "Stellungnahme",
           "text": "Rettungsweg", "project": None}
    html = tpl.render(items=[item], h=hit, request=None)
    assert '<a href="/dokumente/1#c100" data-betrachter="/dokumente/1/ansicht?seite=3&amp;c=100">' in html
    assert '<a href="/dokumente/4#c400" data-betrachter="/dokumente/4/ansicht?c=400">' in html


def test_image_is_shown_directly_with_zoom(client):
    html = panel(client, 2)
    assert '<img class="bt-img" src="/dokumente/2/datei/Foto%20Treppenhaus.jpg"' in html and "data-bt-zoom" in html
    assert "<iframe" not in html


def test_tiff_is_converted_page_by_page(client):
    html = panel(client, 3)
    assert 'src="/dokumente/3/vorschau"' in html and "Seite 1 von 3" in html
    assert 'href="/dokumente/3/ansicht?seite=2" data-bt-wechsel' in html
    html = panel(client, 3, seite=3)
    assert 'src="/dokumente/3/vorschau?seite=3"' in html and "Seite 3 von 3" in html
    assert "seite=4" not in html
    r = client.get("/dokumente/3/vorschau", params={"seite": 2})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"  # black and white: PNG
    img = Image.open(io.BytesIO(r.content))
    assert img.size == (80, 120) and img.mode == "L"
    assert r.headers["cache-control"] == "no-store" and r.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert client.get("/dokumente/3/vorschau", params={"seite": 4}).status_code == 422


def test_bmp_is_converted_to_jpeg(client):
    assert 'src="/dokumente/9/vorschau"' in panel(client, 9)
    r = client.get("/dokumente/9/vorschau")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"


def test_preview_only_for_convertible_images(client):
    for doc_id in (1, 2, 4, 8, 20, 999):
        assert client.get(f"/dokumente/{doc_id}/vorschau").status_code == 404, doc_id


@pytest.mark.parametrize("doc_id,text", [(4, "Die Feuerwehrzufahrt ist frei zu halten."),
                                         (5, "Erläuterungsbericht zum Brandschutz"),
                                         (6, "Protokoll der Begehung")])
def test_mails_and_office_files_show_the_read_text(client, doc_id, text):
    html = panel(client, doc_id)
    assert text in html and "<iframe" not in html and "<img" not in html
    assert "Angezeigt wird der gelesene Text" in html and "Originaldatei herunterladen" in html


def test_text_view_marks_the_cited_section(client):
    html = panel(client, 4, c=401)
    assert 'data-bt-ziel="bt-c401"' in html and '<section class="chunk" id="bt-c401">' in html
    assert 'data-bt-ziel' not in panel(client, 4, c=999)  # a section of another document: no target


def test_heading_markup_of_the_text_is_cleaned(client):
    html = panel(client, 4)
    assert "## Stellungnahme" not in html and "Stellungnahme" in html


def test_pending_attachment_shows_the_note_instead_of_a_frame(client):
    html = panel(client, 20)
    assert "Originaldatei noch nicht da – sie wird automatisch nachgeladen" in html
    assert "<iframe" not in html and "/dokumente/20/datei" not in html
    assert "Anhang der Mail „Plan folgt“" in html


def test_missing_file_shows_the_note(client):
    html = panel(client, 21)
    assert "Die Originaldatei ist nicht verfügbar." in html and "<iframe" not in html
    assert "In neuem Fenster öffnen" not in html


def test_unknown_document(client):
    assert client.get("/dokumente/999/ansicht", headers=HX).status_code == 404


# -- HTML and SVG never as active content ------------------------------------------------------------------
@pytest.mark.parametrize("doc_id", [7, 8])
def test_html_and_svg_are_never_shown_inline(client, doc_id):
    html = panel(client, doc_id)
    assert "<iframe" not in html and "<img" not in html and "<script" not in html
    assert "aus Sicherheitsgründen nicht im Browser angezeigt" in html
    r = client.get(f"/dokumente/{doc_id}/datei")
    assert r.status_code == 200 and r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers["content-security-policy"].endswith("; sandbox")


def test_document_page_does_not_embed_html(client):
    html = client.get("/dokumente/7").text
    assert "<iframe" not in html and "bt-inline" not in html


# -- document page: the same view inline, also without JavaScript ------------------------------------------
def test_document_page_embeds_the_pdf(client):
    html = client.get("/dokumente/1", params={"seite": 3}).text
    assert '<iframe class="bt-frame" src="/dokumente/1/datei/Brandschutzkonzept.pdf#page=3"' in html
    assert "Originaldatei öffnen" in html and "Abschnitt 4 Rettungswege" in html


def test_document_page_pages_through_a_tiff_without_javascript(client):
    html = client.get("/dokumente/3").text
    assert 'src="/dokumente/3/vorschau"' in html and 'href="/dokumente/3?seite=2"' in html
    assert "data-bt-wechsel" not in html
    assert 'src="/dokumente/3/vorschau?seite=2"' in client.get("/dokumente/3", params={"seite": 2}).text


def test_without_javascript_the_viewer_address_leads_to_the_document_page(client):
    r = client.get("/dokumente/1/ansicht", params={"seite": 3, "c": 100}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/dokumente/1?seite=3#c100"


def test_links_keep_the_document_page_as_href(client):
    html = client.get(f"/projekte/{PROJECT}", params={"ordner": f"{PROJECT}/Pläne"}).text
    assert '<a href="/dokumente/1" data-betrachter="/dokumente/1/ansicht">' in html
    assert '<div id="betrachter" class="bt-panel"' in html and "hidden></div>" in html
    assert '<script src="/static/betrachter.js" defer></script>' in html
    assert client.get("/static/betrachter.js").status_code == 200


def test_mail_page_attachment_opens_in_the_viewer(client):
    with db.session(client.app.state.settings.db_path) as conn:
        mid = conn.execute("SELECT mail_id FROM documents WHERE id = 20").fetchone()[0]
    html = client.get(f"/mails/{mid}").text
    assert '<a href="/dokumente/20" data-betrachter="/dokumente/20/ansicht">' in html


# -- headers: framing only by the own origin ---------------------------------------------------------------
@pytest.mark.parametrize("doc_id", [1, 2])
def test_file_route_may_be_framed_by_the_own_origin_only(client, doc_id):
    r = client.get(f"/dokumente/{doc_id}/datei")
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors 'self'" in csp and "frame-ancestors 'none'" not in csp
    assert r.headers["x-frame-options"] == "SAMEORIGIN"
    assert "script-src" not in csp and "default-src 'none'" in csp  # still script-free


def test_pages_frame_only_the_own_origin_and_are_not_framed(client):
    for path in ("/", "/dokumente/1", f"/projekte/{PROJECT}"):
        r = client.get(path)
        csp = r.headers["content-security-policy"]
        assert "frame-src 'self'" in csp and "object-src 'none'" in csp and "frame-ancestors 'none'" in csp
        assert r.headers["x-frame-options"] == "DENY", path
    r = client.get("/dokumente/1/ansicht", headers=HX)
    assert r.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_error_page_under_the_file_route_is_not_frameable(client):
    r = client.get("/dokumente/21/datei", headers={"Accept": "text/html"})
    assert r.status_code == 404 and r.headers["x-frame-options"] == "DENY"


# -- login --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/dokumente/1/ansicht", "/dokumente/3/vorschau", "/dokumente/1/datei"])
def test_viewer_needs_a_login(app, path):
    anonymous = TestClient(app)
    r = anonymous.get(path, headers=HX, follow_redirects=False)
    assert r.status_code == 401 and r.headers["hx-redirect"] == "/login"
    r = anonymous.get(path, headers={"Accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


# -- limits of the conversion -------------------------------------------------------------------------------
def test_too_large_image_is_not_converted(client, monkeypatch):
    monkeypatch.setattr(betrachter, "MAX_PIXELS", 5000)  # the test TIFF has 80 × 120 = 9600 pixels
    html = panel(client, 3)
    assert "Bild zu groß für die Vorschau (80 × 120 Pixel)." in html and "vorschau" not in html
    assert "Plan Erdgeschoss" in html  # the read text instead
    assert client.get("/dokumente/3/vorschau").status_code == 422


def test_decoded_size_is_checked_before_decoding(tmp_path, monkeypatch):
    path = tmp_path / "rgb.tif"
    path.write_bytes(_image("RGB", (100, 100), "TIFF"))
    monkeypatch.setattr(betrachter, "MAX_DECODED_BYTES", 100 * 100 * 3 - 1)
    opened = []
    real_load = Image.Image.load
    monkeypatch.setattr(Image.Image, "load", lambda self: opened.append(1) or real_load(self))
    with pytest.raises(betrachter.PreviewError, match="zu groß"):
        betrachter.preview(path)
    assert not opened  # refused from the header alone


def test_too_large_file_is_not_converted(tmp_path, monkeypatch):
    path = tmp_path / "plan.tif"
    path.write_bytes(_image("L", (10, 10), "TIFF"))
    monkeypatch.setattr(betrachter, "MAX_FILE_BYTES", 10)
    with pytest.raises(betrachter.PreviewError, match="Datei zu groß"):
        betrachter.image_info(path)


def test_large_preview_is_scaled_down(tmp_path, monkeypatch):
    path = tmp_path / "plan.tif"
    path.write_bytes(_image("RGB", (900, 300), "TIFF"))
    monkeypatch.setattr(betrachter, "PREVIEW_EDGE", 200)
    data, media = betrachter.preview(path)
    assert media == "image/jpeg" and Image.open(io.BytesIO(data)).size == (200, 67)


@pytest.mark.filterwarnings("ignore::UserWarning")  # Pillow: "Corrupt EXIF data"
def test_broken_image_gives_a_german_message(tmp_path):
    path = tmp_path / "kaputt.tif"
    path.write_bytes(b"II*\x00kaputt")
    with pytest.raises(betrachter.PreviewError, match="nicht gelesen"):
        betrachter.image_info(path)


def test_one_conversion_at_a_time(tmp_path, monkeypatch):
    path = tmp_path / "plan.tif"
    path.write_bytes(_image("L", (10, 10), "TIFF"))
    monkeypatch.setattr(betrachter, "WAIT_SECONDS", 0.05)
    assert betrachter._ONE_AT_A_TIME.acquire()
    try:
        with pytest.raises(betrachter.PreviewBusy):
            betrachter.preview(path)
    finally:
        betrachter._ONE_AT_A_TIME.release()
    assert betrachter.preview(path)[1] == "image/png"


def test_view_kinds():
    assert betrachter.view_kind(".PDF") == "pdf"
    assert {betrachter.view_kind(e) for e in (".jpg", ".jpeg", ".png", ".gif", ".webp")} == {"bild"}
    assert {betrachter.view_kind(e) for e in (".tif", ".tiff", ".bmp")} == {"wandeln"}
    assert {betrachter.view_kind(e) for e in (".eml", ".msg", ".docx", ".odt", ".txt", ".svg", ".html", "")} == {"text"}
    assert betrachter.is_active(".SVG") and betrachter.is_active(".htm") and not betrachter.is_active(".pdf")


def test_static_script_has_no_inline_code_needs():
    """The script runs under script-src 'self': no eval, no inline handlers, no style attributes."""
    from pathlib import Path

    js = (Path(betrachter.__file__).parent / "static" / "betrachter.js").read_text(encoding="utf-8")
    assert "eval(" not in js and "new Function" not in js and ".style." not in js and "style=" not in js
    assert re.search(r"on\w+=", js.replace("window.onpopstate =", "")) is None
