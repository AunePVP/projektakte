"""Sites (Bauvorhaben) above the orders: PROJECT_PARENT_DIGITS groups the orders of a site as its
Unterprojekte.

Built after the office's share (UEBERGABE.md, 7a – all names here invented): a site 1015_Altstadt with
the orders 101500 … 101503, an intermediate folder 1015_PDF_100303 holding the order 101504, a folder of
plans 1015-Plaene-Aufzug and a note directly in the site; another site with one order; an order without
a site; a copied site (1073_Turnhalle/1071_Schule) and a date folder of another site's order inside a
site (1827_Werk/Fotos/230512_Begehung). Order 101500 is the number of the site (1015) plus "00" – an
order, never the site itself."""
from __future__ import annotations

import importlib.util
import re
import sqlite3
from email.message import EmailMessage
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from projektakte import db, documents, registry
from projektakte.ai.services import AIServices
from projektakte.ask import ask
from projektakte.assign import certain_project, decide, known_projects
from projektakte.config import load_settings
from projektakte.documents import index_all_documents, index_project_documents
from projektakte.indexer import run_index
from projektakte.main import create_app
from projektakte.registry import ProjectFolder, bauvorhaben, get_project, scan_projects, sync_from_folder
from projektakte.search import hybrid_search
from projektakte.web import _project_rows

from .conftest import fake_settings

PATTERN = r"\d{6}"
SITE = "1/10/1015_Altstadt"
ORDERS = {
    "101500": f"{SITE}/101500_Gesamtkonzept",
    "101501": f"{SITE}/101501_Umbau Station",
    "101502": f"{SITE}/101502_Konzept Anbau",
    "101503": f"{SITE}/101503_Begleitung",
    "101504": f"{SITE}/1015_PDF_100303/101504_Vervielfaeltigung",
}
SITE_FILES = {"1015-Plaene-Aufzug/Plan Aufzug.txt", "Notiz.txt", "1015_PDF_100303/Uebersicht.txt"}


def write(root: Path, rel: str, text: str) -> Path:
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    return f


def office_share(root: Path) -> Path:
    write(root, f"{ORDERS['101500']}/101500-Text/Gesamtkonzept.txt",
          "Gesamtkonzept: das Krankenhaus gliedert sich in drei Brandabschnitte.")
    write(root, f"{ORDERS['101501']}/101501-Text/Station.txt",
          "Umbau der Station 3: die Tür zum Treppenraum wird als T30-RS ausgeführt.")
    write(root, f"{ORDERS['101502']}/101502-Text/Anbau.txt",
          "Konzept Anbau: der Anbau erhält eine Brandmeldeanlage der Kategorie 2 nach DIN 14675.")
    write(root, f"{ORDERS['101502']}/Rauchabzug.txt", "Rauchabzug im Treppenraum des Anbaus: 1 m² geometrisch.")
    write(root, f"{ORDERS['101503']}/101503-Mail/Protokoll.txt", "Begleitung: Baustellenbegehung ohne Mängel.")
    write(root, f"{ORDERS['101504']}/101504-Text/Kopien.txt", "Vervielfältigung der Pläne für die Feuerwehr.")
    write(root, f"{SITE}/1015-Plaene-Aufzug/Plan Aufzug.txt", "Plan des Aufzugs mit Vorraum und Druckbelüftung.")
    write(root, f"{SITE}/Notiz.txt", "Notiz zum Bauvorhaben: Ansprechpartner Hausmeister.")
    write(root, f"{SITE}/1015_PDF_100303/Uebersicht.txt", "Übersicht der PDF-Ablage.")
    write(root, "2/28/2842_Filiale/284201_Zwischendecke/284201-Text/Bericht.txt",
          "Zwischendecke über dem Verkaufsraum in EI 30.")
    write(root, "9/99/990100_Einzelauftrag/Gutachten.txt", "Einzelauftrag ohne Bauvorhaben-Ordner.")
    # a copied site: 1071_Schule inside 1073_Turnhalle is a further folder of site 1071, not part of 1073
    write(root, "1/10/1071_Schule/107100_Konzept/Konzept.txt", "Schule: Sicherheitstreppenraum.")
    write(root, "1/10/1071_Schule/Lageplan.txt", "Lageplan der Schule.")
    write(root, "1/10/1073_Turnhalle/1071_Schule/107100_Konzept/Konzept.txt", "Schule: Sicherheitstreppenraum.")
    write(root, "1/10/1073_Turnhalle/1071_Schule/Kopie Lageplan.txt", "Kopie des Lageplans der Schule.")
    write(root, "1/10/1073_Turnhalle/107300_Halle/Halle.txt", "Turnhalle: Versammlungsstätte.")
    write(root, "1/10/1073_Turnhalle/Hallenplan.txt", "Hallenplan.")
    # a date folder inside site 1827 with the number of an order of site 2305: not read with that order,
    # so its files lie in site 1827 outside every project
    write(root, "1/18/1827_Werk/182701_Werkhalle/Werkhalle.txt", "Werkhalle: Sprinkleranlage.")
    write(root, "1/18/1827_Werk/Fotos/230512_Begehung/Foto.txt", "Fotoliste der Begehung im Werk.")
    write(root, "2/23/2305_Bau/230512_Konzept/Konzept.txt", "Konzept Bau 2305.")
    return root


