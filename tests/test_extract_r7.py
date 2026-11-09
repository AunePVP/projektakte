"""Round 7: the extraction worker's limits and life cycle.

* A file refused under EXTRACT_CPU_SECONDS or EXTRACT_MEMORY_MB is read again once that limit is raised
  (project files and mail attachments from the cache) – and only then: raising another limit, lowering
  one, or any limit change for documents incomplete for other reasons reads nothing again.
* The worker ends when the app is killed (kill -9): on Linux by PR_SET_PDEATHSIG, elsewhere by checking
  its parent before every page and model call; it lives on when merely the app's thread that started it
  ends.
* A worker that cannot be started does not end the indexing pass: PDF, Word and mail files are recorded
  as not read and retried later, everything else is processed, the error is logged once per pass."""
import email.message
import errno
import logging
import multiprocessing.context
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path

import pytest

from projektakte import documents, extract_worker
from projektakte import extract as extract_mod
from projektakte.ai.services import AIServices
from projektakte.config import load_settings
from projektakte.extract import Extracted, extract, extraction_profile
from projektakte.extract_worker import ExtractWorker, Limits
from projektakte.indexer import run_index

from .conftest import fake_settings
from .test_extract_r5 import Doc, Vision, draw, jpeg, scan
from .test_extract_r6 import (
    EML,
    cpu_heavy_pdf,
    docx_bytes,
    raw_rgb_pdf,
    scan_pages_pdf,
    strip_scan_pdf,
    text_pdf,
)

pytestmark = pytest.mark.skipif(not hasattr(signal, "SIGXCPU"), reason="POSIX-Grenzen (resource) nötig")
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _no_worker_left():
    yield
    extract_worker.shutdown()


def unsized_pdf() -> bytes:
    """A scan page with one readable image and one without size: incomplete, deterministic, no retry."""
    doc = Doc()
    good = doc.image(jpeg(scan((400, 300))), (400, 300), filt=b"/DCTDecode")
    unsized = doc.image(zlib.compress(b"\x80" * 90_000), (None, None))
    doc.page(draw("A", (400, 0, 0, 300, 50, 400)) + b"\n" + draw("B", (300, 0, 0, 300, 50, 50)),
             {"A": good, "B": unsized})
    return doc.build()


def mail_with(name: str, pdf: bytes, subject="Unterlagen Brandschutz") -> bytes:
    """A mail with one PDF attachment."""
    msg = email.message.EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = "Anna <anna@example.org>", "buero@example.org", subject
    msg["Date"], msg["Message-ID"] = "Mon, 3 Mar 2025 10:00:00 +0100", f"<{name}@example.org>"
    msg.set_content("Anbei die Unterlagen zum Brandschutz.")
    msg.add_attachment(pdf, maintype="application", subtype="pdf", filename=name)
    return msg.as_bytes()


def rows(settings) -> dict:
    conn = sqlite3.connect(settings.db_path)
    try:
        return {r[0]: r[1:] for r in conn.execute(
            "SELECT rel_path, status, message, incomplete, extract_profile, indexed_at, "
            "(SELECT COUNT(*) FROM chunks c WHERE c.doc_id = d.id) FROM documents d")}
    finally:
        conn.close()


def index(fake_ai, tmp_path, **env) -> tuple:
    """One indexing pass with isolation and vision: (settings, stats, extraction profile)."""
    settings = fake_settings(fake_ai, tmp_path / "data", PROJECTS_DIR=tmp_path / "projekte", LLM_VISION=1,
                             EXTRACT_ISOLATION=1, MAIL_DIR=tmp_path / "mails", **env)
    ai = AIServices.from_settings(settings)
    return settings, run_index(settings, ai), extraction_profile(ai.llm)


