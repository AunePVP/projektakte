"""Folder layout of PROJECTS_DIR (structure.py): detection of the project number scheme and depth on
synthetic layouts (one test per rule and per case of the adversarial review: years with pure-digit
numbers, year ranges, internal numbering, offers and invoices, outliers, links, archives …),
PROJECT_FOLDER_DEPTH, the automatic mode (a detection is a proposal that is used only once a person
confirmed it, never switched on its own, retried at most hourly), an unusable PROJECT_NUMBER_PATTERN,
the effective scheme in assignment, search and status page, and the command line tool. The
confirmation itself (status page, CSRF, evidence) is tested in test_struktur_bestaetigung.py."""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
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
from projektakte.registry import number_regex, scan_projects, sync_from_folder
from projektakte.startup import log_startup
from projektakte.structure import analyse, context_statistics, file_statistics, layout_for_index, main

from .conftest import fake_settings

BS = r"BS-\d{4}-\d{4}"
YEAR_NUM = r"\d{4}-\d{3}"
FOUR = r"(?!(?:19|20)\d\d(?!\d))\d{4}"  # four digits, but never a year (rule 5)


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


def found(p) -> tuple:
    return p.pattern, p.depth, p.projects


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
    assert found(a.proposal) == (BS, 2, 12)
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
    assert found(a.proposal) == (r"P\d{5}", 3, 12)
    assert scan_projects(tmp_path, r"P\d{5}", 3)[0].name.startswith("Schule")
    assert scan_projects(tmp_path, r"P\d{5}", 2) == []  # the default depth would not find them


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
    projects = scan_projects(tmp_path, regex, a.proposal.depth)
    assert [p.number for p in projects][:2] == [f"2024{sep}001", f"2024{sep}002"] and projects[0].name == "Name 1"


@pytest.mark.parametrize("sep, regex", [(".", r"\d{4}\.\d{3}"), (" ", r"\d{4}\s\d{3}")])
def test_separators_in_year_folders(tmp_path, sep, regex):
    """Review case M: 2023/2023.001 Projekt, 2023/2023 001 Projekt."""
    mk(tmp_path, *(f"{y}/{y}{sep}{i:03d} Projekt" for y in (2023, 2024) for i in range(1, 15)))
    assert found(analyse(tmp_path).proposal) == (regex, 2, 28)


def test_mixed_lengths_backed_by_many_folders_are_merged(tmp_path):
    """Rule 5: a digit count variant with real backing widens the scheme."""
    mk(tmp_path, *(f"2023-{i:03d} Alt {i}" for i in range(1, 21)), *(f"2024-{i:04d} Neu {i}" for i in range(1, 11)))
    a = analyse(tmp_path)
    # \d{4}-\d{4} could also be a year range (2017-2018): those are kept out by a guard
    guard = r"(?!(?:19|20)\d\d-(?:19|20)\d\d(?!\d))"
    assert found(a.proposal) == (guard + r"\d{4}-\d{3,4}", 2, 30) and a.proposal.outliers == []
    assert a.proposal.description == "4 Ziffern, Bindestrich, 3–4 Ziffern (kein Jahresbereich)"
    mk(tmp_path, "2017-2018 Altakten")
    assert analyse(tmp_path).proposal.projects == 30


def test_mixed_separators_backed_by_many_folders_become_a_class(tmp_path):
    mk(tmp_path, *(f"2024-{i:03d} A" for i in range(1, 11)), *(f"2024_{i:03d} B" for i in range(11, 15)),
       *(f"2024 {i:03d} C" for i in range(15, 19)))
    assert analyse(tmp_path).proposal.pattern == r"\d{4}[-_\s]\d{3}"


def test_single_outliers_do_not_widen_the_scheme(tmp_path):
    """Review cases D and Q: '2015-2018 Altakten', '2024-05 Teamsitzung' and a single longer number
    next to 40 projects stay exceptions – the scheme is not widened to \\d{4}-\\d{2,4}."""
    mk(tmp_path, *(f"2024-{i:03d} Projekt {i}/Pläne" for i in range(1, 41)), "2015-2018 Altakten/irgendwas",
       "2024-05 Teamsitzung", "2024-11 Weihnachtsfeier", "2024-1234 Sonderfall", "_Vorlagen")
    p = analyse(tmp_path).proposal
    assert found(p) == (YEAR_NUM, 2, 40)
    assert p.outliers == ["2024-1234 Sonderfall"]
    assert {"2015-2018 Altakten", "2024-05 Teamsitzung", "2024-11 Weihnachtsfeier", "2024-1234 Sonderfall"} <= set(
        p.exceptions)
    assert all(not e["path"].startswith(("2015-2018", "2024-05 ", "2024-11 ")) for e in p.examples)


def test_exceptions_are_listed(tmp_path):
    bs_projects(tmp_path / "2024", n=5)
    mk(tmp_path, "Archiv/alt/irgendwas", "_Vorlagen", "00_Allgemein", "2024/00_Allgemein")
    a = analyse(tmp_path)
    p = a.proposal
    assert found(p) == (BS, 2, 5)
    assert sorted(p.exceptions) == ["00_Allgemein", "2024/00_Allgemein", "Archiv", "_Vorlagen"]
    assert "Archiv/alt" not in p.exceptions  # inside an exception: not listed separately


def test_random_names_give_no_scheme(tmp_path):
    mk(tmp_path, "Verwaltung", "Akquise", "Fotos", "Muster", "Allgemein", "Kunden", "3D-Modelle", "Haus 3")
    a = analyse(tmp_path)
    assert a.proposal is None and a.exit_code == 1
    assert "Zu wenige Ordner" in a.reason


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
    a = analyse(tmp_path)
    assert a.proposal is None and "kurze Gliederungsnummern" in a.reason


def test_three_digit_numbers_without_letters_are_too_short(tmp_path):
    mk(tmp_path, *(f"{100 + i} Projekt {i}" for i in range(20)))
    a = analyse(tmp_path)
    assert a.proposal is None and "mindestens 4 Ziffern" in a.reason


def test_duplicates_are_reported(tmp_path):
    bs_projects(tmp_path, n=5)
    mk(tmp_path, "BS-2024-0003 Projekt 3 (Kopie)")
    p = analyse(tmp_path).proposal
    assert p.projects == 5 and p.duplicates == [{"number": "BS-2024-0003", "path": "BS-2024-0003 Projekt 3",
                                                  "duplicate": "BS-2024-0003 Projekt 3 (Kopie)"}]


@pytest.mark.parametrize("dups, proposed", [(1, True), (6, False)])
def test_duplicates_in_the_main_scheme_never_hand_over_to_the_archive(tmp_path, dups, proposed):
    """Review case L with colliding numbers: a few duplicates are listed, many make the result unsure
    (exit 1, examples named) – but the archive's older numbering is never proposed instead."""
    mk(tmp_path, *(f"Kunden/{c}/2024/2024-{k * 20 + i:03d} Projekt" for k, c in enumerate(("Stadt", "Kreis"))
                   for i in range(1, 21)))
    mk(tmp_path, *(f"Kunden/Gemeinde/2024/2024-{i:03d} Kopie" for i in range(1, dups + 1)))
    mk(tmp_path, *(f"Archiv/2010/10-{i:03d} Alt" for i in range(1, 15)))
    a = analyse(tmp_path)
    if proposed:
        assert found(a.proposal) == (YEAR_NUM, 4, 40) and len(a.proposal.duplicates) == 1
    else:
        assert a.proposal is None and "Viele doppelte Nummern" in a.reason
        assert "Kunden/Gemeinde/2024/2024-001 Kopie" in a.reason


