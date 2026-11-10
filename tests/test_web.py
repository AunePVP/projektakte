"""Web interface: pages render in German, forms work with and without HTMX."""
import re

import pytest
from fastapi.testclient import TestClient

from projektakte.ai.services import AIServices
from projektakte.demo import generate
from projektakte.indexer import run_index
from projektakte.main import create_app

from .conftest import fake_settings


@pytest.fixture()
def client(tmp_path, fake_ai):
    generate(tmp_path / "demo")
    s = fake_settings(fake_ai, tmp_path, DEMO_MODE=1)
    run_index(s, AIServices.from_settings(s))
    return TestClient(create_app(s, background=False))


HX = {"HX-Request": "true"}


def test_project_list_and_filter(client):
    r = client.get("/")
    assert r.status_code == 200 and 'lang="de"' in r.text
    assert "Grundschule Am Lindenhof" in r.text and "Zuordnungs-Eingang" in r.text
    assert re.search(r'class="badge">\d+<', r.text)  # inbox count in the navigation
    part = client.get("/", params={"q": "Hotel"}, headers=HX).text
    assert "Hotel Seeblick" in part and "Grundschule" not in part and "<html" not in part


def test_project_page_sections(client):
    r = client.get("/projekte/BS-2024-0137")
    assert r.status_code == 200
    for text in ("Frage zu diesem Projekt", "Zeitleiste", "Dokumente", "Mails", "Baugenehmigung.pdf",
                 "Zustimmung zur Abweichung Flurlänge", "nicht vollständig gelesen"):
        assert text in r.text, text
    # the timeline is chronological (newest first)
    dates = re.findall(r'<span class="d">(\d\d)\.(\d\d)\.(\d\d)</span>', r.text)
    iso = [f"{y}{m}{d}" for d, m, y in dates]
    assert iso == sorted(iso, reverse=True) and len(iso) > 5
    assert client.get("/projekte/BS-1999-0001", headers={"Accept": "text/html"}).status_code == 404


def _browser(html: str) -> str:
    return html.split('<div id="dok-browser">', 1)[1].split("</table>", 1)[0]


def test_project_documents_as_file_browser(client):
    """Documents are shown one folder at a time: the top level lists the folders (with their document
    count), a click opens a folder – with HTMX only the browser is replaced, without it the page."""
    top = _browser(client.get("/projekte/BS-2024-0137").text)
    for name in ("01 Genehmigung", "02 Konzept", "05 Pläne"):
        assert f">{name}</a>" in top, name
    assert "Baugenehmigung.pdf" not in top  # files stay inside their folder
    assert "1 Dokument<" in top and "1 nicht vollständig gelesen" in top  # the skipped scan in 05 Pläne
    part = client.get("/projekte/BS-2024-0137/ordner", params={"pfad": "05 Pläne"}, headers=HX)
    assert part.status_code == 200 and "<html" not in part.text
    assert "Fluchtwegplan EG.png" in part.text and "übersprungen" in part.text
    assert "01 Genehmigung" not in part.text and "übergeordneter Ordner" in part.text
    assert "/projekte/BS-2024-0137#inhalt" in part.text  # back to the top level
    # the same folder without JavaScript: the whole page opened there
    page = client.get("/projekte/BS-2024-0137", params={"ordner": "05 Pläne"}).text
    assert "Fluchtwegplan EG.png" in _browser(page) and "<html" in page
    plain = client.get("/projekte/BS-2024-0137/ordner", params={"pfad": "05 Pläne"}, follow_redirects=False)
    assert plain.status_code == 303 and plain.headers["location"].endswith("?ordner=05+Pl%C3%A4ne#inhalt")
    # a folder that does not exist (or tries to leave the project) shows a note, never other files
    gone = _browser(client.get("/projekte/BS-2024-0137", params={"ordner": "../BS-2023-0412"}).text)
    assert "nicht (mehr)" in gone
    assert client.get("/projekte/BS-1999-0001/ordner", headers={**HX, "Accept": "text/html"}).status_code == 404