# -- 1: a higher limit reads files refused under the old one ---------------------------------------------------
def test_limits_lift_only_the_limit_that_cut_a_result_short():
    limits = Limits(memory_mb=2048, cpu_seconds=120)
    assert limits.memory_limit() == "memory=2048" and limits.cpu_limit() == "cpu=120"
    assert Limits(cpu_seconds=1.5).cpu_limit() == "cpu=1.5"
    precise = Limits(cpu_seconds=100.0001)  # "%g" would write 100 and re-read a refused file on every pass
    assert precise.cpu_limit() == "cpu=100.0001" and not precise.lifts(precise.cpu_limit())
    assert limits.lifts("memory=256") and limits.lifts("cpu=1")
    assert not limits.lifts("memory=2048") and not limits.lifts("memory=4096")
    assert not limits.lifts("cpu=120") and not limits.lifts("cpu=600")
    assert limits.lifts("cpu=kaputt") and limits.lifts("unbekannt=1")  # unknown: read once more


def _row(profile: str, *, incomplete=1, retry_after=None) -> dict:
    return {"incomplete": incomplete, "extract_profile": profile, "retry_after": retry_after}


def test_reprocess_due_with_limits():
    now, profile = "2026-11-09 12:00:00", "v6:vision:m"
    refused = documents.stored_profile(profile, Extracted([], status="skipped", incomplete=True, limit="cpu=1"))
    assert refused == "v6:vision:m|limit:cpu=1"
    assert documents.stored_profile(profile, Extracted([], status="skipped", incomplete=True)) == profile
    due = documents.reprocess_due
    assert not due(_row(refused), profile, now, Limits(cpu_seconds=1))  # same limit: nothing new
    assert not due(_row(refused), profile, now, Limits(cpu_seconds=1, memory_mb=8192))  # another limit raised
    assert due(_row(refused), profile, now, Limits(cpu_seconds=120))  # raised
    assert due(_row(refused), profile, now, None)  # read without limits (isolation off)
    assert due(_row(refused), "v6:text", now, Limits(cpu_seconds=1))  # the profile changed as before
    assert not due(_row(refused, incomplete=0), profile, now, None)  # complete: never
    # documents incomplete for other reasons do not depend on the limits
    assert not due(_row(profile), profile, now, Limits(cpu_seconds=1))
    assert not due(_row(profile), profile, now, None)
    assert due(_row(profile, retry_after="2026-11-09 11:00:00"), profile, now, Limits())
    assert due(_row(None), profile, now, Limits())  # rows from before migration 6


def test_limits_for_names_the_limits_a_file_is_read_under(tmp_path):
    from projektakte.ai.llm import LLMClient

    settings = load_settings({"DATA_DIR": str(tmp_path), "EXTRACT_ISOLATION": "1", "EXTRACT_CPU_SECONDS": "7"})
    llm = LLMClient("http://127.0.0.1:9/v1", "m", vision=True)
    assert extract_worker.limits_for("a.pdf", llm) is None  # outside a pass: in this process
    with extract_worker.isolation(settings) as worker:
        assert extract_worker.limits_for("a.pdf", llm) is worker.limits and worker.limits.cpu_seconds == 7
        assert extract_worker.limits_for("Mail.EML", None) is worker.limits
        assert extract_worker.limits_for("Notiz.txt", llm) is None  # text and images: in this process
        assert extract_worker.limits_for("Plan.png", llm) is None
        assert extract_worker.limits_for("a.pdf", Vision()) is None  # a model the worker cannot rebuild