def test_unclear_main_numbering_never_hands_over_to_the_archive(tmp_path):
    """Numbers that restart for every customer are no scheme – the archive's numbering is not
    proposed instead of them."""
    mk(tmp_path, *(f"Kunden/{c}/2024-{i:03d} Projekt" for c in ("Stadt", "Kreis", "Gemeinde") for i in range(1, 21)))
    mk(tmp_path, *(f"Archiv/2010/10-{i:03d} Alt" for i in range(1, 15)))
    a = analyse(tmp_path)
    assert a.proposal is None and "wiederholen sich" in a.reason and "ältere Nummerierung" in a.reason


def test_archive_at_the_same_level_is_no_variant(tmp_path):
    """Kunden/Stadt/2024-017 … and Archiv/2010/10-001 … lie at the same depth: the archive's numbers
    are a scheme of their own, never a 'variant' that widens the current one to \\d{2,4}-\\d{3}."""
    mk(tmp_path, *(f"Kunden/{c}/2024-{k * 20 + i:03d} Projekt" for k, c in enumerate(("Stadt", "Kreis", "Gemeinde"))
                   for i in range(1, 21)))
    mk(tmp_path, *(f"Archiv/2010/10-{i:03d} Alt" for i in range(1, 15)))
    p = analyse(tmp_path).proposal
    assert found(p) == (YEAR_NUM, 3, 60) and p.second_scheme["pattern"] == r"\d{2}-\d{3}"


def test_numbering_widened_in_a_later_year_is_one_scheme(tmp_path):
    """2023/2023-017 … and 2024/2024-1234 …: sibling year folders of one numbering are compared as one
    level, so the longer numbers are a backed variant (rule 5)."""
    mk(tmp_path, *(f"2023/2023-{i:03d} P" for i in range(1, 21)), *(f"2024/2024-{i:04d} P" for i in range(1, 21)))
    p = analyse(tmp_path).proposal
    assert p.pattern.endswith(r"\d{4}-\d{3,4}") and p.projects == 40


def test_a_large_unclear_numbering_is_a_rival(tmp_path):
    """300 folders with three-digit numbers (too short for a scheme) and 10 BS folders: the 10 are
    not proposed as the projects of this share."""
    mk(tmp_path, *(f"{100 + i} Projekt {i}" for i in range(300)), *(f"BS-2024-{i:04d} X" for i in range(1, 11)))
    a = analyse(tmp_path)
    assert a.proposal is None and "etwa gleich oft" in a.reason and "mindestens 4 Ziffern" in a.reason


def test_numbers_restarting_in_every_year_are_no_scheme(tmp_path):
    """Rule 5: numbers must be unique – 2023/0001 and 2024/0001 cannot both be project 0001."""
    mk(tmp_path, *(f"{y}/{i:04d} Projekt" for y in (2022, 2023, 2024) for i in range(1, 11)))
    a = analyse(tmp_path)
    assert a.proposal is None and "wiederholen sich" in a.reason


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
    assert p.duplicates == []  # the same folder through a link is no second project
    assert "Verknüpfung nach draußen" not in p.exceptions and "BS-2024-0099" not in json.dumps(a.to_dict())


def test_link_to_an_ancestor_and_loops_are_listed_once(tmp_path):
    """Review s7b/s7c: 'Aktuell' → 2024 inside the share, a loop to the root and one to its own
    parent: every real folder is read once (rule 8), the scheme is found as the app's scan finds it."""
    root = mk(tmp_path / "p", *(f"{y}/{y}-{i:03d} Projekt" for y in (2022, 2023, 2024) for i in range(1, 21)))
    os.symlink("2024", root / "Aktuell")
    os.symlink(root, root / "2024" / "Schleife")
    os.symlink(root / "2023", root / "2023" / "loop2")
    a = analyse(root)
    assert found(a.proposal) == (YEAR_NUM, 2, 60) and a.proposal.duplicates == []
    assert a.folders == 3 + 60  # nothing listed twice
    assert len(scan_projects(root, YEAR_NUM, 2, duplicates=[])) == 60


def test_link_listed_before_its_target_gives_way(tmp_path):
    """'0_Aktuell' sorts before '2024': the folder on its real path is kept, the link dropped."""
    root = mk(tmp_path / "p", *(f"{y}/{y}-{i:03d} Projekt" for y in (2023, 2024) for i in range(1, 11)))
    os.symlink(root / "2024", root / "0_Aktuell")
    p = analyse(root).proposal
    assert found(p) == (YEAR_NUM, 2, 20) and p.duplicates == []


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
    assert a.folders == 200 + 3 * structure.SAMPLE_MEMBERS  # 200 projects, subfolders of a sample only
    assert a.listings == 1 + structure.SAMPLE_MEMBERS


def test_share_without_numbers_is_read_bounded(tmp_path):
    """No scheme at all: every level opens at most MAX_OPEN_PER_LEVEL folders (evenly spread)."""
    for i in range(1500):
        mk(tmp_path, f"Projekt {chr(65 + i % 26)}{chr(65 + i // 26 % 26)}{chr(65 + i // 676)}/Pläne/Grundrisse")
    a = analyse(tmp_path)
    assert a.proposal is None and a.reason == "Kein Ordnername beginnt mit einer Nummer."
    assert a.listings == 1 + 3 * structure.MAX_OPEN_PER_LEVEL  # instead of 1 + 3 × 1500


def test_many_projects_are_fast(tmp_path):
    mk(tmp_path, *(f"Kunde {c}/{y}/P{10000 + c * 1000 + (y - 2020) * 100 + i} Projekt/01 Pläne"
                   for c in range(5) for y in range(2020, 2025) for i in range(60)))
    started = time.monotonic()
    a = analyse(tmp_path)
    assert found(a.proposal) == (r"P\d{5}", 3, 1500)
    assert time.monotonic() - started < 10 and a.listings < 100


def test_dated_folders_inside_projects_do_not_win(tmp_path):
    for i in range(1, 6):
        mk(tmp_path, *(f"BS-2024-{i:04d} P/{d} Ortstermin" for d in ("2023-05-12", "2023-06-01", "2023-07-15")),
           f"BS-2024-{i:04d} P/Pläne", f"BS-2024-{i:04d} P/Schriftverkehr", f"BS-2024-{i:04d} P/Fotos")
    p = analyse(tmp_path).proposal
    assert found(p) == (BS, 2, 5) and p.levels == {1: 5}


def test_dated_folders_are_no_project_scheme(tmp_path):
    mk(tmp_path, "2023-05-12 Ortstermin", "2023-06-01 Abnahme", "2023-07-15 Begehung", "12.08.2023 Termin",
       "2024-01-09 Besprechung", "20240521 Termin", "20240604 Termin", "20240611 Termin")
    a = analyse(tmp_path)
    assert a.proposal is None and "Datumsangaben" in a.reason


