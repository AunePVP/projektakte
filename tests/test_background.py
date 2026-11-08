"""Background indexing: start-up stays fast, DEMO_MODE creates missing demo data, the web server
keeps answering while indexing, re-indexing can be triggered."""
import shutil
import time

from fastapi.testclient import TestClient

from projektakte.main import create_app

from .conftest import fake_settings


def wait_for_runs(client, n, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.get("/api/status").json()
        if st["indexing"]["runs"] >= n and not st["indexing"]["running"]:
            return st
        time.sleep(0.05)
    raise AssertionError(f"Indexierung nicht fertig: {st}")


def test_startup_is_fast_and_demo_mode_indexes_in_background(tmp_path, fake_ai):
    s = fake_settings(fake_ai, tmp_path, DEMO_MODE=1, INDEX_INTERVAL=0)
    assert not (tmp_path / "demo").exists()
    t0 = time.perf_counter()
    with TestClient(create_app(s)) as client:
        assert client.get("/health").json()["status"] == "ok"
        assert time.perf_counter() - t0 < 5
        st = wait_for_runs(client, 1)
        assert st["indexing"]["last_error"] is None and st["indexing"]["demo_generated"] is True
        assert st["counts"]["projects"] == 8 and st["counts"]["mails"] >= 60
        assert st["counts"]["chunks_without_vector"] == 0 and st["counts"]["inbox"] > 0
        assert client.get("/api/projects").json()["count"] == 8

        # a new mail arrives; re-indexing is triggered on demand
        src = sorted((tmp_path / "demo" / "mails").glob("*.eml"))[0]
        shutil.copy(src, tmp_path / "demo" / "mails" / "zz_neu.eml")
        assert client.post("/api/reindex").status_code == 202
        st = wait_for_runs(client, 2)
        assert st["indexing"]["last_stats"]["mails"]["new"] == 1
        indexer = client.app.state.indexer
    assert not indexer.alive  # stopped on shutdown


def test_health_answers_while_indexing(tmp_path, fresh_fake_ai):
    fresh_fake_ai.options["delay"] = 0.05  # slow AI services → long indexing run
    s = fake_settings(fresh_fake_ai, tmp_path, DEMO_MODE=1, INDEX_INTERVAL=0, EMBED_BATCH=4)
    with TestClient(create_app(s)) as client:
        deadline = time.time() + 10
        while not client.get("/api/status").json()["indexing"]["running"] and time.time() < deadline:
            time.sleep(0.01)
        t0 = time.perf_counter()
        assert client.get("/health").status_code == 200
        assert time.perf_counter() - t0 < 0.5
        assert client.get("/api/status").json()["indexing"]["running"] is True
        fresh_fake_ai.options.update(delay=0, fail=["embed"])  # let the run finish quickly


def test_existing_demo_data_is_not_regenerated(tmp_path, fake_ai):
    from projektakte.demo import generate
    generate(tmp_path / "demo")
    s = fake_settings(fake_ai, tmp_path, DEMO_MODE=1, INDEX_INTERVAL=0)
    with TestClient(create_app(s)) as client:
        st = wait_for_runs(client, 1)
        assert st["indexing"]["demo_generated"] is False


def test_periodic_runs(tmp_path, fake_ai):
    s = fake_settings(fake_ai, tmp_path, DEMO_MODE=1, INDEX_INTERVAL=0.2)
    with TestClient(create_app(s)) as client:
        st = wait_for_runs(client, 3)
        assert st["indexing"]["last_stats"]["documents"]["unchanged"] == 33
        assert st["indexing"]["next_run"]


def test_run_warnings_are_collected():
    from projektakte.background import run_warnings
    stats = {"projects": {"warning": "Projektordner /p nicht vorhanden"}, "documents": {"note": "x"},
             "mails": {"new": 1}, "seconds": 0.1}
    assert run_warnings(stats) == ["Projektordner /p nicht vorhanden"]
    assert run_warnings(None) == []


def test_unmounted_projects_dir_is_reported_in_status(tmp_path, fake_ai):
    s = fake_settings(fake_ai, tmp_path, INDEX_INTERVAL=0, PROJECTS_DIR=tmp_path / "nicht-eingebunden",
                      MAIL_DIR=tmp_path / "mails")
    with TestClient(create_app(s)) as client:
        st = wait_for_runs(client, 1)
        assert st["indexing"]["last_error"] is None
        assert any("nicht vorhanden" in w for w in st["indexing"]["last_warnings"])
        assert st["indexing"]["last_stats"]["projects"]["warning"]