def test_folder_view_levels():
    from projektakte.web import folder_view
    docs = [{"rel_path": p, "filename": p.rsplit("/", 1)[-1], "status": st} for p, st in (
        ("a.txt", "ok"), ("B/x.pdf", "ok"), ("B/C/y.pdf", "error"), ("B/C/D/z.pdf", "ok"), ("b2/w.txt", "ok"))]
    top = folder_view(docs, "")
    assert [f["name"] for f in top["folders"]] == ["B", "b2"] and [d["rel_path"] for d in top["files"]] == ["a.txt"]
    assert top["folders"][0]["count"] == 3 and top["folders"][0]["problems"] == 1 and top["parent"] is None
    b = folder_view(docs, "/B/")
    assert b["folder"] == "B" and [f["name"] for f in b["folders"]] == ["C"] and b["parent"] == ""
    assert [d["rel_path"] for d in b["files"]] == ["B/x.pdf"]
    c = folder_view(docs, "B/C")
    assert [x["path"] for x in c["crumbs"]] == ["B", "B/C"] and c["parent"] == "B"
    assert [f["path"] for f in c["folders"]] == ["B/C/D"] and [d["rel_path"] for d in c["files"]] == ["B/C/y.pdf"]
    assert not folder_view(docs, "Bx")["found"] and not folder_view(docs, "b")["found"]
    assert folder_view(docs, "../B")["folder"] == "B"  # ".." never leaves the project


def test_ask_form_htmx_and_plain(client):
    r = client.post("/fragen", data={"question": "Was fordert Auflage 7 der Baugenehmigung?",
                                     "project": "BS-2024-0137"}, headers=HX)
    assert r.status_code == 200 and "<html" not in r.text
    assert "Außentreppe" in r.text and 'href="#q-src-1"' in r.text and "Baugenehmigung.pdf, S. 2" in r.text
    full = client.post("/fragen", data={"question": "Welche Kantinenpreise gelten im Sommer?", "project": ""})
    assert "<html" in full.text and "nichts gefunden" in full.text
    assert client.get("/fragen").status_code == 200


def test_search_page(client):
    r = client.get("/suche", params={"q": "Löschwasserteich"})
    assert r.status_code == 200
    hits = r.text.split('<ol class="hits">', 1)[1].split("</ol>", 1)[0]  # not the project select
    assert "Industriehalle Nordring" in hits
    nothing = client.get("/suche", params={"q": "xyzzy"}).text
    assert "Keine passenden Treffer" in nothing and '<ol class="hits">' not in nothing


def test_inbox_actions(client):
    page = client.get("/eingang").text
    assert "Bestätigen" in page and "Verwerfen" in page and "Kein Projektbezug" in page
    ids = re.findall(r'id="mail-(\d+)"', page)
    r = client.post(f"/eingang/{ids[0]}", data={"action": "confirm"}, headers=HX)
    assert "Zugeordnet zu" in r.text and 'id="inbox-badge"' in r.text and 'hx-swap-oob="true"' in r.text
    r = client.post(f"/eingang/{ids[1]}", data={"action": "reject"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/eingang"
    r = client.post(f"/eingang/{ids[2]}", data={"action": "assign", "project": "BS-2024-0058"}, headers=HX)
    assert "BS-2024-0058" in r.text
    r = client.post(f"/eingang/{ids[3]}", data={"action": "confirm"}, headers=HX)
    assert r.status_code == 200
    # open redirect protection
    r = client.post(f"/eingang/{ids[4]}", data={"action": "none", "back": "//evil.example"},
                    follow_redirects=False)
    assert r.headers["location"] == "/eingang"


def test_mail_and_document_pages_and_file_download(client):
    mails = client.get("/api/mails", params={"q": "Stellungnahme Brandschutzdienststelle", "limit": 1}).json()
    mid = mails["mails"][0]["id"]
    page = client.get(f"/mails/{mid}").text
    assert "Stellungnahme Feuerwehr.pdf" in page and "Zuordnung ändern" in page
    doc_id = re.search(r'href="/dokumente/(\d+)"', page).group(1)
    assert client.get(f"/dokumente/{doc_id}").status_code == 200
    f = client.get(f"/dokumente/{doc_id}/datei")
    assert f.status_code == 200 and f.content.startswith(b"%PDF")
    docs = client.get("/api/projects/BS-2024-0137/documents").json()["documents"]
    pdf = next(d for d in docs if d["ext"] == ".pdf")
    page = client.get(f"/dokumente/{pdf['id']}").text
    assert "Seite 2" in page and "Auflage 7" in page
    assert client.get(f"/dokumente/{pdf['id']}/datei").headers["content-type"] == "application/pdf"


def test_status_page_and_deps_partial(client):
    assert "Indexierung" in client.get("/status").text
    part = client.get("/status/dienste").text
    assert "Sprachmodell" in part and "erreichbar" in part and "openai-chat" in part


def test_static_assets_are_local(client):
    page = client.get("/").text
    assert 'src="/static/htmx.min.js"' in page and "https://" not in page.split("<main>")[0]
    assert client.get("/static/htmx.min.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200


def test_no_cdn_api_docs(client):
    # Swagger-UI would load scripts from the internet; the schema stays available
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").json()["info"]["title"] == "Projektakte"
