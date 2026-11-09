"""Several folders with the same order (project) number are one project.

Built after the office's share (UEBERGABE.md, 7a – all names here invented): an order in several folders
of its site (111301_Fortschreibung …, 111301_Heisse Bemessung, 111301_Heisse Bemessung-2013-11-04), part
folders without their order folder (119300-Bilder, -CAD, -LV, -Mail directly in the site), part folders
with a date (747900-ea-200214, 747900-ee-200129), orders in an intermediate folder of the site
(1015_Altstadt/1015_PDF_100303/101500_…) and nested copies of a site (1071_X/1071_X/107100_…, a site copied
into another one: 1073_Y/1071_X/107100_…). All folders of a number are read as one project; the primary
folder and the name follow a general rule of form (registry.folder_kind, registry.project_name)."""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from email.message import EmailMessage
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from projektakte import db, documents, registry
from projektakte.ai.services import AIServices
from projektakte.documents import index_all_documents, index_project_documents
from projektakte.indexer import run_index
from projektakte.main import create_app
from projektakte.registry import (BARE, ORDER, PART, folder_kind, project_folders, scan_projects, site_name,
                                  sync_from_folder)
from projektakte.search import hybrid_search

from .conftest import fake_settings

PATTERN = r"\d{6}"
FORTSCHREIBUNG = "1/11/1113_Bau Nord/111301_Fortschreibung Konzept"
HEISS = "1/11/1113_Bau Nord/111301_Heisse Bemessung"
HEISS_KOPIE = "1/11/1113_Bau Nord/111301_Heisse Bemessung-2013-11-04"
GASSE = "1/11/1193_Gasse am Markt 5"
SCHULE = "1/10/1071_Schule/107100_Konzept"
SCHULE_KOPIE = "1/10/1071_Schule/1071_Schule/107100_Konzept"  # a nested copy of the site
TURNHALLE = "1/10/1073_Turnhalle/1071_Schule/107100_Konzept"  # the site copied into another one
ALTBAU = "1/10/1015_Altstadt/1015_PDF_100303/101500_Altbau"  # an order in an intermediate folder


def write(root: Path, rel: str, text: str) -> Path:
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    return f


def office_share(root: Path) -> Path:
    write(root, f"{FORTSCHREIBUNG}/111301-Text/Konzept.txt",
          "Fortschreibung des Brandschutzkonzepts: zweiter Rettungsweg über den Treppenraum Nord.")
    bemessung = "Heißbemessung der Stahlträger im Lager: Feuerwiderstand R 90 nach DIN EN 1993-1-2 nachgewiesen."
    write(root, f"{HEISS}/111301-Text/Bemessung.txt", bemessung)
    write(root, f"{HEISS_KOPIE}/111301-Text/Bemessung.txt", bemessung)  # an identical file in a copy
    for part, text in (("Bilder", "Fotoliste der Begehung am Markt."), ("CAD", "Planliste: Grundriss EG mit Fluchtwegen."),
                       ("LV", "Leistungsverzeichnis Rauchschutztüren T30-RS."), ("Mail", "Notiz zur Brandschau.")):
        write(root, f"{GASSE}/119300-{part}/{part}.txt", text)
    write(root, "7/74/7479_Halle Ost/747900-ea-200214/Brief.txt", "Stellungnahme zur Löschwasserversorgung.")
    write(root, "7/74/7479_Halle Ost/747900-ee-200129/Brief.txt", "Anfrage zur Feuerwehrzufahrt.")
    write(root, f"{ALTBAU}/Gutachten.txt", "Gutachten zum Bestandsschutz der Holzbalkendecke F30-B.")
    konzept = "Konzept Schule: Sicherheitstreppenraum mit Druckbelüftungsanlage."
    write(root, f"{SCHULE}/Konzept.txt", konzept)
    write(root, f"{SCHULE_KOPIE}/Konzept.txt", konzept)
    write(root, f"{SCHULE_KOPIE}/Nachtrag.txt", "Nachtrag: Rauchwarnmelder in den Klassenräumen.")
    write(root, f"{TURNHALLE}/Turnhalle.txt", "Turnhalle: Versammlungsstätte mit 400 Besuchern.")
    write(root, "2/28/2842_Filiale/284201_Zwischendecke/284201-Text/Bericht.txt",
          "Zwischendecke über dem Verkaufsraum in EI 30.")
    return root


