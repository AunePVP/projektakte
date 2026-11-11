"""PROJECTS_TOP_FOLDERS: only the named folders directly in PROJECTS_DIR are searched for projects (e.g. 1,…,9);
other folders there (templates, archives, a stray project folder) are left out."""
from __future__ import annotations

from projektakte import db
from projektakte.config import load_settings
from projektakte.registry import scan_projects, sync_from_folder

PATTERN = r"\d{6}"


def _share(tmp_path):
    root = tmp_path / "projekte"
    for rel in ("1/10/100100_Neubau Kita", "2/21/210300_Umbau Halle", "3/31/310500_Sporthalle",
                "Vorlagen/900100_Muster", "Archiv/alt/880200_Lager", "770300_Lose im Wurzelordner"):
        (root / rel).mkdir(parents=True)
        (root / rel / "Bericht.txt").write_text("Brandschutz", encoding="utf-8")
    return root


def _active(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT number FROM projects WHERE active = 1")}


def test_scan_reads_only_the_named_top_folders(tmp_path):
    root = _share(tmp_path)
    everything = {p.number for p in scan_projects(root, PATTERN, 6)}
    assert everything == {"100100", "210300", "310500", "900100", "880200", "770300"}
    only = {p.number for p in scan_projects(root, PATTERN, 6, top_folders={"1", "2", "3", "4"})}
    assert only == {"100100", "210300", "310500"}


def test_projects_outside_become_inactive(tmp_path):
    root = _share(tmp_path)
    dbp = tmp_path / "x.sqlite3"
    db.init_db(dbp)
    conn = db.connect(dbp)
    sync_from_folder(conn, root, PATTERN, max_depth=6)
    conn.commit()
    assert "900100" in _active(conn)
    stats = sync_from_folder(conn, root, PATTERN, max_depth=6, top_folders=("1", "2", "3"))
    conn.commit()
    assert not stats.get("warning")
    assert _active(conn) == {"100100", "210300", "310500"}


def test_none_of_the_folders_there_changes_nothing(tmp_path):
    root = _share(tmp_path)
    dbp = tmp_path / "x.sqlite3"
    db.init_db(dbp)
    conn = db.connect(dbp)
    sync_from_folder(conn, root, PATTERN, max_depth=6)
    conn.commit()
    before = _active(conn)
    stats = sync_from_folder(conn, root, PATTERN, max_depth=6, top_folders=("8", "9"))
    assert "PROJECTS_TOP_FOLDERS" in stats["warning"]
    assert _active(conn) == before


def test_setting(tmp_path):
    s = load_settings({"DATA_DIR": str(tmp_path), "PROJECTS_TOP_FOLDERS": " 1, 2,3/,, 2 "})
    assert s.projects_top_folders == ("1", "2", "3")
    assert load_settings({"DATA_DIR": str(tmp_path)}).projects_top_folders == ()
