"""The Bildauswertung in the background (vision.VisionWorker): the model reads scan pages while the documents
pass is still running, at most VISION_CONCURRENCY calls at a time, no page lost or read twice, a file changed
meanwhile is not mixed up, it stops at once with the app, and nothing runs with LLM_VISION=0. Also the times
and estimates of the status page (zeiten.py) and the card "Wird gerade gelesen". Only invented names."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import hashlib

import numpy as np

from projektakte import db, documents, embed_worker, reihenfolge, vectors, vision, zeiten
from projektakte.ai.services import AIServices
from projektakte.background import BackgroundIndexer
from projektakte.documents import index_all_documents
from projektakte.main import create_app
from projektakte.registry import sync_from_folder

from .conftest import fake_settings
from .test_bildauswertung_stufen import HANDWRITING, Vision, plain_scan, png

PATTERN = r"\d{6}"


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    reihenfolge.LIVE.reset()
    vision.LIVE.reset()
    embed_worker.LIVE.reset()
    monkeypatch.setattr(vision.VisionWorker, "IDLE_SECONDS", 0.2)
    monkeypatch.setattr(vision.VisionWorker, "ERROR_SECONDS", 0.2)
    monkeypatch.setattr(embed_worker.EmbedWorker, "IDLE_SECONDS", 0.2)
    monkeypatch.setattr(embed_worker.EmbedWorker, "ERROR_SECONDS", 0.2)
    yield
    reihenfolge.LIVE.reset()
    vision.LIVE.reset()
    embed_worker.LIVE.reset()


def _wait(condition, seconds: float = 20.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def _share(root: Path, scans: int = 6, slow_files: int = 8) -> None:
    """Folder 2: 210002 with scanned images (read first: newest number), 210001 with text files that take
    long to read (the documents pass is still busy with them while the model reads the scans)."""
    a = root / "2" / "210002_Neubau Kita"
    a.mkdir(parents=True)
    for k in range(scans):
        (a / f"Skizze{k}.png").write_bytes(png(f"Skizze {k}"))
    b = root / "2" / "210001_Sporthalle"
    b.mkdir(parents=True)
    for k in range(slow_files):
        (b / f"Notiz{k}.txt").write_text(f"Brandschutz Notiz {k}: Rettungsweg", encoding="utf-8")


def _settings(fake_ai, tmp_path, **extra):
    return fake_settings(fake_ai, tmp_path / "data", PROJECTS_DIR=tmp_path / "projekte", MAIL_DIR=tmp_path / "mails",
                         PROJECT_NUMBER_PATTERN=PATTERN, INDEX_INTERVAL=0, **extra)


class Recorder:
    """Wraps the model's describe_image: what the documents pass was doing at each call, how many calls ran
    at once, optionally slow."""

    def __init__(self, llm, delay: float = 0.0):
        self.real, self.delay = llm.describe_image, delay
        self.phases: list[str] = []
        self.active = self.most = self.calls = 0
        self._lock = threading.Lock()
        llm.describe_image = self

    def __call__(self, data, mime, prompt):
        with self._lock:
            self.active += 1
            self.calls += 1
            self.most = max(self.most, self.active)
            self.phases.append(reihenfolge.LIVE.snapshot()["phase"])
        try:
            if self.delay:
                time.sleep(self.delay)
            return self.real(data, mime, prompt)
        finally:
            with self._lock:
                self.active -= 1


def _pending(s):
    with db.session(s.db_path) as conn:
        return [tuple(r) for r in conn.execute("SELECT filename, vision_pending, status, message FROM documents")]


def _without_vector(s) -> int:
    with db.session(s.db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM chunks WHERE embedding IS NULL").fetchone()[0]


def _slow_text_files(monkeypatch, seconds: float, started: threading.Event | None = None) -> None:
    real = documents.prepare_document

    def slow(**kw):
        if kw["rel_path"].endswith(".txt"):
            if started is not None:
                started.set()
            time.sleep(seconds)
        return real(**kw)

    monkeypatch.setattr(documents, "prepare_document", slow)


def test_the_model_reads_scan_pages_while_the_documents_pass_is_still_running(tmp_path, fake_ai, monkeypatch):
    _share(tmp_path / "projekte")
    s = _settings(fake_ai, tmp_path, LLM_VISION=1, VISION_CONCURRENCY=2)
    _slow_text_files(monkeypatch, 0.4)
    ai = AIServices.from_settings(s)
    rec = Recorder(ai.llm, delay=0.05)
    indexer = BackgroundIndexer(s, ai)
    assert indexer.vision is not None
    indexer.start()
    try:
        assert _wait(lambda: rec.calls >= 6 and indexer.state["runs"] >= 1, 40), (rec.calls, indexer.state)
        # most pages were read during the phase "Dokumente" – the text files of 210001 were still being read
        assert rec.phases.count("Dokumente") >= 3, rec.phases
        assert rec.most <= 2
        assert _wait(lambda: vision.LIVE.snapshot()["pending_pages"] == 0)
        st = indexer.status()
        assert st["last_stats"]["vision"] == {"note": "läuft im Hintergrund"}
        assert st["vision"]["enabled"] and st["vision"]["concurrency"] == 2 and st["vision"]["pages"] == 6
    finally:
        indexer.stop()
    with db.session(s.db_path) as conn:
        assert conn.execute("SELECT COALESCE(SUM(vision_pending), 0) FROM documents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM page_texts").fetchone()[0] == 0
        texts = [r[0] for r in conn.execute("SELECT text FROM chunks c JOIN documents d ON d.id = c.doc_id "
                                            "WHERE d.ext = '.png'")]
        assert len(texts) == 6 and all("Fake-Transkription" in t for t in texts)
        assert zeiten.read(conn, zeiten.PAGE_KEY).samples == 6
    assert rec.calls == 6  # every page once, none twice
    assert not indexer.vision.alive


def test_concurrency_cap_no_lost_or_duplicated_pages_with_new_documents_meanwhile(tmp_path):
    """The worker's run with refresh: documents stored while it runs join it; 3 at a time at most."""
    root = tmp_path / "projekte"
    folder = root / "210001_Halle"
    folder.mkdir(parents=True)
    for k in range(5):
        (folder / f"A{k}.png").write_bytes(png(f"A {k}"))
    dbp = tmp_path / "x.sqlite3"
    db.init_db(dbp)
    conn = db.connect(dbp)
    sync_from_folder(conn, root, PATTERN)
    conn.commit()
    llm = Vision(delay=0.1)
    with documents.vision_later():
        index_all_documents(conn, llm, projects_dir=root)
    added = threading.Event()

    def more() -> None:  # the documents pass stores further scans while the model works (own connection)
        time.sleep(0.15)
        other = db.connect(dbp)
        for k in range(4):
            (folder / f"B{k}.png").write_bytes(png(f"B {k}"))
        with documents.vision_later():
            index_all_documents(other, llm, projects_dir=root)
        other.close()
        added.set()

    threading.Thread(target=more, daemon=True).start()
    pass_ = vision.VisionPass(conn, llm, concurrency=3, projects_dir=root, refresh=0.05, live=vision.VisionLive())
    stats = pass_.run()
    assert added.wait(5)
    if vision.pending_counts(conn)["pages"]:  # added after the run's last look: the next run takes them
        stats["pages"] += vision.VisionPass(conn, llm, concurrency=3, projects_dir=root).run()["pages"]
    assert stats["pages"] == 9 and len(llm.prompts) == 9 and llm.most == 3
    assert vision.pending_counts(conn)["pages"] == 0
    docs = conn.execute("SELECT filename, vision_pending, status FROM documents ORDER BY filename").fetchall()
    assert len(docs) == 9 and all(d[1] == 0 and d[2] == "ok" for d in docs)
    assert conn.execute("SELECT COUNT(*) FROM chunks WHERE text LIKE ?", (f"%{HANDWRITING}%",)).fetchone()[0] == 9
    conn.close()


