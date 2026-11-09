"""Project layout, review round 4 (verify3): file and folder names that are no valid UTF-8 never stop
the documents pass and are read losslessly where possible; with PROJECT_NUMBER_PATTERN=auto exactly the
confirmed proposal's depth is read; hand-written lookaheads are no guards; points to check for offers
next to projects and for customer numbers with sub-projects; an empty mount point keeps a pending
proposal; /api/status without PROJECTS_DIR; a later 'no clear scheme' shown after a rejection; the
command line names who confirmed once."""
from __future__ import annotations

import json
import logging
import os
import random
from pathlib import Path

import pytest

from projektakte import db, documents, structure
from projektakte.indexer import run_index
from projektakte.registry import is_guard, loose_pattern, scan_projects, year_range_guard
from projektakte.startup import log_startup
from projektakte.structure import analyse, main

from .test_struktur_bestaetigung import (NAMES, SUBS, YEAR_NUM, active, bad_share, client_for, confirm, meta,
                                         mk, name, pending_id, section, settings_for, year_share)

OBJ = ["Kita Regenbogen", "Grundschule Nord", "Sporthalle Süd", "Rathaus Umbau", "Wohnanlage 12 WE", "Pflegeheim",
       "Hotel Löwen"]


class NoAuth:
    disabled, users, cookie_secure = True, {}, False


def documents_by_project(s) -> dict[str, int]:
    with db.session(s.db_path) as conn:
        return {r[0]: r[1] for r in conn.execute(
            "SELECT project, COUNT(*) FROM documents WHERE mail_id IS NULL GROUP BY project")}


def write(path: bytes, text: bytes = b"Brandschutz Rettungsweg") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(text)


# ============================================================================ 1. names that are no valid UTF-8
def five_projects(root: Path) -> Path:
    for i in range(1, 6):
        (mk(root, f"2024-{i:03d} {name(i)}") / f"2024-{i:03d} {name(i)}" / "Bericht.txt").write_text(
            f"Bericht {i}: Rettungswege geprüft.", encoding="utf-8")
    return root


def test_file_name_in_a_wrong_encoding_never_stops_the_documents(tmp_path, monkeypatch, caplog):
    """verify3 e2: one such file in the first project stopped the documents of every later project at
    every pass, and was extracted again each time."""
    root = five_projects(tmp_path / "p")
    first = os.fsencode(root / f"2024-001 {name(1)}")
    write(first + b"/Pr\xfcfung.txt")
    write(first + b"/Gutachten \xe4lter/Bericht.txt")  # a folder inside the project, too
    write(first + b"/Plan \xfc.dwg")  # not read anyway: counted as unsupported, not as a bad name
    s = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN=YEAR_NUM)
    extracted: list[str] = []
    real = documents.prepare_document
    monkeypatch.setattr(documents, "prepare_document", lambda **kw: extracted.append(kw["rel_path"]) or real(**kw))
    caplog.set_level(logging.WARNING)
    stats = run_index(s)
    assert extracted == ["Bericht.txt"] * 5  # the bad names are never read
    assert documents_by_project(s) == {f"2024-00{i}": 1 for i in range(1, 6)}
    d = stats["documents"]
    assert (d["new"], d["bad_names"], d["skipped"]) == (5, 2, 3) and "warning" not in d
    assert d["bad_name_examples"] == [f"2024-001 {name(1)}/Pr�fung.txt",
                                      f"2024-001 {name(1)}/Gutachten �lter/Bericht.txt"]
    assert "2 Datei(en) nicht gelesen" in caplog.text and "iocharset=utf8" in caplog.text
    # the status page names them; said once – the next pass extracts nothing and warns no more
    caplog.clear()
    extracted.clear()
    stats = run_index(s)
    assert extracted == [] and stats["documents"]["unchanged"] == 5 and stats["documents"]["bad_names"] == 2
    assert "Zeichenkodierung" not in caplog.text
    page = section(client_for(s).get("/status").text)
    assert "2 Dateien in Projektordnern werden nicht gelesen" in page and "Pr�fung.txt" in page
    assert "iocharset=utf8" in page
    # renamed (share mounted with iocharset=utf8): read, and the notice disappears
    os.rename(first + b"/Pr\xfcfung.txt", first + "/Prüfung.txt".encode())
    os.rename(first + b"/Gutachten \xe4lter", first + "/Gutachten älter".encode())
    stats = run_index(s)
    assert stats["documents"]["new"] == 2 and stats["documents"]["bad_names"] == 0
    assert not any("Zeichenkodierung" in n for n in client_for(s).get("/api/status").json()["layout"]["notices"])