@pytest.fixture()
def share(tmp_path):
    return office_share(tmp_path / "projekt")


@pytest.fixture()
def register(tmp_path, share, monkeypatch):
    """The share registered and read (depth 6, no AI); ``extracted`` lists every file extracted."""
    dbp = tmp_path / "x.sqlite3"
    db.init_db(dbp)
    conn = db.connect(dbp)
    extracted: list[str] = []
    real_extract = documents.extract
    monkeypatch.setattr(documents, "extract", lambda *a, **k: extracted.append(a[0]) or real_extract(*a, **k))
    state = {"conn": conn, "root": share, "extracted": extracted}
    run(state)
    extracted.clear()
    yield state
    conn.close()


def run(state: dict, depth: int = 6) -> dict:
    conn = state["conn"]
    stats = sync_from_folder(conn, state["root"], PATTERN, max_depth=depth)
    conn.commit()
    if "warning" not in stats:
        stats["documents"] = index_all_documents(conn, projects_dir=state["root"])
    return stats


def docs(conn, project: str) -> dict[str, str]:
    """rel_path → absolute path of the files of a project."""
    return {r[0]: r[1] for r in conn.execute(
        "SELECT rel_path, path FROM documents WHERE project = ? AND mail_id IS NULL ORDER BY rel_path", (project,))}


def rel(paths, root: Path) -> list[str]:
    return [Path(p).relative_to(root).as_posix() for p in paths]


# ------------------------------------------------------------------------------------- the rule of form
@pytest.mark.parametrize("separator, rest, kind", [
    ("_", "Heisse Bemessung", ORDER), (" ", "Kita Sonnenhof", ORDER), ("_", "Heisse Bemessung-2013-11-04", ORDER),
    ("-", "Bilder", PART), ("-", "Mail", PART), ("-", "ea-200214", PART), ("-", "Xyzzy", PART),  # no word list
    ("-", "Rathaus Neubau", ORDER),  # more than one word after the hyphen: a name
    ("_", "Abgabe_FW", ORDER), (" - ", "Bilder", ORDER), ("--", "Bilder", ORDER), ("", "", BARE), ("_", "  ", BARE)])
def test_folder_kind_is_a_general_rule_of_form(separator, rest, kind):
    assert folder_kind(separator, rest) == kind


def test_site_name(tmp_path):
    root = tmp_path
    assert site_name(root / GASSE / "119300-Bilder", "119300", root) == "Gasse am Markt 5"
    # of folders with an equal number the outermost (the site, not the intermediate folder in it)
    assert site_name(root / "1/10/1015_Altstadt/1015_PDF_100303/101500-Bilder", "101500", root) == "Altstadt"
    # the longest number wins over a shorter one further out
    assert site_name(root / "1/10/1015_Altstadt/10150_Teil A/101500-Bilder", "101500", root) == "Teil A"
    assert site_name(root / "1/11/119300-Bilder", "119300", root) is None  # group folders have no name
    assert site_name(root / "1193/119300-Bilder", "119300", root) is None
    # the number must go on with digits: a year folder is no site of 2024-017
    assert site_name(root / "2024 Projekte/2024-017-Kita", "2024-017", root) is None
    assert site_name(root / "4711_Kunde/119300-Bilder", "119300", root) is None  # another number
    assert site_name(root / "119300-Bilder", "119300", root) is None


# ------------------------------------------------------------------------------------- the scan
def test_folders_of_one_number_are_one_project(share, caplog):
    caplog.set_level(logging.DEBUG, logger="projektakte.registry")
    duplicates: list = []
    found = {p.number: p for p in scan_projects(share, PATTERN, 4, duplicates=duplicates)}
    assert sorted(found) == ["107100", "111301", "119300", "284201", "747900"]  # depth 4: 101500 lies deeper
    p = found["111301"]  # with a proper order folder: it is the primary folder and gives the name
    assert (p.name, rel([p.path], share), rel(p.further, share)) == (
        "Fortschreibung Konzept", [FORTSCHREIBUNG], [HEISS, HEISS_KOPIE])
    p = found["119300"]  # only part folders: the name comes from the site, not "Bilder"
    assert (p.name, rel(p.folders, share)) == (
        "Gasse am Markt 5", [f"{GASSE}/119300-{x}" for x in ("Bilder", "CAD", "LV", "Mail")])
    p = found["747900"]
    assert (p.name, rel(p.folders, share)) == (
        "Halle Ost", ["7/74/7479_Halle Ost/747900-ea-200214", "7/74/7479_Halle Ost/747900-ee-200129"])
    assert (found["284201"].name, found["284201"].further) == ("Zwischendecke", ())
    assert len(duplicates) == 6 and duplicates[0] == ("111301", share / FORTSCHREIBUNG, share / HEISS)
    assert not any("doppelt" in r.getMessage() for r in caplog.records)  # merged folders are no warning