def test_a_file_changed_during_the_bildauswertung_is_not_mixed_up(tmp_path):
    root = tmp_path / "projekte"
    folder = root / "210001_Halle"
    folder.mkdir(parents=True)
    path = folder / "Akte.pdf"
    path.write_bytes(plain_scan(pages=2))
    dbp = tmp_path / "x.sqlite3"
    db.init_db(dbp)
    conn = db.connect(dbp)
    sync_from_folder(conn, root, PATTERN)
    conn.commit()
    with documents.vision_later():
        index_all_documents(conn, Vision(), projects_dir=root)
    old_sha = conn.execute("SELECT sha256 FROM documents").fetchone()[0]

    class Changing(Vision):
        """While the model reads page 1, the file gets a third page and the documents pass reads it again."""
        def describe_image(self, data, mime, prompt):
            if not self.prompts:
                path.write_bytes(plain_scan(pages=3))
                os.utime(path, (time.time() + 10, time.time() + 10))
                other = db.connect(dbp)
                with documents.vision_later():
                    index_all_documents(other, Vision(), projects_dir=root)
                other.close()
            return super().describe_image(data, mime, prompt)

    llm = Changing(answer="Altes Blatt gelesen")
    stats = vision.VisionPass(conn, llm, concurrency=1, projects_dir=root).run()
    doc = conn.execute("SELECT * FROM documents").fetchone()
    assert doc["sha256"] != old_sha
    # the answers for the old file were dropped, the new file's three pages wait for the model
    assert stats["discarded"] >= 1 and stats["pages"] == 0
    assert doc["vision_pending"] == 3
    assert [r[0] for r in conn.execute("SELECT state FROM page_texts ORDER BY page")] == ["pending"] * 3
    assert not conn.execute("SELECT COUNT(*) FROM chunks WHERE text LIKE '%Altes Blatt%'").fetchone()[0]
    stats = vision.VisionPass(conn, Vision(), concurrency=1, projects_dir=root).run()
    assert stats["pages"] == 3 and conn.execute("SELECT vision_pending FROM documents").fetchone()[0] == 0
    conn.close()