def test_a_project_that_cannot_be_stored_never_stops_the_later_ones(tmp_path, monkeypatch):
    """Safety net: whatever text cannot be stored costs that file (or that project), never the pass."""
    root = five_projects(tmp_path / "p")
    s = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN=YEAR_NUM)
    real_write = documents.write_document

    def write_document(conn, doc, **kw):
        if kw["project"] == "2024-002":
            raise UnicodeEncodeError("utf-8", "\udcfc", 0, 1, "surrogates not allowed")
        return real_write(conn, doc, **kw)

    monkeypatch.setattr(documents, "write_document", write_document)
    stats = run_index(s)
    assert stats["documents"]["errors"] == 1 and "warning" not in stats["documents"]
    assert sorted(documents_by_project(s)) == ["2024-001", "2024-003", "2024-004", "2024-005"]
    real_index = documents.index_project_documents
    monkeypatch.setattr(documents, "write_document", real_write)

    def index_project(conn, project, **kw):
        if project["number"] == "2024-003":
            raise UnicodeEncodeError("utf-8", "\udcfc", 0, 1, "surrogates not allowed")
        return real_index(conn, project, **kw)

    monkeypatch.setattr(documents, "index_project_documents", index_project)
    stats = run_index(s)
    assert stats["documents"]["errors"] == 1 and sorted(documents_by_project(s)) == [
        "2024-001", "2024-002", "2024-003", "2024-004", "2024-005"]


def test_project_folders_in_a_wrong_encoding_are_read(tmp_path):
    """verify3 builder follow-up: such project folders are stored with �, and their files were never
    read ("nicht erreichbar"). They are read under their real path now, stored under the printable one."""
    root = bad_share(tmp_path / "p")
    raw = os.fsencode(root)
    write(raw + b"/Jahr \xe4lter/2024-020 Halle/Bericht.txt")  # a container with such a name, too
    s = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN=YEAR_NUM)
    stats = run_index(s)
    assert stats["projects"]["added"] == 9 and stats["documents"]["new"] == 9
    with db.session(s.db_path) as conn:
        paths = [r[0] for r in conn.execute("SELECT path FROM documents WHERE project IN ('2024-001', '2024-020') "
                                            "ORDER BY project")]
        assert paths == [str(root / "2024-001 B�ro 1" / "Bericht.txt"),
                         str(root / "Jahr �lter" / "2024-020 Halle" / "Bericht.txt")]
        hits = conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'rettungsweg'").fetchone()[0]
    assert hits == 9
    again = run_index(s)["documents"]
    assert (again["new"], again["unchanged"], again["removed"]) == (0, 9, 0)
    # a file deleted in such a folder leaves the index
    os.remove(raw + b"/2024-001 B\xfcro 1/Bericht.txt")
    assert run_index(s)["documents"]["removed"] == 1


def test_real_folder_resolves_names_that_differ_only_in_bad_bytes(tmp_path):
    raw = os.fsencode(tmp_path)
    os.makedirs(raw + b"/Pr\xfcf/2024-001 A")
    os.makedirs(raw + b"/Pr\xe4f/2024-002 B")
    os.makedirs(raw + b"/Gem\xfcse/X")
    os.makedirs(raw + b"/Gem\xe4se/X")
    resolve = documents.real_folder
    assert resolve(tmp_path / "Pr�f" / "2024-001 A") == Path(os.fsdecode(raw + b"/Pr\xfcf/2024-001 A"))
    assert resolve(tmp_path / "Pr�f" / "2024-002 B") == Path(os.fsdecode(raw + b"/Pr\xe4f/2024-002 B"))
    assert resolve(tmp_path / "Gem�se" / "X") is None  # truly ambiguous
    assert resolve(tmp_path / "Pr�f" / "fehlt") is None
    assert resolve(tmp_path / "ganz normal") == tmp_path / "ganz normal"


