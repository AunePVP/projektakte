from fastapi.testclient import TestClient

from projektakte import __version__
from projektakte.main import create_app


def test_health_is_ok_without_ai_services(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    client = TestClient(create_app())
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": __version__, "env": "test"}


def test_version():
    client = TestClient(create_app())
    assert client.get("/api/version").json()["version"] == __version__
