"""Shared fixtures: in-process fake AI services (same implementation as python -m
projektakte.fake_ai) on the loopback interface – no network, no real AI services."""
from __future__ import annotations

import pytest

from projektakte.config import load_settings
from projektakte.fake_ai import start_background


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """Never touch ./data of the working copy from tests."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    for var in ("DEMO_MODE", "AI_MODE", "PROJECTS_DIR", "MAIL_DIR"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(scope="session")
def fake_ai():
    server = start_background()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture()
def fresh_fake_ai():
    """A private fake server whose options a test may change freely."""
    server = start_background()
    yield server
    server.shutdown()
    server.server_close()


def fake_env(server, tmp_path, **extra) -> dict:
    base = server.base_url
    env = {
        "DATA_DIR": str(tmp_path),
        "APP_ENV": "test",
        "LLM_URL": f"{base}/v1",
        "EMBED_URL": base,
        "RERANK_URL": f"{base}/rerank",
        "HEALTH_TIMEOUT": "2",
    }
    env.update({k: str(v) for k, v in extra.items()})
    return env


def fake_settings(server, tmp_path, **extra):
    return load_settings(fake_env(server, tmp_path, **extra))