# ============================================================================ 2. auto mode reads the confirmed depth
def test_auto_reads_exactly_the_depth_that_was_confirmed(tmp_path, caplog):
    """verify3 f3: a stale PROJECT_FOLDER_DEPTH=3 next to auto made the app read the exchange copies
    (00_Austausch/2024-005 Kopie …) instead of the real project folders the person confirmed."""
    root = mk(tmp_path / "p", *(f"2024-{i:03d} {name(i)}/Pläne" for i in range(1, 81)),
              *(f"00_Austausch/2024-{i:03d} Kopie für Statiker" for i in (5, 17, 33)))
    s = settings_for(tmp_path, root, PROJECT_FOLDER_DEPTH=3)
    assert s.project_folder_depth == 3 and any("PROJECT_FOLDER_DEPTH=3 gilt nicht zusammen mit "
                                               "PROJECT_NUMBER_PATTERN=auto" in w for w in s.config_warnings)
    run_index(s)
    layout = structure.current_layout(s)
    assert layout.proposal["depth"] == 1 and layout.depth == 1 and layout.ignored_depth == 3
    page = section(client_for(s).get("/status").text)
    assert "PROJECT_FOLDER_DEPTH=3 ist eingestellt, gilt aber nicht zusammen mit PROJECT_NUMBER_PATTERN=auto" in page
    confirm(s)
    run_index(s)
    with db.session(s.db_path) as conn:
        paths = dict(conn.execute("SELECT number, path FROM projects WHERE number IN ('2024-005', '2024-017')"))
    assert paths == {"2024-005": str(root / f"2024-005 {name(5)}"), "2024-017": str(root / f"2024-017 {name(17)}")}
    assert len(active(s)) == 80
    layout = structure.current_layout(s)
    assert layout.depth == 1 and "Ordnertiefe des bestätigten Vorschlags, 1" in layout.notices[0]
    api = client_for(s).get("/api/status").json()["layout"]
    assert api["depth"] == 1 and api["ignored_depth"] == 3 and "PROJECT_FOLDER_DEPTH=3" in api["notices"][0]
    caplog.set_level(logging.WARNING)
    log_startup(s, NoAuth())
    assert "Konfiguration: PROJECT_FOLDER_DEPTH=3 gilt nicht" in caplog.text
    assert "Projektordner: PROJECT_FOLDER_DEPTH=3 ist eingestellt" in caplog.text


def test_auto_with_a_smaller_configured_depth_still_reads_the_projects(tmp_path, monkeypatch, capsys):
    """verify3 f1: PROJECT_FOLDER_DEPTH=1 with projects in year folders – 0 projects read, no notice."""
    root = year_share(tmp_path / "p")
    s = settings_for(tmp_path, root, PROJECT_FOLDER_DEPTH=1)
    run_index(s)
    confirm(s)
    assert run_index(s)["projects"]["added"] == 16
    # a later detection of the same scheme agrees – no new proposal because of the setting
    with db.session(s.db_path) as conn:
        structure.request_recheck(conn, s)
    run_index(s)
    assert structure.current_layout(s).proposal is None
    # the command line says it, too
    for key, value in (("PROJECTS_DIR", root), ("DATA_DIR", s.data_dir), ("PROJECT_NUMBER_PATTERN", "auto"),
                       ("PROJECT_FOLDER_DEPTH", "1")):
        monkeypatch.setenv(key, str(value))
    main(["--ohne-dateien"])
    out = capsys.readouterr().out
    assert "PROJECT_FOLDER_DEPTH gilt nicht zusammen mit PROJECT_NUMBER_PATTERN=auto" in out
    assert "verwendet: \\d{4}-\\d{3}, Tiefe 2" in out


def test_depth_that_matches_the_confirmed_one_is_no_notice(tmp_path):
    root = year_share(tmp_path / "p")
    s = settings_for(tmp_path, root, PROJECT_FOLDER_DEPTH=2)
    run_index(s)
    confirm(s)
    layout = structure.current_layout(s)
    assert layout.ignored_depth == 2 and layout.depth_notice is None and layout.notices == []