def test_word_prefix_needs_more_digits(tmp_path):
    mk(tmp_path, "Haus 12", "Haus 13", "Haus 14", "Haus 15")
    assert analyse(tmp_path).proposal is None
    mk(tmp_path / "b", "PRJ 2024-001 A", "PRJ 2024-002 B", "PRJ 2024-003 C")
    assert analyse(tmp_path / "b").proposal.pattern == r"PRJ\s\d{4}-\d{3}"


def test_numbered_customer_folders_are_containers(tmp_path):
    for k in range(1, 4):
        bs_projects(tmp_path / f"K{k:03d} Kunde {k}", n=3, year=2020 + k)
    p = analyse(tmp_path).proposal
    assert found(p) == (BS, 2, 9)


def test_five_digit_customers_contain_the_projects(tmp_path):
    """Review case K: 10020 Kunde 0/2024-000 Projekt …"""
    mk(tmp_path, *(f"{10020 + c} Kunde {c}/2024-{c * 10 + i:03d} Projekt/Pläne" for c in range(10) for i in range(6)))
    a = analyse(tmp_path)
    assert found(a.proposal) == (YEAR_NUM, 2, 60)
    assert any(r["pattern"] == r"\d{5}" and "enthalten die Projektordner" in r["reason"] for r in a.rejected)


def test_customer_number_as_prefix_of_the_project_number(tmp_path):
    """Review case R: 1020 Stadt/1020-001 Kita … – a three-digit continuation is a numbering of its
    own (rule 3 covers only sub-numbers of up to two digits), so the customers are containers. (With
    one and the same label in every customer – 1020-001 Bauteil – the folders would be records of
    the outer folder, see test_struktur_bestaetigung.)"""
    mk(tmp_path, *(f"{1020 + c} Stadt {c}/{1020 + c}-{i:03d} Objekt {c}.{i}" for c in range(8) for i in range(1, 6)))
    assert found(analyse(tmp_path).proposal) == (YEAR_NUM, 2, 40)


def test_numbered_customers_with_year_folders(tmp_path):
    """K001 Kunde/2024/BS-…: the year folders between customer and project are looked into."""
    mk(tmp_path, *(f"K{c:03d} Kunde {c}/{y}/BS-{y}-{c * 10 + i:04d} Projekt {i}/Pläne"
                   for c in range(1, 7) for y in (2023, 2024) for i in range(3)))
    assert found(analyse(tmp_path).proposal) == (BS, 3, 36)


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


# ============================================================================ review cases: years and short numbers
def test_year_folders_with_pure_digit_numbers(tmp_path):
    """Review blocker J2: 2022/1001 Projekt … – the year folders must not become the projects. The
    scheme excludes years, so the scan of the app passes through 2022, 2023, 2024."""
    n = 1000
    for y in (2022, 2023, 2024):
        for i in range(1, 25):
            n += 1
            mk(tmp_path, f"{y}/{n} Projekt {i}/Pläne")
    mk(tmp_path, "_Vorlagen", "Archiv")
    a = analyse(tmp_path)
    assert found(a.proposal) == (FOUR, 2, 72)
    assert "keine Jahreszahl" in a.proposal.description
    assert {e["number"] for e in a.proposal.examples} <= {str(x) for x in range(1001, 1073)}
    projects = scan_projects(tmp_path, FOUR, 2)
    assert len(projects) == 72 and not any(p.number in ("2022", "2023", "2024") for p in projects)
    rx = number_regex(FOUR)  # in mails: a year is no project number, the number is
    assert [m.group(0) for m in rx.finditer("Projekt 1017 vom 12.05.2024, Termin 2025")] == ["1017"]


def test_running_numbers_that_reach_the_years_are_refused(tmp_path):
    """Review s16: running numbers 1801–2000 in year folders – 1900 … 2000 look like years. No
    scheme can tell them apart, so nothing is proposed (instead of silently dropping them)."""
    mk(tmp_path, *(f"{y}/{1800 + (y - 2020) * 40 + i} Projekt {i}" for y in range(2020, 2025) for i in range(1, 41)))
    a = analyse(tmp_path)
    assert a.proposal is None and a.exit_code == 1
    assert "Jahreszahlen" in a.reason and "101 Ordner beginnen" in a.reason and "2022/1900 Projekt" in a.reason


def test_year_folders_with_two_digit_year_numbers(tmp_path):
    """Review case J: 2022/22001 Projekt …"""
    mk(tmp_path, *(f"{y}/{y % 100}{i:03d} Projekt {i}/Pläne" for y in (2022, 2023, 2024) for i in range(1, 25)),
       "_Vorlagen", "Archiv", "00_Allgemein")
    assert found(analyse(tmp_path).proposal) == (r"\d{5}", 2, 72)


def test_year_range_containers(tmp_path):
    """Review case B: 2017-2018/2017-001 … – the year ranges are containers, not projects."""
    mk(tmp_path, *(f"{r}/{r[:4]}-{i:03d} Projekt {i}/Pläne" for r in ("2017-2018", "2019-2020", "2021-2022")
                   for i in range(1, 21)))
    p = analyse(tmp_path).proposal
    assert found(p) == (YEAR_NUM, 2, 60) and p.levels == {2: 60}


def test_year_range_guard_for_four_by_four_numbers(tmp_path):
    """\\d{4}-\\d{4} numbers inside year-range folders: the range folders are excluded by a guard."""
    mk(tmp_path, *(f"{r}/{r[:4]}-{i:04d} Projekt" for r in ("2017-2018", "2019-2020") for i in range(1, 11)))
    p = analyse(tmp_path).proposal
    assert p.depth == 2 and p.projects == 20 and "kein Jahresbereich" in p.description
    assert scan_projects(tmp_path, p.pattern, 2)[0].number == "2017-0001"


@pytest.mark.parametrize("extra", [[], ["00_Allgemein"], ["_Vorlagen", "Archiv", "00_Allgemein"]])
def test_allgemein_and_five_digit_numbers(tmp_path, extra):
    """Review s17 and case A: '00_Allgemein' and numbered folders inside the projects ('01
    Schriftverkehr') must not prevent the detection of 21001 … (rule 2)."""
    mk(tmp_path, *(f"{21000 + i} Projekt {i}/{sub}" for i in range(1, 41)
                   for sub in ("01 Schriftverkehr", "02 Pläne", "03 Berechnungen")), *extra)
    p = analyse(tmp_path).proposal
    assert found(p) == (r"\d{5}", 2, 40) and sorted(p.exceptions) == sorted(extra)


def test_flat_numbers_with_underscored_internal_folders(tmp_path):
    """Review case J3: 24001_Projekt_1/01_Schriftverkehr …"""
    mk(tmp_path, *(f"{24000 + i}_Projekt_{i}/{sub}" for i in range(1, 30)
                   for sub in ("01_Schriftverkehr", "02_Planunterlagen", "03_Brandschutzkonzept")))
    assert found(analyse(tmp_path).proposal) == (r"\d{5}", 2, 29)