def test_a_document_whose_pages_were_all_read_before_a_restart_is_put_together(tmp_path):
    root = tmp_path / "projekte"
    folder = root / "210001_Halle"
    folder.mkdir(parents=True)
    (folder / "Akte.pdf").write_bytes(plain_scan(pages=2))
    dbp = tmp_path / "x.sqlite3"
    db.init_db(dbp)
    conn = db.connect(dbp)
    sync_from_folder(conn, root, PATTERN)
    conn.commit()
    with documents.vision_later():
        index_all_documents(conn, Vision(), projects_dir=root)
    # the app ended after both pages were stored, before the document was written again
    conn.execute("UPDATE page_texts SET state = 'done', text = 'Gelesen vor dem Neustart' WHERE state = 'pending'")
    conn.commit()
    llm = Vision()
    vision.VisionPass(conn, llm, projects_dir=root).run()
    assert not llm.prompts
    doc = conn.execute("SELECT vision_pending, status FROM documents").fetchone()
    assert tuple(doc) == (0, "ok")
    assert conn.execute("SELECT COUNT(*) FROM chunks WHERE text LIKE '%Gelesen vor dem Neustart%'").fetchone()[0]
    conn.close()


def test_shutdown_stops_the_bildauswertung_at_once(tmp_path, fake_ai):
    _share(tmp_path / "projekte", scans=12, slow_files=0)
    s = _settings(fake_ai, tmp_path, LLM_VISION=1, VISION_CONCURRENCY=3)
    ai = AIServices.from_settings(s)
    rec = Recorder(ai.llm, delay=2.0)  # a slow model
    indexer = BackgroundIndexer(s, ai)
    indexer.start()
    try:
        assert _wait(lambda: rec.active == 3, 30), (rec.calls, rec.active, vision.LIVE.snapshot(),
                                                     indexer.vision.alive, indexer.vision.last_error, indexer.state)
    finally:
        started = time.monotonic()
        indexer.stop()
        took = time.monotonic() - started
    assert took < 3, took  # answers on their way are not waited for
    assert not indexer.vision.alive
    calls = rec.calls
    time.sleep(2.5)
    assert rec.calls == calls  # nothing new after the stop
    with db.session(s.db_path) as conn:  # the pages not read yet wait for the next start
        assert conn.execute("SELECT COALESCE(SUM(vision_pending), 0) FROM documents").fetchone()[0] >= 9