# ============================================================================ 3. hand-written lookaheads are no guards
def test_hand_written_lookahead_gets_no_year_notice(tmp_path):
    """verify3 g3: (?!AN)… excludes the AN- inquiries on purpose – no 'Jahreszahl oder Datum' notice."""
    root = mk(tmp_path / "p", *(f"BS-{i:04d} Projekt/Pläne" for i in range(1, 30)),
              *(f"AN-{i:04d} Anfrage" for i in range(1, 30)))
    s = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN=r"(?!AN)[A-Z]{2}-\d{4}")
    stats = run_index(s)
    assert stats["projects"]["added"] == 29 and "guard_excluded" not in stats["projects"]
    assert client_for(s).get("/api/status").json()["layout"]["notices"] == []
    excluded: list = []
    scan_projects(root, r"(?!AN)[A-Z]{2}-\d{4}", excluded=excluded)
    assert excluded == []


@pytest.mark.parametrize("pattern, loose", [
    (r"(?!AN)[A-Z]{2}-\d{4}", None),
    (r"(?!(?:19|20)\d\d(?!\d))(?!AN)\d{4}", r"(?!AN)\d{4}"),
    (r"(?!(?:19|20)\d\d(?!\d))(?!(?:19|20)\d\d(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?!\d))\d{4,8}", r"\d{4,8}"),
    (r"(?!(?:19|20)\d\d[-_.\s]?(?:19|20)\d\d(?!\d))\d{4}[-_.\s]?\d{4}", r"\d{4}[-_.\s]?\d{4}"),
    (r"(?!(?:19|20)\d\d\s(?:19|20)\d\d(?!\d))\d{4}\s\d{4}", r"\d{4}\s\d{4}"),
    (r"(?!19)\d{4}", None),
])
def test_only_the_detection_guards_are_guards(pattern, loose):
    assert loose_pattern(pattern) == loose


def test_every_guard_the_detection_writes_is_known():
    for sep in ("", "-", "_", r"\.", r"\s", "[-_]", r"[-.\s]?", "-?"):
        assert is_guard(year_range_guard(sep)), sep
    assert is_guard(structure._YEAR_GUARD) and is_guard(structure._DATE_GUARD)
    assert not is_guard("(?!AN)") and not is_guard(r"(?!(?:19|20)\d\d)")


# ============================================================================ 4. offers next to projects
def offers_and_projects(root: Path) -> Path:
    rnd = random.Random(21)
    for y in (2022, 2023, 2024):
        mk(root, *(f"{y}/A-{y}-{i:03d} Angebot {rnd.choice(OBJ)}" for i in range(1, 101)))
        for i in range(1, 36):
            mk(root, *(f"{y}/P-{y}-{i:03d} {OBJ[i % 7]}/{sub}" for sub in SUBS[:2]))
    return root


def test_offers_proposed_as_projects_are_flagged(tmp_path):
    """verify2/3 n1 M2: 300 offers A-… next to 105 projects P-… in the same year folders."""
    p = analyse(offers_and_projects(tmp_path)).proposal
    assert p.pattern == r"A-\d{4}-\d{3}"  # the names alone cannot decide – but the evidence says so
    office = next(n for n in p.notes if "Wort der Büroablage" in n)
    assert office.startswith("100 % der vorgeschlagenen Projektordner") and "statt Projekte?" in office
    richer = next(n for n in p.notes if "eigene Unterordner" in n)
    assert r"PROJECT_NUMBER_PATTERN=P-\d{4}-\d{3} und PROJECT_FOLDER_DEPTH=2" in richer
    assert "100 % seiner Ordner haben eigene Unterordner, von den vorgeschlagenen Projektordnern nur 0 %" in richer
    page_notes = structure.proposal_record(analyse(tmp_path), tmp_path, structure._now())["notes"]
    assert any("Wort der Büroablage" in n for n in page_notes)


def test_offers_only_flagged_by_their_labels(tmp_path):
    """Office words alone (no further scheme): the point to check without a remedy."""
    mk(tmp_path, *(f"{y}/{y}-{i:03d} Angebot {OBJ[i % 7]}" for y in (2023, 2024) for i in range(1, 31)))
    notes = analyse(tmp_path).proposal.notes
    assert len(notes) == 1 and "Wort der Büroablage" in notes[0] and "PROJECT_NUMBER_PATTERN" not in notes[0]