@pytest.mark.parametrize("per_year", [10, 30, 60, 150])
def test_numbered_internal_subfolders_at_any_size(tmp_path, per_year):
    """Review case N: 2023/2023-001 Projekt/2023-001_01 Schriftverkehr … is internal numbering
    (rule 3) – the same result with 10 or 150 projects per year (rule 7: no size threshold)."""
    mk(tmp_path, *(f"{y}/{y}-{i:03d} Projekt {i}/{y}-{i:03d}_{j:02d} {sub}"
                   for y in (2022, 2023, 2024) for i in range(1, per_year + 1)
                   for j, sub in enumerate(("Schriftverkehr", "Pläne", "Brandschutzkonzept"), 1)),
       "2024/00_Allgemein", "_Vorlagen")
    p = analyse(tmp_path).proposal
    assert found(p) == (YEAR_NUM, 2, 3 * per_year) and p.levels == {2: 3 * per_year}


@pytest.mark.parametrize("n", [40, 90, 300])
def test_bs_parts_are_internal(tmp_path, n):
    """Review case C: BS-2024-0137 Schule/BS-2024-0137-01 Teil … at any size."""
    mk(tmp_path, *(f"BS-2024-{i + 100:04d} Schule {i}/BS-2024-{i + 100:04d}-{j:02d} Teil {j}"
                   for i in range(n) for j in range(1, 4)))
    assert found(analyse(tmp_path).proposal) == (BS, 2, n)


def test_names_starting_with_a_digit(tmp_path):
    """Review case G: '2024-001 2 WE Musterstraße' – the digit after the number is part of the name."""
    mk(tmp_path, *(f"2024-{i:03d} {f'{i % 9 + 1} WE Musterstraße' if i % 2 else f'Kita {i}'}/Pläne"
                   for i in range(1, 41)))
    p = analyse(tmp_path).proposal
    assert found(p) == (YEAR_NUM, 2, 40)
    assert any(e["name"].endswith("WE Musterstraße") for e in p.examples)


def test_two_digit_year_numbers_with_dated_subfolders(tmp_path):
    """Review case H: 24-017 Projekt/24-05-12 Ortstermin – the dated subfolders are dates (rule 2)."""
    mk(tmp_path, *(f"24-{i:03d} Projekt {i}/24-05-{i % 28 + 1:02d} Ortstermin" for i in range(1, 40)))
    assert found(analyse(tmp_path).proposal) == (r"\d{2}-\d{3}", 2, 39)


# ============================================================================ review cases: other numberings
def test_offers_with_their_own_numbering_stay_out(tmp_path):
    """Review case E: Angebote/AN-2024-001 … are no projects; the report names them as a hint."""
    mk(tmp_path, *(f"{y}/{y}-{i:03d} Projekt {i}/Pläne" for y in (2023, 2024) for i in range(1, 40)),
       *(f"Angebote/AN-2024-{i:03d} Anfrage Stadt {i}" for i in range(1, 25)), "_Vorlagen", "00_Allgemein")
    p = analyse(tmp_path).proposal
    assert found(p) == (YEAR_NUM, 2, 78)
    assert p.hints == [p.second_scheme] and p.second_scheme["pattern"] == r"AN-\d{4}-\d{3}"
    assert p.second_scheme["where"] == "Angebote" and p.second_scheme["folders"] == 24
    assert p.second_scheme["combined"] == r"(?:\d{4}-\d{3}|AN-\d{4}-\d{3})" and p.second_scheme["depth"] == 2


def test_invoices_deeper_with_their_own_numbering_stay_out(tmp_path):
    """Review case I: 39 projects and Verwaltung/Rechnungen/RE-2024-0001 … (29)."""
    mk(tmp_path, *(f"2024-{i:03d} Projekt {i}/Pläne" for i in range(1, 40)),
       *(f"Verwaltung/Rechnungen/RE-2024-{i:04d}" for i in range(1, 30)))
    p = analyse(tmp_path).proposal
    assert found(p) == (YEAR_NUM, 2, 39)
    hint = p.second_scheme
    assert (hint["pattern"], hint["where"], hint["depth"]) == (r"RE-\d{4}-\d{4}", "Verwaltung/Rechnungen", 3)
    assert len(scan_projects(tmp_path, hint["combined"], hint["depth"])) == 39 + 29


def test_only_offer_folders_are_no_projects(tmp_path):
    mk(tmp_path, *(f"Angebote/AN-2024-{i:03d} Anfrage" for i in range(1, 25)), "Kita Nord", "Schule Süd")
    a = analyse(tmp_path)
    assert a.proposal is None and "Nebenablagen" in a.reason


def test_norms_folder_is_only_a_hint(tmp_path):
    """Review case F: Normen/DIN 18232 Teil … next to BS projects."""
    mk(tmp_path, *(f"BS-2024-{i:04d} Projekt {i}/Pläne" for i in range(1, 40)),
       *(f"Normen/DIN {n} Teil" for n in (4102, 18232, 14675, 4066, 18017, 14096, 18040)))
    p = analyse(tmp_path).proposal
    assert found(p) == (BS, 2, 39) and p.second_scheme["pattern"] == r"DIN\s\d{5}"


@pytest.mark.parametrize("current, archive", [(29, 9), (9, 29), (10, 30)])
def test_archive_in_an_older_numbering_is_named(tmp_path, current, archive):
    """Review O1/O2: current BS projects and Archiv/1998-001 … – the current scheme is proposed
    (never an automatic alternation, rule 6), the archive is named with the setting for both."""
    bs_projects(tmp_path, n=current)
    mk(tmp_path, *(f"Archiv/1998-{i:03d} Alt {i}" for i in range(1, archive + 1)), "_Vorlagen/KW01", "_Vorlagen/KW02")
    p = analyse(tmp_path).proposal
    assert found(p) == (BS, 2, current) and sorted(p.exceptions) == ["Archiv", "_Vorlagen"]
    hint = p.second_scheme
    assert (hint["pattern"], hint["where"], hint["folders"], hint["branch"]) == (YEAR_NUM, "Archiv", archive, "archive")
    assert hint["combined"] == rf"(?:{BS}|\d{{4}}-\d{{3}})"
    assert len(scan_projects(tmp_path, hint["combined"], hint["depth"])) == current + archive


def test_two_equally_common_schemes_give_no_proposal(tmp_path):
    """Rule 7: neither scheme clearly dominates – exit 1 with both candidates."""
    mk(tmp_path, *(f"2024-{i:03d} Projekt {i}" for i in range(1, 31)), *(f"P{30000 + i} Neubau" for i in range(1, 21)))
    a = analyse(tmp_path)
    assert a.proposal is None and a.exit_code == 1 and "etwa gleich oft" in a.reason
    assert {c["pattern"] for c in a.candidates[:2]} == {YEAR_NUM, r"P\d{5}"}


