"""The page type in the search: a filter (search page and API) and a label at hits and sources, the numbers per
type on the document page."""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from projektakte import db, seitentypen
from projektakte.config import load_settings
from projektakte.main import create_app
from projektakte.search import fts_search, hybrid_search, load_hits, page_type_filter, typed_ids

from .seitenhilfe import docs as _docs
from .seitenhilfe import setup_index as _setup
from .seitenhilfe import stage1 as _stage1
from .test_seitenmodell_ablauf import Stub, block, plain_scan, reading, run
from .test_seitenmodell_stufe1 import Big


@pytest.fixture()
def index(tmp_path):
    """Scan.pdf: page 1 Text (the model's text), 2 Plan, 3 Unsicher (probably a plan); Wartung.pdf: a text page
    with a loop cut short; Notiz.txt without page types. Every section speaks of the Rauchabzug. (The search
    shows at most three sections of a document; the newer file is read first.)"""
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan(3), "Wartung.pdf": plain_scan(1),
                                   "Notiz.txt": "Der Rauchabzug wurde in der Besprechung genannt.".encode()},
                        mtimes={"Scan.pdf": 1_700_000_000, "Wartung.pdf": 1_600_000_000})
    _stage1(conn, root, Big())
    run(conn, root, Stub(reading(raw=block("Der Rauchabzug im Treppenraum wurde geprüft.")), reading("Plan"),
                         reading("Unsicher", kopf="Plan", sicherheit=0.4),
                         reading(raw=block("Rauchabzug Wartung Wartung"), schleife=True)))
    yield conn, root
    conn.close()


def pages(conn, **kw) -> list[tuple[str, int | None]]:
    res = hybrid_search(conn, None, "x", "Rauchabzug", k=20, **kw)
    return sorted((h.title, h.page) for h in res.hits)


def test_no_filter_returns_all(index):
    conn, _root = index
    assert pages(conn) == [("Notiz.txt", None), ("Scan.pdf", 1), ("Scan.pdf", 2), ("Scan.pdf", 3), ("Wartung.pdf", 1)]


def test_filter_plan_only(index):
    conn, _root = index
    assert pages(conn, page_types=["Plan"]) == [("Scan.pdf", 2)]
    assert pages(conn, page_types=["Text"]) == [("Scan.pdf", 1), ("Wartung.pdf", 1)]
    assert pages(conn, page_types=["Plan", "Unsicher"]) == [("Scan.pdf", 2), ("Scan.pdf", 3)]


def test_filter_ohne_returns_sections_without_a_type(index):
    conn, _root = index
    assert pages(conn, page_types=["ohne"]) == [("Notiz.txt", None)]
    assert pages(conn, page_types=["ohne", "Plan"]) == [("Notiz.txt", None), ("Scan.pdf", 2)]


def test_unknown_type_is_refused(index):
    conn, _root = index
    with pytest.raises(ValueError):
        hybrid_search(conn, None, "x", "Rauchabzug", page_types=["Tabelle"])
    assert page_type_filter([]) == ("", [])


def test_typed_ids_filters_candidates_of_the_vector_search(index):
    conn, _root = index
    ids = [cid for cid, _score in fts_search(conn, "Rauchabzug", k=50)]
    assert len(ids) == 5
    plan = typed_ids(conn, ids, ["Plan"])
    assert len(plan) == 1 and load_hits(conn, list(plan))[next(iter(plan))].page == 2
    assert typed_ids(conn, ids, []) == set(ids)


def test_hit_carries_type_and_label(index):
    conn, _root = index
    by_page = {(h.title, h.page): h for h in hybrid_search(conn, None, "x", "Rauchabzug", k=20).hits}
    text, plan, unsure = (by_page[("Scan.pdf", n)] for n in (1, 2, 3))
    assert (text.page_type, text.page_label) == ("Text", None)
    assert (plan.page_type, plan.page_label) == ("Plan", "Plan")
    assert unsure.page_label == "Typ unsicher (vermutlich Plan)"
    assert by_page[("Wartung.pdf", 1)].page_label == "Schleife"
    note = by_page[("Notiz.txt", None)]
    assert (note.page_type, note.page_label) == (None, None)
    d = plan.to_dict()
    assert d["page_type"] == "Plan" and d["page_label"] == "Plan" and d["label"].endswith("S. 2")


# -- pages and API ----------------------------------------------------------------------------------------------
@pytest.fixture()
def client(index, tmp_path):
    conn, root = index
    conn.commit()
    s = load_settings({"DATA_DIR": str(tmp_path / "data"), "APP_ENV": "test", "AUTH_DISABLED": "1",
                       "PROJECTS_DIR": str(root), "AI_MODE": "aus", "LLM_URL": "http://127.0.0.1:9/v1",
                       "EMBED_URL": "http://127.0.0.1:9", "RERANK_URL": "http://127.0.0.1:9/rerank",
                       "EMBED_TIMEOUT": "0.2", "RERANK_TIMEOUT": "0.2"})
    s.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn.execute("VACUUM INTO ?", (str(s.db_path),))
    return TestClient(create_app(s, background=False))


def test_api_filters_by_page_type(client):
    hits = client.get("/api/search", params={"q": "Rauchabzug", "page_type": "Plan"}).json()["hits"]
    assert [(h["title"], h["page"], h["page_type"]) for h in hits] == [("Scan.pdf", 2, "Plan")]
    both = client.get("/api/search", params={"q": "Rauchabzug", "page_type": ["Plan", "ohne"]}).json()["hits"]
    assert sorted(h["title"] for h in both) == ["Notiz.txt", "Scan.pdf"]
    assert len(client.get("/api/search", params={"q": "Rauchabzug"}).json()["hits"]) == 5


def test_api_rejects_unknown_page_type(client):
    assert client.get("/api/search", params={"q": "Rauchabzug", "page_type": "Tabelle"}).status_code == 422


def test_search_page_shows_select_and_label(client):
    html = client.get("/suche", params={"q": "Rauchabzug"}).text
    assert 'name="seitentyp"' in html
    for option in ("alle Seitentypen", "Text", "Plan", "Foto", "Typ unsicher", "nicht eingeordnet"):
        assert option in html
    assert 'title="Seitentyp">Plan<' in html
    assert "Typ unsicher (vermutlich Plan)" in html and 'title="Seitentyp">Schleife<' in html


def test_search_page_filters(client):
    html = client.get("/suche", params={"q": "Rauchabzug", "seitentyp": "Plan"}).text
    assert "Scan.pdf, S. 2" in html and "Scan.pdf, S. 1" not in html and "Notiz.txt" not in html
    assert '<option value="Plan" selected>' in html
    # an unknown value is no filter (the page is still shown)
    assert "Notiz.txt" in client.get("/suche", params={"q": "Rauchabzug", "seitentyp": "Tabelle"}).text


def test_document_page_shows_summary(client, index):
    conn, _root = index
    doc = _docs(conn)["Scan.pdf"]
    html = client.get(f"/dokumente/{doc['id']}").text
    assert "Seitentypen" in html and "1 Text, 1 Plan, 1 unsicher" in html
    assert "1 Text, 1 Schleife" in client.get(f"/dokumente/{_docs(conn)['Wartung.pdf']['id']}").text
    other = client.get(f"/dokumente/{_docs(conn)['Notiz.txt']['id']}").text
    assert "Seitentypen" not in other