def test_projects_with_a_few_office_words_are_not_flagged(tmp_path):
    mk(tmp_path, *(f"{y}/{y}-{i:03d} {'Verwaltungsgebäude Angebot' if i % 4 == 0 else OBJ[i % 7]}/Pläne"
                   for y in (2023, 2024) for i in range(1, 31)),
       *(f"{y}/RE-{y}-{i:04d} Rechnung" for y in (2023, 2024) for i in range(1, 11)))
    p = analyse(tmp_path).proposal
    assert p.pattern == YEAR_NUM and p.notes == []  # the structured scheme is the proposed one


def test_a_more_structured_scheme_beside_is_flagged(tmp_path):
    """Leaf folders proposed, a scheme with folders of its own in the same folders – no office words."""
    mk(tmp_path, *(f"{y}/X-{y}-{i:03d} {OBJ[i % 7]}" for y in (2023, 2024) for i in range(1, 61)),
       *(f"{y}/{y}-{i:03d} {OBJ[i % 7]}/{sub}" for y in (2023, 2024) for i in range(1, 26) for sub in SUBS))
    p = analyse(tmp_path).proposal
    assert p.pattern == r"X-\d{4}-\d{3}"
    assert len(p.notes) == 1 and r"die Ordner von \d{4}-\d{3} die Projekte?" in p.notes[0]


# ============================================================================ 5. customer numbers with sub-projects
@pytest.mark.parametrize("prefix, base, expression", [("", 1020, r"\d{4}-\d{2}"), ("", 10020, r"\d{5}-\d{2}"),
                                                      ("K", 1020, r"K\d{4}-\d{2}")])
def test_customers_holding_numbered_projects_are_flagged(tmp_path, prefix, base, expression):
    """verify2/3 C1/C1b/C2: 30 customers with 1–6 sub-projects each ('1020 Stadt Aachen/1020-01 Kita')."""
    rnd = random.Random(3)
    for c in range(30):
        customer = f"{prefix}{base + c} {rnd.choice(['Stadt Aachen', 'Wohnbau eG', 'Müller GmbH'])}"
        mk(tmp_path, *(f"{customer}/{prefix}{base + c}-{j:02d} {rnd.choice(OBJ)}/{sub}"
                       for j in range(1, rnd.randint(3, 7)) for sub in SUBS))
    p = analyse(tmp_path).proposal
    assert p.projects == 30
    note = next(n for n in p.notes if "sind vielleicht die Unterordner die Projekte?" in n)
    assert note.startswith("Die vorgeschlagenen Ordner enthalten je mehrere nummerierte Unterordner mit eigener "
                           "Ablage") and f"(etwa {expression})" in note and "PROJECT_FOLDER_DEPTH=2" in note


def test_the_review_case_is_flagged_on_the_status_page(tmp_path):
    root = mk(tmp_path / "p", *(f"{1020 + c} {NAMES[c % 10]}/{1020 + c}-{i:02d} {name(c * 5 + i)}/Pläne"
                                for c in range(20) for i in range(1, 5)))
    s = settings_for(tmp_path, root)
    run_index(s)
    page = section(client_for(s).get("/status").text)
    assert "Bitte prüfen" in page and "sind vielleicht die Unterordner die Projekte?" in page


@pytest.mark.parametrize("layout", ["outline", "records", "few", "empty"])
def test_inner_numbering_that_is_no_project_is_not_flagged(tmp_path, layout):
    """Internal outlines (2024-017_01 Pläne …), records of one kind (PB-… Prüfbericht), one or two
    sub-projects per project and empty placeholders are no point to check."""
    paths = []
    k = 1000
    for i in range(1, 41):
        project = f"2024-{i:03d} {name(i)}"
        if layout == "outline":
            paths += [f"{project}/2024-{i:03d}_{j:02d} {sub}/Dateien" for j, sub in enumerate(
                ("Schriftverkehr", "Pläne", "Brandschutzkonzept", "Fotos"), 1)]
        elif layout == "records":  # reports with a file each, next to the project's own folders
            paths.append(f"{project}/Pläne")
            for _ in range(3):
                k += 1
                paths.append(f"{project}/PB-24-{k:04d} Prüfbericht")
        elif layout == "few":
            paths += [f"{project}/2024-{i:03d}-{j:02d} {OBJ[(i + j) % 7]}/Pläne" for j in (1, 2)]
        else:
            paths += [f"{project}/2024-{i:03d}-{j:02d} {OBJ[(i + j) % 7]}" for j in (1, 2, 3, 4)]
    mk(tmp_path, *paths)
    for path in paths:
        if "Prüfbericht" in path:
            (tmp_path / path / "Bericht.pdf").write_bytes(b"x")
    p = analyse(tmp_path).proposal
    assert (p.pattern, p.projects) == (YEAR_NUM, 40) and p.notes == []