def test_memory_shortage_names_the_limit_only_within_one(monkeypatch):
    """The worker's memory limit cut the result short: the result names it. A strip scan whose page image
    cannot be put together for lack of memory is read only partly – with more memory, it is read whole."""
    def no_memory(*args, **kwargs):
        raise MemoryError

    monkeypatch.setattr(extract_mod, "_compose", no_memory)
    r = extract(Path("Streifen.pdf"), strip_scan_pdf(), llm=Vision())
    assert (r.status, r.incomplete, r.retry, r.limit) == ("ok", True, False, None)  # no limit: nothing to raise
    monkeypatch.setattr(extract_mod, "MEMORY_LIMIT_MB", 512)
    r = extract(Path("Streifen.pdf"), strip_scan_pdf(), llm=Vision())
    assert (r.status, r.incomplete, r.retry, r.limit) == ("ok", True, False, "memory=512")
    monkeypatch.setattr(extract_mod, "_docx", no_memory)
    r = extract(Path("Bericht.docx"), docx_bytes())
    assert (r.status, r.limit, r.message) == ("skipped", "memory=512",
                                              "Datei zu groß zum Lesen (Speichergrenze von 512 MB erreicht)")
    # complete results and results incomplete for other reasons name no limit – also right after a file
    # that ran out of memory (the shortage belongs to that file)
    assert extract(Path("Konzept.pdf"), text_pdf()).limit is None
    assert extract(Path("Ohne.pdf"), unsized_pdf(), llm=Vision()).limit is None


def test_raising_the_cpu_limit_reads_a_refused_file_again(fake_ai, tmp_path):
    """A file refused at 1 s of CPU time stays refused while the limit stays (or another limit is
    raised), and is read once EXTRACT_CPU_SECONDS is raised; lowering it again reads nothing. A document
    incomplete for another reason is never read again for a limit change."""
    project = tmp_path / "projekte" / "BS-2024-0011 Werkhalle"
    project.mkdir(parents=True)
    (project / "Aufwendig.pdf").write_bytes(cpu_heavy_pdf(pages=3, mb=2.0))  # about 5 s of CPU time
    (project / "Ohne.pdf").write_bytes(unsized_pdf())
    settings, stats, profile = index(fake_ai, tmp_path, EXTRACT_CPU_SECONDS=1)
    first = rows(settings)
    assert first["Aufwendig.pdf"][:4] == ("skipped", "Datei zu aufwendig zum Lesen (über 1 s Rechenzeit)", 1,
                                          f"{profile}|limit:cpu=1")
    assert first["Ohne.pdf"][2:4] == (1, profile)
    for env in ({"EXTRACT_CPU_SECONDS": 1}, {"EXTRACT_CPU_SECONDS": 1, "EXTRACT_MEMORY_MB": 4096}):
        _, stats, _ = index(fake_ai, tmp_path, **env)
        assert (stats["documents"]["unchanged"], stats["documents"]["reprocessed"]) == (2, 0), env
    _, stats, _ = index(fake_ai, tmp_path, EXTRACT_CPU_SECONDS=60)
    assert stats["documents"]["reprocessed"] == 1
    now = rows(settings)
    assert now["Aufwendig.pdf"][:4] == ("ok", None, 0, profile) and now["Aufwendig.pdf"][5]
    assert now["Ohne.pdf"] == first["Ohne.pdf"]  # not read again
    _, stats, _ = index(fake_ai, tmp_path, EXTRACT_CPU_SECONDS=30)
    assert stats["documents"]["reprocessed"] == 0


def test_raising_the_memory_limit_reads_refused_files_and_attachments_again(fake_ai, tmp_path):
    """A scan whose image does not fit into 256 MB – as a project file and as a mail attachment, which
    cannot be touched to force a new reading – is read once EXTRACT_MEMORY_MB is raised."""
    project = tmp_path / "projekte" / "BS-2024-0012 Lager"
    project.mkdir(parents=True)
    big = raw_rgb_pdf()
    (project / "Gross.pdf").write_bytes(big)
    (tmp_path / "mails").mkdir()
    (tmp_path / "mails" / "anhang.eml").write_bytes(mail_with("Anhang.pdf", big))
    settings, stats, profile = index(fake_ai, tmp_path, EXTRACT_MEMORY_MB=256)
    assert stats["mails"]["new"] == 1
    refused = "Scan ohne Textebene – Bilder zu groß zum Lesen (Speicher- oder Datengrenze überschritten)"
    for name in ("Gross.pdf", "Anhang.pdf"):
        assert rows(settings)[name][:4] == ("skipped", refused, 1, f"{profile}|limit:memory=256"), name
    _, stats, _ = index(fake_ai, tmp_path, EXTRACT_MEMORY_MB=256, EXTRACT_CPU_SECONDS=600)  # another limit
    assert (stats["documents"]["reprocessed"], stats["attachments"]["reprocessed"]) == (0, 0)
    _, stats, _ = index(fake_ai, tmp_path, EXTRACT_MEMORY_MB=2048)
    assert (stats["documents"]["reprocessed"], stats["attachments"]["reprocessed"]) == (1, 1)
    for name in ("Gross.pdf", "Anhang.pdf"):
        status, _message, incomplete, stored, _at, chunks = rows(settings)[name]
        assert (status, incomplete, stored) == ("ok", 0, profile) and chunks, name


