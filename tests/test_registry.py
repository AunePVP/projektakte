from fastapi.testclient import TestClient

from projektakte import db
from projektakte.config import load_settings
from projektakte.demo import generate
from projektakte.indexer import run_index
from projektakte.main import create_app
from projektakte.registry import number_regex, scan_projects, sync_projects


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
    s = load_settings({"DATA_DIR": str(tmp_path), "DEMO_MODE": "1"})
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
