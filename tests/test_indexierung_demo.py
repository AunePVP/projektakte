"""ops-05: switching from DEMO_MODE to real data removes the invented demo projects and demo mails
from search and answers – also when the real MAIL_DIR is missing or empty, and when the demo folder
was deleted meanwhile. Human decisions stay; demo folders configured as the source stay."""
import json
import shutil

import pytest

from projektakte import db, index
from projektakte.ai.services import AIServices
from projektakte.ask import ask
from projektakte.config import load_settings
from projektakte.demo import generate
from projektakte.demo_cleanup import DEMO_META, purge_demo_data
from projektakte.indexer import run_index
from projektakte.search import hybrid_search

from .conftest import fake_settings

DEMO_FACT = "Stellungnahme der Brandschutzdienststelle zum Löschwasserteich"


@pytest.fixture()
def demo_index(tmp_path):
    """The demo data set indexed with DEMO_MODE=1 (no AI services: fast)."""
    generate(tmp_path / "demo")
    s = load_settings({"DATA_DIR": str(tmp_path), "DEMO_MODE": "1"})
    run_index(s)
    with db.session(s.db_path) as conn:
        assert db.get_meta(conn, DEMO_META)
        conn.execute("INSERT INTO assign_decisions(source_key, action, project, decided_by) "
                     "SELECT source_key, 'assign', 'BS-2024-0137', 'anna' FROM mails ORDER BY id LIMIT 1")
    return s


def real_settings(tmp_path, **extra):
    real = tmp_path / "real"
    proj = real / "projekte" / "BS-2026-0001 Rathaus Nord"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "Notiz.txt").write_text("Rathaus Nord: Brandmeldeanlage nach DIN 14675.", encoding="utf-8")
    env = {"DATA_DIR": str(tmp_path), "PROJECTS_DIR": str(real / "projekte"), "MAIL_DIR": str(real / "mails")}
    env.update({k: str(v) for k, v in extra.items()})
    return load_settings(env)


def counts(s):
    with db.session(s.db_path) as conn:
        def one(sql):
            return conn.execute(sql).fetchone()[0]
        return {"projects": one("SELECT COUNT(*) FROM projects"),
                "mails": one("SELECT COUNT(*) FROM mails"),
                "demo_documents": one(f"SELECT COUNT(*) FROM documents WHERE path LIKE '{s.demo_dir}%'"),
                "decisions": one("SELECT COUNT(*) FROM assign_decisions"),
                "marker": db.get_meta(conn, DEMO_META) is not None}


def test_switch_to_real_data_with_missing_mail_dir_removes_demo_content(demo_index, tmp_path):
    s = real_settings(tmp_path)  # MAIL_DIR does not exist (yet)
    cached = [f for f in (s.cache_dir / "attachments").rglob("*") if f.is_file()]
    assert cached  # the demo mails' attachments
    stats = run_index(s)
    assert stats["demo_purge"] == {"projects": 8, "documents": 33, "mails": 76, "cache_files": len(cached)}
    assert not [f for f in (s.cache_dir / "attachments").rglob("*") if f.is_file()]  # no orphaned files
    c = counts(s)
    assert c == {"projects": 1, "mails": 0, "demo_documents": 0, "decisions": 1, "marker": False}
    with db.session(s.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == \
            conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] > 0
        assert {r[0] for r in conn.execute("SELECT DISTINCT project FROM chunks")} == {"BS-2026-0001"}
        assert conn.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'eml\\_stamp:%' ESCAPE '\\'").fetchone()[0] == 0
    # once only: the next run has nothing to remove
    assert "demo_purge" not in run_index(s)


def test_search_and_answers_no_longer_cite_demo_projects(tmp_path, fake_ai):
    generate(tmp_path / "demo")
    demo = fake_settings(fake_ai, tmp_path, DEMO_MODE=1)
    ai = AIServices.from_settings(demo)
    run_index(demo, ai)
    with db.session(demo.db_path) as conn:
        assert hybrid_search(conn, ai, str(demo.db_path), DEMO_FACT, k=5).hits
    real = tmp_path / "real"
    (real / "mails").mkdir(parents=True)  # empty real MAIL_DIR: the eml import deletes nothing itself
    (real / "projekte").mkdir()
    s = fake_settings(fake_ai, tmp_path, PROJECTS_DIR=real / "projekte", MAIL_DIR=real / "mails")
    stats = run_index(s, ai)
    assert stats["demo_purge"]["mails"] == 76 and stats["demo_purge"]["projects"] == 8
    with db.session(s.db_path) as conn:
        assert hybrid_search(conn, ai, str(s.db_path), DEMO_FACT, k=5).hits == []
        result = ask(conn, ai, str(s.db_path), f"Was fordert die {DEMO_FACT}?")
        assert result.status != "answered" and result.sources == []


def test_demo_folders_configured_as_source_are_kept(demo_index, tmp_path):
    before = counts(demo_index)
    s = load_settings({"DATA_DIR": str(tmp_path), "PROJECTS_DIR": str(demo_index.projects_dir),
                       "MAIL_DIR": str(demo_index.mail_dir)})
    assert "demo_purge" not in run_index(s)
    assert counts(s) == before  # marker kept: switching to real folders later still cleans up
    # only the demo mails are the configured source: the demo projects go, the mails stay
    s = real_settings(tmp_path, MAIL_DIR=demo_index.mail_dir)
    stats = run_index(s)
    assert (stats["demo_purge"]["projects"], stats["demo_purge"]["mails"]) == (8, 0)
    assert counts(s)["mails"] == 76 and counts(s)["marker"]
    with db.session(s.db_path) as conn:
        result = purge_demo_data(conn, s)
    assert "MAIL_DIR zeigt in den Demo-Ordner" in result["note"] and result["mails"] == 0


def test_demo_data_from_an_older_version_without_marker_is_removed(demo_index, tmp_path):
    with db.session(demo_index.db_path) as conn:
        conn.execute("DELETE FROM meta WHERE key = ?", (DEMO_META,))
    stats = run_index(real_settings(tmp_path))
    assert (stats["demo_purge"]["projects"], stats["demo_purge"]["mails"]) == (8, 76)


def test_demo_mails_are_found_even_when_the_demo_folder_was_deleted(demo_index, tmp_path):
    shutil.rmtree(demo_index.demo_dir)
    s = real_settings(tmp_path)
    assert run_index(s)["demo_purge"]["mails"] == 76
    assert counts(s)["mails"] == 0


def test_real_mail_with_a_demo_file_name_stays(demo_index, tmp_path):
    with db.session(demo_index.db_path) as conn:
        key = conn.execute("SELECT source_key FROM mails WHERE source = 'eml' ORDER BY id LIMIT 1").fetchone()[0]
        conn.execute("UPDATE mails SET message_id = '<echt-4711@kmw.example>' WHERE source_key = ?", (key,))
    s = real_settings(tmp_path)
    assert run_index(s)["demo_purge"]["mails"] == 75
    with db.session(s.db_path) as conn:
        assert [r[0] for r in conn.execute("SELECT source_key FROM mails")] == [key]


def test_purge_demo_command(demo_index, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DEMO_MODE", "1")
    assert index.main(["--purge-demo"]) == 2
    assert "DEMO_MODE=1" in capsys.readouterr().err
    monkeypatch.setenv("DEMO_MODE", "0")
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path / "real" / "projekte"))
    assert index.main(["--purge-demo"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["projects"], out["documents"], out["mails"]) == (8, 33, 76)
    assert counts(demo_index)["projects"] == 0 and counts(demo_index)["decisions"] == 1