@pytest.mark.parametrize("depth, numbers, schule", [
    (4, ["107100", "111301", "119300", "284201", "747900"], [SCHULE]),
    (5, ["101500", "107100", "111301", "119300", "284201", "747900"], [SCHULE, SCHULE_KOPIE, TURNHALLE]),
    (6, ["101500", "107100", "111301", "119300", "284201", "747900"], [SCHULE, SCHULE_KOPIE, TURNHALLE])])
def test_deeper_orders_join_their_order(share, depth, numbers, schule):
    """Depth 4 reads the orders where the rule puts them; from depth 5 on orders in an intermediate folder
    and nested copies are found too – a nested copy as a further folder of its order, the shallowest
    folder stays the primary one."""
    found = {p.number: p for p in scan_projects(share, PATTERN, depth)}
    assert sorted(found) == numbers
    assert rel(found["107100"].folders, share) == schule and found["107100"].name == "Konzept"
    if "101500" in found:
        assert (found["101500"].name, rel(found["101500"].folders, share)) == ("Altbau", [ALTBAU])


def test_the_order_folder_is_primary_whatever_sorts_first(tmp_path):
    root = tmp_path / "p"
    for rel_path in ("2/28/2842_Filiale/284201-Bilder", "2/28/2842_Filiale/284201_Zwischendecke",
                     "2/28/2842_Filiale/1/284201_Zwischendecke Kopie",  # deeper: after the shallow one
                     "1/11/1193_Gasse/119300", "1/11/1193_Gasse/119300-Bilder"):
        (root / rel_path).mkdir(parents=True)
    found = {p.number: p for p in scan_projects(root, PATTERN, 6)}
    # "284201-Bilder" sorts before "284201_Zwischendecke", but the order folder is the primary one
    assert rel(found["284201"].folders, root) == ["2/28/2842_Filiale/284201_Zwischendecke",
                                                  "2/28/2842_Filiale/1/284201_Zwischendecke Kopie",
                                                  "2/28/2842_Filiale/284201-Bilder"]
    assert found["284201"].name == "Zwischendecke"
    # a folder with the bare number is the order folder without a name: primary, named after its site
    assert rel(found["119300"].folders, root) == ["1/11/1193_Gasse/119300", "1/11/1193_Gasse/119300-Bilder"]
    assert found["119300"].name == "Gasse"


def test_projects_of_one_folder_keep_their_names(tmp_path):
    """Other layouts are not affected: a single folder named with one word after a hyphen keeps that
    word when no site above it continues its number."""
    root = tmp_path / "p"
    for rel_path in ("P20001-Rathaus", "P20002 Halle", "Kunden/P20003-Kita"):
        (root / rel_path).mkdir(parents=True)
    assert [(p.number, p.name) for p in scan_projects(root, r"P\d{5}", 2)] == [
        ("P20001", "Rathaus"), ("P20002", "Halle"), ("P20003", "Kita")]
    years = tmp_path / "j"
    for rel_path in ("2024/2024-017-Kita", "2024/2024-018 Schule", "2024 Archiv/2024-019"):
        (years / rel_path).mkdir(parents=True)
    assert [(p.number, p.name) for p in scan_projects(years, r"\d{4}-\d{3}", 2)] == [
        ("2024-017", "Kita"), ("2024-018", "Schule"), ("2024-019", "2024-019")]