def test_no_background_bildauswertung_without_vision(tmp_path, fake_ai):
    _share(tmp_path / "projekte", scans=1, slow_files=0)
    s = _settings(fake_ai, tmp_path, LLM_VISION=0)
    indexer = BackgroundIndexer(s, AIServices.from_settings(s))
    assert indexer.vision is None and indexer.status()["vision"] is None
    stats = indexer.run_once()
    assert stats["vision"]["note"] == "ausgeschaltet (LLM_VISION=0)"
    with TestClient(create_app(s, background=False)) as client:
        page = client.get("/status").text
        assert "Bildauswertung läuft," not in page and "Bildauswertung</span>" not in page


def test_the_phase_at_the_end_of_a_pass_is_skipped_while_the_background_worker_reads(tmp_path):
    assert vision._PASS_LOCK.acquire(blocking=False)
    try:
        assert vision.run_vision(None, Vision(), object()) == {"note": "läuft im Hintergrund"}
    finally:
        vision._PASS_LOCK.release()


def test_files_of_a_folder_switched_off_do_not_go_to_the_model_mail_attachments_do(tmp_path):
    root = tmp_path / "projekte"
    for folder, name in (("1", "110001_Kita"), ("3", "310001_Halle")):
        (root / folder / name).mkdir(parents=True)
        (root / folder / name / "Skizze.png").write_bytes(png(name))
    dbp = tmp_path / "x.sqlite3"
    db.init_db(dbp)
    conn = db.connect(dbp)
    sync_from_folder(conn, root, PATTERN)
    conn.commit()
    with documents.vision_later():
        index_all_documents(conn, Vision(), projects_dir=root)
    reihenfolge.change_folder(conn, root, (), "3", "aus")
    conn.commit()
    llm = Vision()
    live = vision.VisionLive()
    stats = vision.VisionPass(conn, llm, projects_dir=root, order=reihenfolge.VisionOrder(conn, root, ()),
                              live=live).run()
    assert stats["pages"] == 1 and live.snapshot()["off_pages"] == 1
    left = conn.execute("SELECT d.project FROM documents d WHERE vision_pending > 0").fetchall()
    assert [r[0] for r in left] == ["310001"]  # waits until the folder is switched on again
    conn.close()


# -- the card and the status page ---------------------------------------------------------------------------
def test_card_shows_real_file_count_times_and_the_bildauswertung(tmp_path, fake_ai, monkeypatch):
    """The files are counted one by one (not in steps of 25), with elapsed times, the average per file read
    and – once enough projects were read – a rough estimate for the queue; the Bildauswertung with its speed."""
    root = tmp_path / "projekte"
    _share(root, scans=1, slow_files=30)
    s = _settings(fake_ai, tmp_path, LLM_VISION=1)
    counts: list[int] = []
    real_step = reihenfolge.Live.step

    def step(self, number, seen, total):
        real_step(self, number, seen, total)
        counts.append(self.snapshot()["seen"])

    monkeypatch.setattr(reihenfolge.Live, "step", step)
    started = threading.Event()
    gate = threading.Event()
    real = documents.prepare_document

    def held(**kw):
        if kw["rel_path"] == "Notiz7.txt":  # stop the pass in the middle of 210001 for a look at the card
            started.set()
            gate.wait(20)
        return real(**kw)

    monkeypatch.setattr(documents, "prepare_document", held)
    db.init_db(s.db_path)
    with db.session(s.db_path) as conn:  # enough projects and pages measured before (moving averages)
        for _ in range(6):
            zeiten.add(conn, zeiten.PROJECT_KEY, 3600.0)
        zeiten.add(conn, zeiten.FILE_KEY, 120.0, 40)
        for _ in range(12):
            zeiten.add(conn, zeiten.PAGE_KEY, 48.0)
    app = create_app(s, background=True)
    with TestClient(app) as client:
        try:
            assert started.wait(30)
            part = client.get("/status/reihenfolge/jetzt").text
            assert 'class="rf-now-body"' in part and "rf-spin" not in part  # the spinner is not swapped
            assert "210001" in part
            seen = reihenfolge.LIVE.snapshot()["seen"]
            assert f"{seen} von 30 Dateien" in part and seen % 25 != 0
            assert "Lauf seit" in part and "Projekt seit" in part
            assert "je gelesene Datei" in part and "gelesen" in part
            assert "Warteschlange" not in part or "grob" in part
            assert "Bildauswertung" in part
            api = client.get("/api/reihenfolge").json()
            assert api["current"]["files_done"] == seen and api["current"]["seconds_running"] >= 0
            assert api["current"]["files_read"] >= 1 and api["vision"]["concurrency"] == 3
            page = client.get("/status").text
            assert "Bildauswertung" in page and 'id="rf-jetzt"' in page
            assert page.index('id="rf-jetzt-inhalt"') < page.index('class="rf-spin"')
        finally:
            gate.set()
        assert _wait(lambda: app.state.indexer.state["runs"] >= 1, 30)
    assert set(range(1, 31)) <= set(counts)  # every single number, not steps of 25


