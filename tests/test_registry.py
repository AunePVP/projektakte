from fastapi.testclient import TestClient

from projektakte import db
from projektakte.config import load_settings
from projektakte.demo import generate
from projektakte.indexer import run_index
from projektakte.main import create_app
from projektakte.registry import (number_regex, projects_root_problem, scan_projects, sync_from_folder,
                                  sync_projects)

PATTERN = r"BS-\d{4}-\d{4}"


def test_scan_finds_projects_at_depth_two_and_ignores_others(tmp_path):
    (tmp_path / "2024" / "BS-2024-0137 Grundschule Am Lindenhof" / "01 Genehmigung").mkdir(parents=True)
    (tmp_path / "BS-2023-0412_Pflegeheim Sonnenhang").mkdir()
    (tmp_path / "BS-2023-0412_Pflegeheim Sonnenhang" / "BS-2099-0001 Unterordner").mkdir()
    (tmp_path / "Vorlagen").mkdir()
    (tmp_path / ".versteckt" / "BS-2020-0001 X").mkdir(parents=True)
    (tmp_path / "BS-2024-01377 zu lang").mkdir()
    found = scan_projects(tmp_path, r"BS-\d{4}-\d{4}")
    assert [(p.number, p.name) for p in found] == [
        ("BS-2023-0412", "Pflegeheim Sonnenhang"), ("BS-2024-0137", "Grundschule Am Lindenhof")]


def test_custom_pattern(tmp_path):
    (tmp_path / "P-12345 Rathaus").mkdir()
    (tmp_path / "BS-2024-0137 Schule").mkdir()
    assert [p.number for p in scan_projects(tmp_path, r"P-\d{5}")] == ["P-12345"]


def test_number_regex_boundaries():
    rx = number_regex(r"BS-\d{4}-\d{4}")
    assert rx.findall("Betreff: BS-2024-0137/Auflagen, bs-2023-0412.") == ["BS-2024-0137", "bs-2023-0412"]
    assert rx.findall("XBS-2024-0137 BS-2024-01370") == []


def test_sync_marks_removed_projects_inactive(tmp_path):
    root = tmp_path / "p"
    (root / "BS-2024-0001 A").mkdir(parents=True)
    (root / "BS-2024-0002 B").mkdir()
    dbp = tmp_path / "x.sqlite3"
    db.init_db(dbp)
    with db.session(dbp) as c:
        assert sync_projects(c, scan_projects(root, r"BS-\d{4}-\d{4}"))["added"] == 2
    (root / "BS-2024-0002 B").rmdir()
    with db.session(dbp) as c:
        stats = sync_projects(c, scan_projects(root, r"BS-\d{4}-\d{4}"))
        assert stats["deactivated"] == 1
        assert [r[0] for r in c.execute("SELECT number FROM projects WHERE active=1")] == ["BS-2024-0001"]


def test_projects_api_with_demo_data(tmp_path):
    generate(tmp_path / "demo")
    s = load_settings({"AUTH_DISABLED": "1", "DATA_DIR": str(tmp_path), "DEMO_MODE": "1"})
    stats = run_index(s)
    assert stats["projects"]["added"] == 8
    client = TestClient(create_app(s))
    data = client.get("/api/projects").json()
    assert data["count"] == 8
    numbers = [p["number"] for p in data["projects"]]
    assert "BS-2024-0137" in numbers
    assert client.get("/api/projects", params={"q": "Kita"}).json()["projects"][0]["number"] == "BS-2025-0019"
    p = client.get("/api/projects/BS-2024-0137").json()
    assert p["name"] == "Grundschule Am Lindenhof"
    assert p["path"] == "BS-2024-0137 Grundschule Am Lindenhof"
    assert client.get("/api/projects/bs-2024-0137").status_code == 200
    assert client.get("/api/projects/BS-1999-0000").status_code == 404
    # read-only: indexing never changes the source tree
    before = sorted(str(p) for p in (tmp_path / "demo").rglob("*"))
    run_index(s)
    assert sorted(str(p) for p in (tmp_path / "demo").rglob("*")) == before


# -- ops-06: an unavailable PROJECTS_DIR changes nothing ---------------------------------------------
def _registered(tmp_path):
    root = tmp_path / "p"
    (root / "BS-2024-0001 A").mkdir(parents=True)
    (root / "BS-2024-0001 A" / "Notiz.txt").write_text("Brandwand im Achsraster 4.", encoding="utf-8")
    (root / "BS-2024-0002 B").mkdir()
    dbp = tmp_path / "x.sqlite3"
    db.init_db(dbp)
    with db.session(dbp) as c:
        assert sync_from_folder(c, root, PATTERN)["added"] == 2
    return root, dbp


