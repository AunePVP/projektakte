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
    for text in ("Frage zu diesem Projekt", "Zeitachse", "Dokumente", "Mails", "Baugenehmigung.pdf",
                 "Zustimmung zur Abweichung Flurlänge", "übersprungen"):
        assert text in r.text, text
    # timeline is chronological (newest first)
    dates = re.findall(r'<span class="date">(\d\d)\.(\d\d)\.(\d{4})</span>', r.text)
    iso = [f"{y}{m}{d}" for d, m, y in dates]
    assert iso == sorted(iso, reverse=True) and len(iso) > 5
    assert client.get("/projekte/BS-1999-0001", headers={"Accept": "text/html"}).status_code == 404


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
    assert r.status_code == 200 and "Industriehalle Nordring" in r.text


def test_inbox_actions(client):
    page = client.get("/eingang").text
    assert "Bestätigen" in page and "Verwerfen" in page and "Kein Projektbezug" in page
    ids = re.findall(r'id="mail-(\d+)"', page)
    r = client.post(f"/eingang/{ids[0]}", data={"action": "confirm"}, headers=HX)
    assert "Zugeordnet zu" in r.text
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