@pytest.fixture()
def share(tmp_path):
    return office_share(tmp_path / "projekt")


@pytest.fixture()
def register(tmp_path, share, monkeypatch):
    """The share registered and read with sites (depth 6, no AI); ``extracted`` lists every file
    extracted after the first pass."""
    dbp = tmp_path / "x.sqlite3"
    db.init_db(dbp)
    conn = db.connect(dbp)
    extracted: list[str] = []
    real_extract = documents.extract
    monkeypatch.setattr(documents, "extract", lambda *a, **k: extracted.append(a[0]) or real_extract(*a, **k))
    state = {"conn": conn, "root": share, "extracted": extracted}
    state["first"] = run(state)
    extracted.clear()
    yield state
    conn.close()


def run(state: dict, digits: int = 4, depth: int = 6) -> dict:
    conn = state["conn"]
    stats = sync_from_folder(conn, state["root"], PATTERN, max_depth=depth, parent_digits=digits)
    conn.commit()
    if "warning" not in stats:
        stats["documents"] = index_all_documents(conn, projects_dir=state["root"])
    return stats


def docs(conn, project: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT rel_path FROM documents WHERE project = ? AND mail_id IS NULL",
                                       (project,))}


def row(conn, number: str) -> dict:
    return dict(conn.execute("SELECT * FROM projects WHERE number = ?", (number,)).fetchone())


# ---------------------------------------------------------------------------------------- detection
def test_orders_of_a_site_are_its_unterprojekte(share):
    found = scan_projects(share, PATTERN, 6)
    grouped = {p.number: p for p in bauvorhaben(found, share, 4)}
    site = grouped["1015"]
    assert site.kind == "bauvorhaben" and site.name == "Altstadt" and site.path == share / SITE and not site.further
    # order 101500 is an order of site 1015 – the two numbers never clash
    for number in ORDERS:
        assert grouped[number].parent == "1015" and grouped[number].kind == "projekt"
    assert grouped["101504"].path == share / ORDERS["101504"]  # the intermediate folder is no site
    assert grouped["284201"].parent == "2842" and grouped["2842"].name == "Filiale"
    assert grouped["990100"].parent is None  # group folders 9/99 have another number of digits
    assert "99" not in grouped and "9" not in grouped
    # the copied site is a further folder of site 1071, site 1073 is its own folder
    assert grouped["1071"].path == share / "1/10/1071_Schule"
    assert grouped["1071"].further == (share / "1/10/1073_Turnhalle/1071_Schule",)
    assert grouped["1073"].path == share / "1/10/1073_Turnhalle" and grouped["107300"].parent == "1073"
    # the order of site 2305 keeps its own site; the stranger in 1827 is no folder of it
    assert grouped["230512"].parent == "2305" and grouped["182701"].parent == "1827"
    # scan_projects itself is unchanged (the structure check and the filing check count its orders)
    assert all(p.kind == "projekt" and p.parent is None for p in found)


def test_detection_rules(tmp_path):
    root = tmp_path / "r"
    for rel in ("2024_Auftraege/202401_Kita", "1015/101500_Bare", "1015_X/1015_Y/101599_Tief", "4711_Z/1234_Kopie"):
        (root / rel).mkdir(parents=True)
    found = [ProjectFolder("202401", "Kita", root / "2024_Auftraege/202401_Kita"),
             ProjectFolder("101500", "Bare", root / "1015/101500_Bare"),
             ProjectFolder("101599", "Tief", root / "1015_X/1015_Y/101599_Tief"),
             ProjectFolder("ABC123", "Buchstaben", root / "4711_Z/1234_Kopie"),
             ProjectFolder("1015", "Selbst", root / "1015_X")]
    out = {p.number: p for p in bauvorhaben(found, root, 4)}
    # a site number that is itself a scanned project is left out (a pattern of several lengths)
    assert out["101500"].parent is None and out["101599"].parent is None and out["1015"].kind == "projekt"
    assert out["202401"].parent == "2024"  # what PROJECT_PARENT_DIGITS is for: a person decides
    assert out["ABC123"].parent is None
    without = {p.number: p for p in bauvorhaben(found[:3], root, 4)}
    assert without["1015"].path == root / "1015" and without["1015"].name == "1015"  # bare number: no name
    assert without["1015"].further == (root / "1015_X",)  # outermost of 1015_X/1015_Y
    assert bauvorhaben(found, root, 0) == found
    assert all(p.parent is None for p in bauvorhaben(found, root, 6))  # as long as the numbers: nothing


# ---------------------------------------------------------------------------------------- register
def test_sites_are_registered_next_to_their_orders(register):
    conn, first = register["conn"], register["first"]
    assert first["bauvorhaben"] == 6 and first["projects"] == 11
    assert "merged" in first and first["merged"]["projects"] == 1  # 107100 only: a copied site is no merged order
    site, order = get_project(conn, "1015"), get_project(conn, "101500")
    assert (site["kind"], site["parent"], site["name"]) == ("bauvorhaben", None, "Altstadt")
    assert (order["kind"], order["parent"], order["name"]) == ("projekt", "1015", "Gesamtkonzept")
    assert db.project_scope(conn, "1015") == ["1015", *sorted(ORDERS)]
    assert db.project_scope(conn, "101501") == ["101501"]  # never the site, never a sibling
    assert db.project_scope(conn, "101500") == ["101500"]
    assert db.project_scope(conn, "990100") == ["990100"] and db.project_scope(conn, "unbekannt") == ["unbekannt"]
    assert registry.project_folders(conn, "1071") == [str(register["root"] / "1/10/1071_Schule"),
                                                      str(register["root"] / "1/10/1073_Turnhalle/1071_Schule")]
    assert registry.active_count(conn) == 11