def test_realistic_messy_share(tmp_path):
    """Review case P: year folders, 01_ subfolders, dated subfolders, 00_Allgemein, an archive in an
    older numbering below a year-range folder, offer folders with a year."""
    paths = []
    for y in range(2019, 2025):
        for i in range(1, 21 + (y % 7) * 3):
            p = f"{y}/{y}-{i:03d} Kita Regenbogen"
            paths += [f"{p}/01_Schriftverkehr", f"{p}/02_Pläne", f"{p}/{y}-0{i % 9 + 1}-{10 + i % 18} Ortstermin"]
        paths.append(f"{y}/00_Allgemein")
    paths += [f"Archiv/2005-2015/{y % 100:02d}-{i:03d} Altprojekt" for y in range(2005, 2016) for i in range(1, 6)]
    paths += ["_Vorlagen/Brandschutzkonzept", "00_Allgemein/Normen/DIN 4102", "Akquise/2024 Anfragen",
              "Akquise/2025 Anfragen"]
    mk(tmp_path, *paths)
    p = analyse(tmp_path).proposal
    projects = sum(20 + (y % 7) * 3 for y in range(2019, 2025))
    assert found(p) == (YEAR_NUM, 2, projects)
    assert (p.second_scheme["pattern"], p.second_scheme["where"], p.second_scheme["depth"]) == (
        r"\d{2}-\d{3}", "Archiv/2005-2015", 3)
    assert not any("2005-2015" in e["path"] for e in p.examples)


# ============================================================================ stability
def test_result_does_not_depend_on_the_size_of_the_share(tmp_path):
    """Rule 7: the same layout with few or many projects gives the same scheme and depth, and
    adding projects keeps it."""
    def layout(root: Path, per_year: int) -> None:
        mk(root, *(f"{y}/{y}-{i:03d} P/{sub}" for y in (2023, 2024) for i in range(1, per_year + 1)
                   for sub in ("00_Allgemein", "01 Schriftverkehr", f"{y}-{i:03d}_02 Pläne", "2024-05-12 Termin")),
           "Archiv/2010/10-001 Alt", "_Vorlagen")
    results = set()
    for n in (4, 12, 40, 120):
        layout(tmp_path / str(n), n)
        p = analyse(tmp_path / str(n)).proposal
        results.add((p.pattern, p.depth))
        assert p.projects == 2 * n
    assert results == {(YEAR_NUM, 2)}


def test_auto_mode_stays_stable_when_projects_are_added(tmp_path):
    """Review s9: 29 current projects and an archive of 30 in an older numbering; three more
    projects later – the confirmed scheme stays and no new proposal is made."""
    root = tmp_path / "p"
    bs_projects(root, n=29)
    mk(root, *(f"Archiv/1998-{i:03d} Alt" for i in range(1, 31)))
    s = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN="auto")
    assert run_index(s)["structure"]["detected"] is True
    assert _confirm(s)["pattern"] == BS
    mk(root, *(f"BS-2024-{i:04d} Neu" for i in range(30, 33)))
    _age_check(s)
    stats = run_index(s)
    assert "proposal" not in stats["structure"] and structure.current_layout(s).warning is None
    assert stats["projects"]["projects"] == 32


@pytest.mark.parametrize("a, b, same", [
    (r"(?:BS-\d{4}-\d{4}|\d{4}-\d{3})", r"(?:\d{4}-\d{3}|BS-\d{4}-\d{4})", True),
    (r"BS-\d{4}-\d{4}|\d{4}-\d{3}", r"(?:\d{4}-\d{3}|BS-\d{4}-\d{4})", True),
    (r"(?:\d{4}-\d{3})", r"\d{4}-\d{3}", True),
    (r"(?!(?:19|20)\d\d(?!\d))\d{4}", r"(?!(?:19|20)\d\d(?!\d))\d{4}", True),
    (r"(?:A|B)C", r"(?:B|A)C", False),
    (r"[|]x|y", r"y|[|]x", True),
    (r"\d{4}-\d{3}", r"\d{4}-\d{3,4}", False),
])
def test_schemes_are_compared_by_their_alternatives(a, b, same):
    assert (structure.alternatives(a) == structure.alternatives(b)) is same


def test_stored_scheme_written_differently_is_no_difference(tmp_path):
    """Rule 9: a stored scheme is compared by meaning – the same alternatives, or the same folders."""
    root = mk(tmp_path / "p", *(f"2024-{i:03d} Projekt {i}" for i in range(1, 8)))
    s = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN="auto")
    run_index(s)
    _confirm(s)
    for stored in (r"[0-9]{4}-[0-9]{3}", r"(?:\d{4}-\d{3}|AN-\d{4}-\d{3})"):
        with db.session(s.db_path) as conn:
            rec = json.loads(db.get_meta(conn, structure.CONFIRMED_KEY))
            db.set_meta(conn, structure.CONFIRMED_KEY, json.dumps({**rec, "pattern": stored}))
        _age_check(s)
        stats = run_index(s)
        assert "proposal" not in stats["structure"] and _meta(s, structure.PROPOSAL_KEY) is None
        assert structure.current_layout(s).pattern == stored


# ============================================================================ PROJECT_FOLDER_DEPTH and pattern settings
@pytest.mark.parametrize("raw, depth, warned", [
    ("", 2, False), ("1", 1, False), ("3", 3, False), ("6", 6, False), ("0", 2, True), ("7", 2, True),
    ("zwei", 2, True), ("auto", 2, True),
])
def test_folder_depth_setting(raw, depth, warned):
    s = load_settings({"PROJECT_FOLDER_DEPTH": raw})
    assert s.project_folder_depth == depth
    assert any(w.startswith("PROJECT_FOLDER_DEPTH=") for w in s.config_warnings) is warned


@pytest.mark.parametrize("raw, depth, warned", [("", None, False), ("auto", None, False), ("3", 3, True),
                                                ("9", None, True)])
def test_folder_depth_with_automatic_pattern(raw, depth, warned):
    """A number is kept (the status page names it) but not used with auto – said at start-up."""
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
    for pattern in (BS, r"P\d{5}", r"\d{4}[-_\s]\d{3}", r"(?:BS|BV)-\d{4}-\d{4}", DEFAULT_PROJECT_NUMBER_PATTERN, FOUR):
        assert pattern_problem(pattern) is None


def test_invalid_pattern_does_not_crash_and_changes_nothing(tmp_path, caplog):
    root = tmp_path / "p"
    bs_projects(root, n=3)
    run_index(settings_for(tmp_path, root))
    bad = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN="BS-(\\d{4}")
    caplog.set_level(logging.INFO)
    stats = run_index(bad)
    assert "unbrauchbar" in stats["projects"]["warning"] and stats["projects"]["projects"] == 3
    assert stats["documents"] == {"note": "nicht abgeglichen – kein gültiges Projektnummern-Schema"}
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


def test_pattern_setting_ignores_stray_blanks():
    assert load_settings({"PROJECT_NUMBER_PATTERN": " P\\d{5} "}).project_number_pattern == r"P\d{5}"


# ============================================================================ automatic mode
def _meta(s, key):
    with db.session(s.db_path) as conn:
        return db.get_meta(conn, key)


def _age_check(s, hours=2):
    """Make the latest detection older than the re-check interval."""
    with db.session(s.db_path) as conn:
        rec = json.loads(db.get_meta(conn, structure.CHECK_KEY))
        when = datetime.now(timezone.utc) - timedelta(hours=hours)
        db.set_meta(conn, structure.CHECK_KEY, json.dumps({**rec, "checked_at": when.isoformat(timespec="seconds")}))