def test_zustand_names_both_activities(tmp_path, fake_ai, monkeypatch):
    s = _settings(fake_ai, tmp_path, LLM_VISION=1)
    monkeypatch.setattr(vision.VisionWorker, "alive", property(lambda self: True))
    with TestClient(create_app(s, background=False)) as client:
        worker = client.app.state.indexer.vision
        assert worker is not None
        vision.LIVE.enable(3)
        vision.LIVE.pending(40, 0)
        client.app.state.indexer.state.update(running=True, phase="Dokumente")
        page = client.get("/status").text
        assert "läuft (Dokumente) · Bildauswertung läuft, 3 gleichzeitig" in page
        assert "40 Seiten ausstehend" in client.get("/status/reihenfolge/jetzt").text


# -- formatting and calculations ----------------------------------------------------------------------------
def test_german_durations():
    assert [zeiten.dauer(x) for x in (0, 45, 200, 720, 3600, 8040, 50 * 3600, 3 * 86400, None)] == [
        "0 s", "45 s", "3 Min. 20 s", "12 Min.", "1 Std.", "2 Std. 14 Min.", "2 Tage 2 Std.", "3 Tage", "–"]
    assert [zeiten.etwa(x) for x in (10, 50, 1500, 8040, 20 * 3600, 3 * 86400, 2.5 * 86400, 30 * 86400,
                                     200 * 86400, None)] == [
        "unter 1 Min.", "≈ 1 Min.", "≈ 25 Min.", "≈ 2 Std. 15 Min.", "≈ 20 Std.", "≈ 3 Tage", "≈ 2,5 Tage",
        "≈ 4 Wochen", "≈ 7 Monate", "–"]
    assert [zeiten.je(x) for x in (0.001, 0.42, 4.25, 48, 130)] == ["unter 0,1 s", "0,4 s", "4,2 s", "48 s",
                                                                   "2 Min. 10 s"]


def test_moving_averages_survive_a_restart_and_follow_change(tmp_path):
    dbp = tmp_path / "x.sqlite3"
    db.init_db(dbp)
    with db.session(dbp) as conn:
        assert zeiten.read(conn, zeiten.PAGE_KEY) == zeiten.Mean(None, 0)
        for _ in range(10):
            zeiten.add(conn, zeiten.PAGE_KEY, 50.0)
        zeiten.add(conn, zeiten.FILE_KEY, 30.0, 10)  # 10 files in 30 s
    with db.session(dbp) as conn:  # a new connection (restart): the same values
        assert zeiten.read(conn, zeiten.PAGE_KEY) == zeiten.Mean(50.0, 10)
        assert zeiten.read(conn, zeiten.FILE_KEY).value == pytest.approx(3.0)
        for _ in range(100):  # the model got faster: the average follows
            zeiten.add(conn, zeiten.PAGE_KEY, 20.0)
        mean = zeiten.read(conn, zeiten.PAGE_KEY)
        assert mean.samples == 110 and 20 < mean.value < 25
        conn.execute("UPDATE meta SET value = 'kaputt' WHERE key = ?", (zeiten.PAGE_KEY,))
        assert zeiten.read(conn, zeiten.PAGE_KEY) == zeiten.Mean(None, 0)