def test_a_share_named_in_the_hyphen_form_keeps_the_words(tmp_path):
    """Where projects are named "<number>-<word>" in numbered group folders, the word stays the name – the
    group folder whose digits the number continues (1_Hochbau above 1234-Kita) is no site. In the office's
    filing ("<number>_<name>") a lone part folder is named after its site."""
    root = tmp_path / "p"
    for rel_path in ("1_Hochbau/1234-Kita", "1_Hochbau/1301-Rathaus", "2_Tiefbau/2101-Brücke"):
        (root / rel_path).mkdir(parents=True)
    assert [(p.number, p.name) for p in scan_projects(root, r"\d{4}", 2)] == [
        ("1234", "Kita"), ("1301", "Rathaus"), ("2101", "Brücke")]
    for rel_path, pattern in (("24 Projekte/24018-Feuerwehr", r"\d{5}"), ("2024_Aufträge/202401-Schule", r"\d{6}")):
        other = tmp_path / rel_path.split("/")[0]
        (other / rel_path).mkdir(parents=True)
        assert [p.name for p in scan_projects(other, pattern, 2)] == [rel_path.split("-", 1)[1]]
    office = tmp_path / "o"
    (office / "2/28/2844_Lager/284401-Mail").mkdir(parents=True)
    (office / "2/28/2842_Filiale/284201_Zwischendecke").mkdir(parents=True)
    (office / "2/28/2843_Halle/284301_Halle").mkdir(parents=True)
    assert {p.number: p.name for p in scan_projects(office, PATTERN, 4)}["284401"] == "Lager"


def test_a_folder_with_the_number_of_another_sites_order_is_not_read_with_it(tmp_path, caplog):
    """A date as number (231020_Bilder für Konformität in site 1910) next to the real order 231020 of site
    2310: not merged into that order (its files belong to another site) – whichever comes first in the
    scan – and named in a warning. Without a folder in its own site nothing changes (other layouts); a year
    folder of the site in between (1827_Werk/2013_Archiv/…/182701_…) is no other site."""
    root = tmp_path / "p"
    real = root / "2/23/2310_Halle/231020_Brandschutzkonzept"
    date = root / "1/19/1910_Lager/231020_Bilder für Konformität"
    deeper = root / "1/18/1827_Werk/Fotos/230512_Begehung"
    real2 = root / "2/23/2305_Bau Süd/230512_Konzept"
    copy = root / "1/18/1827_Werk/2013_Archiv/Alt/182701_Altauftrag"
    order = root / "1/18/1827_Werk/182701_Auftrag"
    for folder in (real, date, deeper, real2, copy, order):
        folder.mkdir(parents=True)
    foreign: list = []
    caplog.set_level(logging.WARNING, logger="projektakte.registry")
    found = {p.number: p for p in scan_projects(root, PATTERN, 6, foreign=foreign)}
    assert found["231020"].folders == (real,) and found["231020"].name == "Brandschutzkonzept"
    assert found["230512"].folders == (real2,)
    assert found["182701"].folders == (order, copy)  # the archived copy in its own site: read together
    assert sorted((n, f) for n, f, _ in foreign) == [("230512", deeper), ("231020", date)]
    assert not caplog.records  # the caller reports them
    scan_projects(root, PATTERN, 6)  # the indexing pass: a warning for each
    assert sum("liegt aber in einem Bauvorhaben mit anderer Nummer" in r.getMessage() for r in caplog.records) == 2
    # alone, the dated folder is read as an order of its own (the filing check reports it)
    lone = tmp_path / "q"
    (lone / "1/19/1910_Lager/231020_Bilder für Konformität").mkdir(parents=True)
    assert [p.name for p in scan_projects(lone, PATTERN, 4)] == ["Bilder für Konformität"]
    # other layouts: no folder lies in a site of its number – the folders are read together as before
    other = tmp_path / "r"
    for rel_path in ("4711_Kunde Meier/202401_Schule", "4712_Kunde Schulz/202401_Schule Kopie"):
        (other / rel_path).mkdir(parents=True)
    assert [len(p.folders) for p in scan_projects(other, PATTERN, 2)] == [2]


def test_a_folder_reached_twice_is_read_once(tmp_path):
    """Links inside the share: the same folder under another name counts once (on its real path), a link
    to a folder inside the order is not read a second time."""
    root = tmp_path / "p"
    order = root / "1193_Gasse" / "119300_Konzept"
    (order / "119300-Bilder").mkdir(parents=True)
    (root / "Links").mkdir()
    os.symlink(order / "119300-Bilder", root / "Links" / "119300-Bilder")
    os.symlink(order, root / "Links" / "119300_Verknüpfung")
    os.symlink(order, root / "0_Aktuell")  # a link to the order that sorts before it
    duplicates: list = []
    [p] = scan_projects(root, PATTERN, 2, duplicates=duplicates)
    assert (p.path, p.further, duplicates) == (order, (), [])


