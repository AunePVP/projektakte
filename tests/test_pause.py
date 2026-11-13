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
    from projektakte.pause import EMBED_PAUSE

    PAUSE.resume()
    EMBED_PAUSE.resume()
    yield
    PAUSE.resume()
    EMBED_PAUSE.resume()


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


# -- the embeddings alone ---------------------------------------------------------------------------------
def _without_vector(s) -> int:
    from projektakte import db

    conn = db.connect(s.db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM chunks WHERE embedding IS NULL").fetchone()[0]
    finally:
        conn.close()


def _wait(cond, seconds: float = 20.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.2)
    return False


def test_embed_paused_holds_back_the_embeddings_alone(tmp_path, fake_ai):
    from projektakte.pause import EMBED_PAUSE

    s = fake_settings(fake_ai, tmp_path, DEMO_MODE=1, INDEX_INTERVAL=0, EMBED_PAUSED=1)
    assert s.embed_paused is True
    with TestClient(create_app(s)) as client:
        wait_for_runs(client, 1)  # the documents pass is not held: it runs and stores sections
        assert EMBED_PAUSE.paused and not PAUSE.paused
        time.sleep(1.0)
        assert _without_vector(s) > 0  # but none gets a vector
        page = client.get("/status").text
        assert "Embeddings <strong>pausiert</strong>" in page and "Embeddings fortsetzen" in page
        assert "Indexierung pausieren" in page  # the indexing as a whole still runs
        part = client.get("/status/reihenfolge/jetzt").text
        assert "pausiert" in part and "ohne Vektor (die Volltextsuche findet sie schon)" in part
        assert client.get("/api/status").json()["indexing"]["embeddings"]["pause"]["paused"] is True
        # resumed on the status page: the vectors are made
        assert client.post("/status/embeddings-fortsetzen", follow_redirects=False).status_code == 303
        assert _wait(lambda: _without_vector(s) == 0)
        assert "Embeddings pausieren" in client.get("/status").text
        # and again through the API
        assert client.post("/api/embeddings/pause").json() == {"status": "pausiert"} and EMBED_PAUSE.paused
        assert client.post("/api/embeddings/resume").json() == {"status": "läuft"} and not EMBED_PAUSE.paused
    assert not EMBED_PAUSE.paused  # the end of the app releases it (the next app starts as configured)


def test_embeddings_run_unless_paused(tmp_path, fake_ai):
    from projektakte.pause import EMBED_PAUSE

    s = fake_settings(fake_ai, tmp_path, DEMO_MODE=1, INDEX_INTERVAL=0)
    assert s.embed_paused is False
    with TestClient(create_app(s)) as client:
        wait_for_runs(client, 1)
        assert not EMBED_PAUSE.paused and _wait(lambda: _without_vector(s) == 0)
        assert "Embeddings pausieren" in client.get("/status").text