def test_customer_folders_with_projects_labelled_projekt(tmp_path):
    """Round 1 case R, a regression of the record rule: '1020 Stadt/1020-001 Projekt' (all labelled
    'Projekt', files only) – the customers were proposed. A label that says 'Projekt' is no record kind."""
    for c in range(8):
        for i in range(1, 6):
            (mk(tmp_path, f"{1020 + c} Stadt {c}/{1020 + c}-{i:03d} Projekt") / f"{1020 + c} Stadt {c}"
             / f"{1020 + c}-{i:03d} Projekt" / "a.pdf").write_bytes(b"x")
    p = analyse(tmp_path).proposal
    assert (p.pattern, p.depth, p.projects) == (r"\d{4}-\d{3}", 2, 40)


# ============================================================================ 6. an empty mount point
def test_empty_mount_point_keeps_the_pending_proposal(tmp_path):
    """verify3 f4: the share briefly not mounted (an empty directory) deleted the pending proposal – the
    person who clicked 'Übernehmen' got 409, and it stayed away for up to an hour."""
    root = year_share(tmp_path / "p")
    s = settings_for(tmp_path, root)
    run_index(s)
    wanted = pending_id(s)
    os.rename(root, tmp_path / "weg")
    root.mkdir()
    with db.session(s.db_path) as conn:
        structure.request_recheck(conn, s)
    stats = run_index(s)
    assert "enthält keine Ordner mehr" in stats["projects"]["warning"]
    assert "der Vorschlag bleibt bestehen" in stats["projects"]["warning"]
    layout = structure.current_layout(s)
    assert layout.state == "pending" and layout.proposal["id"] == wanted
    root.rmdir()
    os.rename(tmp_path / "weg", root)
    run_index(s)
    response = client_for(s).post("/status/projektstruktur/uebernehmen", data={"vorschlag": wanted},
                                  follow_redirects=False)
    assert response.status_code == 303 and structure.current_layout(s).pattern == YEAR_NUM
    assert run_index(s)["projects"]["added"] == 16


def test_a_detection_that_saw_no_folder_keeps_the_proposal(tmp_path):
    """The share emptied while it was read (or the analysis failed): the pending proposal stays."""
    root = year_share(tmp_path / "p")
    s = settings_for(tmp_path, root)
    run_index(s)
    wanted = pending_id(s)
    for result in (structure.Analysis(root=str(root), max_depth=6, reason="Der Ordner enthält keine Unterordner"),
                   structure.Analysis(root=str(root), max_depth=6, reason="Erkennung fehlgeschlagen (OSError: x)")):
        with db.session(s.db_path) as conn:
            structure._apply_detection(conn, s, result, structure._now())
        assert structure.current_layout(s).proposal["id"] == wanted


def test_confirmed_scheme_and_an_empty_share(tmp_path):
    root = year_share(tmp_path / "p")
    s = settings_for(tmp_path, root)
    run_index(s)
    confirm(s)
    run_index(s)
    check = meta(s, structure.CHECK_KEY)
    for p in list(root.iterdir()):
        os.rename(p, tmp_path / p.name)
    with db.session(s.db_path) as conn:
        structure.request_recheck(conn, s)
    stats = run_index(s)
    assert "keine Projekte mehr" in stats["projects"]["warning"] and len(active(s)) == 16
    assert meta(s, structure.CHECK_KEY)["outcome"] == check["outcome"] == "proposal"  # nothing detected
    assert structure.current_layout(s).notices == []