def test_estimates_only_with_enough_samples():
    assert zeiten.project_eta(10, 100, 50) is None  # fewer than 20 files gone through
    assert zeiten.project_eta(20, 100, 40) == pytest.approx(160)  # 2 s per file, 80 left
    assert zeiten.project_eta(100, 100, 40) == 0.0
    few = zeiten.Mean(600.0, 4)
    enough = zeiten.Mean(600.0, 5)
    files = zeiten.Mean(3.0, 20)
    assert zeiten.queue_eta(100, few, files) is None
    assert zeiten.queue_eta(100, enough, zeiten.Mean(3.0, 19)) is None
    assert zeiten.queue_eta(100, enough, files) == pytest.approx(60000)
    assert zeiten.queue_eta(0, few, files) == 0.0
    rate = zeiten.vision_rate(zeiten.Mean(48.0, 10), 3, 900)
    assert rate["per_hour"] == pytest.approx(225) and rate["eta"] == pytest.approx(900 * 16)
    assert zeiten.vision_rate(zeiten.Mean(48.0, 9), 3, 900)["eta"] is None


# -- embeddings in the background ------------------------------------------------------------------------------
def test_embeddings_are_made_while_documents_are_still_being_read(tmp_path, fake_ai, monkeypatch):
    _share(tmp_path / "projekte", scans=0, slow_files=10)
    s = _settings(fake_ai, tmp_path, LLM_VISION=0, EMBED_CONCURRENCY=2)
    _slow_text_files(monkeypatch, 0.3)
    ai = AIServices.from_settings(s)
    phases: list[str] = []
    real = ai.embed.embed

    def embed(texts, **kw):
        if texts != [vectors.PROBE_TEXT]:
            phases.append(reihenfolge.LIVE.snapshot()["phase"])
        return real(texts, **kw)

    ai.embed.embed = embed
    indexer = BackgroundIndexer(s, ai)
    assert indexer.embedder is not None
    indexer.start()
    try:
        assert _wait(lambda: indexer.state["runs"] >= 1, 40)
        assert _wait(lambda: _without_vector(s) == 0, 20)
        assert indexer.state["last_stats"]["embeddings"] == {"note": "laufen im Hintergrund"}
    finally:
        indexer.stop()
    assert phases.count("Dokumente") >= 3, phases  # while the text files were still being read
    with db.session(s.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM chunks WHERE embedding IS NULL").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] >= 10
        assert int(db.get_meta(conn, "embed_generation", "0")) >= 1  # searchable
    assert not indexer.embedder.alive


class TextEmbedder:
    """An embedding client whose vector is a function of the text (so a vector stored for the wrong text is
    found); ``during`` runs inside the first call of a round (the documents pass writing meanwhile)."""
    model, batch_size = "test-embed", 4

    def __init__(self):
        self.texts: list[str] = []
        self.during = None
        self._lock = threading.Lock()

    @staticmethod
    def vector(text: str) -> list[float]:
        v = np.frombuffer(hashlib.sha256(text.encode()).digest(), dtype=np.uint8).astype(np.float32)[:8] + 1
        return list(v / np.linalg.norm(v))

    def embed(self, texts, **kw):
        with self._lock:
            self.texts.extend(texts)
            during, self.during = self.during, None
        if during:
            during()
        return [self.vector(t) for t in texts]


def test_no_section_embedded_twice_or_lost_when_a_document_is_written_anew_meanwhile(tmp_path):
    root = tmp_path / "projekte"
    folder = root / "210001_Halle"
    folder.mkdir(parents=True)
    note = folder / "Notiz.txt"
    note.write_text("\n\n".join(f"Absatz {k}: Rettungsweg und Brandabschnitt " * 20 for k in range(12)),
                    encoding="utf-8")
    (folder / "Zweite.txt").write_text("Feuerwehrzufahrt geprüft " * 30, encoding="utf-8")
    dbp = tmp_path / "x.sqlite3"
    db.init_db(dbp)
    conn = db.connect(dbp)
    sync_from_folder(conn, root, PATTERN)
    conn.commit()
    index_all_documents(conn, projects_dir=root)
    first = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert first >= 4
    client = TextEmbedder()

    def rewrite():  # the documents pass reads the changed note meanwhile: its sections are deleted, new ones
        note.write_text("\n\n".join(f"Neu {k}: Rauchabzug im Treppenraum " * 20 for k in range(12)),
                        encoding="utf-8")
        os.utime(note, (time.time() + 10, time.time() + 10))
        other = db.connect(dbp)
        index_all_documents(other, projects_dir=root)
        other.close()

    client.during = rewrite
    s = type("S", (), {"db_path": dbp, "embed_concurrency": 2})()
    worker = embed_worker.EmbedWorker(s, client, live=embed_worker.EmbedLive())
    rounds = 0
    while worker.run_round(conn) and rounds < 50:
        rounds += 1
    rows = conn.execute(
        "SELECT c.text, c.heading, c.embedding, d.filename, p.name AS project_name FROM chunks c "
        "JOIN documents d ON d.id = c.doc_id LEFT JOIN projects p ON p.number = c.project").fetchall()
    assert rows and all(r["embedding"] is not None for r in rows)  # none lost
    embedded = [t for t in client.texts if t != vectors.PROBE_TEXT]
    for r in rows:
        text = vectors.embedding_text(r["filename"], r["project_name"], r["heading"], r["text"])
        assert np.allclose(vectors.from_blob(r["embedding"]), TextEmbedder.vector(text))  # its own text's
        assert embedded.count(text) == 1  # embedded once
    assert any("Neu 3" in r["text"] for r in rows) and not any("Absatz 3" in r["text"] for r in rows)
    conn.close()


def test_the_card_shows_every_activity_and_never_idle_while_one_runs(tmp_path, fake_ai, monkeypatch):
    s = _settings(fake_ai, tmp_path, LLM_VISION=1)
    monkeypatch.setattr(vision.VisionWorker, "alive", property(lambda self: True))
    monkeypatch.setattr(embed_worker.EmbedWorker, "alive", property(lambda self: True))
    with TestClient(create_app(s, background=False)) as client:
        idle = client.get("/status/reihenfolge/jetzt").text
        assert "Alles gelesen" in idle and "rf-now-body idle" in idle and "every 30s" in idle
        # the indexing pass waits, but the Bildauswertung and the embeddings run: not idle
        vision.LIVE.enable(3)
        vision.LIVE.set_busy(True)
        vision.LIVE.pending(120, 0)
        vision.LIVE.calls(2, "Pruefbericht.pdf", 7)
        embed_worker.LIVE.enable(2)
        embed_worker.LIVE.update(busy=True, in_flight=1, pending=5400, done=600,
                                 current={"project": "210001", "file": "Notiz.txt"})
        part = client.get("/status/reihenfolge/jetzt").text
        assert "Alles gelesen" not in part and "rf-now-body idle" not in part and "every 4s" in part
        assert "2 von 3 gleichzeitig beim Modell" in part and "Pruefbericht.pdf, Seite 7" in part
        assert "120 Seiten ausstehend" in part
        assert "1 von 2 Stapeln beim Dienst" in part and "600 Abschnitte fertig" in part
        assert "5.400 Abschnitte ohne Vektor" in part and "Notiz.txt" in part
        # a pass in a phase without a project (mails): its phase and time, never an empty card
        live = reihenfolge.LIVE
        live.begin()
        live.set_phase("Mails")
        part = client.get("/status/reihenfolge/jetzt").text
        assert ">Mails<" in part and "Lauf seit" in part and "Bildauswertung" in part and "Embeddings" in part
        page = client.get("/status").text
        assert "Bildauswertung läuft, 3 gleichzeitig" in page and "Embeddings laufen" in page
        api = client.get("/api/reihenfolge").json()
        assert api["embeddings"]["pending"] == 5400 and api["vision"]["in_flight"] == 2


def test_embed_concurrency_setting(tmp_path):
    from projektakte.config import load_settings

    assert load_settings({"DATA_DIR": str(tmp_path)}).embed_concurrency == 2
    assert load_settings({"DATA_DIR": str(tmp_path), "EMBED_CONCURRENCY": "1"}).embed_concurrency == 1
    s = load_settings({"DATA_DIR": str(tmp_path), "EMBED_CONCURRENCY": "20"})
    assert s.embed_concurrency == 2 and any("EMBED_CONCURRENCY" in w for w in s.config_warnings)