def _confirm(s, user="pruefer"):
    """Confirm the pending proposal, as a person does on the status page."""
    with db.session(s.db_path) as conn:
        layout = structure.current_layout(s, conn)
        assert layout.proposal, layout.problem
        return structure.confirm_proposal(conn, s, layout.proposal["id"], user)


def _active(s) -> list[str]:
    with db.session(s.db_path) as conn:
        return [r[0] for r in conn.execute("SELECT number FROM projects WHERE active = 1 ORDER BY number")]


def test_auto_proposes_and_uses_only_a_confirmed_scheme(tmp_path, caplog):
    root = tmp_path / "p"
    mk(root, *(f"2024/2024-{i:03d} Projekt {i}" for i in range(1, 5)), "Archiv")
    s = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN="auto")
    caplog.set_level(logging.INFO, logger="projektakte.structure")
    stats = run_index(s)
    # a proposal: nothing is read as a project before a person confirmed it
    assert stats["projects"]["added"] == 0 and stats["structure"]["detected"] is True
    assert stats["projects"]["warning"].startswith(structure.PENDING_TEXT)
    proposal = json.loads(_meta(s, structure.PROPOSAL_KEY))
    assert (proposal["pattern"], proposal["depth"], proposal["projects"]) == (YEAR_NUM, 2, 4)
    assert proposal["root"] == os.path.abspath(root) and proposal["examples"][0] == "2024/2024-001 Projekt 1"
    assert _meta(s, structure.CONFIRMED_KEY) is None and structure.PENDING_TEXT in caplog.text
    assert structure.current_layout(s).pattern is None
    confirmed = _confirm(s)
    assert confirmed["confirmed_by"] == "pruefer" and confirmed["confirmed_via"] == "Statusseite"
    assert run_index(s)["projects"]["added"] == 4
    layout = structure.current_layout(s)
    assert layout.pattern == YEAR_NUM and layout.depth == 2 and layout.source.startswith("bestätigt am")
    assert layout.source.endswith("von pruefer")
    # status page and start-up line show it
    client = TestClient(create_app(s, background=False))
    page = client.get("/status").text
    assert r"<code>\d{4}-\d{3}</code>" in page and "bestätigt am" in page
    assert client.get("/api/status").json()["layout"]["pattern"] == YEAR_NUM
    caplog.clear()
    caplog.set_level(logging.INFO, logger="projektakte.startup")
    log_startup(s, NoAuth())
    assert r"Projektnummern=\d{4}-\d{3} (bestätigt am" in caplog.text
    assert "Tiefe 2 (automatisch)" in caplog.text


def test_auto_year_folders_with_pure_digit_numbers(tmp_path):
    """Review s4: with auto mode the 72 real projects are stored and indexed, not the year folders."""
    root = tmp_path / "p"
    n = 1000
    for y in (2022, 2023, 2024):
        for i in range(1, 25):
            n += 1
            (mk(root, f"{y}/{n} Projekt {i}") / f"{y}/{n} Projekt {i}" / "Bericht.txt").write_text(
                f"Brandschutzkonzept Projekt {n}", encoding="utf-8")
    s = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN="auto")
    assert run_index(s)["structure"]["proposal"] == FOUR
    _confirm(s)
    stats = run_index(s)
    assert stats["structure"]["pattern"] == FOUR and stats["projects"]["added"] == 72
    assert len(_active(s)) == 72 and "2022" not in _active(s)
    with db.session(s.db_path) as conn:
        assert conn.execute("SELECT COUNT(DISTINCT project) FROM documents").fetchone()[0] == 72


def test_auto_iso_dates_in_mails_are_no_project_numbers(tmp_path):
    """Review s14: year-month folders next to the projects do not widen the scheme, so a mail with
    an ISO date is not assigned to a fake project '2024-05'."""
    root = tmp_path / "p"
    for i in range(1, 41):
        (mk(root, f"2024-{i:03d} Projekt {i}") / f"2024-{i:03d} Projekt {i}" / "Bericht.txt").write_text(
            f"Bericht zu Projekt {i}", encoding="utf-8")
    mk(root, "2024-05 Teamsitzung", "2024-11 Weihnachtsfeier")
    s = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN="auto")
    _eml(s.mail_dir / "a.eml", "Ortstermin Kita", "Hallo, wir treffen uns am 2024-05-21 auf der Baustelle.")
    run_index(s)
    _confirm(s)
    stats = run_index(s)
    assert stats["structure"]["pattern"] == YEAR_NUM and stats["projects"]["projects"] == 40
    assert "2024-05" not in _active(s)
    with db.session(s.db_path) as conn:
        assert conn.execute("SELECT project FROM mails").fetchone()[0] is None


def test_auto_later_different_detection_is_a_new_proposal(tmp_path, caplog):
    root = tmp_path / "p"
    mk(root, *(f"2024-{i:03d} Projekt {i}" for i in range(1, 5)))
    s = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN="auto")
    run_index(s)
    _confirm(s)
    run_index(s)
    confirmed_before = _meta(s, structure.CONFIRMED_KEY)
    # the share now shows a second, more frequent numbering (new projects, or another share mounted there)
    mk(root, *(f"P{20000 + i}_Neubau {i}" for i in range(1, 9)))
    _age_check(s)
    caplog.set_level(logging.WARNING)
    stats = run_index(s)
    new = r"P\d{5}"  # never an automatic alternation (rule 6)
    assert _meta(s, structure.CONFIRMED_KEY) == confirmed_before
    assert stats["structure"]["proposal"] == new and stats["structure"]["pattern"] == YEAR_NUM
    assert stats["projects"]["added"] == 0 and len(_active(s)) == 4  # still the confirmed scheme
    assert f"{structure.PENDING_TEXT} – {new}" in caplog.text
    layout = structure.current_layout(s)
    assert layout.pattern == YEAR_NUM and new in layout.warning and "--uebernehmen" in layout.warning
    assert "findet derzeit 4 Projektordner" in layout.warning and layout.state == "confirmed"
    page = TestClient(create_app(s, background=False)).get("/status").text
    assert "Neuer Vorschlag" in page and "Übernehmen" in page
    # still pending at the next check: warned only once
    caplog.clear()
    _age_check(s)
    run_index(s)
    assert structure.PENDING_TEXT not in caplog.text and structure.current_layout(s).proposal["pattern"] == new


def test_auto_difference_disappears_when_the_share_is_back(tmp_path):
    root = tmp_path / "p"
    mk(root, *(f"2024-{i:03d} Projekt {i}" for i in range(1, 5)))
    s = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN="auto")
    run_index(s)
    _confirm(s)
    extra = [f"P{20000 + i}_Neubau {i}" for i in range(1, 9)]
    mk(root, *extra)
    _age_check(s)
    run_index(s)
    assert structure.current_layout(s).proposal
    for name in extra:
        (root / name).rmdir()
    _age_check(s)
    run_index(s)
    assert structure.current_layout(s).proposal is None and _meta(s, structure.PROPOSAL_KEY) is None