def test_missing_projects_dir_deactivates_nothing(tmp_path):
    root, dbp = _registered(tmp_path)
    with db.session(dbp) as c:
        stats = sync_from_folder(c, tmp_path / "nicht-eingebunden", PATTERN)
        assert "nicht vorhanden" in stats["warning"] and stats["deactivated"] == 0
        assert c.execute("SELECT COUNT(*) FROM projects WHERE active = 1").fetchone()[0] == 2


def test_projects_dir_suddenly_empty_deactivates_nothing(tmp_path):
    root, dbp = _registered(tmp_path)
    empty = tmp_path / "leer"
    empty.mkdir()
    with db.session(dbp) as c:
        stats = sync_from_folder(c, empty, PATTERN)
        assert "keine Projekte mehr" in stats["warning"] and "bisher 2 aktiv" in stats["warning"]
        assert c.execute("SELECT COUNT(*) FROM projects WHERE active = 1").fetchone()[0] == 2


def test_projects_dir_that_is_a_file_is_reported(tmp_path):
    f = tmp_path / "datei"
    f.write_text("x")
    assert "kein Ordner" in projects_root_problem(f)
    assert projects_root_problem(tmp_path) is None


def test_empty_projects_dir_on_a_fresh_install_is_fine(tmp_path):
    dbp = tmp_path / "x.sqlite3"
    db.init_db(dbp)
    (tmp_path / "leer").mkdir()
    with db.session(dbp) as c:
        stats = sync_from_folder(c, tmp_path / "leer", PATTERN)
    assert "warning" not in stats and stats["projects"] == 0


def test_unreadable_subfolder_keeps_its_projects_active(tmp_path):
    root = tmp_path / "p"
    (root / "2023" / "BS-2023-0001 Alt").mkdir(parents=True)
    (root / "2024" / "BS-2024-0001 Neu").mkdir(parents=True)
    dbp = tmp_path / "x.sqlite3"
    db.init_db(dbp)
    with db.session(dbp) as c:
        sync_projects(c, scan_projects(root, PATTERN))
        # 2023 could not be listed this time (share hiccup, permissions)
        folders = [f for f in scan_projects(root, PATTERN) if f.number != "BS-2023-0001"]
        stats = sync_projects(c, folders, unreadable=[root / "2023"])
        assert stats["deactivated"] == 0
        assert c.execute("SELECT active FROM projects WHERE number = 'BS-2023-0001'").fetchone()[0] == 1


def test_scan_reports_unreadable_folders(tmp_path, monkeypatch):
    root = tmp_path / "p"
    (root / "2024" / "BS-2024-0001 Neu").mkdir(parents=True)
    real_iterdir = type(root).iterdir

    def iterdir(self):
        if self.name == "2024":
            raise PermissionError(13, "Permission denied", str(self))
        return real_iterdir(self)

    monkeypatch.setattr(type(root), "iterdir", iterdir)
    unreadable = []
    assert scan_projects(root, PATTERN, unreadable=unreadable) == []
    assert unreadable == [root / "2024"]


def test_run_index_skips_projects_and_documents_while_unmounted(tmp_path):
    generate(tmp_path / "demo")
    s = load_settings({"AUTH_DISABLED": "1", "DATA_DIR": str(tmp_path), "DEMO_MODE": "1"})
    first = run_index(s)
    docs = first["documents"]["new"]
    unmounted = load_settings({"AUTH_DISABLED": "1", "DATA_DIR": str(tmp_path), "DEMO_MODE": "1",
                               "PROJECTS_DIR": str(tmp_path / "fehlt")})
    stats = run_index(unmounted)
    assert stats["projects"]["warning"] and stats["projects"]["deactivated"] == 0
    assert "nicht abgeglichen" in stats["documents"]["note"]
    with db.session(s.db_path) as c:
        assert c.execute("SELECT COUNT(*) FROM projects WHERE active = 1").fetchone()[0] == 8
        assert c.execute("SELECT COUNT(*) FROM documents WHERE mail_id IS NULL").fetchone()[0] == docs
    assert run_index(s)["documents"]["unchanged"] == docs  # nothing to redo once it is back
