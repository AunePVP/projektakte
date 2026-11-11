"""Order of the documents pass (status page "Reihenfolge", reihenfolge.py): folder order (INDEX_FOLDER_ORDER,
set on the status page, switched off), pinned projects, the done state per project and its requeue, the
project list with tabs and search, the card "Wird gerade gelesen", the API, access control, many projects,
and the Bildauswertung in the same order. Only invented project names."""
from __future__ import annotations

import os
import secrets
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from projektakte import db, documents, reihenfolge, vision
from projektakte.auth import hash_password
from projektakte.config import load_settings
from projektakte.documents import index_all_documents
from projektakte.extract import extraction_profile
from projektakte.indexer import run_index
from projektakte.main import create_app
from projektakte.registry import sync_from_folder

from .test_bildauswertung_stufen import Vision, png

PATTERN = r"\d{6}"
NAMES = ["Neubau Kita", "Umbau Lagerhalle", "Sanierung Rathaus", "Sporthalle", "Bürogebäude Nord"]
SHARE = {"1": ["110001", "110002"], "2": ["210001", "210005", "210003"], "3": ["310001", "310002"]}


@pytest.fixture(autouse=True)
def _live_reset():
    reihenfolge.LIVE.reset()
    yield
    reihenfolge.LIVE.reset()


def make_share(root: Path, share: dict[str, list[str]] = SHARE) -> Path:
    for folder, numbers in share.items():
        for i, number in enumerate(numbers):
            p = root / folder / f"{number}_{NAMES[i % len(NAMES)]}"
            p.mkdir(parents=True)
            (p / "Notiz.txt").write_text(f"Brandschutz Projekt {number}: Rettungsweg geprüft.", encoding="utf-8")
    return root


def settings(tmp_path, **extra):
    env = {"DATA_DIR": str(tmp_path / "data"), "APP_ENV": "test", "AUTH_DISABLED": "1", "DEMO_MODE": "0",
           "PROJECTS_DIR": str(tmp_path / "projekte"), "MAIL_DIR": str(tmp_path / "mails"),
           "PROJECT_NUMBER_PATTERN": PATTERN}
    env.update({k: str(v) for k, v in extra.items()})
    return load_settings(env)


@pytest.fixture()
def recorded(monkeypatch):
    """The projects in the order the documents pass takes them."""
    seen: list[str] = []
    real = documents.index_project_documents

    def spy(conn, project, *a, **kw):
        seen.append(project["number"])
        return real(conn, project, *a, **kw)

    monkeypatch.setattr(documents, "index_project_documents", spy)
    return seen


def client_for(s) -> TestClient:
    return TestClient(create_app(s, background=False))


def projects_row(s, number: str) -> dict:
    with db.session(s.db_path) as conn:
        return dict(conn.execute("SELECT * FROM projects WHERE number = ?", (number,)).fetchone())


# -- the order of the pass ---------------------------------------------------------------------------------
def test_default_order_numbered_folders_highest_first_newest_number_first(tmp_path, recorded):
    make_share(tmp_path / "projekte")
    run_index(settings(tmp_path))
    assert recorded == ["310002", "310001", "210005", "210003", "210001", "110002", "110001"]


def test_index_folder_order_from_the_environment(tmp_path, recorded):
    make_share(tmp_path / "projekte")
    s = settings(tmp_path, INDEX_FOLDER_ORDER="2, 1")
    assert s.index_folder_order == ("2", "1")
    run_index(s)
    # the listed folders first, the others after them in the default order
    assert recorded == ["210005", "210003", "210001", "110002", "110001", "310002", "310001"]


def test_wish_of_the_office_first_folder_3_then_2_set_in_the_web_ui(tmp_path, recorded):
    make_share(tmp_path / "projekte", {"1": ["110001"], "2": ["210001"], "3": ["310001"], "4": ["410001"]})
    s = settings(tmp_path)
    run_index(s)  # registers the projects (default: 4, 3, 2, 1)
    with db.session(s.db_path) as conn:
        conn.execute("UPDATE projects SET done_at = NULL")  # all waiting again
    c = client_for(s)
    page = c.get("/status")
    assert page.status_code == 200 and "Reihenfolge der Ordner" in page.text
    for _ in range(3):  # 4 down to the end: 3, 2, 1, 4
        assert c.post("/status/reihenfolge/ordner", data={"ordner": "4", "aktion": "runter"},
                      follow_redirects=False).status_code == 303
    recorded.clear()
    run_index(s)
    assert recorded == ["310001", "210001", "110001", "410001"]
    # stored in the database: a restart (new app) shows and uses the same order
    restarted = client_for(s)
    data = restarted.get("/api/reihenfolge").json()
    assert [f["name"] for f in data["folders"]] == ["3", "2", "1", "4"] and data["customized"] is True