def test_auto_half_mounted_empty_share_changes_nothing(tmp_path):
    root = tmp_path / "p"
    mk(root, *(f"2024-{i:03d} Projekt {i}" for i in range(1, 5)))
    s = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN="auto")
    run_index(s)
    _confirm(s)
    run_index(s)
    stored = _meta(s, structure.CONFIRMED_KEY)
    for p in list(root.iterdir()):
        p.rmdir()
    _age_check(s)
    stats = run_index(s)
    assert "keine Projekte mehr" in stats["projects"]["warning"]
    assert _meta(s, structure.CONFIRMED_KEY) == stored and _meta(s, structure.PROPOSAL_KEY) is None
    assert len(_active(s)) == 4


def test_auto_unmounted_share_before_any_detection(tmp_path):
    s = settings_for(tmp_path, tmp_path / "nicht-eingebunden", PROJECT_NUMBER_PATTERN="auto")
    stats = run_index(s)
    assert "nicht vorhanden" in stats["projects"]["warning"] and _meta(s, structure.PROPOSAL_KEY) is None
    assert _meta(s, structure.CONFIRMED_KEY) is None


def test_auto_without_a_clear_scheme_skips_the_sync_and_the_assignment(tmp_path):
    root = mk(tmp_path / "p", "Verwaltung", "Akquise", "Fotos")
    s = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN="auto")
    _eml(s.mail_dir / "a.eml", "Frage zu 2024-017", "Hallo")
    stats = run_index(s)
    assert "Kein eindeutiges Projektnummern-Schema" in stats["projects"]["warning"]
    assert "python -m projektakte.structure" in stats["projects"]["warning"]
    assert "Kein Ordnername beginnt mit einer Nummer" in stats["projects"]["warning"]
    assert stats["assignment"]["note"].startswith("nicht ausgeführt")
    with db.session(s.db_path) as conn:
        assert conn.execute("SELECT assign_status FROM mails").fetchone()[0] == "pending"
    assert _meta(s, structure.PROPOSAL_KEY) is None and _meta(s, structure.CONFIRMED_KEY) is None
    page = TestClient(create_app(s, background=False)).get("/status").text
    assert "Kein eindeutiges Schema erkannt" in page and "Nächste Prüfung frühestens" in page
    # once the folders are there, the next detection (at most hourly) proposes; confirmed, it syncs and assigns
    mk(root, "2024-017 Kita", "2024-018 Schule", "2024-019 Halle")
    _age_check(s)
    stats = run_index(s)
    assert stats["projects"]["added"] == 0 and stats["structure"]["proposal"] == YEAR_NUM
    _confirm(s)
    stats = run_index(s)
    assert stats["projects"]["added"] == 3 and json.loads(_meta(s, structure.CHECK_KEY))["outcome"] == "proposal"
    with db.session(s.db_path) as conn:
        assert tuple(conn.execute("SELECT project, assign_status FROM mails").fetchone()) == ("2024-017", "assigned")


def test_auto_without_a_scheme_retries_at_most_hourly(tmp_path, monkeypatch):
    """Rule 9: no clear scheme – the share is not read again at every pass (SMB traffic)."""
    root = mk(tmp_path / "p", *(f"Projekt {c}" for c in "ABCDEF"))
    s = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN="auto")
    calls = []
    real = structure.analyse
    monkeypatch.setattr(structure, "analyse", lambda *a, **k: calls.append(1) or real(*a, **k))
    now = datetime.now(timezone.utc)
    db.init_db(s.db_path)
    with db.session(s.db_path) as conn:
        layout, info = layout_for_index(conn, s, now=now)
        assert layout.pattern is None and info["detected"] is False and calls == [1]
        layout, _ = layout_for_index(conn, s, now=now + timedelta(minutes=30))
        assert calls == [1] and "Kein Ordnername" in layout.problem  # the reason of the last check
        assert "--uebernehmen" in layout.problem and "nächste Prüfung frühestens" in layout.summary()
        layout_for_index(conn, s, now=now + structure.RECHECK_INTERVAL + timedelta(minutes=1))
        assert calls == [1, 1]
    # another PROJECTS_DIR is detected at once
    other = mk(tmp_path / "q", *(f"2024-{i:03d} X" for i in range(1, 6)))
    s2 = settings_for(tmp_path, other, PROJECT_NUMBER_PATTERN="auto")
    with db.session(s2.db_path) as conn:
        layout = layout_for_index(conn, s2, now=now + timedelta(minutes=31))[0]
        assert layout.pattern is None and layout.proposal["pattern"] == YEAR_NUM


def test_accept_command_switches(tmp_path, monkeypatch, capsys):
    root = tmp_path / "p"
    mk(root, *(f"2024-{i:03d} Projekt {i}" for i in range(1, 4)))
    s = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN="auto")
    run_index(s)
    _confirm(s)
    run_index(s)
    for p in list(root.iterdir()):
        p.rename(root / p.name.replace("2024-", "P10"))  # the office renamed everything: P10001 …
    _age_check(s)
    run_index(s)
    assert structure.current_layout(s).proposal["pattern"] == r"P\d{5}"
    monkeypatch.setenv("PROJECTS_DIR", str(root))
    monkeypatch.setenv("DATA_DIR", str(s.data_dir))
    monkeypatch.setenv("PROJECT_NUMBER_PATTERN", "auto")
    assert main(["--uebernehmen", "--ohne-dateien"]) == 0
    out = capsys.readouterr().out
    assert "Übernommen (bestätigt von " in out and "(Kommandozeile)): P\\d{5}, Tiefe 2" in out
    layout = structure.current_layout(s)
    assert layout.pattern == r"P\d{5}" and layout.proposal is None
    assert layout.confirmed["confirmed_via"] == "Kommandozeile"
    stats = run_index(s)
    assert stats["projects"]["added"] == 3 and _active(s) == ["P10001", "P10002", "P10003"]


def test_accept_refuses_another_folder(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path / "a"))
    with pytest.raises(SystemExit) as exc:
        main([str(tmp_path / "b"), "--uebernehmen"])
    err = capsys.readouterr().err
    assert exc.value.code == structure.EXIT_USAGE == 3 and "PFAD weglassen" in err and "Fehler:" in err


def test_detection_for_another_projects_dir_is_not_used(tmp_path):
    """E.g. the demo data set detected with "auto" before the switch to the real share."""
    first = mk(tmp_path / "demo", *(f"BS-2024-{i:04d} X" for i in range(1, 4)))
    demo = settings_for(tmp_path, first, PROJECT_NUMBER_PATTERN="auto")
    run_index(demo)
    _confirm(demo)
    assert structure.current_layout(demo).pattern == BS
    real = mk(tmp_path / "echt", *(f"P{30000 + i} Y" for i in range(1, 4)))
    s = settings_for(tmp_path, real, PROJECT_NUMBER_PATTERN="auto")
    assert structure.current_layout(s).pattern is None and structure.current_layout(s).state == "unchecked"
    run_index(s)
    assert structure.current_layout(s).pattern is None
    _confirm(s)
    assert structure.current_layout(s).pattern == r"P\d{5}"


