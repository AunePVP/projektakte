"""A project folder that is missing for a while (share hiccup, folder renamed and renamed back): its
files leave search and answers at once, but are deleted only after 24 h and 3 consecutive passes. If
the folder comes back sooner, they return unchanged – same ids, no new extraction or embedding.
Also: broken links and link loops are reported as such."""
import logging
from datetime import timedelta

import numpy as np
import pytest

from projektakte import db, documents, registry
from projektakte.ai.services import AIServices
from projektakte.demo import generate
from projektakte.documents import index_all_documents
from projektakte.indexer import run_index
from projektakte.registry import scan_projects, sync_from_folder, sync_projects
from projektakte.search import hybrid_search
from projektakte.vectors import VECTOR_INDEX, to_blob

from .conftest import fake_settings

PATTERN = r"BS-\d{4}-\d{4}"


@pytest.fixture()
def two_projects(tmp_path, monkeypatch):
    root = tmp_path / "projekte"
    a = root / "2024" / "BS-2024-0001 Testhalle"
    b = root / "2024" / "BS-2024-0002 Kita"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    (a / "Notiz.txt").write_text("Rettungsweg über den Treppenraum Nord, Rauchabzug 1 m².", encoding="utf-8")
    (a / "Auflagen.md").write_text("# Auflagen\nBrandwand zwischen Halle und Lager in F90.", encoding="utf-8")
    (b / "Notiz.txt").write_text("Kita: zweiter Rettungsweg über Außentreppe.", encoding="utf-8")
    dbp = tmp_path / "x.sqlite3"
    db.init_db(dbp)
    conn = db.connect(dbp)
    clock = {"now": registry._utcnow()}
    monkeypatch.setattr(registry, "_utcnow", lambda: clock["now"])
    extracted = []
    real_extract = documents.extract
    monkeypatch.setattr(documents, "extract", lambda *a, **k: extracted.append(a[0]) or real_extract(*a, **k))
    state = {"conn": conn, "root": root, "a": a, "clock": clock, "extracted": extracted, "db": dbp}
    run(state)
    # simple vectors so that vector search can be checked as well
    for (cid,) in conn.execute("SELECT id FROM chunks").fetchall():
        conn.execute("UPDATE chunks SET embedding = ? WHERE id = ?", (to_blob([1.0, cid / 1000.0]), cid))
    db.set_meta(conn, "embed_generation", "1")
    conn.commit()
    extracted.clear()
    yield state
    conn.close()


def run(state, hours: float = 0.0, unreadable=()):
    """One pass (register + documents), ``hours`` after the previous one."""
    state["clock"]["now"] += timedelta(hours=hours)
    conn = state["conn"]
    folders = scan_projects(state["root"], PATTERN)
    stats = sync_projects(conn, folders, unreadable=unreadable)
    conn.commit()
    stats["documents"] = index_all_documents(conn, projects_dir=state["root"])
    return stats


def file_ids(conn, project):
    return sorted(r[0] for r in conn.execute(
        "SELECT id FROM documents WHERE project = ? AND mail_id IS NULL", (project,)))


def fts_projects(conn):
    return {r[0] for r in conn.execute("SELECT DISTINCT c.project FROM chunks_fts f JOIN chunks c ON c.id = f.rowid")}


def vector_projects(state, project=None):
    conn = state["conn"]
    hits = VECTOR_INDEX.search(conn, str(state["db"]), np.array([1.0, 0.0]), project=project, k=50)
    ids = [c for c, _ in hits]
    if not ids:
        return set()
    return {r[0] for r in conn.execute(f"SELECT DISTINCT project FROM chunks WHERE id IN ({','.join(map(str, ids))})")}


def missing(conn, project):
    return tuple(conn.execute("SELECT active, missing_since IS NOT NULL, missing_runs FROM projects WHERE number = ?",
                              (project,)).fetchone())