def test_a_link_loop_ends_at_the_depth(tmp_path):
    root = office_share(tmp_path / "p")
    os.symlink(root, root / "1" / "Schleife")
    found = {p.number: p for p in scan_projects(root, PATTERN, 6)}
    assert rel(found["111301"].folders, root) == [FORTSCHREIBUNG, HEISS, HEISS_KOPIE]  # no path through the loop


# ------------------------------------------------------------------------------------- the documents
def test_files_of_all_folders_are_read_once(register):
    s = register
    conn, root = s["conn"], s["root"]
    assert docs(conn, "111301") == {
        "111301-Text/Konzept.txt": str(root / FORTSCHREIBUNG / "111301-Text/Konzept.txt"),
        "111301_Heisse Bemessung-2013-11-04/111301-Text/Bemessung.txt":
            str(root / HEISS_KOPIE / "111301-Text/Bemessung.txt"),
        "111301_Heisse Bemessung/111301-Text/Bemessung.txt": str(root / HEISS / "111301-Text/Bemessung.txt")}
    # files of the primary folder keep their path inside it, those of further folders carry their folder
    assert set(docs(conn, "119300")) == {"Bilder.txt", "119300-CAD/CAD.txt", "119300-LV/LV.txt",
                                         "119300-Mail/Mail.txt"}
    assert set(docs(conn, "107100")) == {"Konzept.txt", "1071_Schule/107100_Konzept/Konzept.txt",
                                         "1071_Schule/107100_Konzept/Nachtrag.txt",
                                         "1073_Turnhalle/1071_Schule/107100_Konzept/Turnhalle.txt"}
    assert list(docs(conn, "101500")) == ["Gutachten.txt"]
    assert project_folders(conn, "111301") == [str(root / FORTSCHREIBUNG), str(root / HEISS), str(root / HEISS_KOPIE)]
    assert conn.execute("SELECT name FROM projects WHERE number = '119300'").fetchone()[0] == "Gasse am Markt 5"
    # a second pass reads nothing again and changes nothing
    stats = run(s)
    assert s["extracted"] == []
    d = stats["documents"]
    assert (d["new"], d["changed"], d["moved"], d["removed"], d["unchanged"]) == (0, 0, 0, 0, 15)
    assert stats["folders_changed"] == 0 and stats["merged"]["projects"] == 4