def test_ui_order_wins_over_the_environment_until_reset(tmp_path):
    make_share(tmp_path / "projekte")
    s = settings(tmp_path, INDEX_FOLDER_ORDER="1")
    run_index(s)
    c = client_for(s)
    assert [f["name"] for f in c.get("/api/reihenfolge").json()["folders"]] == ["1", "3", "2"]
    assert "Voreinstellung aus" in c.get("/status").text
    c.post("/status/reihenfolge/ordner", data={"ordner": "2", "aktion": "hoch"})
    assert [f["name"] for f in c.get("/api/reihenfolge").json()["folders"]] == ["1", "2", "3"]
    r = c.post("/status/reihenfolge/standard", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("#reihenfolge")
    data = c.get("/api/reihenfolge").json()
    assert [f["name"] for f in data["folders"]] == ["1", "3", "2"] and data["customized"] is False


def test_a_folder_switched_off_is_not_read_but_stays_searchable(tmp_path, recorded):
    make_share(tmp_path / "projekte")
    s = settings(tmp_path)
    run_index(s)
    c = client_for(s)
    r = c.post("/status/reihenfolge/ordner", data={"ordner": "3", "aktion": "aus"}, headers={"HX-Request": "true"})
    assert "Ordner 3 ist ausgeschaltet – seine Projekte werden nicht gelesen" in r.text
    assert "aus – wird nicht gelesen" in r.text
    folders = c.get("/api/reihenfolge").json()["folders"]
    assert [(f["name"], f["enabled"], f["rank"]) for f in folders] == [("2", True, 1), ("1", True, 2), ("3", False, None)]
    # a new and a changed file in the folder switched off: not read; what was read stays (and stays searchable)
    project = next((tmp_path / "projekte" / "3").glob("310002_*"))
    (project / "Neu.txt").write_text("Brandmeldeanlage Zusatzvermerk", encoding="utf-8")
    (project / "Notiz.txt").write_text("Geaendert: Sprinkleranlage", encoding="utf-8")
    os.utime(project / "Notiz.txt", (time.time() + 5, time.time() + 5))
    with db.session(s.db_path) as conn:
        conn.execute("UPDATE projects SET done_at = NULL")
    recorded.clear()
    stats = run_index(s)
    assert recorded == ["210005", "210003", "210001", "110002", "110001"]
    assert stats["projects"].get("deactivated", 0) == 0
    row = projects_row(s, "310002")
    assert row["active"] == 1 and row["done_at"] is None  # still registered, not deleted, not marked done
    with db.session(s.db_path) as conn:
        docs = [r[0] for r in conn.execute("SELECT filename FROM documents WHERE project = '310002'")]
        hits = conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'Rettungsweg' AND rowid IN "
                            "(SELECT id FROM chunks WHERE project = '310002')").fetchone()[0]
    assert docs == ["Notiz.txt"] and hits == 1
    search = c.get("/suche", params={"q": "Rettungsweg 310002"})
    assert search.status_code == 200
    # the list: not in the queue, "Ordner ausgeschaltet" in "Alle"; pinning is refused
    data = c.get("/api/reihenfolge").json()
    assert data["switched_off"] == 2 and all(p["number"][0] != "3" for p in data["projects"])
    page = c.get("/status").text
    assert "2 Projekte in ausgeschalteten Ordnern werden nicht gelesen" in page
    alle = c.get("/api/reihenfolge?tab=alle").json()["projects"]
    assert {p["number"]: p["state"] for p in alle}["310002"] == "off"
    assert "Ordner ausgeschaltet" in c.get("/status?tab=alle").text
    r = c.post("/status/reihenfolge/vorziehen", data={"nummer": "310002"}, follow_redirects=False)
    assert r.status_code == 409 and "ausgeschalteten Ordner 3" in r.text
    assert c.post("/api/reihenfolge/vorziehen", json={"number": "310002"}).status_code == 400
    assert projects_row(s, "310002")["pinned"] is None
    # a switched-off folder cannot be moved above the folders switched on; switched on it is read again
    r = c.post("/status/reihenfolge/ordner", data={"ordner": "3", "aktion": "hoch"}, follow_redirects=False)
    assert r.status_code == 409 and "nicht weiter verschieben" in r.text
    c.post("/status/reihenfolge/ordner", data={"ordner": "3", "aktion": "an"})
    assert [f["name"] for f in c.get("/api/reihenfolge").json()["folders"]] == ["2", "1", "3"]
    recorded.clear()
    run_index(s)
    assert recorded[:2] == ["310002", "310001"]  # not done: first now
    with db.session(s.db_path) as conn:
        assert sorted(r[0] for r in conn.execute("SELECT filename FROM documents WHERE project = '310002'")) == [
            "Neu.txt", "Notiz.txt"]