# -- 2: the worker ends with the app -----------------------------------------------------------------------------
APP = r'''
import sys, threading, time
from pathlib import Path
from projektakte.ai.llm import LLMClient
from projektakte.extract_worker import ExtractWorker

if __name__ == "__main__":
    url, scan, warm = sys.argv[1], Path(sys.argv[2]).read_bytes(), Path(sys.argv[3]).read_bytes()
    worker = ExtractWorker()
    # started by a thread that ends: the kernel signals the worker then too, and it must live on
    starter = threading.Thread(target=worker.extract, args=("warm.pdf", warm, None))
    starter.start()
    starter.join()
    time.sleep(0.3)
    print(worker.pid, flush=True)
    worker.extract("Scan.pdf", scan, LLMClient(url + "/v1", "fake-vision", vision=True, timeout=60))
'''


def _state(pid: int) -> str:
    """'<state> <parent pid>' of a process (from /proc), 'gone' if it no longer exists."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            fields = fh.read().rsplit(")", 1)[1].split()
    except FileNotFoundError:
        return "gone"
    return f"{fields[0]} {fields[1]}"


def _wait_for(condition, timeout=20.0):
    end = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < end, "Bedingung nicht eingetreten"
        time.sleep(0.02)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc und PR_SET_PDEATHSIG nur unter Linux")
def test_the_worker_ends_when_the_app_is_killed(fresh_fake_ai, tmp_path):
    """kill -9 of the app while its worker reads a 30-page scan with a slow model: before, the orphaned
    worker went on calling the model for the rest of the document (a real server would get up to 500
    calls). Now it ends at once, the call in flight is dropped and no further call reaches the model."""
    fresh_fake_ai.options["delay"] = 1.0
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    (tmp_path / "scan.pdf").write_bytes(scan_pages_pdf(30))
    (tmp_path / "warm.pdf").write_bytes(text_pdf())
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    app = subprocess.Popen([sys.executable, str(tmp_path / "app.py"), fresh_fake_ai.base_url,
                            str(tmp_path / "scan.pdf"), str(tmp_path / "warm.pdf")],
                           stdout=subprocess.PIPE, text=True, cwd=ROOT, env=env)
    worker = None
    try:
        worker = int(app.stdout.readline())
        _wait_for(lambda: fresh_fake_ai.calls.get("chat", 0) >= 2)
        # the worker started by the ended thread is alive and reads the scan for the app
        state, parent = _state(worker).split()
        assert state not in ("Z", "X") and int(parent) == app.pid
        app.kill()
        app.wait()
        calls = fresh_fake_ai.calls.get("chat", 0)
        _wait_for(lambda: _state(worker) == "gone" or _state(worker).startswith(("Z", "X")), timeout=5)
        time.sleep(2.5)  # more than two model calls' time
        assert fresh_fake_ai.calls.get("chat", 0) <= calls + 1  # at most a call being sent at the kill
    finally:
        if app.poll() is None:
            app.kill()
        if worker and _state(worker) != "gone":
            try:
                os.kill(worker, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_the_worker_lives_on_when_the_thread_that_started_it_ends():
    w = ExtractWorker()
    try:
        starter = threading.Thread(target=w.extract, args=("a.pdf", text_pdf(), None))
        starter.start()
        starter.join()
        pid = w.pid
        time.sleep(0.3)  # the kernel's signal for the ended thread has arrived
        assert w.extract("b.pdf", text_pdf(), None).status == "ok"
        assert w.pid == pid and w.started == 1
    finally:
        w.close()


class Left(BaseException):
    pass


@pytest.fixture()
def leave(monkeypatch):
    def left():
        raise Left
    monkeypatch.setattr(extract_worker, "_leave", left)


class _Conn:
    def __init__(self, broken=False):
        self.broken, self.sent = broken, []

    def send_bytes(self, data):
        if self.broken:
            raise BrokenPipeError(errno.EPIPE, "Broken pipe")
        self.sent.append(data)


class _Model:
    vision, model = True, "m"

    def __init__(self):
        self.calls = 0

    def describe_image(self, image, mime, prompt):
        self.calls += 1
        return type("Result", (), {"text": "Brandwand F90"})()


def test_fallback_without_the_kernel_signal_checks_the_app_before_each_model_call(leave):
    """Where PR_SET_PDEATHSIG is missing: the worker checks before every model call that the app is still
    its parent, and ends when the app no longer reads its messages – instead of counting a failed call as
    a model failure and reading the next page."""
    model, conn = _Model(), _Conn()
    reporting = extract_worker._Reporting(model, conn, os.getppid())
    assert reporting.describe_image(b"x", "image/png", "p").text == "Brandwand F90" and conn.sent
    with pytest.raises(Left):  # another parent: the app has gone
        extract_worker._Reporting(model, conn, -1).describe_image(b"x", "image/png", "p")
    assert model.calls == 1  # the model is not called any more
    with pytest.raises(Left):  # the app does not read the progress message
        extract_worker._Reporting(model, _Conn(broken=True), os.getppid()).describe_image(b"x", "image/png", "p")
    with pytest.raises(Left):  # nor a log record
        extract_worker._Forward(_Conn(broken=True)).emit(
            logging.LogRecord("projektakte.extract", logging.WARNING, __file__, 1, "Seite 3", (), None))
    extract_worker._check_app(os.getppid())  # still the parent: goes on


def test_the_app_is_checked_before_every_page(monkeypatch):
    pages = []
    monkeypatch.setattr(extract_mod, "PAGE_CHECK", lambda: pages.append(1))
    assert extract(Path("Konzept.pdf"), text_pdf()).status == "ok" and len(pages) == 1
    pages.clear()
    assert extract(Path("Scan.pdf"), scan_pages_pdf(3), llm=Vision()).status == "ok"
    assert len(pages) == 6  # each page for its text, then each page without text for the model


# -- 3: a worker that cannot start ------------------------------------------------------------------------------
def test_a_worker_that_cannot_start_records_files_for_a_retry_and_logs_once(monkeypatch, caplog):
    attempts = []
    real = multiprocessing.context.SpawnProcess.start

    def no_process(self):
        attempts.append(1)
        raise OSError(errno.EAGAIN, "Resource temporarily unavailable")

    monkeypatch.setattr(multiprocessing.context.SpawnProcess, "start", no_process)
    w = ExtractWorker()
    try:
        with caplog.at_level(logging.ERROR, logger="projektakte.extract_worker"):
            results = [w.extract(name, data, None) for name, data in (
                ("Konzept.pdf", text_pdf()), ("Bericht.docx", docx_bytes()), ("Mail.eml", EML))]
        for r in results:
            assert (r.status, r.incomplete, r.retry, r.limit, r.pages) == ("error", True, True, None, [])
            assert r.message == "Datei nicht gelesen (Leseprozess für Dateien startet nicht) – neuer Versuch später"
        assert len(attempts) == 1  # not tried again for every file
        errors = [rec.getMessage() for rec in caplog.records if rec.levelno == logging.ERROR]
        assert len(errors) == 1 and "Resource temporarily unavailable" in errors[0] and "später erneut" in errors[0]
        w.new_pass()  # the next pass tries again – and the worker starts now
        monkeypatch.setattr(multiprocessing.context.SpawnProcess, "start", real)
        assert w.extract("Konzept.pdf", text_pdf(), None).status == "ok" and w.started == 1
    finally:
        w.close()


def test_start_timeout_names_the_wait(monkeypatch, caplog):
    monkeypatch.setattr(extract_worker, "_START_SECONDS", 0.0)
    w = ExtractWorker()
    try:
        with caplog.at_level(logging.ERROR, logger="projektakte.extract_worker"):
            assert w.extract("a.pdf", text_pdf(), None).retry
        assert "startet nicht (nicht bereit nach 0 s)" in caplog.records[-1].getMessage()
        assert w.pid is None  # the process that did not get ready is gone
    finally:
        w.close()


def test_an_indexing_pass_goes_on_when_the_worker_cannot_start(fake_ai, tmp_path, monkeypatch, caplog):
    """Before, ExtractWorkerError ended the pass in the documents phase: no mail was imported, nothing
    embedded or assigned – on every pass. Now PDF, Word and mail files (also attachments) are recorded as
    not read and retried with the usual back-off; text files, mails, embeddings and assignment go on."""
    project = tmp_path / "projekte" / "BS-2024-0013 Schule"
    project.mkdir(parents=True)
    (project / "Konzept.pdf").write_bytes(text_pdf())
    (project / "Bericht.docx").write_bytes(docx_bytes())
    (project / "Notiz.txt").write_text("Rauchabzug 1 m² im Treppenraum.", encoding="utf-8")
    (tmp_path / "mails").mkdir()
    (tmp_path / "mails" / "anhang.eml").write_bytes(mail_with("Anhang.pdf", text_pdf(), "BS-2024-0013 Pläne"))
    monkeypatch.setattr(extract_worker, "_START_SECONDS", 0.0)  # an overloaded system: never ready in time
    with caplog.at_level(logging.ERROR, logger="projektakte.extract_worker"):
        settings, stats, _ = index(fake_ai, tmp_path)
    assert (stats["documents"]["new"], stats["mails"]["new"]) == (3, 1)
    assert stats["embeddings"]["embedded"] > 0 and "assignment" in stats
    errors = [rec for rec in caplog.records if rec.levelno == logging.ERROR]
    assert len(errors) == 1 and "startet nicht" in errors[0].getMessage()  # once per pass
    conn = sqlite3.connect(settings.db_path)
    stored = {r[0]: r[1:] for r in conn.execute(
        "SELECT rel_path, status, incomplete, attempts, retry_after IS NOT NULL FROM documents")}
    conn.close()
    assert stored["Notiz.txt"] == ("ok", 0, 0, 0)
    for name in ("Konzept.pdf", "Bericht.docx", "Anhang.pdf"):
        assert stored[name] == ("error", 1, 1, 1), name
    # the worker starts again: the next pass whose retry time is due reads them
    monkeypatch.setattr(extract_worker, "_START_SECONDS", 60.0)
    conn = sqlite3.connect(settings.db_path)
    conn.execute("UPDATE documents SET retry_after = '2000-01-01 00:00:00' WHERE retry_after IS NOT NULL")
    conn.commit()
    conn.close()
    caplog.clear()
    _, stats, _ = index(fake_ai, tmp_path)
    assert (stats["documents"]["reprocessed"], stats["attachments"]["reprocessed"]) == (2, 1)
    assert not [rec for rec in caplog.records if rec.levelno == logging.ERROR]
    assert all(r[0] == "ok" and r[5] for r in rows(settings).values())
