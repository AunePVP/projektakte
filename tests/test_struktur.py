"""Folder layout of PROJECTS_DIR (structure.py): detection of the project number scheme and depth on
synthetic layouts, PROJECT_FOLDER_DEPTH, the automatic mode (detect once, store, never switch on its
own), an unusable PROJECT_NUMBER_PATTERN, the effective scheme in assignment, search and status page,
and the command line tool."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from projektakte import db, structure, web
from projektakte.ai.services import AIServices
from projektakte.config import DEFAULT_PROJECT_NUMBER_PATTERN, load_settings, pattern_problem
from projektakte.indexer import run_index
from projektakte.main import create_app
from projektakte.registry import scan_projects, sync_from_folder
from projektakte.startup import log_startup
from projektakte.structure import analyse, context_statistics, file_statistics, layout_for_index, main

from .conftest import fake_settings

BS = r"BS-\d{4}-\d{4}"


def mk(root: Path, *paths: str) -> Path:
    for p in paths:
        (root / p).mkdir(parents=True, exist_ok=True)
    return root


def bs_projects(root: Path, prefix: str = "", n: int = 5, year: int = 2024) -> list[str]:
    names = [f"{prefix}BS-{year}-{i:04d} Projekt {i}" for i in range(1, n + 1)]
    mk(root, *names)
    return names


def settings_for(tmp_path: Path, root: Path, **extra):
    mail = tmp_path / "mails"
    mail.mkdir(parents=True, exist_ok=True)
    env = {"DATA_DIR": str(tmp_path / "data"), "PROJECTS_DIR": str(root), "MAIL_DIR": str(mail),
           "APP_ENV": "test", "AUTH_DISABLED": "1"}
    env.update({k: str(v) for k, v in extra.items()})
    return load_settings(env)


class NoAuth:
    disabled, users, cookie_secure = True, {}, False


# ============================================================================ detection on layouts
def test_flat_bs_projects(tmp_path):
    bs_projects(tmp_path, n=6)
    a = analyse(tmp_path)
    # directly in the share: the default depth 2 finds the same folders (and later year folders too)
    assert a.exit_code == 0 and a.proposal.pattern == BS and a.proposal.depth == 2
    assert a.proposal.projects == 6 and a.proposal.exceptions_total == 0 and a.proposal.levels == {1: 6}
    assert analyse(tmp_path, max_depth=1).proposal.depth == 1
    assert a.proposal.description == "„BS“, Bindestrich, 4 Ziffern, Bindestrich, 4 Ziffern"
    assert a.proposal.examples[0] == {"number": "BS-2024-0001", "name": "Projekt 1", "path": "BS-2024-0001 Projekt 1"}


def test_year_folders(tmp_path):
    for year in (2022, 2023, 2024):
        bs_projects(tmp_path / str(year), n=4, year=year)
    a = analyse(tmp_path)
    assert (a.proposal.pattern, a.proposal.depth, a.proposal.projects) == (BS, 2, 12)
    assert a.proposal.levels == {2: 12}
    assert sorted(name for name, _ in a.proposal.parents) == ["2022", "2023", "2024"]


def test_customer_year_project_depth_three(tmp_path):
    n = 0
    for customer in ("Stadt Musterstadt", "Landkreis Beispiel", "Gemeinde Talheim"):
        for year in ("2023", "2024"):
            for i in range(2):
                n += 1
                mk(tmp_path, f"{customer}/{year}/P{10000 + n * 37}_Schule {n}/Pläne")
    a = analyse(tmp_path)
    assert (a.proposal.pattern, a.proposal.depth, a.proposal.projects) == (r"P\d{5}", 3, 12)
    assert scan_projects(tmp_path, r"P\d{5}", 3)[0].name.startswith("Schule")
    assert scan_projects(tmp_path, r"P\d{5}", 2) == []  # the default depth would not find them


def test_mixed_lengths_are_merged(tmp_path):
    mk(tmp_path, "2024-017 Kita Regenbogen", "2024-018 Schule", "2024-1234 Rathaus", "2023-099 Halle",
       "2023-1000 Turnhalle")
    a = analyse(tmp_path)
    assert a.proposal.pattern == r"\d{4}-\d{3,4}" and a.proposal.projects == 5
    assert a.proposal.description == "4 Ziffern, Bindestrich, 3–4 Ziffern"


def test_prefix_without_separator(tmp_path):
    mk(tmp_path, "P12345_Schule", "P12346 Kita", "P20001-Rathaus", "p20002 Halle")
    a = analyse(tmp_path)
    assert a.proposal.pattern == r"P\d{5}" and a.proposal.projects == 4
    assert [p.number for p in scan_projects(tmp_path, a.proposal.pattern, a.proposal.depth)] == [
        "P12345", "P12346", "P20001", "P20002"]


@pytest.mark.parametrize("sep, regex", [("_", r"\d{4}_\d{3}"), (" ", r"\d{4}\s\d{3}"), (".", r"\d{4}\.\d{3}")])
def test_numbers_with_underscore_space_or_dot(tmp_path, sep, regex):
    mk(tmp_path, *(f"2024{sep}{i:03d} Name {i}" for i in range(1, 6)))
    a = analyse(tmp_path)
    assert a.proposal.pattern == regex
    found = scan_projects(tmp_path, regex, a.proposal.depth)
    assert [p.number for p in found][:2] == [f"2024{sep}001", f"2024{sep}002"] and found[0].name == "Name 1"


def test_mixed_separators_become_a_class(tmp_path):
    mk(tmp_path, "2024-001 A", "2024-002 B", "2024_003 C", "2024 004 D")
    assert analyse(tmp_path).proposal.pattern == r"\d{4}[-_\s]\d{3}"


def test_exceptions_are_listed(tmp_path):
    bs_projects(tmp_path / "2024", n=5)
    mk(tmp_path, "Archiv/alt/irgendwas", "_Vorlagen", "00_Allgemein", "2024/00_Allgemein")
    a = analyse(tmp_path)
    p = a.proposal
    assert (p.pattern, p.depth, p.projects) == (BS, 2, 5)
    assert sorted(p.exceptions) == ["00_Allgemein", "2024/00_Allgemein", "Archiv", "_Vorlagen"]
    assert "Archiv/alt" not in p.exceptions  # inside an exception: not listed separately


def test_exceptions_with_an_older_numbering_get_a_hint(tmp_path):
    bs_projects(tmp_path, n=8)
    mk(tmp_path, "2019-001 Altprojekt", "2019-002 Altprojekt", "Archiv")
    a = analyse(tmp_path)
    assert a.proposal.pattern == BS
    hint = a.proposal.second_scheme
    assert hint["pattern"] == r"\d{4}-\d{3}" and hint["combined"] == rf"(?:{BS}|\d{{4}}-\d{{3}})"
    assert len(scan_projects(tmp_path, hint["combined"], 1)) == 10


def test_random_names_give_no_scheme(tmp_path):
    mk(tmp_path, "Verwaltung", "Akquise", "Fotos", "Muster", "Allgemein", "Kunden", "3D-Modelle", "Haus 3")
    a = analyse(tmp_path)
    assert a.proposal is None and a.exit_code == 1
    assert "Kein Ordnername" in a.reason or "Zu wenige" in a.reason


def test_bare_years_only_give_no_scheme(tmp_path):
    mk(tmp_path, "2019", "2020", "2021", "2022", "2023/Rechnungen", "2024/Rechnungen")
    a = analyse(tmp_path)
    assert a.proposal is None
    assert "Jahreszahlen" in a.reason


def test_never_a_bare_number(tmp_path):
    """Folders starting with numbers of every length: \\d+ would match all of them – no scheme."""
    mk(tmp_path, "1 Allgemein", "22 Vorlagen", "333 Muster", "4444 Fotos", "55555 Pläne", "7 Sonstiges", "88 Alt")
    a = analyse(tmp_path)
    assert a.proposal is None and a.exit_code == 1


def test_short_numbered_subfolders_are_no_projects(tmp_path):
    mk(tmp_path, "00_Allgemein", "01_Vorlagen", "02_Schriftverkehr", "03_Pläne", "04_Fotos")
    assert analyse(tmp_path).proposal is None


def test_duplicates_are_reported(tmp_path):
    bs_projects(tmp_path, n=5)
    mk(tmp_path, "BS-2024-0003 Projekt 3 (Kopie)")
    p = analyse(tmp_path).proposal
    assert p.projects == 5 and p.duplicates == [{"number": "BS-2024-0003", "path": "BS-2024-0003 Projekt 3",
                                                  "duplicate": "BS-2024-0003 Projekt 3 (Kopie)"}]


def test_unicode_names(tmp_path):
    mk(tmp_path, "BS-2024-0001 Kita Sonnenblume – Übergang", "BS-2024-0002 Straße am Bärenzwinger",
       "BS-2024-0003 Œuvre 日本", "Ablage für Ämter")
    a = analyse(tmp_path)
    assert a.proposal.pattern == BS and a.proposal.projects == 3
    assert a.proposal.exceptions == ["Ablage für Ämter"]
    assert {e["name"] for e in a.proposal.examples} >= {"Straße am Bärenzwinger", "Œuvre 日本"}


def test_symlinks_inside_count_outside_are_skipped(tmp_path):
    root = tmp_path / "p"
    bs_projects(root, n=4)
    outside = mk(tmp_path, "fremd/BS-2099-0001 Draußen")
    os.symlink(outside / "fremd", root / "Verknüpfung nach draußen")
    os.symlink(root / "BS-2024-0001 Projekt 1", root / "BS-2024-0001 Verknüpfung")
    os.symlink(root / "fehlt", root / "BS-2024-0099 kaputt")
    a = analyse(root)
    p = a.proposal
    assert p.projects == 4
    assert [d["duplicate"] for d in p.duplicates] == ["BS-2024-0001 Verknüpfung"]
    assert "Verknüpfung nach draußen" not in p.exceptions and "BS-2024-0099" not in json.dumps(a.to_dict())


def test_unreadable_folder_is_reported(tmp_path, monkeypatch):
    bs_projects(tmp_path / "2023", n=3, year=2023)
    bs_projects(tmp_path / "2024", n=3)
    blocked = tmp_path / "2024"
    real_scandir, real_iterdir = os.scandir, Path.iterdir

    def scandir(path="."):
        if Path(path) == blocked:
            raise PermissionError(13, "Permission denied", str(path))
        return real_scandir(path)

    def iterdir(self):
        if self == blocked:
            raise PermissionError(13, "Permission denied", str(self))
        return real_iterdir(self)

    monkeypatch.setattr(structure.os, "scandir", scandir)
    monkeypatch.setattr(Path, "iterdir", iterdir)
    a = analyse(tmp_path)
    assert a.unreadable == ["2024"]
    assert a.proposal.pattern == BS and a.proposal.projects == 3 and a.proposal.unreadable == ["2024"]


def test_very_many_folders_are_bounded(tmp_path):
    names = [f"BS-2024-{i:04d} P" for i in range(1, 1201)]
    mk(tmp_path, *names, "BS-2024-0001 P/Unterordner")
    a = analyse(tmp_path, max_folders=300)
    assert a.folders <= 300 and a.truncated
    assert a.proposal.pattern == BS and a.proposal.projects == 1200  # verified with the full scan


def test_only_a_sample_of_project_folders_is_entered(tmp_path):
    for i in range(1, 201):
        mk(tmp_path, *(f"BS-2024-{i:04d} P/{sub}" for sub in ("01 Pläne", "02 Post", "2023-05-12 Termin")))
    a = analyse(tmp_path)
    assert a.proposal.pattern == BS and a.proposal.depth == 2 and a.proposal.levels == {1: 200}
    assert a.folders < 200 + 3 * 40  # 200 projects, subfolders of a sample only


def test_dated_folders_inside_projects_do_not_win(tmp_path):
    for i in range(1, 6):
        mk(tmp_path, *(f"BS-2024-{i:04d} P/{d} Ortstermin" for d in ("2023-05-12", "2023-06-01", "2023-07-15")),
           f"BS-2024-{i:04d} P/Pläne", f"BS-2024-{i:04d} P/Schriftverkehr", f"BS-2024-{i:04d} P/Fotos")
    p = analyse(tmp_path).proposal
    assert (p.pattern, p.depth, p.projects) == (BS, 2, 5) and p.levels == {1: 5}


def test_numbered_customer_folders_are_containers(tmp_path):
    for k in range(1, 4):
        bs_projects(tmp_path / f"K{k:03d} Kunde {k}", n=3, year=2020 + k)
    p = analyse(tmp_path).proposal
    assert (p.pattern, p.depth, p.projects) == (BS, 2, 9)


def test_single_stray_match_deeper_does_not_raise_the_depth(tmp_path):
    bs_projects(tmp_path / "2024", n=12)
    mk(tmp_path, "_Vorlagen/Muster/Alt/BS-0000-0000 Vorlage")
    p = analyse(tmp_path).proposal
    assert p.depth == 2 and p.projects == 12 and p.deeper == ["_Vorlagen/Muster/Alt/BS-0000-0000 Vorlage"]


def test_depth_one_when_the_default_would_find_more(tmp_path):
    bs_projects(tmp_path, n=12)
    mk(tmp_path, "_Vorlagen/BS-0000-0000 Vorlage")  # a single stray one level down
    p = analyse(tmp_path).proposal
    assert p.depth == 1 and p.projects == 12 and p.deeper == ["_Vorlagen/BS-0000-0000 Vorlage"]


def test_missing_root_and_empty_root(tmp_path):
    a = analyse(tmp_path / "fehlt")
    assert a.exit_code == 2 and "nicht vorhanden" in a.problem
    (tmp_path / "leer").mkdir()
    a = analyse(tmp_path / "leer")
    assert a.exit_code == 1 and "keine Unterordner" in a.reason


def test_analysis_is_read_only(tmp_path):
    for year in (2023, 2024):
        bs_projects(tmp_path / str(year), n=4, year=year)
    (tmp_path / "2024" / "BS-2024-0001 Projekt 1" / "a.txt").write_text("x")
    before = sorted((str(p), p.stat().st_mtime_ns) for p in tmp_path.rglob("*"))
    a = analyse(tmp_path)
    file_statistics(a.projects, tmp_path)
    assert sorted((str(p), p.stat().st_mtime_ns) for p in tmp_path.rglob("*")) == before


# ============================================================================ PROJECT_FOLDER_DEPTH and pattern settings
@pytest.mark.parametrize("raw, depth, warned", [
    ("", 2, False), ("1", 1, False), ("3", 3, False), ("6", 6, False), ("0", 2, True), ("7", 2, True),
    ("zwei", 2, True), ("auto", 2, True),
])
def test_folder_depth_setting(raw, depth, warned):
    s = load_settings({"PROJECT_FOLDER_DEPTH": raw})
    assert s.project_folder_depth == depth
    assert any(w.startswith("PROJECT_FOLDER_DEPTH=") for w in s.config_warnings) is warned


@pytest.mark.parametrize("raw, depth, warned", [("", None, False), ("auto", None, False), ("3", 3, False),
                                                ("9", None, True)])
def test_folder_depth_with_automatic_pattern(raw, depth, warned):
    s = load_settings({"PROJECT_NUMBER_PATTERN": " Auto ", "PROJECT_FOLDER_DEPTH": raw})
    assert s.project_number_pattern == "auto" and s.project_pattern_auto
    assert s.project_folder_depth == depth
    assert any(w.startswith("PROJECT_FOLDER_DEPTH=") for w in s.config_warnings) is warned


def test_scan_and_sync_use_the_depth(tmp_path):
    bs_projects(tmp_path / "Kunde" / "2024", n=3)
    assert scan_projects(tmp_path, BS) == [] and len(scan_projects(tmp_path, BS, 3)) == 3
    dbp = tmp_path / "x.sqlite3"
    db.init_db(dbp)
    with db.session(dbp) as conn:
        assert sync_from_folder(conn, tmp_path, BS, max_depth=3)["added"] == 3


def test_run_index_uses_project_folder_depth(tmp_path):
    root = tmp_path / "p"
    bs_projects(root / "Kunde" / "2024", n=3)
    assert run_index(settings_for(tmp_path, root))["projects"]["projects"] == 0
    assert run_index(settings_for(tmp_path, root, PROJECT_FOLDER_DEPTH=3))["projects"]["added"] == 3


@pytest.mark.parametrize("pattern", ["BS-(\\d{4}", "[", "a)(b", "(?P<num>\\d+)", "\\d*", "(x|)"])
def test_unusable_pattern_is_named(pattern):
    assert pattern_problem(pattern)
    s = load_settings({"PROJECT_NUMBER_PATTERN": pattern})
    assert s.project_number_pattern == pattern
    assert any(w.startswith(f"PROJECT_NUMBER_PATTERN={pattern} ist unbrauchbar") for w in s.config_warnings)


def test_valid_patterns_pass():
    for pattern in (BS, r"P\d{5}", r"\d{4}[-_\s]\d{3}", r"(?:BS|BV)-\d{4}-\d{4}", DEFAULT_PROJECT_NUMBER_PATTERN):
        assert pattern_problem(pattern) is None


def test_invalid_pattern_does_not_crash_and_changes_nothing(tmp_path, caplog):
    root = tmp_path / "p"
    bs_projects(root, n=3)
    run_index(settings_for(tmp_path, root))
    bad = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN="BS-(\\d{4}")
    caplog.set_level(logging.INFO)
    stats = run_index(bad)
    assert "unbrauchbar" in stats["projects"]["warning"] and stats["projects"]["projects"] == 3
    assert stats["documents"] == {"note": "nicht abgeglichen – Projektordner nicht verfügbar"}
    assert stats["assignment"]["note"].startswith("nicht ausgeführt")
    with db.session(bad.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM projects WHERE active = 1").fetchone()[0] == 3
    client = TestClient(create_app(bad, background=False))
    assert "Konfiguration: PROJECT_NUMBER_PATTERN=BS-(\\d{4} ist unbrauchbar" in caplog.text
    page = client.get("/status").text
    assert "Nummernschema" in page and "ist unbrauchbar" in page
    assert client.get("/suche", params={"q": "BS 2024 0001"}).status_code == 200


def test_sync_from_folder_refuses_an_unusable_pattern(tmp_path):
    dbp = tmp_path / "x.sqlite3"
    db.init_db(dbp)
    with db.session(dbp) as conn:
        stats = sync_from_folder(conn, tmp_path, "[")
    assert "unbrauchbar" in stats["warning"] and stats["added"] == 0


# ============================================================================ automatic mode
def _meta(s, key):
    with db.session(s.db_path) as conn:
        return db.get_meta(conn, key)


def _age_check(s, hours=2):
    with db.session(s.db_path) as conn:
        when = datetime.now(timezone.utc) - timedelta(hours=hours)
        db.set_meta(conn, structure.CHECKED_KEY, when.isoformat(timespec="seconds"))


def _active(s) -> list[str]:
    with db.session(s.db_path) as conn:
        return [r[0] for r in conn.execute("SELECT number FROM projects WHERE active = 1 ORDER BY number")]


def test_auto_detects_once_and_stores(tmp_path, caplog):
    root = tmp_path / "p"
    mk(root, *(f"2024/2024-{i:03d} Projekt {i}" for i in range(1, 5)), "Archiv")
    s = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN="auto")
    caplog.set_level(logging.INFO, logger="projektakte.structure")
    stats = run_index(s)
    assert stats["projects"]["added"] == 4 and stats["structure"]["detected"] is True
    stored = json.loads(_meta(s, structure.STORED_KEY))
    assert (stored["pattern"], stored["depth"], stored["projects"]) == (r"\d{4}-\d{3}", 2, 4)
    assert stored["root"] == os.path.abspath(root) and stored["examples"][0] == "2024-001 Projekt 1"
    assert "automatisch erkannt und gespeichert" in caplog.text
    layout = structure.current_layout(s)
    assert layout.pattern == r"\d{4}-\d{3}" and layout.depth == 2 and layout.source.startswith("automatisch erkannt am")
    # status page and start-up line show it
    client = TestClient(create_app(s, background=False))
    page = client.get("/status").text
    assert r"<code>\d{4}-\d{3}</code>" in page and "automatisch erkannt am" in page
    assert client.get("/api/status").json()["layout"]["pattern"] == r"\d{4}-\d{3}"
    caplog.clear()
    caplog.set_level(logging.INFO, logger="projektakte.startup")
    log_startup(s, NoAuth())
    assert r"Projektnummern=\d{4}-\d{3} (automatisch erkannt am" in caplog.text
    assert "Tiefe 2 (automatisch)" in caplog.text


def test_auto_later_different_detection_keeps_the_stored_one_and_warns(tmp_path, caplog):
    root = tmp_path / "p"
    mk(root, *(f"2024-{i:03d} Projekt {i}" for i in range(1, 5)))
    s = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN="auto")
    run_index(s)
    stored_before = _meta(s, structure.STORED_KEY)
    # the share now shows a second numbering as well (new projects, or another share mounted there)
    mk(root, *(f"P{20000 + i}_Neubau {i}" for i in range(1, 9)))
    _age_check(s)
    caplog.set_level(logging.WARNING)
    stats = run_index(s)
    new = r"(?:P\d{5}|\d{4}-\d{3})"
    assert _meta(s, structure.STORED_KEY) == stored_before
    assert stats["structure"]["differs"] == new and "warning" not in stats["structure"]
    assert stats["projects"]["added"] == 0 and len(_active(s)) == 4  # still the stored scheme
    assert f"eine neue Erkennung ergibt {new}" in caplog.text
    layout = structure.current_layout(s)
    assert layout.pattern == r"\d{4}-\d{3}" and new in layout.warning and "--uebernehmen" in layout.warning
    assert "findet derzeit 4 Projektordner" in layout.warning
    page = TestClient(create_app(s, background=False)).get("/status").text
    assert "Abweichung" in page and "--uebernehmen" in page
    # pending: checked at every pass, warned only once
    caplog.clear()
    run_index(s)
    assert "eine neue Erkennung" not in caplog.text and structure.current_layout(s).differs


def test_auto_difference_disappears_when_the_share_is_back(tmp_path):
    root = tmp_path / "p"
    mk(root, *(f"2024-{i:03d} Projekt {i}" for i in range(1, 5)))
    s = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN="auto")
    run_index(s)
    extra = [f"P{20000 + i}_Neubau {i}" for i in range(1, 9)]
    mk(root, *extra)
    _age_check(s)
    run_index(s)
    assert structure.current_layout(s).differs
    for name in extra:
        (root / name).rmdir()
    run_index(s)
    assert structure.current_layout(s).differs is None and _meta(s, structure.DIFFERS_KEY) is None


def test_auto_half_mounted_empty_share_changes_nothing(tmp_path):
    root = tmp_path / "p"
    mk(root, *(f"2024-{i:03d} Projekt {i}" for i in range(1, 5)))
    s = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN="auto")
    run_index(s)
    stored = _meta(s, structure.STORED_KEY)
    for p in list(root.iterdir()):
        p.rmdir()
    _age_check(s)
    stats = run_index(s)
    assert "keine Projekte mehr" in stats["projects"]["warning"]
    assert _meta(s, structure.STORED_KEY) == stored and _meta(s, structure.DIFFERS_KEY) is None
    assert len(_active(s)) == 4


def test_auto_unmounted_share_before_any_detection(tmp_path):
    s = settings_for(tmp_path, tmp_path / "nicht-eingebunden", PROJECT_NUMBER_PATTERN="auto")
    stats = run_index(s)
    assert "nicht vorhanden" in stats["projects"]["warning"] and _meta(s, structure.STORED_KEY) is None


def test_auto_without_a_clear_scheme_skips_the_sync_and_the_assignment(tmp_path):
    root = mk(tmp_path / "p", "Verwaltung", "Akquise", "Fotos")
    s = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN="auto")
    _eml(s.mail_dir / "a.eml", "Frage zu 2024-017", "Hallo")
    stats = run_index(s)
    assert "Kein eindeutiges Projektnummern-Schema" in stats["projects"]["warning"]
    assert "python -m projektakte.structure" in stats["projects"]["warning"]
    assert stats["assignment"]["note"].startswith("nicht ausgeführt")
    with db.session(s.db_path) as conn:
        assert conn.execute("SELECT assign_status FROM mails").fetchone()[0] == "pending"
    assert _meta(s, structure.STORED_KEY) is None
    page = TestClient(create_app(s, background=False)).get("/status").text
    assert "noch nicht erkannt" in page
    # once the folders are there, the next pass detects, syncs and assigns
    mk(root, "2024-017 Kita", "2024-018 Schule", "2024-019 Halle")
    stats = run_index(s)
    assert stats["projects"]["added"] == 3
    with db.session(s.db_path) as conn:
        assert tuple(conn.execute("SELECT project, assign_status FROM mails").fetchone()) == ("2024-017", "assigned")


def test_accept_command_switches(tmp_path, monkeypatch, capsys):
    root = tmp_path / "p"
    mk(root, *(f"2024-{i:03d} Projekt {i}" for i in range(1, 4)))
    s = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN="auto")
    run_index(s)
    for p in list(root.iterdir()):
        p.rename(root / p.name.replace("2024-", "P10"))  # the office renamed everything: P10001 …
    _age_check(s)
    run_index(s)
    assert structure.current_layout(s).differs["pattern"] == r"P\d{5}"
    monkeypatch.setenv("PROJECTS_DIR", str(root))
    monkeypatch.setenv("DATA_DIR", str(s.data_dir))
    monkeypatch.setenv("PROJECT_NUMBER_PATTERN", "auto")
    assert main(["--uebernehmen", "--ohne-dateien"]) == 0
    out = capsys.readouterr().out
    assert "Übernommen: P\\d{5}, Tiefe 2" in out
    layout = structure.current_layout(s)
    assert layout.pattern == r"P\d{5}" and layout.differs is None
    stats = run_index(s)
    assert stats["projects"]["added"] == 3 and _active(s) == ["P10001", "P10002", "P10003"]


def test_accept_refuses_another_folder(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path / "a"))
    with pytest.raises(SystemExit) as exc:
        main([str(tmp_path / "b"), "--uebernehmen"])
    assert exc.value.code == 2 and "PFAD weglassen" in capsys.readouterr().err


def test_detection_for_another_projects_dir_is_not_used(tmp_path):
    """E.g. the demo data set detected with "auto" before the switch to the real share."""
    first = mk(tmp_path / "demo", *(f"BS-2024-{i:04d} X" for i in range(1, 4)))
    run_index(settings_for(tmp_path, first, PROJECT_NUMBER_PATTERN="auto"))
    real = mk(tmp_path / "echt", *(f"P{30000 + i} Y" for i in range(1, 4)))
    s = settings_for(tmp_path, real, PROJECT_NUMBER_PATTERN="auto")
    assert structure.current_layout(s).pattern is None
    run_index(s)
    assert structure.current_layout(s).pattern == r"P\d{5}"


def test_configured_depth_wins_over_the_detected_one(tmp_path):
    root = mk(tmp_path / "p", *(f"2024/BS-2024-{i:04d} X" for i in range(1, 4)), "Archiv/2010/BS-2010-0001 Alt")
    s = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN="auto", PROJECT_FOLDER_DEPTH="3")
    run_index(s)
    layout = structure.current_layout(s)
    assert layout.depth == 3 and not layout.depth_auto and len(_active(s)) == 4


def test_recheck_waits_for_the_interval(tmp_path, monkeypatch):
    root = mk(tmp_path / "p", *(f"2024-{i:03d} X" for i in range(1, 4)))
    s = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN="auto")
    run_index(s)
    calls = []
    real = structure.analyse
    monkeypatch.setattr(structure, "analyse", lambda *a, **k: calls.append(1) or real(*a, **k))
    with db.session(s.db_path) as conn:
        layout_for_index(conn, s)
        assert calls == []
        layout_for_index(conn, s, now=datetime.now(timezone.utc) + structure.RECHECK_INTERVAL * 2)
        assert calls == [1]


# ============================================================================ effective scheme everywhere
def _eml(path: Path, subject: str, body: str) -> None:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "Bauamt <bauamt@stadt.example>", "buero@ib.example", subject
    m["Date"] = "Tue, 18 Jun 2024 10:30:00 +0200"
    m["Message-ID"] = f"<{path.stem}@stadt.example>"
    m.set_content(body)
    path.write_bytes(bytes(m))


def test_effective_pattern_reaches_assignment_search_and_integration(tmp_path, fake_ai):
    root = tmp_path / "p"
    for i, name in enumerate(("Schule Nord", "Kita Süd", "Rathaus"), start=1):
        folder = mk(root, f"P1234{i}_{name}") / f"P1234{i}_{name}"
        (folder / "Bericht.txt").write_text(f"Brandschutzbericht zu P1234{i} {name}: Rettungswege geprüft.",
                                            encoding="utf-8")
    mail = tmp_path / "mails"
    mail.mkdir()
    _eml(mail / "a.eml", "Rückfrage zu P12342", "Bitte die Rettungswege prüfen.")
    s = fake_settings(fake_ai, tmp_path, PROJECTS_DIR=root, MAIL_DIR=mail, PROJECT_NUMBER_PATTERN="auto")
    run_index(s, AIServices.from_settings(s))
    assert structure.current_layout(s).pattern == r"P\d{5}"
    with db.session(s.db_path) as conn:
        row = conn.execute("SELECT project, assign_status, assign_method FROM mails").fetchone()
    assert tuple(row) == ("P12342", "assigned", "subject")
    client = TestClient(create_app(s, background=False))
    page = client.get("/suche", params={"q": "P 12341"}).text
    assert 'href="/projekte/P12341"' in page and "Zum Projekt P12341" in page
    assert "<mark>P12341</mark>" in page
    res = client.post("/api/integration/suggest", json={"subject": "Termin P12343", "body": "Hallo"}).json()
    assert res["suggestions"][0]["number"] == "P12343" and res["suggestions"][0]["confidence"] == "sicher"


def test_ui_pattern_before_anything_is_detected(tmp_path):
    s = settings_for(tmp_path, tmp_path / "leer", PROJECT_NUMBER_PATTERN="auto")
    assert structure.number_pattern(s) == structure.NO_MATCH
    client = TestClient(create_app(s, background=False))
    assert client.get("/suche", params={"q": "BS 2024 0137"}).status_code == 200
    assert web.join_spaced_codes("BS 2024 0137", structure.NO_MATCH) == "BS 2024 0137"


def test_code_pattern_of_is_looked_up_once_per_request(tmp_path, monkeypatch):
    from types import SimpleNamespace

    calls = []
    real = structure.number_pattern
    monkeypatch.setattr(structure, "number_pattern", lambda s, conn=None: calls.append(1) or real(s, conn))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=load_settings({}))),
                              state=SimpleNamespace())
    assert web.code_pattern_of(request) == DEFAULT_PROJECT_NUMBER_PATTERN
    assert web.code_pattern_of(request) == DEFAULT_PROJECT_NUMBER_PATTERN and calls == [1]


def test_projects_named_in_uses_the_scheme(tmp_path):
    dbp = tmp_path / "x.sqlite3"
    db.init_db(dbp)
    with db.session(dbp) as conn:
        conn.execute("INSERT INTO projects(number, name, path) VALUES('P12345', 'Schule', '/x'), "
                     "('2024 017', 'Kita', '/y')")
        assert [p["number"] for p in web.projects_named_in(conn, "Pläne P12345", r"P\d{5}")] == ["P12345"]
        assert [p["number"] for p in web.projects_named_in(conn, "Akte 2024 017 Kita", r"\d{4}\s\d{3}")] == ["2024 017"]
        assert web.projects_named_in(conn, "Pläne P12345", structure.NO_MATCH) == []


def test_status_page_and_startup_show_a_configured_layout(tmp_path, caplog):
    s = settings_for(tmp_path, tmp_path / "p", PROJECT_FOLDER_DEPTH="3")
    caplog.set_level(logging.INFO, logger="projektakte.startup")
    client = TestClient(create_app(s, background=False))
    assert r"Projektnummern=BS-\d{4}-\d{4} (eingestellt), Tiefe 3" in caplog.text
    page = client.get("/status").text
    assert r"<code>BS-\d{4}-\d{4}</code> – eingestellt" in page and "bis zu 2 Ebenen darunter" in page
    assert client.get("/api/status").json()["layout"]["depth"] == 3


# ============================================================================ file and context statistics
def test_file_statistics_name_what_is_not_read(tmp_path):
    root = tmp_path / "p"
    for i in range(1, 4):
        folder = mk(root, f"BS-2024-{i:04d} P/Unterordner") / f"BS-2024-{i:04d} P"
        (folder / "Bericht.pdf").write_bytes(b"%PDF-1.4")
        (folder / "Unterordner" / "Notiz.txt").write_text("x")
        (folder / "Kalkulation.xlsx").write_bytes(b"x" * 10)
        (folder / "Mail.msg").write_bytes(b"x" * 20)
        (folder / "Plan.dwg").write_bytes(b"x" * 30)
        (folder / "~$Bericht.docx").write_bytes(b"lock")
        (folder / ".versteckt.pdf").write_bytes(b"x")
    a = analyse(root)
    f = file_statistics(a.projects, root)
    assert (f["files"], f["readable"], f["not_readable"]) == (15, 6, 9)
    assert [e["ext"] for e in f["unsupported"]] == [".dwg", ".msg", ".xlsx"]
    assert f["per_project"][0] == {"number": "BS-2024-0001", "files": 5, "bytes": 8 + 1 + 10 + 20 + 30,
                                   "readable": 2, "too_big": 0}
    bounded = file_statistics(a.projects, root, max_files=4)
    assert bounded["truncated"] and bounded["files"] == 4


def test_context_statistics_from_the_database(tmp_path):
    root = tmp_path / "p"
    text = "\n\n".join(f"Abschnitt {i}: Die Rettungswege im Geschoss {i} sind baulich gesichert und "
                       f"führen über zwei notwendige Treppenräume ins Freie." for i in range(60))
    (mk(root, "BS-2024-0001 Gross") / "BS-2024-0001 Gross" / "Bericht.txt").write_text(text, encoding="utf-8")
    (mk(root, "BS-2024-0002 Klein") / "BS-2024-0002 Klein" / "Notiz.txt").write_text("Kurz.", encoding="utf-8")
    mk(root, "BS-2024-0003 Leer")
    s = settings_for(tmp_path, root)
    run_index(s)
    with db.session(s.db_path) as conn:
        chunked = conn.execute("SELECT SUM(LENGTH(text)), COUNT(*) FROM chunks "
                               "WHERE project = 'BS-2024-0001'").fetchone()
    c = context_statistics(s.db_path)
    big = next(p for p in c["projects"] if p["number"] == "BS-2024-0001")
    assert chunked[1] > 3 and big["chars"] < chunked[0]  # the repeated overlap counts once
    assert 0.9 * len(text) <= big["chars"] <= 1.02 * len(text)
    assert big["tokens"] == structure.tokens_for(big["chars"]) and big["documents"] == 1
    assert big["fits"] == {"32k": True, "128k": True, "1M": True}
    assert c["summary"]["projects"] == 3 and c["summary"]["documents_with_text"] == 2
    assert c["projects"][-1]["tokens"] == 0
    assert context_statistics(tmp_path / "fehlt.sqlite3") is None


def test_fits_keeps_room_for_question_and_answer():
    assert structure.fits(28_000) == {"32k": True, "128k": True, "1M": True}
    assert structure.fits(28_001) == {"32k": False, "128k": True, "1M": True}
    assert structure.fits(2_000_000) == {"32k": False, "128k": False, "1M": False}
    assert structure.tokens_for(350) == 100


# ============================================================================ command line
def test_cli_text_output_and_exit_code(tmp_path, monkeypatch, capsys):
    root = tmp_path / "p"
    for year in (2023, 2024):
        mk(root, *(f"{year}/{year}-{i:03d} Projekt" for i in range(1, 5)))
    (root / "2024" / "2024-001 Projekt" / "Liste.xlsx").write_bytes(b"x")
    mk(root, "_Vorlagen")
    monkeypatch.setenv("PROJECTS_DIR", str(root))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "Ausdruck:  \\d{4}-\\d{3}" in out and "Tiefe:     2" in out and "8 gefunden" in out
    assert "PROJECT_NUMBER_PATTERN=\\d{4}-\\d{3}\n  PROJECT_FOLDER_DEPTH=2" in out
    assert "Ausnahmen" in out and "_Vorlagen" in out
    assert "Aktuelle Einstellung: PROJECT_NUMBER_PATTERN=BS-\\d{4}-\\d{4}, PROJECT_FOLDER_DEPTH=2" in out
    assert "findet 0 Projektordner" in out and ".xlsx" in out and "keine Datenbank" in out


def test_cli_no_scheme_and_unreadable(tmp_path, capsys):
    mk(tmp_path / "p", "Verwaltung", "Fotos")
    assert main([str(tmp_path / "p")]) == 1
    assert "Kein eindeutiges Projektnummern-Schema" in capsys.readouterr().out
    assert main([str(tmp_path / "fehlt")]) == 2
    assert "FEHLER" in capsys.readouterr().out


def test_cli_json_with_database(tmp_path, monkeypatch, capsys):
    root = tmp_path / "p"
    for i in range(1, 4):
        (mk(root, f"P{40000 + i} X") / f"P{40000 + i} X" / "a.txt").write_text("Rettungsweg " * 50, encoding="utf-8")
    s = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN=r"P\d{5}", PROJECT_FOLDER_DEPTH="1")
    run_index(s)
    monkeypatch.setenv("PROJECT_NUMBER_PATTERN", r"P\d{5}")
    monkeypatch.setenv("PROJECT_FOLDER_DEPTH", "1")
    assert main([str(root), "--json", "--data-dir", str(s.data_dir), "--tiefe", "2"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["exit_code"] == 0 and data["proposal"]["pattern"] == r"P\d{5}" and data["max_depth"] == 2
    assert data["env"] == [r"PROJECT_NUMBER_PATTERN=P\d{5}", "PROJECT_FOLDER_DEPTH=2"]
    assert data["current"]["projects"] == 3 and data["files"]["files"] == 3
    assert data["context"]["summary"]["projects"] == 3 and data["context"]["projects"][0]["tokens"] > 100
    assert data["accepted"] is None


def test_cli_text_shows_the_context_table(tmp_path, capsys):
    root = tmp_path / "p"
    for i in range(1, 4):
        (mk(root, f"BS-2024-{i:04d} X") / f"BS-2024-{i:04d} X" / "a.txt").write_text("Brandwand " * 300)
    s = settings_for(tmp_path, root)
    run_index(s)
    assert main([str(root), "--data-dir", str(s.data_dir), "--ohne-dateien"]) == 0
    out = capsys.readouterr().out
    assert "Textmenge im Index" in out and "32k" in out and "128k" in out and "1M" in out
    assert "passen ganz hinein: 32k: 3 von 3" in out and "Dateien in" not in out


def test_cli_rejects_a_bad_depth(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--tiefe", "9"])
    assert exc.value.code == 2


def test_database_never_written_by_the_analysis(tmp_path, monkeypatch, capsys):
    root = mk(tmp_path / "p", *(f"BS-2024-{i:04d} X" for i in range(1, 4)))
    s = settings_for(tmp_path, root)
    run_index(s)
    before = Path(s.db_path).stat().st_mtime_ns
    with sqlite3.connect(s.db_path) as conn:
        rows = conn.execute("SELECT key, value FROM meta ORDER BY key").fetchall()
    monkeypatch.setenv("DATA_DIR", str(s.data_dir))
    assert main([str(root)]) == 0
    capsys.readouterr()
    assert Path(s.db_path).stat().st_mtime_ns == before
    with sqlite3.connect(s.db_path) as conn:
        assert conn.execute("SELECT key, value FROM meta ORDER BY key").fetchall() == rows


def test_dated_folders_are_no_project_scheme(tmp_path):
    mk(tmp_path, "2023-05-12 Ortstermin", "2023-06-01 Abnahme", "2023-07-15 Begehung", "12.08.2023 Termin",
       "2024-01-09 Besprechung")
    a = analyse(tmp_path)
    assert a.proposal is None and "Datumsangaben" in a.reason


def test_word_prefix_needs_more_digits(tmp_path):
    mk(tmp_path, "Haus 12", "Haus 13", "Haus 14", "Haus 15")
    assert analyse(tmp_path).proposal is None
    mk(tmp_path / "b", "PRJ 2024-001 A", "PRJ 2024-002 B", "PRJ 2024-003 C")
    assert analyse(tmp_path / "b").proposal.pattern == r"PRJ\s\d{4}-\d{3}"


def test_pattern_setting_ignores_stray_blanks():
    assert load_settings({"PROJECT_NUMBER_PATTERN": " P\\d{5} "}).project_number_pattern == r"P\d{5}"


def test_current_and_older_numbering_are_combined(tmp_path):
    """Current projects in the root, an archive in an older numbering: both are projects."""
    bs_projects(tmp_path, n=10)
    mk(tmp_path, *(f"Archiv/1998-{i:03d} Alt {i}" for i in range(1, 31)), "_Vorlagen/KW01", "_Vorlagen/KW02",
       "_Vorlagen/KW03")
    p = analyse(tmp_path).proposal
    assert p.pattern == rf"(?:\d{{4}}-\d{{3}}|{BS})" and p.depth == 2 and p.projects == 40
    assert " – oder – " in p.description and p.exceptions == ["_Vorlagen"]


def test_detection_failure_never_stops_the_indexing(tmp_path, monkeypatch):
    root = mk(tmp_path / "p", *(f"2024-{i:03d} X" for i in range(1, 4)))
    s = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN="auto")
    run_index(s)

    def broken(*a, **k):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(structure, "analyse", broken)
    _age_check(s)
    stats = run_index(s)
    assert stats["projects"]["projects"] == 3 and "warning" not in stats["projects"]
    assert structure.current_layout(s).pattern == r"\d{4}-\d{3}"
    s2 = settings_for(tmp_path / "neu", root, PROJECT_NUMBER_PATTERN="auto")
    assert "fehlgeschlagen" in run_index(s2)["projects"]["warning"]