def test_files_of_a_site_outside_its_orders_are_its_own(register):
    conn = register["conn"]
    assert docs(conn, "1015") == SITE_FILES
    assert docs(conn, "101504") == {"101504-Text/Kopien.txt"}
    assert docs(conn, "101500") == {"101500-Text/Gesamtkonzept.txt"}
    # a copied site: its loose files belong to site 1071, never to 1073, whose own files stay its own
    assert docs(conn, "1071") == {"Lageplan.txt", "1073_Turnhalle/1071_Schule/Kopie Lageplan.txt"}
    assert docs(conn, "1073") == {"Hallenplan.txt"}
    # a stranger (a date folder with another site's order number) is in no project: the site reads it
    assert docs(conn, "1827") == {"Fotos/230512_Begehung/Foto.txt"}
    assert docs(conn, "230512") == {"Konzept.txt"}
    # every file once
    paths = [r[0] for r in conn.execute("SELECT path FROM documents")]
    assert len(paths) == len(set(paths)) == len([p for p in register["root"].rglob("*") if p.is_file()])


def test_a_second_pass_reads_nothing_again(register):
    stats = run(register)
    assert (stats["documents"]["new"], stats["documents"]["changed"], stats["documents"]["removed"]) == (0, 0, 0)
    assert stats["added"] == stats["updated"] == 0 and not register["extracted"]


def test_switching_on_and_off_reads_no_order_file_again(register):
    conn = register["conn"]
    off = run(register, digits=0)
    assert off["bauvorhaben"] == 0 and off["deactivated"] == 6
    assert get_project(conn, "101500")["parent"] is None and not get_project(conn, "1015")["active"]
    assert db.project_scope(conn, "1015") == ["1015"]  # no order points at the site any more
    assert not register["extracted"]  # orders untouched, site files hidden, not read
    hidden = conn.execute("SELECT COUNT(*) FROM chunks_fts f JOIN chunks c ON c.id = f.rowid "
                          "WHERE c.project = '1015'").fetchone()[0]
    assert hidden == 0 and len(docs(conn, "1015")) == 3  # kept during the grace period
    on = run(register)
    assert on["reactivated"] == 6 and get_project(conn, "101500")["parent"] == "1015"
    assert not register["extracted"] and on["documents"]["new"] == 0


def test_a_site_whose_orders_are_gone_waits_for_the_grace_period(register, monkeypatch):
    import shutil

    root, conn = register["root"], register["conn"]
    shutil.rmtree(root / "2/28")
    stats = run(register)
    assert stats["deactivated"] == 2  # the order and its site
    assert not get_project(conn, "2842")["active"] and row(conn, "2842")["missing_since"]


def test_an_unreadable_site_folder_changes_nothing(register, monkeypatch):
    conn, root = register["conn"], register["root"]
    site = root / SITE
    real_iterdir = Path.iterdir

    def iterdir(self):
        if self == site:
            raise PermissionError(13, "Zugriff verweigert")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    stats = sync_from_folder(conn, root, PATTERN, max_depth=6, parent_digits=4)
    assert stats["deactivated"] == 0
    assert get_project(conn, "1015")["active"] and get_project(conn, "101501")["parent"] == "1015"


def test_a_site_never_takes_over_a_file_of_an_order(register):
    """Defensive: even without the exclusion a site's pass leaves the files of its orders alone."""
    conn = register["conn"]
    site = dict(get_project(conn, "1015"))
    index_project_documents(conn, {**site, "exclude": ()}, projects_dir=register["root"])
    assert docs(conn, "1015") == SITE_FILES and docs(conn, "101501") == {"101501-Text/Station.txt"}
    assert not register["extracted"]


def test_a_database_of_the_previous_version_gets_the_columns(tmp_path):
    dbp = tmp_path / "alt.sqlite3"
    conn = sqlite3.connect(dbp)
    for i, script in enumerate(db.MIGRATIONS[:8], start=1):
        conn.executescript("BEGIN;" + script + f"PRAGMA user_version = {i}; COMMIT;")
    conn.execute("INSERT INTO projects(number, name, path) VALUES('101500', 'Alt', '/x')")
    conn.commit()
    conn.close()
    db.init_db(dbp)
    with db.session(dbp) as conn:
        assert dict(conn.execute("SELECT parent, kind FROM projects").fetchone()) == {"parent": None, "kind": "projekt"}