def test_a_vanished_further_folder_leaves_the_index(register, caplog):
    s = register
    conn, root = s["conn"], s["root"]
    caplog.set_level(logging.INFO, logger="projektakte.registry")
    shutil.rmtree(root / HEISS)
    stats = run(s)
    assert stats["documents"]["removed"] == 1 and s["extracted"] == []
    assert sorted(docs(conn, "111301")) == ["111301-Text/Konzept.txt",
                                            "111301_Heisse Bemessung-2013-11-04/111301-Text/Bemessung.txt"]
    assert project_folders(conn, "111301") == [str(root / FORTSCHREIBUNG), str(root / HEISS_KOPIE)]
    assert conn.execute("SELECT active FROM projects WHERE number = '111301'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] == \
        conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert any("bisherige(r) Ordner nicht mehr dabei" in r.getMessage() for r in caplog.records)
    # the primary folder going away: the next folder becomes the primary one, its files are renamed only
    shutil.rmtree(root / FORTSCHREIBUNG)
    stats = run(s)
    assert stats["documents"]["removed"] == 1 and s["extracted"] == []
    assert list(docs(conn, "111301")) == ["111301-Text/Bemessung.txt"]
    assert conn.execute("SELECT name, path FROM projects WHERE number = '111301'").fetchone()[:] == (
        "Heisse Bemessung-2013-11-04", str(root / HEISS_KOPIE))


def test_a_renamed_further_folder_is_not_read_again(register):
    s = register
    conn, root = s["conn"], s["root"]
    ids = sorted(r[0] for r in conn.execute("SELECT id FROM documents WHERE project = '119300'"))
    (root / GASSE / "119300-CAD").rename(root / GASSE / "119300-Plaene")
    stats = run(s)
    assert s["extracted"] == [] and (stats["documents"]["moved"], stats["documents"]["removed"]) == (1, 0)
    assert set(docs(conn, "119300")) == {"Bilder.txt", "119300-Plaene/CAD.txt", "119300-LV/LV.txt",
                                         "119300-Mail/Mail.txt"}
    assert sorted(r[0] for r in conn.execute("SELECT id FROM documents WHERE project = '119300'")) == ids
    # two vanished files that fit equally well: none is taken, the new file is read
    shutil.rmtree(root / HEISS)
    shutil.rmtree(root / HEISS_KOPIE)
    write(root, "1/11/1113_Bau Nord/111301_Heissbemessung/111301-Text/Bemessung.txt", "Heißbemessung, neu.")
    stats = run(s)
    assert (stats["documents"]["moved"], stats["documents"]["new"], stats["documents"]["removed"]) == (0, 1, 2)


def test_a_new_order_folder_takes_over_without_reading_again(register):
    s = register
    conn, root = s["conn"], s["root"]
    write(root, f"{GASSE}/119300_Brandschutzkonzept/Konzept.txt", "Konzept Gasse am Markt.")
    stats = run(s)
    assert stats["documents"]["new"] == 1 and [Path(x).name for x in s["extracted"]] == ["Konzept.txt"]
    assert sorted(docs(conn, "119300")) == ["119300-Bilder/Bilder.txt", "119300-CAD/CAD.txt", "119300-LV/LV.txt",
                                            "119300-Mail/Mail.txt", "Konzept.txt"]
    assert conn.execute("SELECT name FROM projects WHERE number = '119300'").fetchone()[0] == "Brandschutzkonzept"


def test_unreadable_folders_and_unmounted_shares_remove_nothing(register, monkeypatch):
    s = register
    conn, root = s["conn"], s["root"]
    before = {n: docs(conn, n) for n in ("107100", "111301", "119300")}
    folders = project_folders(conn, "107100")
    # a folder above a further folder cannot be listed: the further folder and its files stay
    # (stat of anything inside fails as well, as without the x right: Path.is_dir raises there)
    blocked = root / "1/10/1073_Turnhalle"
    real_iterdir, real_is_dir = Path.iterdir, Path.is_dir

    def iterdir(self):
        if self == blocked:
            raise PermissionError(13, "Permission denied")
        return real_iterdir(self)

    def is_dir(self):
        if self != blocked and self.is_relative_to(blocked):
            raise PermissionError(13, "Permission denied", str(self))
        return real_is_dir(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    monkeypatch.setattr(Path, "is_dir", is_dir)
    stats = run(s)
    assert stats["unreadable"] == [str(blocked)] and stats["documents"]["removed"] == 0
    assert project_folders(conn, "107100") == folders and docs(conn, "107100") == before["107100"]
    # the primary folder itself below the unreadable folder: the pass goes on, nothing is removed
    conn.execute("UPDATE projects SET path = ? WHERE number = '107100'", (str(blocked / "1071_Schule/107100_Konzept"),))
    conn.commit()
    stats = run(s)
    assert stats["documents"]["removed"] == 0 and docs(conn, "107100") == before["107100"]
    assert docs(conn, "119300") == before["119300"]  # later projects are still read
    monkeypatch.setattr(Path, "iterdir", real_iterdir)
    monkeypatch.setattr(Path, "is_dir", real_is_dir)
    # the share not mounted (empty mount point): nothing changes at all
    away = root.parent / "weg"
    root.rename(away)
    root.mkdir()
    stats = run(s)
    assert "keine Projekte mehr" in stats["warning"]
    assert {n: docs(conn, n) for n in before} == before and project_folders(conn, "107100") == folders
    root.rmdir()
    away.rename(root)
    # a further folder not reachable while the documents are read (gone after the scan): its files stay
    project = dict(conn.execute("SELECT number, name, path FROM projects WHERE number = '111301'").fetchone())
    project["further"] = [str(root / HEISS), str(root / "1/11/1113_Bau Nord/111301_nicht erreichbar")]
    conn.execute("INSERT INTO documents(path, project, rel_path, filename, ext, status) VALUES(?, '111301', ?, ?, "
                 "'.txt', 'ok')", (str(root / "1/11/1113_Bau Nord/111301_nicht erreichbar/Akte.txt"),
                                   "111301_nicht erreichbar/Akte.txt", "Akte.txt"))
    conn.commit()
    stats = index_project_documents(conn, project, projects_dir=root)
    assert stats["removed"] == 1  # the copy that is no folder of the project any more …
    assert "111301_nicht erreichbar/Akte.txt" in docs(conn, "111301")  # … but not the unreachable folder


def test_a_project_gone_completely_waits_for_the_grace_period(register):
    s = register
    conn, root = s["conn"], s["root"]
    before = docs(conn, "119300")
    away = root.parent / "weg"
    (root / GASSE).rename(away)
    stats = run(s)
    assert (stats["deactivated"], stats["documents_hidden"], stats["documents"]["removed"]) == (1, 4, 0)
    assert docs(conn, "119300") == before and len(project_folders(conn, "119300")) == 4
    away.rename(root / GASSE)
    stats = run(s)
    assert (stats["reactivated"], stats["documents_restored"]) == (1, 4)
    assert docs(conn, "119300") == before and s["extracted"] == []


def test_one_info_line_per_pass_at_most(tmp_path, share, caplog):
    dbp = tmp_path / "y.sqlite3"
    db.init_db(dbp)
    caplog.set_level(logging.DEBUG, logger="projektakte.registry")
    with db.session(dbp) as conn:
        sync_from_folder(conn, share, PATTERN, max_depth=6)
    infos = [r for r in caplog.records if r.levelno >= logging.INFO and "mehreren Ordnern" in r.getMessage()]
    assert len(infos) == 1 and "4 Projektnummer(n)" in infos[0].getMessage()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    caplog.clear()
    with db.session(dbp) as conn:  # nothing changed: no line above debug
        sync_from_folder(conn, share, PATTERN, max_depth=6)
    assert not [r for r in caplog.records if r.levelno >= logging.INFO]


def test_a_database_of_the_previous_version_keeps_working(tmp_path, monkeypatch):
    """Migration 8 only adds the folder table: projects and documents stored by the previous version are
    used as they are – nothing is read again – and further folders are added on the next pass."""
    root = tmp_path / "p"
    write(root, f"{FORTSCHREIBUNG}/Konzept.txt", "Fortschreibung des Brandschutzkonzepts.")
    write(root, f"{GASSE}/119300-Bilder/Fotos.txt", "Fotoliste.")
    dbp = tmp_path / "alt.sqlite3"
    conn = sqlite3.connect(dbp)  # the schema of the previous version (user_version 7)
    for i, script in enumerate(db.MIGRATIONS[:7], start=1):
        conn.executescript("BEGIN;" + script + f"PRAGMA user_version = {i}; COMMIT;")
    conn.execute("INSERT INTO projects(number, name, path) VALUES('111301', 'Fortschreibung Konzept', ?)",
                 (str(root / FORTSCHREIBUNG),))
    conn.execute("INSERT INTO projects(number, name, path) VALUES('119300', 'Bilder', ?)",
                 (str(root / GASSE / "119300-Bilder"),))
    conn.commit()
    conn.close()
    db.init_db(dbp)
    conn = db.connect(dbp)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS) >= 8
    assert conn.execute("SELECT COUNT(*) FROM project_folders").fetchone()[0] == 0
    assert project_folders(conn, "111301") == [str(root / FORTSCHREIBUNG)]  # one folder, as before
    index_all_documents(conn, projects_dir=root)  # what the previous version stored
    extracted: list[str] = []
    real_extract = documents.extract
    monkeypatch.setattr(documents, "extract", lambda *a, **k: extracted.append(a[0]) or real_extract(*a, **k))
    stats = run({"conn": conn, "root": root})
    assert extracted == [] and stats["documents"]["unchanged"] == 2
    assert conn.execute("SELECT name FROM projects WHERE number = '119300'").fetchone()[0] == "Gasse am Markt 5"
    assert sorted(docs(conn, "119300")) == ["Fotos.txt"]
    write(root, f"{HEISS}/Bemessung.txt", "Heißbemessung R 90.")
    stats = run({"conn": conn, "root": root})
    assert [Path(x).name for x in extracted] == ["Bemessung.txt"] and stats["folders_changed"] == 1
    assert sorted(docs(conn, "111301")) == ["111301_Heisse Bemessung/Bemessung.txt", "Konzept.txt"]
    # a project removed by the demo cleanup takes its folders with it
    conn.execute("DELETE FROM projects WHERE number = '111301'")
    assert conn.execute("SELECT COUNT(*) FROM project_folders WHERE project = '111301'").fetchone()[0] == 0
    conn.close()


# ------------------------------------------------------------------------------------- the whole app
def _eml(path: Path, subject: str, body: str) -> None:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "Bauamt <bauamt@stadt.example>", "buero@ib.example", subject
    m["Date"] = "Tue, 18 Jun 2024 10:30:00 +0200"
    m["Message-ID"] = f"<{path.stem}@stadt.example>"
    m.set_content(body)
    path.write_bytes(bytes(m))


def test_one_project_in_search_answers_mails_and_pages(tmp_path, share, fake_ai):
    mails = tmp_path / "mails"
    mails.mkdir()
    _eml(mails / "a.eml", "Rückfrage zu 119300 Gasse am Markt", "Bitte die Rauchschutztüren prüfen.")
    _eml(mails / "b.eml", "Heißbemessung 111301", "Ist R 90 nachgewiesen?")
    s = fake_settings(fake_ai, tmp_path, PROJECTS_DIR=share, MAIL_DIR=mails, PROJECT_NUMBER_PATTERN=PATTERN,
                      PROJECT_FOLDER_DEPTH=6)
    ai = AIServices.from_settings(s)
    stats = run_index(s, ai)
    assert stats["projects"]["merged"]["projects"] == 4 and "warning" not in stats["projects"]
    with db.session(s.db_path) as conn:
        # mails are assigned by number, as before – to the one project
        assert {r[0]: r[1] for r in conn.execute("SELECT subject, project FROM mails")} == {
            "Rückfrage zu 119300 Gasse am Markt": "119300", "Heißbemessung 111301": "111301"}
        # a file of a further folder is found in its project, named with its folder
        hits = hybrid_search(conn, ai, str(s.db_path), "Heißbemessung Stahlträger", project="111301").hits
        assert hits and hits[0].project == "111301" and hits[0].rel_path.startswith("111301_Heisse Bemessung")
        hits = hybrid_search(conn, ai, str(s.db_path), "Leistungsverzeichnis Rauchschutztüren", project="119300").hits
        assert any(h.rel_path == "119300-LV/LV.txt" for h in hits)
        assert all(h.project == "119300" for h in hits)
    client = TestClient(create_app(s, background=False))
    page = client.get("/projekte/119300").text
    assert "Gasse am Markt 5" in page and "4 Ordner mit dieser Nummer, zusammen gelesen" in page
    for part in ("Bilder", "CAD", "LV", "Mail"):
        assert f"119300-{part}" in page
    assert "Hauptordner" in page
    single = client.get("/projekte/284201").text
    assert "Ordner: 2/28/2842_Filiale/284201_Zwischendecke" in single and "Hauptordner" not in single
    data = client.get("/api/projects/111301").json()
    assert data["path"] == FORTSCHREIBUNG and data["folders"] == [FORTSCHREIBUNG, HEISS, HEISS_KOPIE]
    assert client.get("/api/projects/284201").json()["folders"] == ["2/28/2842_Filiale/284201_Zwischendecke"]
    listed = client.get("/api/projects/111301/documents").json()
    assert listed["count"] == 3
    further = next(d for d in listed["documents"] if d["rel_path"].startswith("111301_Heisse Bemessung/"))
    original = client.get(f"/dokumente/{further['id']}/datei")  # the original file of a further folder
    assert original.status_code == 200 and "Heißbemessung" in original.text
    # a second pass changes nothing
    again = run_index(s, ai)["documents"]
    assert (again["new"], again["changed"], again["removed"]) == (0, 0, 0)


def test_the_rule_takes_no_group_name_of_the_pattern(tmp_path):
    """The separator is read from the positions of the match: a pattern may use any group name that
    the configuration allows (only num and name are taken)."""
    root = tmp_path / "p"
    for rel_path in ("1193_Gasse/119300-Bilder", "1193_Gasse/119300-CAD"):
        (root / rel_path).mkdir(parents=True)
    [p] = scan_projects(root, r"(?P<sep>\d{4})\d{2}", 2)
    assert (p.number, p.name, len(p.folders)) == ("119300", "Gasse", 2)