def test_a_folder_switched_off_during_the_pass_stops_its_project(tmp_path, monkeypatch):
    make_share(tmp_path / "projekte", {"1": ["110001"], "3": ["310001", "310002"]})
    project = next((tmp_path / "projekte" / "3").glob("310002_*"))
    for k in range(6):
        (project / f"Datei{k}.txt").write_text(f"Vermerk {k} Rauchabzug", encoding="utf-8")
    s = settings(tmp_path)
    run_index(s)
    for k in range(6, 12):  # new files: read in the next pass – until the folder is switched off
        (project / f"Datei{k}.txt").write_text(f"Vermerk {k} Rauchabzug", encoding="utf-8")
    with db.session(s.db_path) as conn:
        conn.execute("UPDATE projects SET done_at = NULL")
    monkeypatch.setattr(reihenfolge, "OFF_CHECK_SECONDS", 0)
    read: list[str] = []
    real = documents.prepare_document

    def prepare(**kw):
        read.append(kw["rel_path"])
        if len(read) == 2:  # someone switches folder 3 off while 310002 is read
            with db.session(s.db_path) as other:
                reihenfolge.change_folder(other, s.projects_dir, (), "3", "aus")
        return real(**kw)

    monkeypatch.setattr(documents, "prepare_document", prepare)
    seen: list[str] = []
    spy_real = documents.index_project_documents
    monkeypatch.setattr(documents, "index_project_documents",
                        lambda conn, p, *a, **kw: (seen.append(p["number"]), spy_real(conn, p, *a, **kw))[1])
    run_index(s)
    assert seen == ["310002", "110001"]  # 310001 not started any more
    assert len(read) == 2  # stopped after the file it was reading
    row = projects_row(s, "310002")
    assert row["done_at"] is None and row["active"] == 1
    with db.session(s.db_path) as conn:  # nothing removed: the files not gone through this time stay
        assert conn.execute("SELECT COUNT(*) FROM documents WHERE project = '310002'").fetchone()[0] == 9


def test_sites_with_their_orders_follow_the_folder_of_the_site_when_switched_off(tmp_path, recorded):
    root = tmp_path / "projekte"
    for folder, site, orders in (("2", "2842_Wohnanlage", ["284201_Neubau Kita"]),
                                 ("1", "1015_Rathaus", ["101500_Sanierung Rathaus"])):
        for o in orders:
            (root / folder / site / o).mkdir(parents=True)
            (root / folder / site / o / "Notiz.txt").write_text("Rettungsweg", encoding="utf-8")
        (root / folder / site / "Lageplan.txt").write_text("Lageplan Feuerwehrzufahrt", encoding="utf-8")
    s = settings(tmp_path, PROJECT_FOLDER_DEPTH="3", PROJECT_PARENT_DIGITS="4")
    db.init_db(s.db_path)
    with db.session(s.db_path) as conn:
        sync_from_folder(conn, s.projects_dir, PATTERN, max_depth=3, parent_digits=4)
        reihenfolge.change_folder(conn, s.projects_dir, (), "2", "aus")
    run_index(s)
    assert recorded == ["101500", "1015"]  # the site 2842 and its order are both off
    assert projects_row(s, "284201")["active"] == 1 and projects_row(s, "2842")["active"] == 1