# ---------------------------------------------------------------------------------------- settings
@pytest.mark.parametrize("env, digits, warned", [
    ({}, 0, False),
    ({"PROJECT_PARENT_DIGITS": "4", "PROJECT_NUMBER_PATTERN": r"\d{6}"}, 4, False),
    ({"PROJECT_PARENT_DIGITS": "4", "PROJECT_NUMBER_PATTERN": "auto"}, 4, False),
    ({"PROJECT_PARENT_DIGITS": "vier", "PROJECT_NUMBER_PATTERN": r"\d{6}"}, 0, True),
    ({"PROJECT_PARENT_DIGITS": "-1", "PROJECT_NUMBER_PATTERN": r"\d{6}"}, 0, True),
    ({"PROJECT_PARENT_DIGITS": "6", "PROJECT_NUMBER_PATTERN": r"\d{6}"}, 0, True),
    ({"PROJECT_PARENT_DIGITS": "4"}, 0, True),  # BS-2024-0001 has no all-digit number
    ({"PROJECT_PARENT_DIGITS": "0", "PROJECT_NUMBER_PATTERN": r"\d{6}"}, 0, False),
])
def test_setting(tmp_path, env, digits, warned):
    s = load_settings({"DATA_DIR": str(tmp_path), **env})
    assert s.project_parent_digits == digits
    assert any("PROJECT_PARENT_DIGITS" in w for w in s.config_warnings) == warned


# ---------------------------------------------------------------------------------------- mails
def test_no_number_in_a_mail_ever_names_a_site(register):
    conn = register["conn"]
    known = known_projects(conn)
    assert "1015" not in known and "101500" in known
    assert "1015" in known_projects(conn, sites=True)
    # even a pattern that also matches four digits never yields the site, nor calls it unknown
    rx = registry.number_regex(r"\d{4}(?:\d{2})?")
    sites = frozenset({"1015"})
    assert certain_project("Bauvorhaben 1015", "", [], rx, known, sites) == (None, None, "Keine Projektnummer in der Mail.")
    assert certain_project("1015 / 101501", "", [], rx, known, sites)[0] == "101501"


def _eml(path: Path, subject: str, body: str, sender: str = "Bauamt <bauamt@stadt.example>",
         date: str = "Tue, 18 Jun 2024 10:30:00 +0200") -> None:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = sender, "buero@ib.example", subject
    m["Date"] = date
    m["Message-ID"] = f"<{path.stem}@stadt.example>"
    m.set_content(body)
    path.write_bytes(bytes(m))


# ---------------------------------------------------------------------------------------- end to end
@pytest.fixture()
def office(tmp_path, share, fake_ai):
    mails = tmp_path / "mails"
    mails.mkdir()
    _eml(mails / "a.eml", "101501 Station 3", "Die Rauchschutztür im Umbau der Station 3 ist eingebaut.")
    _eml(mails / "b.eml", "Bauvorhaben 1015 Altstadt", "Allgemeine Frage zum Bauvorhaben.")
    _eml(mails / "c.eml", "Rückfrage 101500 Gesamtkonzept", "Wie viele Brandabschnitte hat das Gesamtkonzept?",
         date="Mon, 03 Mar 2025 09:00:00 +0100")
    s = fake_settings(fake_ai, tmp_path, PROJECTS_DIR=share, MAIL_DIR=mails, PROJECT_NUMBER_PATTERN=PATTERN,
                      PROJECT_FOLDER_DEPTH=6, PROJECT_PARENT_DIGITS=4)
    ai = AIServices.from_settings(s)
    stats = run_index(s, ai)
    assert "warning" not in stats["projects"] and stats["projects"]["bauvorhaben"] == 6
    return s, ai


def test_mails_are_assigned_to_orders_only(office):
    s, _ = office
    with db.session(s.db_path) as conn:
        got = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT subject, project, assign_status FROM mails")}
        assert got["101501 Station 3"] == ("101501", "assigned")
        assert got["Rückfrage 101500 Gesamtkonzept"] == ("101500", "assigned")
        assert got["Bauvorhaben 1015 Altstadt"][0] != "1015"  # never by number or content
        assert conn.execute("SELECT COUNT(*) FROM mails WHERE suggested_project = '1015'").fetchone()[0] == 0
        mail_id = conn.execute("SELECT id FROM mails WHERE subject = 'Bauvorhaben 1015 Altstadt'").fetchone()[0]
        # a person may assign it to the site
        assert decide(conn, mail_id, "assign", "1015", user="tester")["project"] == "1015"
    assert run_index(s, office[1])["assignment"]  # the decision holds on the next pass
    with db.session(s.db_path) as conn:
        assert conn.execute("SELECT project, assign_status FROM mails WHERE id = ?", (mail_id,)).fetchone()[:] == \
            ("1015", "confirmed")


def test_search_on_a_site_covers_its_unterprojekte(office):
    s, ai = office
    with db.session(s.db_path) as conn:
        hits = hybrid_search(conn, ai, str(s.db_path), "Brandmeldeanlage Anbau", project="1015").hits
        assert hits and hits[0].project == "101502" and hits[0].parent == "1015" and hits[0].parent_name == "Altstadt"
        own = hybrid_search(conn, ai, str(s.db_path), "Aufzug Vorraum Druckbelüftung", project="1015").hits
        assert own[0].project == "1015" and own[0].rel_path == "1015-Plaene-Aufzug/Plan Aufzug.txt"
        mails = hybrid_search(conn, ai, str(s.db_path), "Rauchschutztür eingebaut", project="1015").hits
        assert any(h.kind == "mail" and h.project == "101501" for h in mails)
        for query in ("Brandmeldeanlage Anbau", "Aufzug Vorraum", "Brandabschnitte Krankenhaus"):
            sibling = hybrid_search(conn, ai, str(s.db_path), query, project="101501").hits
            assert all(h.project == "101501" for h in sibling)  # never the site or a sibling
        # 101500 is the order, not the site
        assert all(h.project == "101500" for h in
                   hybrid_search(conn, ai, str(s.db_path), "Brandmeldeanlage Aufzug Station", project="101500").hits)


