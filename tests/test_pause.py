"""Pausing the indexing: the gate holds all background work between two steps, resuming goes on."""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from projektakte.main import create_app
from projektakte.pause import PAUSE, PauseGate

from .conftest import fake_settings
from .test_background import wait_for_runs


@pytest.fixture(autouse=True)
def _gate_open():
    PAUSE.resume()
    yield
    PAUSE.resume()


def test_gate_blocks_until_resumed():
    gate = PauseGate()
    gate.wait()  # running: returns at once
    assert gate.pause() is True and gate.pause() is False and gate.paused and gate.since
    done = threading.Event()
    threading.Thread(target=lambda: (gate.wait(), done.set()), daemon=True).start()
    assert not done.wait(0.7)
    assert gate.resume() is True and gate.resume() is False
    assert done.wait(2) and not gate.paused and gate.since is None


def test_gate_wait_ends_on_stop():
    gate = PauseGate()
    gate.pause()
    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    t0 = time.monotonic()
    gate.wait(stop)
    assert time.monotonic() - t0 < 3 and gate.paused


def test_pause_holds_a_new_pass_until_resumed(tmp_path, fake_ai):
    s = fake_settings(fake_ai, tmp_path, DEMO_MODE=1, INDEX_INTERVAL=0)
    with TestClient(create_app(s)) as client:
        wait_for_runs(client, 1)
        assert client.post("/api/pause").json() == {"status": "pausiert"}
        st = client.get("/api/status").json()["indexing"]
        assert st["pause"]["paused"] is True and st["pause"]["since"]
        assert "pausiert" in client.get("/status").text
        client.post("/api/reindex")
        time.sleep(1.5)
        assert client.get("/api/status").json()["indexing"]["runs"] == 1  # no pass started
        assert client.post("/status/fortsetzen", follow_redirects=False).status_code == 303
        wait_for_runs(client, 2)
        assert client.get("/api/status").json()["indexing"]["pause"]["paused"] is False
        assert "Indexierung pausieren" in client.get("/status").text


def test_shutdown_while_paused_does_not_hang(tmp_path, fake_ai):
    s = fake_settings(fake_ai, tmp_path, DEMO_MODE=1, INDEX_INTERVAL=0)
    with TestClient(create_app(s)) as client:
        wait_for_runs(client, 1)
        client.post("/api/pause")
        indexer = client.app.state.indexer
    assert not indexer.alive and not PAUSE.paused