# ============================================================================ 7. cosmetics
def test_api_status_never_carries_the_projects_dir(tmp_path):
    """verify3 p1: the records keep the absolute PROJECTS_DIR – /api/status leaves it out, in every state."""
    root = year_share(tmp_path / "p")
    s = settings_for(tmp_path, root)
    client = client_for(s)

    def api() -> str:
        return json.dumps(client.get("/api/status").json()["layout"], ensure_ascii=False)

    run_index(s)
    assert str(root) not in api() and json.loads(api())["proposal"]["pattern"] == YEAR_NUM  # pending
    with db.session(s.db_path) as conn:
        structure.reject_proposal(conn, s, pending_id(s), "pruefer")
    assert str(root) not in api() and json.loads(api())["rejected"]["pattern"] == YEAR_NUM  # rejected
    with db.session(s.db_path) as conn:
        structure.request_recheck(conn, s, forget_rejection=True)
    run_index(s)
    confirm(s)
    run_index(s)
    data = json.loads(api())
    assert str(root) not in api() and data["confirmed"]["pattern"] == YEAR_NUM and "root" not in data["check"]
    unclear = mk(tmp_path / "q", "Verwaltung", "Fotos")  # no clear scheme
    s2 = settings_for(tmp_path / "zwei", unclear)
    run_index(s2)
    text = json.dumps(client_for(s2).get("/api/status").json()["layout"], ensure_ascii=False)
    assert json.loads(text)["state"] == "none" and str(unclear) not in text


def test_no_clear_scheme_after_a_rejection_is_shown(tmp_path):
    """verify3 f4 part 2: the rejection hid a later detection that found no clear scheme."""
    root = year_share(tmp_path / "p")
    s = settings_for(tmp_path, root)
    run_index(s)
    with db.session(s.db_path) as conn:
        structure.reject_proposal(conn, s, pending_id(s), "pruefer")
    for p in list(root.iterdir()):
        os.rename(p, tmp_path / p.name)
    mk(root, "Verwaltung", "Fotos")
    with db.session(s.db_path) as conn:
        structure.request_recheck(conn, s)
    run_index(s)
    layout = structure.current_layout(s)
    assert layout.state == "rejected" and layout.unclear
    page = section(client_for(s).get("/status").text)
    assert "Abgelehnt am" in page and "Kein eindeutiges Schema erkannt" in page
    assert "Kein Ordnername beginnt mit einer Nummer." in page
    assert client_for(s).get("/api/status").json()["layout"]["unclear"] is True


def test_command_line_confirmation_names_the_way_once(tmp_path, monkeypatch, capsys, caplog):
    root = year_share(tmp_path / "p")
    s = settings_for(tmp_path, root)
    for key, value in (("PROJECTS_DIR", root), ("DATA_DIR", s.data_dir), ("PROJECT_NUMBER_PATTERN", "auto")):
        monkeypatch.setenv(key, str(value))
    caplog.set_level(logging.WARNING, logger="projektakte.structure")
    assert main(["--uebernehmen", "--ohne-dateien"]) == 0
    capsys.readouterr()
    line = next(r.getMessage() for r in caplog.records if "Projektstruktur bestätigt" in r.getMessage())
    assert line.count("(Kommandozeile)") == 1 and "Tiefe 2" in line
    with db.session(s.db_path) as conn:
        structure.store_confirmed(conn, structure.current_layout(s, conn).confirmed, "anna", "Statusseite")
    assert "bestätigt von anna (Statusseite):" in caplog.text


def test_file_statistics_name_files_in_a_wrong_encoding(tmp_path, capsys):
    root = five_projects(tmp_path / "p")
    write(os.fsencode(root / f"2024-002 {name(2)}") + b"/Pr\xfcfung.pdf")
    a = analyse(root)
    stats = structure.file_statistics(a.projects, root)
    assert (stats["readable"], stats["bad_names"], stats["files"]) == (5, 1, 6)
    assert main([str(root)]) == 0
    assert "nicht lesbar, Name mit falscher Zeichenkodierung: 1 – Freigabe mit iocharset=utf8 einbinden" in (
        capsys.readouterr().out)
