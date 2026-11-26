"""What the pages show about reading and page types: the state of a document that is still being read, the type
of each page, typing a page by hand, pages to look at, and a site with a single order."""
from __future__ import annotations

import pytest

from projektakte import db, seitentypen
from projektakte.web import doc_state

from .seitenhilfe import docs as _docs
from .seitenhilfe import stage1 as _stage1
from .seitenhilfe import text_of as _text
from .test_seitenmodell_stufe1 import Big
from .test_seitentyp_suche import client, index  # noqa: F401 – fixtures


# -- the state of a document ------------------------------------------------------------------------------------
def test_state_of_a_scan_that_waits_is_not_skipped():
    state = doc_state({"status": "skipped", "vision_pending": 10, "incomplete": 1, "pages": 10,
                       "message": "Bildauswertung ausstehend: 10 Seite(n)"})
    assert state == {"label": "wartet auf Bildauswertung", "warn": False, "detail": "10 Seiten noch nicht gelesen"}


def test_state_of_a_document_partly_read():
    state = doc_state({"status": "ok", "vision_pending": 172, "incomplete": 1, "pages": 184, "method": "vision"})
    assert state == {"label": "wird gelesen", "warn": False, "detail": "172 von 184 Seiten noch nicht gelesen"}


def test_state_of_a_digital_document_whose_pages_wait_for_their_type():
    state = doc_state({"status": "ok", "vision_pending": 3, "incomplete": 0, "pages": 3})
    assert state == {"label": "", "warn": False, "detail": "indexiert – Seitentyp für 3 Seiten offen"}


def test_state_of_finished_and_failed_documents():
    assert doc_state({"status": "ok", "vision_pending": 0, "method": "vision"}) == \
        {"label": "", "warn": False, "detail": "indexiert (per Bildauswertung)"}
    assert doc_state({"status": "ok"}) == {"label": "", "warn": False, "detail": "indexiert"}
    assert doc_state({"status": "error", "message": "Datei nicht lesbar"}) == \
        {"label": "Fehler", "warn": True, "detail": "Datei nicht lesbar"}
    assert doc_state({"status": "skipped", "vision_pending": 0, "message": "Format .dwg nicht unterstützt"}) == \
        {"label": "übersprungen", "warn": True, "detail": "Format .dwg nicht unterstützt"}


def test_document_page_and_list_say_that_pages_wait(client, index):
    conn, _root = index
    doc = _docs(conn)["Scan.pdf"]
    with db.session(client.app.state.settings.db_path) as c:
        c.execute("UPDATE documents SET vision_pending = 2, incomplete = 1, pages = 3 WHERE id = ?", (doc["id"],))
    html = client.get(f"/dokumente/{doc['id']}").text
    assert "wird gelesen" in html and "2 von 3 Seiten noch nicht gelesen" in html
    listing = client.get("/projekte/BS-2024-0001").text
    assert "wird gelesen" in listing


# -- page types on the document page ----------------------------------------------------------------------------
def test_document_page_shows_the_type_of_each_page(client, index):
    conn, _root = index
    doc = _docs(conn)["Scan.pdf"]
    html = client.get(f"/dokumente/{doc['id']}").text
    assert "Seite 2 · Plan" in html and "Seite 3 · Typ unsicher (vermutlich Plan)" in html
    assert "Seite 1 · " not in html  # a plain text page needs no label
    # the pages to look at, with a form to type them by hand
    assert f'action="/dokumente/{doc["id"]}/seiten/3/typ"' in html
    assert f'action="/dokumente/{doc["id"]}/seiten/1/typ"' not in html


def test_typing_a_page_by_hand(client, index):
    conn, root = index
    doc = _docs(conn)["Scan.pdf"]
    r = client.post(f"/dokumente/{doc['id']}/seiten/3/typ", data={"typ": "Leer"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/dokumente/{doc['id']}#seiten"
    with db.session(client.app.state.settings.db_path) as c:
        row = c.execute("SELECT * FROM page_types WHERE doc_id = ? AND page = 3", (doc["id"],)).fetchone()
        assert (row["typ"], row["source"], row["kopf"]) == ("Leer", "mensch", "Plan")
        d = c.execute("SELECT incomplete, extract_profile FROM documents WHERE id = ?", (doc["id"],)).fetchone()
        assert (d["incomplete"], d["extract_profile"]) == (1, "")  # read again at the next pass, without the model
    assert client.post(f"/dokumente/{doc['id']}/seiten/3/typ", data={"typ": "Tabelle"}).status_code == 422
    assert client.post(f"/dokumente/{doc['id']}/seiten/99/typ", data={"typ": "Plan"}).status_code == 404


def test_a_page_typed_empty_by_hand_loses_its_text_at_the_next_pass(index):
    conn, root = index
    doc = _docs(conn)["Scan.pdf"]
    assert _text(conn, "Scan.pdf").count("Seite gescannt") == 2  # pages 2 (Plan) and 3 (Unsicher) keep the OCR layer
    seitentypen.set_by_hand(conn, doc["id"], 3, "Leer")
    conn.commit()
    _stage1(conn, root, Big())
    assert _text(conn, "Scan.pdf").count("Seite gescannt") == 1
    row = seitentypen.known(conn, doc["id"])[3]
    assert (row["typ"], row["source"]) == ("Leer", "mensch")
    assert _docs(conn)["Scan.pdf"]["vision_pending"] == 0  # the model was not needed


def test_pages_to_look_at(client, index):
    conn, _root = index
    doc = _docs(conn)["Scan.pdf"]
    html = client.get("/seiten").text
    assert "Scan.pdf" in html and "Typ unsicher (vermutlich Plan)" in html
    assert f'action="/dokumente/{doc["id"]}/seiten/3/typ"' in html
    assert "Wartung.pdf" in html and "Schleife" in html
    assert "Notiz.txt" not in html
    counts = client.get("/api/status").json()["counts"]["seiten"]
    assert counts["Text"] == 2 and counts["Plan"] == 1 and counts["Unsicher"] == 1 and counts["Schleife"] == 1


# -- a site with a single order ---------------------------------------------------------------------------------
def _site(client, orders: int) -> None:
    with db.session(client.app.state.settings.db_path) as c:
        c.execute("INSERT INTO projects(number, name, path, kind) VALUES('3011', 'Musterring 17', '/p/3011', 'bauvorhaben')")
        for k in range(orders):
            c.execute("INSERT INTO projects(number, name, path, parent) VALUES(?, 'Musterring 17', ?, '3011')",
                      (f"30110{k}", f"/p/3011/30110{k}"))


def test_site_with_a_single_order_opens_the_order(client):
    _site(client, 1)
    r = client.get("/projekte/3011", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/projekte/301100"
    assert client.get("/projekte/3011?bauvorhaben=1", follow_redirects=False).status_code == 200


def test_site_with_several_orders_stays(client):
    _site(client, 2)
    assert client.get("/projekte/3011", follow_redirects=False).status_code == 200


def test_site_with_files_of_its_own_stays(client):
    _site(client, 1)
    with db.session(client.app.state.settings.db_path) as c:
        c.execute("INSERT INTO documents(project, path, rel_path, filename, ext, sha256, status) "
                  "VALUES('3011', '/p/3011/a.txt', 'a.txt', 'a.txt', '.txt', 'x', 'ok')")
    assert client.get("/projekte/3011", follow_redirects=False).status_code == 200