def test_a_question_on_the_site_is_answered_from_an_unterprojekt(office):
    s, ai = office
    with db.session(s.db_path) as conn:
        r = ask(conn, ai, str(s.db_path), "Welche Brandmeldeanlage erhält der Anbau?", project="1015")
        assert r.status == "answered" and "Kategorie 2" in r.answer
        assert {src["project"] for src in r.sources} == {"101502"}
        # the same question on a sibling order: its facts are out of reach
        other = ask(conn, ai, str(s.db_path), "Welche Brandmeldeanlage erhält der Anbau?", project="101501")
        assert other.status != "answered" and "Kategorie 2" not in other.answer
        assert all(c["project"] == "101501" for c in other.context)
        # the latest state of a site: the newest sources of its orders count, too
        news = ask(conn, ai, str(s.db_path), "Was gibt es Neues?", project="1015")
        assert {c["project"] for c in news.context} & set(ORDERS)
        assert {c["project"] for c in news.context} <= {"1015", *ORDERS}


def test_pages_and_api(office):
    s, _ = office
    client = TestClient(create_app(s, background=False))
    listing = client.get("/").text
    site_row = listing.index('href="/projekte/1015"')
    assert site_row < listing.index('href="/projekte/101500"') < listing.index('href="/projekte/101504"')
    assert 'class="sub"' in listing and "Bauvorhaben" in listing and "5 Unterprojekte" in listing
    # the filter for an order shows its site above it
    filtered = client.get("/", params={"q": "Anbau"}, headers={"HX-Request": "true"}).text
    assert 'href="/projekte/1015"' in filtered and 'href="/projekte/101502"' in filtered
    assert 'href="/projekte/101501"' not in filtered
    page = client.get("/projekte/1015").text
    assert "Unterprojekte" in page and "Frage zu diesem Bauvorhaben (mit allen Unterprojekten)" in page
    for number in ORDERS:
        assert f'href="/projekte/{number}"' in page
    assert "Plan Aufzug.txt" in page and "Station.txt" not in page  # own documents only
    order = client.get("/projekte/101501").text
    assert re.search(r'Bauvorhaben: <a href="/projekte/1015">1015 Altstadt</a>', order)
    assert "Unterprojekte" not in order
    data = client.get("/api/projects/1015").json()
    assert data["kind"] == "bauvorhaben" and data["parent"] is None
    assert [c["number"] for c in data["children"]] == sorted(ORDERS)
    child = client.get("/api/projects/101500").json()
    assert (child["kind"], child["parent"], child["parent_name"], child["children"]) == ("projekt", "1015", "Altstadt", [])
    listed = {p["number"]: p for p in client.get("/api/projects").json()["projects"]}
    assert listed["1015"]["children"] == sorted(ORDERS) and "children" not in listed["101500"]
    assert client.get("/api/projects/1015/documents").json()["count"] == 3
    st = client.get("/api/status").json()["counts"]
    assert (st["projects"], st["bauvorhaben"]) == (11, 6)
    found = client.get("/suche", params={"q": "Rauchabzug", "project": "1015"}).text
    assert 'href="/projekte/101502"' in found  # hits of Unterprojekte link their project
    found = client.get("/suche", params={"q": "Rauchabzug", "project": "101502"}).text
    assert 'href="/projekte/101502"' not in found
    named = client.get("/suche", params={"q": "1015 Aufzug"}).text
    assert "Zum Bauvorhaben 1015 Altstadt" in named
    options = client.get("/fragen").text
    assert "1015 Altstadt (Bauvorhaben, mit Unterprojekten)" in options


# ---------------------------------------------------------------------------------------- last activity
def test_last_activity_ignores_dates_in_the_future(tmp_path):
    from datetime import date

    dbp = tmp_path / "x.sqlite3"
    db.init_db(dbp)
    with db.session(dbp) as conn:
        conn.execute("INSERT INTO projects(number, name, path) VALUES('101500', 'A', '/a')")
        for i, d in enumerate(("2030-06-29", "2024-05-01", None)):
            conn.execute("INSERT INTO documents(path, project, rel_path, filename, ext, doc_date) "
                         "VALUES(?, '101500', 'x', 'x', '.txt', ?)", (f"/a/{i}", d))
        conn.execute("INSERT INTO mails(source, source_key, date, project) VALUES('eml', 'k1', "
                     "'2031-01-01T10:00:00+00:00', '101500')")
        conn.execute("INSERT INTO mails(source, source_key, date, project) VALUES('eml', 'k2', "
                     "'2024-04-30T10:00:00+00:00', '101500')")
        (p,) = _project_rows(conn, "", today=date(2026, 11, 10))
        assert (p["docs"], p["mails"], p["last"]) == (3, 2, "2024-05-01")
        (p,) = _project_rows(conn, "", today=date(2031, 1, 1))
        assert p["last"] == "2031-01-01"
        conn.execute("DELETE FROM documents WHERE doc_date != '2030-06-29' OR doc_date IS NULL")
        conn.execute("DELETE FROM mails")
        (p,) = _project_rows(conn, "", today=date(2026, 11, 10))
        assert p["last"] is None and p["docs"] == 1