def test_unknown_folder_and_action_are_refused(tmp_path):
    make_share(tmp_path / "projekte")
    s = settings(tmp_path)
    run_index(s)
    c = client_for(s)
    assert c.post("/status/reihenfolge/ordner", data={"ordner": "9", "aktion": "hoch"}).status_code == 409
    assert c.post("/status/reihenfolge/ordner", data={"ordner": "1", "aktion": "weg"}).status_code == 409
    r = c.post("/status/reihenfolge/ordner", data={"ordner": "9", "aktion": "hoch"}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and 'class="note warn"' in r.text and "enthält keine Projekte" in r.text


def test_pending_projects_first_then_the_done_ones_are_checked_again(tmp_path, recorded):
    make_share(tmp_path / "projekte")
    s = settings(tmp_path)
    run_index(s)
    with db.session(s.db_path) as conn:  # a pass interrupted after the folder 3: the rest is still waiting
        conn.execute("UPDATE projects SET done_at = NULL WHERE number LIKE '1%' OR number LIKE '2%'")
    recorded.clear()
    run_index(s)
    assert recorded == ["210005", "210003", "210001", "110002", "110001", "310002", "310001"]


def test_sites_stay_together_with_their_orders():
    def e(number, kind="projekt", parent=None, folder="2", done=False, pinned=None):
        return {"number": number, "kind": kind, "parent": parent, "folder": folder, "done": done, "pinned": pinned,
                "group": parent or number}
    items = [e("2842", "bauvorhaben"), e("284201", parent="2842"), e("284205", parent="2842"),
             e("2901", "bauvorhaben"), e("290100", parent="2901"), e("285000"), e("1015", "bauvorhaben", folder="1"),
             e("101500", parent="1015", folder="1")]
    order = reihenfolge.ordered(items, [{"name": "2", "enabled": True}, {"name": "1", "enabled": True}])
    assert [x["number"] for x in order] == ["290100", "2901", "285000", "284205", "284201", "2842", "101500", "1015"]


def test_orders_of_a_site_count_in_the_sites_folder(tmp_path, recorded):
    root = tmp_path / "projekte"
    for folder, site, orders in (("2", "2842_Wohnanlage", ["284201_Neubau Kita", "284205_Sporthalle"]),
                                 ("1", "1015_Rathaus", ["101500_Sanierung Rathaus"])):
        for o in orders:
            (root / folder / site / o).mkdir(parents=True)
            (root / folder / site / o / "Notiz.txt").write_text("Rettungsweg", encoding="utf-8")
        (root / folder / site / "Lageplan.txt").write_text("Lageplan Feuerwehrzufahrt", encoding="utf-8")
    s = settings(tmp_path, PROJECT_FOLDER_DEPTH="3", PROJECT_PARENT_DIGITS="4", INDEX_FOLDER_ORDER="1,2")
    run_index(s)
    assert recorded == ["101500", "1015", "284205", "284201", "2842"]
    data = client_for(s).get("/api/reihenfolge?tab=alle").json()
    assert {p["number"]: p["folder"] for p in data["projects"]}["284201"] == "2"


# -- pinning -----------------------------------------------------------------------------------------------
def test_pinned_projects_come_first_in_pin_order_and_are_unpinned_when_done(tmp_path, recorded):
    make_share(tmp_path / "projekte")
    s = settings(tmp_path)
    run_index(s)
    with db.session(s.db_path) as conn:
        conn.execute("UPDATE projects SET done_at = NULL")
    c = client_for(s)
    assert c.post("/status/reihenfolge/vorziehen", data={"nummer": "110001"}, follow_redirects=False).status_code == 303
    c.post("/status/reihenfolge/vorziehen", data={"nummer": "210001"})
    c.post("/status/reihenfolge/vorziehen", data={"nummer": "110001"})  # twice: keeps its place
    queue = c.get("/api/reihenfolge").json()["projects"]
    assert [(p["number"], p["state"], p["position"]) for p in queue[:3]] == [
        ("110001", "pin", 1), ("210001", "pin", 2), ("310002", "wait", 3)]
    page = c.get("/status").text
    assert "vorgezogen" in page and "zurück" in page
    recorded.clear()
    run_index(s)
    assert recorded[:3] == ["110001", "210001", "310002"]
    assert projects_row(s, "110001")["pinned"] is None and projects_row(s, "210001")["pinned"] is None


def test_unpin_puts_a_project_back(tmp_path):
    make_share(tmp_path / "projekte")
    s = settings(tmp_path)
    run_index(s)
    with db.session(s.db_path) as conn:
        conn.execute("UPDATE projects SET done_at = NULL")
    c = client_for(s)
    c.post("/status/reihenfolge/vorziehen", data={"nummer": "110001"})
    r = c.post("/status/reihenfolge/zurueck", data={"nummer": "110001", "tab": "alle", "q": "11"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/status?tab=alle&q=11#reihenfolge"
    assert projects_row(s, "110001")["pinned"] is None
    assert c.post("/status/reihenfolge/vorziehen", data={"nummer": "999999"}).status_code == 409


def test_order_changed_during_the_pass_applies_to_the_projects_not_started(tmp_path, monkeypatch):
    make_share(tmp_path / "projekte")
    s = settings(tmp_path)
    db.init_db(s.db_path)
    with db.session(s.db_path) as conn:
        sync_from_folder(conn, s.projects_dir, PATTERN)
    seen: list[str] = []
    real = documents.index_project_documents

    def spy(conn, project, *a, **kw):
        seen.append(project["number"])
        if len(seen) == 1:  # someone pins a project on the status page while the first one is read
            with db.session(s.db_path) as other:
                reihenfolge.pin(other, "110001")
        return real(conn, project, *a, **kw)

    monkeypatch.setattr(documents, "index_project_documents", spy)
    with db.session(s.db_path) as conn:
        index_all_documents(conn, projects_dir=s.projects_dir)
    assert seen[:2] == ["310002", "110001"] and len(seen) == 7


# -- done state ------------------------------------------------------------------------------------------
def test_done_state_with_timestamp_and_skipped_files(tmp_path, monkeypatch):
    make_share(tmp_path / "projekte")
    next((tmp_path / "projekte" / "2").glob("210005_*")).joinpath("Riesig.txt").write_text("x" * 5000, encoding="utf-8")
    monkeypatch.setattr(documents, "MAX_FILE_BYTES", 1000)
    s = settings(tmp_path)
    c = client_for(s)
    data = c.get("/api/reihenfolge").json()
    assert data["queue"] == 0 and data["projects"] == []
    run_index(s)
    row = projects_row(s, "210005")
    assert row["done_at"] and row["done_skipped"] == 1 and row["done_profile"] == extraction_profile(None)
    assert projects_row(s, "310001")["done_skipped"] == 0
    data = c.get("/api/reihenfolge?tab=fertig").json()
    assert data["done"] == 7 and data["queue"] == 0
    states = {p["number"]: (p["state"], p["skipped"]) for p in data["projects"]}
    assert states["210005"] == ("done", 1) and states["310001"] == ("done", 0)
    page = c.get("/status?tab=fertig").text
    assert "fertig, 1 übersprungen" in page and "✓ fertig" in page
    assert "2 von 2 Projekten fertig" in page and "3 von 3 Projekten fertig" in page
    assert "Die Warteschlange ist leer" in c.get("/status").text


def test_a_changed_file_puts_the_project_back_into_the_queue_until_it_is_read(tmp_path, monkeypatch):
    make_share(tmp_path / "projekte")
    s = settings(tmp_path)
    run_index(s)
    first = projects_row(s, "210003")["done_at"]
    note = tmp_path / "projekte" / "2" / "210003_Sanierung Rathaus" / "Notiz.txt"
    note.write_text("Neue Auflage: Rauchabzug im Treppenraum.", encoding="utf-8")
    os.utime(note, (time.time() + 5, time.time() + 5))
    real = documents.prepare_document

    def crash(**kw):  # the app stops while the changed file is read
        raise RuntimeError("Leseprozess beendet")

    monkeypatch.setattr(documents, "prepare_document", crash)
    with pytest.raises(RuntimeError):
        with db.session(s.db_path) as conn:
            index_all_documents(conn, projects_dir=s.projects_dir)
    assert projects_row(s, "210003")["done_at"] is None  # back in the queue
    assert projects_row(s, "210001")["done_at"] == first or projects_row(s, "210001")["done_at"]
    c = client_for(s)
    assert [p["number"] for p in c.get("/api/reihenfolge").json()["projects"]] == ["210003"]
    monkeypatch.setattr(documents, "prepare_document", real)
    seen: list[str] = []
    spy_real = documents.index_project_documents
    monkeypatch.setattr(documents, "index_project_documents",
                        lambda conn, p, *a, **kw: (seen.append(p["number"]), spy_real(conn, p, *a, **kw))[1])
    run_index(s)
    assert seen[0] == "210003"  # taken first after the restart
    assert projects_row(s, "210003")["done_at"] is not None
    assert c.get("/api/reihenfolge").json()["queue"] == 0


def test_another_extraction_profile_means_not_done(tmp_path):
    make_share(tmp_path / "projekte")
    s = settings(tmp_path)
    run_index(s)
    with db.session(s.db_path) as conn:
        v = reihenfolge.view(conn, s.projects_dir, extraction_profile(None))
        assert len(v.queue) == 0
        v = reihenfolge.view(conn, s.projects_dir, "v99:vision:anderes-modell")
        assert len(v.queue) == 7  # LLM_VISION switched on, another model: every project is read again


def test_unreachable_project_is_not_marked_done(tmp_path):
    make_share(tmp_path / "projekte")
    s = settings(tmp_path)
    db.init_db(s.db_path)
    with db.session(s.db_path) as conn:
        sync_from_folder(conn, s.projects_dir, PATTERN)
        conn.execute("UPDATE projects SET path = path || '-weg' WHERE number = '110001'")
        index_all_documents(conn, projects_dir=s.projects_dir)
    assert projects_row(s, "110001")["done_at"] is None and projects_row(s, "110002")["done_at"] is not None


# -- the page: list, tabs, search, the card, forms without JavaScript ---------------------------------------
def test_tabs_search_and_row_limit(tmp_path):
    share = {"1": [f"1{i:05d}" for i in range(60)], "2": [f"2{i:05d}" for i in range(15)]}
    make_share(tmp_path / "projekte", share)
    s = settings(tmp_path)
    db.init_db(s.db_path)
    with db.session(s.db_path) as conn:
        sync_from_folder(conn, s.projects_dir, PATTERN)
        conn.execute("UPDATE projects SET done_at = '2026-11-10T08:15:00+00:00', done_profile = ? "
                     "WHERE number LIKE '2%'", (extraction_profile(None),))
    c = client_for(s)
    page = c.get("/status").text
    assert "50 von 60 angezeigt" in page and 'aria-current="page">Warteschlange' in page
    assert page.count('action="/status/reihenfolge/vorziehen"') == 50
    done = c.get("/status?tab=fertig").text
    assert "✓ fertig" in done and "10.11.2026" in done and "angezeigt" not in done
    assert done.count("<tr") == 16  # header + 15
    # search: part of the number, across all projects (the search form searches the tab "Alle")
    found = c.get("/status?tab=alle&q=00012").text
    assert "<mark>00012</mark>" in found and found.count("<tr") == 3  # 100012, 200012
    found = c.get("/status?tab=alle&q=10005").text
    assert found.count("<tr") == 11  # 100050 … 100059
    assert "Keine Projektnummer mit „777“" in c.get("/status?tab=alle&q=777").text
    # HTMX: only the list, with the tab of the search form out of band
    part = c.get("/status/reihenfolge/liste?tab=fertig", headers={"HX-Request": "true"})
    assert part.text.lstrip().startswith("<input") and 'id="rf-liste"' in part.text and "<html" not in part.text
    assert 'id="rf-tab" value="fertig" hx-swap-oob="true"' in part.text
    # without JavaScript the list URL leads to the status page
    r = c.get("/status/reihenfolge/liste?tab=fertig&q=2", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/status?tab=fertig&q=2#reihenfolge"
    api = c.get("/api/reihenfolge?tab=alle&q=10005&limit=5").json()
    assert api["total"] == 10 and api["count"] == 5


def test_forms_work_without_javascript_and_with_htmx(tmp_path):
    make_share(tmp_path / "projekte")
    s = settings(tmp_path)
    db.init_db(s.db_path)
    with db.session(s.db_path) as conn:
        sync_from_folder(conn, s.projects_dir, PATTERN)
    c = client_for(s)
    page = c.get("/status").text
    for form in ('action="/status/reihenfolge/ordner"', 'action="/status/reihenfolge/vorziehen"',
                 'action="/status#reihenfolge" method="get"'):
        assert form in page
    assert 'name="aktion" value="hoch" disabled' in page  # the first folder cannot move up
    r = c.post("/status/reihenfolge/vorziehen", data={"nummer": "110002"}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and r.text.lstrip().startswith('<div id="reihenfolge"')
    assert "110002 vorgezogen – kommt als Nächstes dran." in r.text and "<html" not in r.text
    r = c.post("/status/reihenfolge/ordner", data={"ordner": "1", "aktion": "hoch"}, headers={"HX-Request": "true"})
    assert "Ordner 1 ist jetzt an Stelle 2." in r.text


def test_card_now_shows_the_project_files_phase_and_the_next_three(tmp_path):
    make_share(tmp_path / "projekte")
    s = settings(tmp_path)
    db.init_db(s.db_path)
    with db.session(s.db_path) as conn:
        sync_from_folder(conn, s.projects_dir, PATTERN)
        plan = reihenfolge.Plan(conn, s.projects_dir, (), extraction_profile(None))
        pending = plan.order([dict(r) for r in conn.execute("SELECT number, name, path, kind FROM projects")])
    c = client_for(s)
    idle = c.get("/status/reihenfolge/jetzt").text
    assert 'hx-trigger="every 30s"' in idle and "Als Nächstes" in idle and "310002" in idle
    live = reihenfolge.LIVE
    live.begin()
    live.set_phase("Dokumente")
    live.start(pending[0], "3", pending, 1, plan.version)
    live.files("310002", 38, 120)
    part = c.get("/status/reihenfolge/jetzt").text
    assert 'hx-trigger="every 4s"' in part and 'class="rf-now-body"' in part and "rf-spin" not in part
    assert "310002" in part and "Ordner 3" in part and "38 von 120 Dateien" in part and ">Dokumente<" in part
    assert 'max="120" value="38"' in part
    assert "Danach: " in part and part.index("310001") < part.index("210005") < part.index("210003")
    page = c.get("/status").text
    assert "wird gelesen" in page
    # pinned meanwhile: "Danach" shows it at once (the pass sorts again before its next project)
    c.post("/status/reihenfolge/vorziehen", data={"nummer": "110001"})
    part = c.get("/status/reihenfolge/jetzt").text
    assert part.index("110001") < part.index("310001")
    api = c.get("/api/reihenfolge").json()["current"]
    assert api["project"]["number"] == "310002" and api["files_done"] == 38 and api["next"][0] == "110001"
    live.set_phase("Bildauswertung – 3 Seite(n) gelesen")
    live.vision_project("210001", "2")
    part = c.get("/status/reihenfolge/jetzt").text
    assert ">Bildauswertung<" in part and "210001" in part and "3 Seite(n) gelesen" in part


def test_live_state_ends_with_the_pass(tmp_path):
    make_share(tmp_path / "projekte")
    run_index(settings(tmp_path))
    snap = reihenfolge.LIVE.snapshot()
    assert snap["running"] is False and snap["project"] is None and snap["pending"] == []


# -- access control ----------------------------------------------------------------------------------------
PW = "Brandschutz-2025!"


def test_changes_need_a_login_and_are_refused_from_other_sites(tmp_path):
    make_share(tmp_path / "projekte")
    s = settings(tmp_path, AUTH_DISABLED="", AUTH_USERS="anna:" + hash_password(PW, iterations=1000),
                 SECRET_KEY=secrets.token_urlsafe(48))
    db.init_db(s.db_path)
    with db.session(s.db_path) as conn:
        sync_from_folder(conn, s.projects_dir, PATTERN)
    anonymous = client_for(s)
    posts = (("/status/reihenfolge/ordner", {"data": {"ordner": "1", "aktion": "hoch"}}),
             ("/status/reihenfolge/standard", {"data": {}}),
             ("/status/reihenfolge/vorziehen", {"data": {"nummer": "110001"}}),
             ("/status/reihenfolge/zurueck", {"data": {"nummer": "110001"}}),
             ("/api/reihenfolge/vorziehen", {"json": {"number": "110001"}}),
             ("/api/reihenfolge/ordner", {"json": {"order": ["1"]}}))
    for path, kw in posts:
        r = anonymous.post(path, follow_redirects=False, **kw)
        assert r.status_code in (303, 401), path
        assert r.status_code == 401 or r.headers["location"].startswith("/login"), path
    for path in ("/api/reihenfolge", "/status/reihenfolge/jetzt"):
        assert anonymous.get(path, follow_redirects=False).status_code in (303, 401)
    assert projects_row(s, "110001")["pinned"] is None
    user = client_for(s)
    user.post("/login", data={"username": "anna", "password": PW}, follow_redirects=False)
    for path, kw in posts:
        r = user.post(path, headers={"Sec-Fetch-Site": "cross-site"}, follow_redirects=False, **kw)
        assert r.status_code == 403, path
    assert projects_row(s, "110001")["pinned"] is None
    assert user.post("/status/reihenfolge/vorziehen", data={"nummer": "110001"},
                     follow_redirects=False).status_code == 303
    assert projects_row(s, "110001")["pinned"] == 1


# -- API ---------------------------------------------------------------------------------------------------
def test_api_order_and_pinning(tmp_path):
    make_share(tmp_path / "projekte")
    s = settings(tmp_path)
    db.init_db(s.db_path)
    with db.session(s.db_path) as conn:
        sync_from_folder(conn, s.projects_dir, PATTERN)
    c = client_for(s)
    data = c.post("/api/reihenfolge/ordner", json={"order": ["1"], "off": ["3"]}).json()
    assert [(f["name"], f["enabled"]) for f in data["folders"]] == [("1", True), ("2", True), ("3", False)]
    assert [p["number"] for p in data["projects"]][:2] == ["110002", "110001"]
    assert c.post("/api/reihenfolge/ordner", json={"order": ["7"]}).status_code == 400
    assert c.post("/api/reihenfolge/vorziehen", json={"number": "310001"}).status_code == 400  # folder 3 is off
    data = c.post("/api/reihenfolge/vorziehen", json={"number": "210001"}).json()
    assert data["projects"][0]["number"] == "210001" and data["projects"][0]["state"] == "pin"
    data = c.post("/api/reihenfolge/zurueck", json={"number": "210001"}).json()
    assert data["projects"][-1]["number"] == "210001"
    assert c.post("/api/reihenfolge/vorziehen", json={"number": "000000"}).status_code == 400
    data = c.post("/api/reihenfolge/ordner", json={"reset": True}).json()
    assert data["customized"] is False and [f["name"] for f in data["folders"]] == ["3", "2", "1"]
    assert set(data) >= {"folders", "current", "queue", "done", "projects", "total"}


# -- many projects ----------------------------------------------------------------------------------------
def test_status_page_stays_fast_with_15000_projects(tmp_path, monkeypatch):
    s = settings(tmp_path)
    db.init_db(s.db_path)
    root = str(s.projects_dir)
    profile = extraction_profile(None)
    rows = []
    for i in range(15000):
        folder = str(i % 6 + 1)
        number = f"{folder}{i:05d}"
        rows.append((number, NAMES[i % 5], f"{root}/{folder}/{number}_{NAMES[i % 5]}",
                     "2026-11-10T08:00:00+00:00" if i % 3 == 0 else None, profile if i % 3 == 0 else None))
    with db.session(s.db_path) as conn:
        conn.executemany("INSERT INTO projects(number, name, path, done_at, done_profile) VALUES(?, ?, ?, ?, ?)", rows)
        conn.executemany("INSERT INTO documents(path, project, rel_path, filename, ext) VALUES(?, ?, 'a.txt', 'a.txt', "
                         "'.txt')", [(f"{r[2]}/a{k}.txt", r[0]) for r in rows[:3000] for k in range(3)])
    statements: list[str] = []
    real_connect = db.connect

    def counting(path):
        conn = real_connect(path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(db, "connect", counting)
    c = client_for(s)
    c.get("/status")  # warm-up (templates)
    statements.clear()
    started = time.perf_counter()
    page = c.get("/status")
    took = time.perf_counter() - started
    assert page.status_code == 200 and "50 von 10.000 angezeigt" in page.text
    assert "2.500 von 2.500 Projekten fertig" in page.text and "0 von 2.500 Projekten fertig" in page.text
    assert len(statements) < 60, len(statements)  # no query per project
    assert took < 5, took
    statements.clear()
    started = time.perf_counter()
    part = c.get("/status/reihenfolge/liste?tab=alle&q=4012", headers={"HX-Request": "true"})
    assert part.status_code == 200 and "<mark>4012</mark>" in part.text
    assert c.get("/status/reihenfolge/jetzt").status_code == 200
    assert time.perf_counter() - started < 5 and len(statements) < 30


# -- Bildauswertung ---------------------------------------------------------------------------------------
def test_vision_follows_pins_and_folder_order_newest_document_first_inside(tmp_path, monkeypatch):
    root = tmp_path / "projekte"
    files = {("1", "110001_Neubau Kita"): ["a.png"], ("3", "310001_Sporthalle"): ["b.png", "c.png"],
             ("2", "210001_Rathaus"): ["d.png"]}
    t = time.time() - 1000
    for (folder, name), pngs in files.items():
        for k, f in enumerate(pngs):
            p = root / folder / name / f
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(png(f))
            t += 10
            os.utime(p, (t, t))
    s = settings(tmp_path, INDEX_FOLDER_ORDER="3,2")
    db.init_db(s.db_path)
    conn = db.connect(s.db_path)
    sync_from_folder(conn, root, PATTERN)
    conn.commit()
    llm = Vision()
    with documents.vision_later():
        index_all_documents(conn, llm, projects_dir=root, folder_order=s.index_folder_order)
    assert vision.pending_counts(conn)["documents"] == 4
    read: list[str] = []
    real = vision.VisionPass._read

    def spy(self, row):
        read.append(row["rel_path"])
        return real(self, row)

    monkeypatch.setattr(vision.VisionPass, "_read", spy)
    reihenfolge.pin(conn, "110001")
    conn.commit()
    order = reihenfolge.VisionOrder(conn, root, s.index_folder_order)
    vision.VisionPass(conn, llm, concurrency=1, projects_dir=root, order=order).run()
    # pinned project first, then folder 3 (its newer file first), then folder 2
    assert read == ["a.png", "c.png", "b.png", "d.png"]
    conn.close()


def test_run_vision_uses_the_order(tmp_path, monkeypatch):
    got = {}

    class Pass:
        def __init__(self, conn, llm, **kw):
            got.update(kw)

        def run(self):
            return {}

    monkeypatch.setattr(vision, "VisionPass", Pass)
    s = settings(tmp_path, INDEX_FOLDER_ORDER="3")
    vision.run_vision(None, Vision(), s)
    assert isinstance(got["order"], reihenfolge.VisionOrder) and got["order"].default == ("3",)