def test_briefly_missing_folder_comes_back_unchanged(two_projects):
    s = two_projects
    conn = s["conn"]
    ids = file_ids(conn, "BS-2024-0001")
    chunks = sorted(r[0] for r in conn.execute("SELECT id FROM chunks WHERE project = 'BS-2024-0001'"))
    away = s["root"] / "weg"
    s["a"].rename(away)

    stats = run(s, hours=0.1)
    assert (stats["deactivated"], stats["documents_hidden"], stats["documents_removed"]) == (1, 2, 0)
    assert stats["documents"]["removed"] == 0
    assert file_ids(conn, "BS-2024-0001") == ids  # kept …
    assert fts_projects(conn) == {"BS-2024-0002"}  # … but neither full text …
    assert vector_projects(s) == {"BS-2024-0002"}  # … nor vectors find them
    assert vector_projects(s, project="BS-2024-0001") == set()
    assert missing(conn, "BS-2024-0001") == (0, 1, 1)

    away.rename(s["a"])
    stats = run(s, hours=0.1)
    assert (stats["reactivated"], stats["documents_restored"]) == (1, 2)
    assert stats["documents"]["unchanged"] == 3 and stats["documents"]["new"] == 0
    assert s["extracted"] == []  # nothing extracted again …
    assert file_ids(conn, "BS-2024-0001") == ids  # … same documents, links /dokumente/{id} stay valid
    assert sorted(r[0] for r in conn.execute(
        "SELECT id FROM chunks WHERE project = 'BS-2024-0001' AND embedding IS NOT NULL")) == chunks  # vectors kept
    assert fts_projects(conn) == {"BS-2024-0001", "BS-2024-0002"}
    assert vector_projects(s) == {"BS-2024-0001", "BS-2024-0002"}
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] == \
        conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert missing(conn, "BS-2024-0001") == (1, 0, 0)


def test_files_are_deleted_only_after_24_hours_and_3_passes(two_projects):
    s = two_projects
    conn = s["conn"]
    s["a"].rename(s["root"] / "weg")
    assert run(s)["documents_hidden"] == 2
    assert run(s, hours=1)["documents_removed"] == 0  # 2nd pass
    assert run(s, hours=1)["documents_removed"] == 0  # 3rd pass, but only 2 h
    assert file_ids(conn, "BS-2024-0001")
    assert run(s, hours=22.5)["documents_removed"] == 2  # 4th pass, 24.5 h
    assert file_ids(conn, "BS-2024-0001") == []
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] == \
        conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    # the project stays known as inactive; later passes have nothing left to delete
    assert missing(conn, "BS-2024-0001")[:2] == (0, 1)
    assert run(s, hours=1)["documents_removed"] == 0


def test_a_long_gap_alone_is_not_enough(two_projects):
    """Server off over the weekend: 60 h but only two passes – the files stay until the third."""
    s = two_projects
    s["a"].rename(s["root"] / "weg")
    run(s)
    assert run(s, hours=60)["documents_removed"] == 0
    assert run(s, hours=0.1)["documents_removed"] == 2


def test_reappearing_starts_the_grace_period_again(two_projects):
    s = two_projects
    conn = s["conn"]
    away = s["root"] / "weg"
    s["a"].rename(away)
    run(s)
    run(s, hours=20)
    away.rename(s["a"])
    run(s, hours=1)
    s["a"].rename(away)
    run(s, hours=1)  # missing again: pass 1 of a new period
    assert run(s, hours=5)["documents_removed"] == 0  # 27 h after the first disappearance
    assert run(s, hours=5)["documents_removed"] == 0  # pass 3, but only 10 h
    assert run(s, hours=15)["documents_removed"] == 2
    assert file_ids(conn, "BS-2024-0001") == []


def test_folder_that_cannot_be_listed_does_not_count_as_missing(two_projects):
    s = two_projects
    s["a"].rename(s["root"] / "weg")
    run(s)
    for _ in range(3):  # the year folder cannot be listed – no proof that the project is gone
        assert run(s, hours=12, unreadable=[s["root"] / "2024"])["documents_removed"] == 0
    assert missing(s["conn"], "BS-2024-0001") == (0, 1, 1)


def test_folder_back_under_another_path_is_moved_not_extracted(two_projects):
    s = two_projects
    conn = s["conn"]
    ids = file_ids(conn, "BS-2024-0001")
    s["a"].rename(s["root"] / "weg")
    run(s)
    target = s["root"] / "2025" / "BS-2024-0001 Testhalle Nord"
    target.parent.mkdir()
    (s["root"] / "weg").rename(target)
    stats = run(s, hours=1)
    assert stats["reactivated"] == 1 and stats["documents"]["moved"] == 2 and s["extracted"] == []
    assert file_ids(conn, "BS-2024-0001") == ids
    titles = [r[0] for r in conn.execute("SELECT f.title FROM chunks_fts f JOIN chunks c ON c.id = f.rowid "
                                         "WHERE c.project = 'BS-2024-0001'")]
    assert len(titles) == 2 and all("testhalle nord" in t for t in titles)