def test_a_site_row_counts_its_unterprojekte(register):
    rows = {p["number"]: p for p in _project_rows(register["conn"], "")}
    assert rows["1015"]["docs"] == 3 + 6 and rows["1015"]["own_docs"] == 3
    assert rows["101500"]["docs"] == 1 and rows["101500"]["depth"] == 1 and rows["1015"]["depth"] == 0
    order = [p["number"] for p in _project_rows(register["conn"], "")]
    assert order.index("1015") + 1 == order.index("101500") and order.index("990100") < order.index("1015")


# ---------------------------------------------------------------------------------------- tools
def _ablage():
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("ablage_pruefen_bv", root / "scripts" / "ablage_pruefen.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_filing_check_with_sites(share):
    ablage = _ablage()
    plain = ablage.check(share, depth=6)
    with_sites = ablage.check(share, depth=6, site_digits=4)
    outside = lambda r: sum(r.outside.values())  # noqa: E731
    assert outside(plain) > outside(with_sites)
    assert not any(k.startswith("1/10/1015_Altstadt") for k in with_sites.outside)
    assert sum(with_sites.ext_site.values()) >= len(SITE_FILES)
    text = ablage.format_report(with_sites, pattern=PATTERN, depth=6, examples=5, seconds=0, site_digits=4)
    assert "Bauvorhaben (PROJECT_PARENT_DIGITS=4): 6" in text and "Dateien im Bauvorhaben außerhalb der Aufträge" in text
    assert "Bauvorhaben (PROJECT_PARENT_DIGITS" not in ablage.format_report(plain, pattern=PATTERN, depth=6,
                                                                             examples=5, seconds=0)


def test_structure_report_suggests_the_setting(share):
    from projektakte import structure

    a = structure.analyse(share, max_depth=6)
    assert a.proposal is not None
    report = structure.format_report(a)
    assert "PROJECT_PARENT_DIGITS=4 fasst Aufträge zu Bauvorhaben zusammen" in report
    assert all("PARENT" not in line for line in structure.env_lines(a.proposal))


def test_an_order_folder_in_a_wrong_encoding_is_left_out_of_its_site(register):
    """A share mounted without iocharset: an order folder whose name is no valid UTF-8 is registered with
    � – the site's walk still recognises it and never reads its files."""
    import os

    root, conn = register["root"], register["conn"]
    folder = os.path.join(os.fsencode(root / SITE), b"101505_M\xfcll")
    os.makedirs(os.path.join(folder, b"101505-Text"))
    with open(os.path.join(folder, b"101505-Text", b"Bericht.txt"), "w", encoding="utf-8") as fh:
        fh.write("Bericht zum Müllraum: Wände in F90.")
    stats = run(register)
    assert get_project(conn, "101505")["parent"] == "1015" and "�" in get_project(conn, "101505")["path"]
    assert docs(conn, "101505") == {"101505-Text/Bericht.txt"}
    assert docs(conn, "1015") == SITE_FILES and stats["documents"]["new"] == 1


# ---------------------------------------------------------------------------------------- review findings
@pytest.fixture()
def mini(tmp_path, monkeypatch):
    """An empty database on a share of its own (``files``: rel path → text, written by the test);
    ``extracted`` lists every file extracted, ``advance(days)`` moves the register's clock."""
    from datetime import timedelta

    root = tmp_path / "ablage"
    root.mkdir()
    dbp = tmp_path / "m.sqlite3"
    db.init_db(dbp)
    conn = db.connect(dbp)
    extracted: list[str] = []
    real_extract = documents.extract
    monkeypatch.setattr(documents, "extract", lambda *a, **k: extracted.append(a[0]) or real_extract(*a, **k))
    t0 = registry._utcnow()
    clock = {"days": 0}
    monkeypatch.setattr(registry, "_utcnow", lambda: t0 + timedelta(days=clock["days"]))
    state = {"conn": conn, "root": root, "extracted": extracted}
    state["advance"] = lambda days: clock.__setitem__("days", clock["days"] + days)
    yield state
    conn.close()


def _block(monkeypatch, *folders: Path) -> None:
    """``Path.iterdir`` refuses these folders (as a share does for a folder without rights)."""
    real_iterdir = Path.iterdir

    def iterdir(self):
        if self in folders:
            raise PermissionError(13, "Zugriff verweigert")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)