def test_configured_depth_is_not_used_with_auto(tmp_path):
    """Review verify3 f1/f3: the person confirms evidence computed at the detected depth – exactly that
    depth is read; a configured PROJECT_FOLDER_DEPTH is named on the status page, never applied."""
    root = mk(tmp_path / "p", *(f"2024/BS-2024-{i:04d} X" for i in range(1, 4)), "Archiv/2010/BS-2010-0001 Alt")
    s = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN="auto", PROJECT_FOLDER_DEPTH="1")
    assert any(w.startswith("PROJECT_FOLDER_DEPTH=1 gilt nicht zusammen mit") for w in s.config_warnings)
    run_index(s)
    assert "PROJECT_FOLDER_DEPTH=1 ist eingestellt" in structure.current_layout(s).notices[0]
    _confirm(s)
    run_index(s)
    layout = structure.current_layout(s)
    assert (layout.depth, layout.depth_auto, layout.ignored_depth) == (3, True, 1) and len(_active(s)) == 4
    assert "gelesen wird die Ordnertiefe des bestätigten Vorschlags, 3" in layout.notices[0]


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


def test_detection_failure_never_stops_the_indexing(tmp_path, monkeypatch):
    root = mk(tmp_path / "p", *(f"2024-{i:03d} X" for i in range(1, 4)))
    s = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN="auto")
    run_index(s)
    _confirm(s)
    run_index(s)

    def broken(*a, **k):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(structure, "analyse", broken)
    _age_check(s)
    stats = run_index(s)
    assert stats["projects"]["projects"] == 3 and "warning" not in stats["projects"]
    assert structure.current_layout(s).pattern == YEAR_NUM
    s2 = settings_for(tmp_path / "neu", root, PROJECT_NUMBER_PATTERN="auto")
    assert "fehlgeschlagen" in run_index(s2)["projects"]["warning"]


def test_folder_names_that_are_no_utf8_are_stored(tmp_path):
    """A name that is not valid UTF-8 (old SMB share) must not break storing the detection."""
    root = tmp_path / "p"
    root.mkdir()
    for i in range(1, 6):
        os.makedirs(os.fsencode(root) + b"/2024-%03d B\xfcro" % i)
    s = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN="auto")
    db.init_db(s.db_path)
    with db.session(s.db_path) as conn:
        layout, info = layout_for_index(conn, s)
    assert layout.proposal["pattern"] == YEAR_NUM and info["detected"] is True
    assert structure.current_layout(s).proposal["examples"][0] == "2024-001 B�ro"
    _confirm(s)
    assert structure.current_layout(s).examples[0] == "2024-001 B�ro"


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
    with db.session(s.db_path) as conn:
        assert conn.execute("SELECT project FROM mails").fetchone()[0] is None  # nothing before confirming
    _confirm(s)
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


def test_year_guard_works_in_the_search_helpers():
    assert web.join_spaced_codes("Akte 1017 2024", FOUR) == "Akte 1017 2024"
    assert re.fullmatch(f"(?:{FOUR})", "1017") and not re.fullmatch(f"(?:{FOUR})", "2024")


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
    assert "Dateien in 8 Projektordnern" in out


def test_cli_names_a_second_scheme_with_the_setting(tmp_path, capsys):
    root = tmp_path / "p"
    bs_projects(root, n=12)
    mk(root, *(f"Archiv/2005-2015/{i:02d}-{i:03d} Alt" for i in range(1, 8)))
    assert main([str(root), "--ohne-dateien"]) == 0
    out = capsys.readouterr().out
    assert (r"Weiteres Schema in Archiv/2005-2015: \d{2}-\d{3}, 7 Ordner" in out
            and r"wenn das auch Projekte sind: PROJECT_NUMBER_PATTERN=(?:BS-\d{4}-\d{4}|\d{2}-\d{3}) und "
                r"PROJECT_FOLDER_DEPTH=3" in out)


def test_cli_no_scheme_and_unreadable(tmp_path, capsys):
    mk(tmp_path / "p", "Verwaltung", "Fotos")
    assert main([str(tmp_path / "p")]) == 1
    assert "Kein eindeutiges Projektnummern-Schema" in capsys.readouterr().out
    assert main([str(tmp_path / "fehlt")]) == 2
    assert "FEHLER" in capsys.readouterr().out


def test_cli_lists_the_closest_candidates(tmp_path, capsys):
    mk(tmp_path / "p", *(f"2024-{i:03d} Projekt {i}" for i in range(1, 31)),
       *(f"P{30000 + i} Neubau" for i in range(1, 21)))
    assert main([str(tmp_path / "p")]) == 1
    out = capsys.readouterr().out
    assert "Nächste Kandidaten:" in out and r"\d{4}-\d{3} – Ebene 1, 30 Ordner" in out and r"P\d{5} – Ebene 1" in out


def test_cli_empty_folder_has_no_empty_headers(tmp_path, capsys):
    (tmp_path / "leer").mkdir()
    assert main([str(tmp_path / "leer")]) == 1
    out = capsys.readouterr().out
    assert "keine Unterordner" in out and "Ordner je Ebene" not in out and "Nächste Kandidaten" not in out
    assert "Gelesen: 0 Ordner" in out


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
    assert data["accepted"] is None and data["candidates"][0]["verdict"] == "gewählt"


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


@pytest.mark.parametrize("argv, message", [
    (["--tiefe", "9"], "Option --tiefe: 9 liegt nicht zwischen 1 und 6"),
    (["--tiefe", "x"], "Option --tiefe: „x“ ist keine ganze Zahl"),
    (["--max-ordner", "0"], "Option --max-ordner: 0 ist kleiner als 1"),
    (["--gibtsnicht"], "unbekannte Angaben: --gibtsnicht"),
    (["--data-dir"], "Option --data-dir: erwartet einen Wert"),
])
def test_cli_usage_errors_are_german_and_exit_3(capsys, argv, message):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    err = capsys.readouterr().err
    assert exc.value.code == 3 and message in err and err.startswith("Aufruf: python -m projektakte.structure")
    assert "usage:" not in err and "error:" not in err and "argument" not in err


def test_cli_help_is_german(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--hilfe"])
    out = capsys.readouterr().out
    assert exc.value.code == 0 and out.startswith("Aufruf: ") and "Optionen:" in out and "Argumente:" in out
    flat = " ".join(out.split())
    assert "3 = falscher Aufruf" in flat and "2 = Ordner nicht lesbar" in flat
    assert "usage" not in out and "show this help" not in out and "options:" not in out


def test_german_plurals():
    assert structure.de_count(1, "passender Ordner", "passende Ordner") == "1 passender Ordner"
    assert structure.de_count(2, "passender Ordner", "passende Ordner") == "2 passende Ordner"
    assert structure.de_count(1200, "Datei", "Dateien") == "1.200 Dateien"


def test_rejection_reasons_use_plurals(tmp_path):
    mk(tmp_path, "2024-001 A", "2024-002 B", "P12345 X", "P12346 Y")
    a = analyse(tmp_path)
    reasons = {c["pattern"]: c["reason"] for c in a.candidates}
    assert reasons == {YEAR_NUM: "nur 2 passende Ordner", r"P\d{5}": "nur 2 passende Ordner"}
    assert "(r)" not in json.dumps(a.to_dict(), ensure_ascii=False)


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