def test_documents_of_an_older_version_without_grace_period_are_purged(two_projects):
    s = two_projects
    conn = s["conn"]
    conn.execute("UPDATE projects SET active = 0 WHERE number = 'BS-2024-0001'")  # missing_since NULL
    conn.commit()
    assert index_all_documents(conn, projects_dir=s["root"])["removed"] == 2


def test_migration_adds_columns_to_an_existing_register(tmp_path):
    dbp = tmp_path / "old.sqlite3"
    conn = db.connect(dbp)
    for i, script in enumerate(db.MIGRATIONS[:6], start=1):
        conn.executescript("BEGIN;" + script + f"PRAGMA user_version = {i}; COMMIT;")
    conn.execute("INSERT INTO projects(number, name, path, active) VALUES('BS-2020-0001', 'Alt', '/p/x', 0)")
    conn.commit()
    conn.close()
    db.init_db(dbp)
    conn = db.connect(dbp)
    assert tuple(conn.execute("SELECT active, missing_since, missing_runs FROM projects").fetchone()) == (0, None, 0)


# -- whole pipeline: demo data, search with the (fake) AI services -----------------------------------
def test_briefly_missing_demo_project_with_search(tmp_path, fake_ai):
    generate(tmp_path / "demo")
    s = fake_settings(fake_ai, tmp_path, DEMO_MODE=1)
    ai = AIServices.from_settings(s)
    run_index(s, ai)
    proj = next(s.projects_dir.glob("BS-2024-0137*"))
    with db.session(s.db_path) as conn:
        ids = file_ids(conn, "BS-2024-0137")

    def school_hits():
        with db.session(s.db_path) as conn:
            hits = hybrid_search(conn, ai, str(s.db_path), "Außentreppe Grundschule Baugenehmigung", k=10).hits
            return [h for h in hits if h.project == "BS-2024-0137" and h.kind == "document"]

    assert school_hits()
    away = tmp_path / "weg"
    proj.rename(away)
    stats = run_index(s, ai)
    assert stats["projects"]["deactivated"] == 1 and stats["documents"]["removed"] == 0
    assert school_hits() == []
    away.rename(proj)
    stats = run_index(s, ai)
    assert stats["projects"]["reactivated"] == 1 and stats["documents"]["new"] == 0
    assert stats["embeddings"]["embedded"] == 0
    with db.session(s.db_path) as conn:
        assert file_ids(conn, "BS-2024-0137") == ids
    assert school_hits()


# -- links: the log says what is wrong ------------------------------------------------------------------
def test_broken_links_and_loops_are_reported_as_such(tmp_path, caplog):
    root = tmp_path / "projekte"
    proj = root / "BS-2024-0001 Testhalle"
    proj.mkdir(parents=True)
    (proj / "Notiz.txt").write_text("Brandwand.", encoding="utf-8")
    (proj / "kaputt.txt").symlink_to(proj / "gibt-es-nicht.txt")
    (proj / "schleife.txt").symlink_to(proj / "schleife.txt")
    (tmp_path / "geheim.txt").write_text("Zebrafink", encoding="utf-8")
    (proj / "aussen.txt").symlink_to(tmp_path / "geheim.txt")
    (root / "BS-2024-0009 Weg").symlink_to(tmp_path / "fehlt", target_is_directory=True)
    dbp = tmp_path / "x.sqlite3"
    db.init_db(dbp)
    caplog.set_level(logging.DEBUG)
    with db.session(dbp) as conn:
        assert sync_from_folder(conn, root, PATTERN)["added"] == 1
        assert index_all_documents(conn, projects_dir=root)["new"] == 1
    log = "\n".join(r.getMessage() for r in caplog.records)
    assert f"Verknüpfung ohne Ziel übersprungen: {proj / 'kaputt.txt'}" in log
    assert f"Verknüpfung mit Schleife übersprungen: {proj / 'schleife.txt'}" in log
    assert f"Verknüpfung auf Ziel außerhalb von PROJECTS_DIR übersprungen: {proj / 'aussen.txt'}" in log
    assert f"Verknüpfung ohne Ziel übersprungen: {root / 'BS-2024-0009 Weg'}" in log
    assert "kaputt.txt" not in "".join(line for line in log.splitlines() if "außerhalb" in line)