def test_an_unreadable_intermediate_folder_keeps_its_site(mini, monkeypatch):
    """The only order of a site lies in an intermediate folder that cannot be listed: the site stays
    active with its own files – over several passes and days – and its order keeps pointing at it."""
    root, conn = mini["root"], mini["conn"]
    write(root, "1/20/1200_Bahnhof/Notiz.txt", "Notiz zum Bahnhof.")
    write(root, "1/20/1200_Bahnhof/Plaene/plan.txt", "Plan der Bahnsteige.")
    write(root, "1/20/1200_Bahnhof/1200_PDF_0101/120004_Kopien/k.txt", "Kopien für die Feuerwehr.")
    write(root, "1/10/1015_Altstadt/101501_Umbau/a.txt", "Umbau Station.")
    run(mini)
    assert docs(conn, "1200") == {"Notiz.txt", "Plaene/plan.txt"} and docs(conn, "120004") == {"k.txt"}
    _block(monkeypatch, root / "1/20/1200_Bahnhof/1200_PDF_0101")
    for _ in range(4):
        mini["advance"](1)
        stats = run(mini)
        assert stats["unreadable"] == [str(root / "1/20/1200_Bahnhof/1200_PDF_0101")]
        assert stats["deactivated"] == 0
    assert get_project(conn, "1200")["active"] and get_project(conn, "120004")["parent"] == "1200"
    assert docs(conn, "1200") == {"Notiz.txt", "Plaene/plan.txt"} and docs(conn, "120004") == {"k.txt"}
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts f JOIN chunks c ON c.id = f.rowid "
                        "WHERE c.project = '1200'").fetchone()[0] > 0  # still found
    assert db.project_scope(conn, "1200") == ["1200", "120004"]
    # switched off: the site goes into its grace period although a folder in it cannot be listed
    mini["advance"](1)
    assert run(mini, digits=0)["deactivated"] >= 1
    assert not get_project(conn, "1200")["active"]


def test_an_unreadable_folder_in_a_copied_site_keeps_the_copy_with_its_site(mini, monkeypatch):
    root, conn = mini["root"], mini["conn"]
    write(root, "1/10/1071_Schule/107100_Konzept/Konzept.txt", "Schule: Sicherheitstreppenraum.")
    write(root, "1/10/1073_Turnhalle/107300_Halle/Halle.txt", "Turnhalle.")
    write(root, "1/10/1073_Turnhalle/1071_Schule/Archiv/107100_Konzept/c.txt", "Kopie des Konzepts.")
    write(root, "1/10/1073_Turnhalle/1071_Schule/Kopie Lageplan.txt", "Kopie des Lageplans.")
    run(mini)
    copy = str(root / "1/10/1073_Turnhalle/1071_Schule")
    assert copy in registry.project_folders(conn, "1071")
    assert docs(conn, "1071") == {"1073_Turnhalle/1071_Schule/Kopie Lageplan.txt"} and docs(conn, "1073") == set()
    mini["extracted"].clear()
    _block(monkeypatch, root / "1/10/1073_Turnhalle/1071_Schule/Archiv")
    for _ in range(2):
        run(mini)
        assert copy in registry.project_folders(conn, "1071")
        assert docs(conn, "1071") == {"1073_Turnhalle/1071_Schule/Kopie Lageplan.txt"} and docs(conn, "1073") == set()
    monkeypatch.undo()  # readable again
    run(mini)
    assert docs(conn, "1071") == {"1073_Turnhalle/1071_Schule/Kopie Lageplan.txt"}
    assert not mini["extracted"]  # nothing went back and forth


def test_an_order_that_left_the_register_hands_its_folder_to_the_site(mini):
    """PROJECT_FOLDER_DEPTH lowered: an order deeper than it leaves the register; after the grace period
    its files belong to its site – as when the share is registered with that depth from the start."""
    root, conn = mini["root"], mini["conn"]
    write(root, "1/10/1015_Altstadt/Notiz.txt", "Notiz.")
    write(root, "1/10/1015_Altstadt/101501_Umbau/a.txt", "Umbau Station.")
    write(root, "1/10/1015_Altstadt/1015_PDF_100303/101504_Kopien/k.txt", "Kopien für die Feuerwehr.")
    run(mini, depth=6)
    assert docs(conn, "101504") == {"k.txt"}
    for _ in range(6):
        mini["advance"](3)
        run(mini, depth=4)
    assert not get_project(conn, "101504")["active"] and docs(conn, "101504") == set()
    assert docs(conn, "1015") == {"Notiz.txt", "1015_PDF_100303/101504_Kopien/k.txt"}


def test_a_folder_that_moves_from_one_site_to_another_is_taken_over_unread(mini):
    root, conn = mini["root"], mini["conn"]
    write(root, "1/10/1071_Schule/107100_Konzept/Konzept.txt", "Schule: Sicherheitstreppenraum.")
    write(root, "1/10/1073_Turnhalle/107300_Halle/Halle.txt", "Turnhalle.")
    write(root, "1/10/1073_Turnhalle/1071_Schule/Kopie Lageplan.txt", "Kopie des Lageplans der Schule.")
    run(mini)
    assert docs(conn, "1073") == {"1071_Schule/Kopie Lageplan.txt"}
    mini["extracted"].clear()
    # a copy of an order turns the folder into a further folder of site 1071
    write(root, "1/10/1073_Turnhalle/1071_Schule/107100_Konzept/c.txt", "Kopie des Konzepts.")
    run(mini)
    assert docs(conn, "1071") == {"1073_Turnhalle/1071_Schule/Kopie Lageplan.txt"} and docs(conn, "1073") == set()
    assert [Path(p).name for p in mini["extracted"]] == ["c.txt"]  # the plan was moved, not read again
    hit = conn.execute("SELECT c.project, f.title FROM chunks_fts f JOIN chunks c ON c.id = f.rowid "
                       "WHERE chunks_fts MATCH 'lageplans'").fetchall()
    assert [r[0] for r in hit] == ["1071"] and "1071" in hit[0][1]
    # and the other way round (the site that gives the folder away is read first)
    import shutil

    shutil.rmtree(root / "1/10/1073_Turnhalle/1071_Schule/107100_Konzept")
    mini["extracted"].clear()
    run(mini)
    assert docs(conn, "1073") == {"1071_Schule/Kopie Lageplan.txt"} and docs(conn, "1071") == set()
    assert not mini["extracted"]


