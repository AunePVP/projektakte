"""ops-09: public, data-poor readiness endpoint and the Docker HEALTHCHECK command."""
import json
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

import projektakte.main as main_mod
from projektakte import healthcheck
from projektakte.auth import hash_password
from projektakte.config import load_settings
from projektakte.main import create_app


def make_app(tmp_path, *, background=False, **extra):
    env = {"DATA_DIR": str(tmp_path), "APP_ENV": "test", "SECRET_KEY": secrets.token_urlsafe(48),
           "AUTH_USERS": "anna:" + hash_password("x" * 12, iterations=1000), "INDEX_INTERVAL": "300",
           "LLM_API_KEY": "geheimer-schluessel"}
    env.update(extra)
    return create_app(load_settings(env), background=background)


def test_ready_is_public_and_data_poor(tmp_path):
    client = TestClient(make_app(tmp_path))
    r = client.get("/health/ready")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["checks"] == {"database": "ok", "indexer": "off"}
    assert data["indexing"]["pending_vectors"] is None and data["indexing"]["runs"] == 0  # no run yet
    text = r.text
    for leak in (str(tmp_path), "geheimer-schluessel", "anna", "192.168"):
        assert leak not in text


def test_ready_reports_dead_indexer(tmp_path):
    client = TestClient(make_app(tmp_path, background=True))  # no lifespan: the thread never started
    r = client.get("/health/ready")
    assert r.status_code == 503
    assert r.json()["status"] == "error" and r.json()["checks"]["indexer"] == "stopped"


def test_ready_with_running_indexer(tmp_path):
    app = make_app(tmp_path, background=True, INDEX_INTERVAL="0", PROJECTS_DIR=str(tmp_path / "leer"),
                   MAIL_DIR=str(tmp_path / "leer"))
    with TestClient(app) as client:
        r = client.get("/health/ready")
        assert r.status_code == 200 and r.json()["checks"]["indexer"] == "ok"


def test_ready_reports_broken_database(tmp_path):
    app = make_app(tmp_path)
    app.state.settings.db_path.unlink()
    app.state.settings.db_path.write_bytes(b"das ist keine Datenbank" * 100)
    r = TestClient(app).get("/health/ready")
    assert r.status_code == 503 and r.json()["checks"]["database"] == "error"


def test_ready_reports_missing_database_without_creating_it(tmp_path):
    app = make_app(tmp_path)
    for suffix in ("", "-wal", "-shm"):
        (tmp_path / f"projektakte.sqlite3{suffix}").unlink(missing_ok=True)
    assert TestClient(app).get("/health/ready").status_code == 503
    assert not (tmp_path / "projektakte.sqlite3").exists()


def test_ready_probe_has_constant_cost(tmp_path, monkeypatch):
    """diff-review: the public probe must not scan the sections table (COUNT over 300,000 sections
    took 4 s with a cold cache – longer than the HEALTHCHECK timeout, and anybody in the LAN could
    trigger it). Pending vectors come from the indexer's last run instead."""
    app = make_app(tmp_path)
    statements = []
    real_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(main_mod.sqlite3, "connect", traced_connect)
    app.state.indexer.state.update(runs=1, last_finished=_iso(timedelta(minutes=1)),
                                   last_stats={"embeddings": {"embedded": 10, "pending": 7, "error": None}})
    data = TestClient(app).get("/health/ready").json()
    assert data["checks"]["database"] == "ok" and data["indexing"]["pending_vectors"] == 7
    queries = [q for q in statements if q.lstrip().upper().startswith("SELECT")]
    assert queries and all("LIMIT 1" in q and "COUNT" not in q.upper() for q in queries), statements
    app.state.indexer.state.update(last_stats={"embeddings": {"error": "Zeitüberschreitung"}})
    assert TestClient(app).get("/health/ready").json()["indexing"]["pending_vectors"] is None


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) - delta).isoformat(timespec="seconds")


def test_ready_degraded_when_indexing_is_stale_or_failing(tmp_path):
    app = make_app(tmp_path)
    client = TestClient(app)
    state = app.state.indexer.state
    state.update(runs=5, last_finished=_iso(timedelta(minutes=2)), last_error=None)
    data = client.get("/health/ready").json()
    assert data["status"] == "ok" and 100 <= data["indexing"]["last_success_age_s"] <= 140
    state.update(last_finished=_iso(timedelta(hours=3)))
    r = client.get("/health/ready")
    assert r.status_code == 200 and r.json()["status"] == "degraded" and r.json()["indexing"]["stale"]
    state.update(last_finished=_iso(timedelta(minutes=1)), last_error="ConnectionError: /srv/geheim/pfad")
    r = client.get("/health/ready")
    assert r.json()["status"] == "degraded" and r.json()["indexing"]["last_run_failed"]
    assert r.json()["indexing"]["last_success_age_s"] is None and "/srv/geheim" not in r.text
    state.update(last_success=_iso(timedelta(minutes=10)))  # if the indexer records it
    assert 500 <= client.get("/health/ready").json()["indexing"]["last_success_age_s"] <= 700


def test_health_unchanged(tmp_path):
    from projektakte import __version__

    assert TestClient(make_app(tmp_path)).get("/health").json() == {"status": "ok", "version": __version__,
                                                                   "env": "test"}


# -- python -m projektakte.healthcheck -----------------------------------------------------------------
@pytest.fixture()
def fake_server():
    replies = {"status": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps({"status": "ok" if replies["status"] == 200 else "error"}).encode()
            self.send_response(replies["status"])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            replies["path"] = self.path

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server, replies
    server.shutdown()
    server.server_close()


def test_healthcheck_exit_codes(fake_server, monkeypatch, capsys):
    server, replies = fake_server
    monkeypatch.setenv("PORT", str(server.server_port))
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")  # must be ignored
    assert healthcheck.main([]) == 0 and replies["path"] == "/health/ready"
    replies["status"] = 503
    assert healthcheck.main([]) == 1
    assert "nicht bereit" in capsys.readouterr().out
    server_port = server.server_port
    server.shutdown()
    server.server_close()
    assert healthcheck.main([str(server_port)]) == 1