def test_switching_the_setting_off_logs_one_line(register, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="projektakte.registry"):
        run(register, digits=0)
    assert not [r for r in caplog.records if "fehlt" in r.getMessage()]
    summary = [r.getMessage() for r in caplog.records if "Bauvorhaben ausgeblendet" in r.getMessage()]
    assert len(summary) == 1 and summary[0].startswith("6 Bauvorhaben ausgeblendet")
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="projektakte.registry"):
        run(register)
    assert [r.getMessage() for r in caplog.records if "wieder" in r.getMessage()] == ["6 Bauvorhaben wieder eingeblendet"]


def test_a_missing_site_folder_is_still_warned_about(register, caplog):
    import logging
    import shutil

    shutil.rmtree(register["root"] / "2/28")
    with caplog.at_level(logging.WARNING, logger="projektakte.registry"):
        run(register)
    msgs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("Bauvorhaben-Ordner fehlt: 2842") for m in msgs)
    assert any(m.startswith("Projektordner fehlt: 284201") for m in msgs)


def test_the_sender_bonus_never_goes_to_a_sibling_because_of_the_site(office):
    from projektakte.assign import vote_projects

    s, ai = office
    text = "Umbau der Station 3 Tür Treppenraum T30-RS"
    with db.session(s.db_path) as conn:
        alone = vote_projects(conn, text, None, {"101502"}, vector_key=str(s.db_path))
        assert alone.bonus_for == "101502"  # one project only: the bonus as before
        both = vote_projects(conn, text, None, {"101502", "1015"}, vector_key=str(s.db_path))
        assert both.bonus_for is None and both.ranked[0][0] == "101501"
        assert vote_projects(conn, text, None, {"1015"}, vector_key=str(s.db_path)).bonus_for is None


def test_a_standard_number_offers_no_site(office):
    s, _ = office
    client = TestClient(create_app(s, background=False))
    for q in ("Feuerwiderstand nach DIN 1015", "DIN EN 1015 Prüfung", "VdS 1015"):
        assert "Zum Bauvorhaben" not in client.get("/suche", params={"q": q}).text
    assert "Zum Bauvorhaben 1015 Altstadt" in client.get("/suche", params={"q": "Altstadt 1015"}).text


def test_order_wording_is_a_project_question_only_on_a_site():
    from projektakte.textutil import is_enumeration_question, is_project_question

    for q in ("Welchen Auftrag hat das Bauamt erteilt?", "Wer hat welchen Auftrag erhalten?"):
        assert not is_project_question(q) and is_project_question(q, orders=True)
    q = "Welche Aufträge hat der Bauherr vergeben?"
    assert not is_enumeration_question(q) and is_enumeration_question(q, orders=True)
    assert is_project_question("In welchem Unterprojekt liegt der Plan?")
    assert is_project_question("In welchem Projekt liegt der Plan?")


def test_a_filtered_list_counts_the_whole_site(register):
    full = {p["number"]: p for p in _project_rows(register["conn"], "")}
    shown = {p["number"]: p for p in _project_rows(register["conn"], "Anbau")}
    assert set(shown) == {"1015", "101502"}
    for key in ("docs", "mails", "last"):
        assert shown["1015"][key] == full["1015"][key]
    assert len(shown["1015"]["subprojects"]) == 5 and len(shown["1015"]["children"]) == 1


def test_no_hint_for_year_folders(tmp_path):
    from projektakte import structure

    root = tmp_path / "jahre"
    for year in (2022, 2023, 2024):
        for i in range(8):
            write(root, f"{year}_Auftraege/{year}{i + 1:02d}_Auftrag{i}/Bericht.txt", "Bericht.")
    a = structure.analyse(root, max_depth=4)
    assert a.proposal is not None and structure.parent_digits_hint(a) is None
    assert "PROJECT_PARENT_DIGITS" not in structure.format_report(a)


def test_filing_check_reads_a_stranger_in_a_site_for_the_site(share):
    ablage = _ablage()
    plain = ablage.check(share, depth=6)
    assert any("1827_Werk/Fotos/230512_Begehung" in str(p) for p, _ in plain.issues.get("fremde_nummer", []))
    with_sites = ablage.check(share, depth=6, site_digits=4)
    assert not any("230512_Begehung" in str(p) for p, _ in with_sites.issues.get("fremde_nummer", []))
    (hint,) = with_sites.issues["fremde_nummer_bauvorhaben"]
    assert "1827_Werk/Fotos/230512_Begehung" in str(hint[0]) and "als Datei des Bauvorhabens" in hint[1]
    assert ablage.KINDS["fremde_nummer_bauvorhaben"][0] == "HINWEIS"
