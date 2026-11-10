"""Round 6: files are read in a worker process with operating-system limits (memory, CPU time per file, a
wall-clock backstop), so no crafted attachment can exhaust the app's memory or stall indexing for long;
and the remaining holes of the scan reading: soft masks are measured, counted, shared and applied (never
in chains), JPEG 2000 and masked JPEGs are decoded at reduced scale, a page is scaled before it is put
together in colour, decodings are charged by their work, vector drawings are blank pages, and images
that are too large or have no size are told apart and not retried."""
import io
import os
import signal
import sqlite3
import threading
import time
import zlib
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, features

from projektakte import documents, extract_worker
from projektakte import extract as extract_mod
from projektakte.ai.llm import LLMClient
from projektakte.config import load_settings
from projektakte.extract import extract
from projektakte.extract_worker import ExtractionAborted, ExtractWorker, Limits

from .conftest import fake_settings
from .test_extract_r5 import Doc, Vision, child, draw, flate, grid, jpeg, measure, scan

pytestmark = pytest.mark.skipif(not hasattr(signal, "SIGXCPU"), reason="POSIX-Grenzen (resource) nötig")


# -- files ------------------------------------------------------------------------------------------------------
def text_pdf(lines=("Brandschutzkonzept Kita Regenbogen", "Rettungsweg über Treppenraum Nord, F90")) -> bytes:
    doc = Doc()
    font = doc.add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    shown = b" ".join(b"(%s) Tj 0 -16 Td" % line.encode("latin-1") for line in lines)
    content = b"BT /F1 12 Tf 50 780 Td " + shown + b" ET"
    doc.pages.append((doc.stream(b"", content), b"<< /Font << /F1 %d 0 R >> >>" % font))
    return doc.build()


def strip_scan_pdf(n=6, text="Rauchabzug Treppenraum") -> bytes:
    doc = Doc()
    original = scan((600, 600), text=text)
    ids = {}
    for k in range(n):
        strip = original.crop((0, 100 * k, 600, 100 * (k + 1)))
        ids[f"S{k}"] = doc.image(jpeg(strip), strip.size, filt=b"/DCTDecode")
    doc.page(b"\n".join(draw(name, (480, 0, 0, 80, 50, 700 - 80 * (k + 1))) for k, name in enumerate(ids)), ids)
    return doc.build()


def scan_pages_pdf(pages=1) -> bytes:
    """``pages`` pages, each with an image of its own (one model call per page)."""
    doc = Doc()
    for k in range(pages):
        img = doc.image(jpeg(scan((400, 300), text=f"Seite {k} Brandwand")), (400, 300), filt=b"/DCTDecode")
        doc.page(draw("Im1", (400, 0, 0, 300, 50, 400)), {"Im1": img})
    return doc.build()


def docx_bytes() -> bytes:
    import docx

    document = docx.Document()
    document.add_heading("Prüfbericht Brandmeldeanlage", level=1)
    document.add_paragraph("Die Anlage ist betriebsbereit; Mängel: keine.")
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


EML = ("From: Anna <anna@example.org>\nTo: buero@example.org\nSubject: Auflagen Brandwand\n"
       "Date: Mon, 3 Mar 2025 10:00:00 +0100\nContent-Type: text/plain; charset=utf-8\n\n"
       "Die Brandwand zwischen Halle und Lager ist in F90 auszuführen.\n").encode()


def cpu_heavy_pdf(pages=20, mb=2.0) -> bytes:
    """Pages with distinct content of a few MB of text operators: about 100 KB, pypdf's text extraction
    parses every page (about 35 s of CPU time for the default arguments)."""
    doc = Doc()
    font = doc.add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    unit = b"BT /F1 12 Tf 10 10 Td (x) Tj ET\n"
    for k in range(pages):
        content = b"%% %d\n" % k + unit * int(mb * 1_000_000 / len(unit))
        doc.pages.append((doc.stream(b"/Filter /FlateDecode", zlib.compress(content, 9)),
                          b"<< /Font << /F1 %d 0 R >> >>" % font))
    return doc.build()


def raw_rgb_pdf(size=(4000, 6000)) -> bytes:
    """One raw RGB image of 72 MB (just under pypdf's 75 MB): about 300 MB to decode."""
    w, h = size
    doc = Doc()
    img = doc.image(zlib.compress(bytes(range(256)) * (w * h * 3 // 256), 6), size, cs=b"/DeviceRGB")
    doc.page(draw("Im1", (500, 0, 0, 750, 40, 40)), {"Im1": img})
    return doc.build()


def rss_mb() -> float:
    return next(int(line.split()[1]) for line in open("/proc/self/status") if line.startswith("VmRSS:")) / 1024


@pytest.fixture()
def worker():
    workers = []

    def make(**limits) -> ExtractWorker:
        w = ExtractWorker(Limits(**limits))
        workers.append(w)
        return w
    yield make
    for w in workers:
        w.close()


def vision_llm(server, **kw) -> LLMClient:
    return LLMClient(f"{server.base_url}/v1", "fake-vision", vision=True, **{"timeout": 30, **kw})


def wait_for(condition, timeout=20.0):
    end = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < end, "Bedingung nicht eingetreten"
        time.sleep(0.02)


# -- settings ----------------------------------------------------------------------------------------------------
def test_isolation_is_on_by_default_except_in_tests():
    s = load_settings({})
    assert (s.extract_isolation, s.extract_memory_mb, s.extract_cpu_seconds, s.extract_wall_seconds) == (
        True, 2048, 120.0, 3600.0)
    assert load_settings({"APP_ENV": "production"}).extract_isolation
    assert not load_settings({"APP_ENV": "test"}).extract_isolation
    assert load_settings({"APP_ENV": "test", "EXTRACT_ISOLATION": "1"}).extract_isolation
    assert not load_settings({"EXTRACT_ISOLATION": "0"}).extract_isolation
    s = load_settings({"EXTRACT_MEMORY_MB": "4096", "EXTRACT_CPU_SECONDS": "30", "EXTRACT_WALL_SECONDS": "600"})
    assert (s.extract_memory_mb, s.extract_cpu_seconds, s.extract_wall_seconds) == (4096, 30.0, 600.0)
    assert s.config_warnings == ()


def test_unusable_extract_limits_warn_and_fall_back():
    s = load_settings({"EXTRACT_MEMORY_MB": "viel", "EXTRACT_CPU_SECONDS": "0", "EXTRACT_WALL_SECONDS": "-5"})
    assert (s.extract_memory_mb, s.extract_cpu_seconds, s.extract_wall_seconds) == (2048, 120.0, 3600.0)
    assert len(s.config_warnings) == 3 and all("EXTRACT_" in w for w in s.config_warnings)
    s = load_settings({"EXTRACT_MEMORY_MB": "64"})
    assert s.extract_memory_mb == 256 and "zu klein" in s.config_warnings[0]


def test_llm_client_is_rebuilt_from_its_configuration():
    original = LLMClient("http://ki:8000/v1/", "qwen-vl", timeout=33.5, max_tokens=900, vision=True,
                         api_key="geheim", extra_body={"top_k": 20})
    copy = LLMClient.from_config(original.config())
    assert type(copy) is LLMClient and copy is not original
    for attr in ("url", "model", "timeout", "max_tokens", "vision", "extra_body", "_headers"):
        assert getattr(copy, attr) == getattr(original, attr), attr
    assert copy._headers == {"Authorization": "Bearer geheim"}


# -- the worker ----------------------------------------------------------------------------------------------------
def test_isolated_extraction_gives_the_same_results(fake_ai, worker):
    """Text PDF, strip scan read by the vision model, Word, mail: identical with and without isolation,
    in one reused worker process."""
    llm = vision_llm(fake_ai)
    w = worker()
    for name, data in (("Konzept.pdf", text_pdf()), ("Scan.pdf", strip_scan_pdf()), ("Bericht.docx", docx_bytes()),
                       ("Mail.eml", EML), ("Leer.pdf", text_pdf(()))):
        isolated = w.extract(name, data, llm)
        assert isolated == extract(Path(name), data, llm=llm), name
    assert isolated.status == "empty"
    assert w.started == 1 and w.pid is not None and w.pid != os.getpid()
    no_vision = w.extract("Scan.pdf", strip_scan_pdf(), None)
    assert (no_vision.status, no_vision.incomplete) == ("skipped", True)


def test_cpu_limit_records_the_file_and_the_next_file_gets_a_new_worker(worker):
    """About 35 s of CPU time from 100 KB (pypdf's text extraction): the kernel ends the worker at the CPU
    limit; the file is recorded as too costly – deterministic, no retry – and indexing goes on."""
    w = worker(cpu_seconds=2)
    data = cpu_heavy_pdf()
    assert len(data) < 150_000
    started = time.monotonic()
    r = w.extract("Aufwendig.pdf", data, None)
    assert time.monotonic() - started < 25
    assert (r.status, r.incomplete, r.retry, r.pages) == ("skipped", True, False, [])
    assert r.message == "Datei zu aufwendig zum Lesen (über 2 s Rechenzeit)"
    ok = w.extract("Konzept.pdf", text_pdf(), None)
    assert ok.status == "ok" and w.started == 2


def test_memory_limit_keeps_the_app_small(worker):
    """A raw colour image that takes about 300 MB to decode, with 256 MB allowed: the worker's allocation
    fails (MemoryError) – the image is too large to read, not retried – while the app process does not
    grow. The worker stays usable."""
    w = worker(memory_mb=256)
    w.extract("warm.pdf", text_pdf(), None)  # the worker runs; the app's memory from here on
    before = rss_mb()
    # the image is too large before the model is called: an unreachable one does not matter
    r = w.extract("Gross.pdf", raw_rgb_pdf(), LLMClient("http://127.0.0.1:9/v1", "m", vision=True, timeout=1))
    assert (r.status, r.incomplete, r.retry) == ("skipped", True, False)
    assert r.message == "Scan ohne Textebene – Bilder zu groß zum Lesen (Speicher- oder Datengrenze überschritten)"
    assert rss_mb() - before < 40
    assert w.extract("Konzept.pdf", text_pdf(), None).status == "ok"


def test_a_file_beyond_the_memory_limit_is_too_big_to_read(worker):
    w = worker(memory_mb=256)
    body = b"Brandschutz Zeile mit etwas Text\n" * 1_800_000  # 60 MB mail body
    r = w.extract("Riesig.eml", EML.split(b"\n\n")[0] + b"\n\n" + body, None)
    assert (r.status, r.incomplete, r.retry) == ("skipped", True, False)
    assert r.message == "Datei zu groß zum Lesen (Speichergrenze von 256 MB erreicht)"


@pytest.mark.parametrize("sig, text, retry", [
    (signal.SIGKILL, "Leseprozess vom Betriebssystem beendet", False),
    (signal.SIGSEGV, "Leseprozess abgestürzt: SIGSEGV", False),
])
def test_worker_ended_while_reading_is_recorded_and_restarted(fresh_fake_ai, worker, sig, text, retry):
    fresh_fake_ai.options["delay"] = 3.0
    llm = vision_llm(fresh_fake_ai)
    w = worker()
    w.extract("warm.pdf", text_pdf(), None)
    result = {}
    reader = threading.Thread(target=lambda: result.update(r=w.extract("Scan.pdf", scan_pages_pdf(), llm)))
    reader.start()
    wait_for(lambda: fresh_fake_ai.calls.get("chat"))  # the worker waits for the model
    os.kill(w.pid, sig)
    reader.join(20)
    r = result["r"]
    assert (r.status, r.incomplete, r.retry) == ("skipped", True, retry) and text in r.message
    fresh_fake_ai.options["delay"] = 0.0
    assert w.extract("Scan.pdf", scan_pages_pdf(), llm).status == "ok" and w.started == 2


def test_worker_killed_while_idle_is_started_again(worker):
    w = worker()
    assert w.extract("a.pdf", text_pdf(), None).status == "ok"
    pid = w.pid
    os.kill(pid, signal.SIGKILL)
    wait_for(lambda: w.pid is None)
    assert w.extract("b.pdf", text_pdf(), None).status == "ok"
    assert w.started == 2 and w.pid not in (None, pid)


def test_wall_clock_backstop_is_retried_later_and_model_answers_restart_it(fresh_fake_ai, worker):
    """A worker that waits without using CPU (a hanging model call) is ended after EXTRACT_WALL_SECONDS
    without progress – not deterministic, so retried with back-off. Every model answer starts the backstop
    again: a scan read page by page may take longer in total."""
    fresh_fake_ai.options["delay"] = 4.0
    w = worker(wall_seconds=1.5)
    started = time.monotonic()
    r = w.extract("Scan.pdf", scan_pages_pdf(), vision_llm(fresh_fake_ai))
    assert time.monotonic() - started < 3.9
    assert (r.status, r.incomplete, r.retry) == ("error", True, True)
    assert r.message == "Datei nicht fertig gelesen (über 1,5 s ohne Fortschritt) – neuer Versuch später"
    fresh_fake_ai.options["delay"] = 1.2
    w = worker(wall_seconds=3)
    started = time.monotonic()
    r = w.extract("Scan.pdf", scan_pages_pdf(4), vision_llm(fresh_fake_ai))
    assert time.monotonic() - started > 4  # longer than the backstop in total
    assert (r.status, r.incomplete, len(r.pages)) == ("ok", False, 4)


def test_close_while_reading_abandons_the_file(fresh_fake_ai, worker):
    """App shutdown: the file being read is abandoned without a result (nothing is recorded for it, it
    is read again on the next start)."""
    fresh_fake_ai.options["delay"] = 5.0
    w = worker()
    w.extract("warm.pdf", text_pdf(), None)
    outcome = {}

    def read():
        try:
            outcome["r"] = w.extract("Scan.pdf", scan_pages_pdf(), vision_llm(fresh_fake_ai))
        except ExtractionAborted as exc:
            outcome["aborted"] = exc
    reader = threading.Thread(target=read)
    reader.start()
    wait_for(lambda: fresh_fake_ai.calls.get("chat"))
    started = time.monotonic()
    w.close()
    reader.join(10)
    assert "aborted" in outcome and "r" not in outcome
    assert time.monotonic() - started < 5 and w.pid is None
    with pytest.raises(ExtractionAborted):
        w.extract("a.pdf", text_pdf(), None)


def test_worker_log_records_and_model_errors_reach_the_app(fresh_fake_ai, worker, caplog):
    fresh_fake_ai.options["fail"] = ["chat"]
    llm = vision_llm(fresh_fake_ai)
    doc = Doc()
    doc.page(draw("Im1", (300, 0, 0, 300, 50, 50)),
             {"Im1": doc.image(b"kein JPEG", (300, 300), filt=b"/DCTDecode"),
              "Im2": doc.image(jpeg(scan((300, 300))), (300, 300), filt=b"/DCTDecode")})
    doc.page(draw("Im2", (300, 0, 0, 300, 50, 50)), {"Im2": doc.image(jpeg(scan((300, 300))), (300, 300),
                                                                    filt=b"/DCTDecode")})
    with caplog.at_level("WARNING"):
        r = worker().extract("Scan.pdf", doc.build(), llm)
    assert (r.status, r.retry) == ("error", True)
    messages = [(rec.name, rec.getMessage()) for rec in caplog.records]
    assert any(name == "projektakte.extract" and "Bild nicht lesbar" in text for name, text in messages)
    assert any(name == "projektakte.extract" and "Bildauswertung fehlgeschlagen" in text for name, text in messages)
    assert llm.last_error and "500" in llm.last_error  # the status page shows the model's last error


# -- the indexing pass -------------------------------------------------------------------------------------------
def test_prepare_document_uses_the_worker_only_during_a_pass_with_isolation(tmp_path, monkeypatch):
    calls = []
    real = documents.extract
    monkeypatch.setattr(documents, "extract", lambda path, data, llm=None: calls.append(path.name) or real(
        path, data, llm=llm))
    settings = load_settings({"DATA_DIR": str(tmp_path), "EXTRACT_ISOLATION": "1"})
    with extract_worker.isolation(settings) as w:
        assert extract_worker.current() is w and w is not None
        assert documents.prepare_document(rel_path="a/Konzept.pdf", data=text_pdf(), mtime=None).result.status == "ok"
        documents.prepare_document(rel_path="Notiz.txt", data=b"Rettungsweg Nord", mtime=None)  # only decoded
        documents.prepare_document(rel_path="b.pdf", data=text_pdf(), mtime=None, llm=Vision())  # a stub model
        assert w.started == 1
    assert extract_worker.current() is None
    documents.prepare_document(rel_path="c.pdf", data=text_pdf(), mtime=None)
    assert calls == ["Notiz.txt", "b.pdf", "c.pdf"]
    with extract_worker.isolation(load_settings({"DATA_DIR": str(tmp_path), "APP_ENV": "test"})) as w:
        assert w is None and extract_worker.current() is None
    extract_worker.shutdown()
    assert extract_worker._worker.closed


def _project(tmp_path) -> Path:
    root = tmp_path / "projekte"
    proj = root / "BS-2024-0007 Lagerhalle"
    proj.mkdir(parents=True)
    (proj / "Konzept.pdf").write_bytes(text_pdf())
    (proj / "Scan.pdf").write_bytes(strip_scan_pdf())
    (proj / "Bericht.docx").write_bytes(docx_bytes())
    (proj / "Mail.eml").write_bytes(EML)
    (proj / "Notiz.txt").write_text("Rauchabzug 1 m² im Treppenraum.", encoding="utf-8")
    return root


def test_indexing_pass_with_isolation_stores_the_same(fake_ai, tmp_path):
    from projektakte.ai.services import AIServices
    from projektakte.indexer import run_index

    root = _project(tmp_path)
    stored = {}
    for isolation in ("0", "1"):
        settings = fake_settings(fake_ai, tmp_path / f"data{isolation}", PROJECTS_DIR=root, LLM_VISION=1,
                                 EXTRACT_ISOLATION=isolation, MAIL_DIR=tmp_path / "keine-mails")
        run_index(settings, AIServices.from_settings(settings))
        conn = sqlite3.connect(settings.db_path)
        stored[isolation] = (
            conn.execute("SELECT rel_path, status, message, method, incomplete, pages FROM documents "
                         "ORDER BY rel_path").fetchall(),
            conn.execute("SELECT text FROM chunks ORDER BY doc_id, ord").fetchall())
        conn.close()
    assert stored["0"] == stored["1"] and len(stored["1"][0]) == 5
    assert extract_worker._worker is not None and extract_worker._worker.started >= 1
    extract_worker.shutdown()


def test_background_indexer_stop_ends_the_worker(fake_ai, tmp_path):
    from projektakte.ai.services import AIServices
    from projektakte.background import BackgroundIndexer

    settings = fake_settings(fake_ai, tmp_path / "data", PROJECTS_DIR=_project(tmp_path), EXTRACT_ISOLATION=1,
                             MAIL_DIR=tmp_path / "keine-mails")
    indexer = BackgroundIndexer(settings, AIServices.from_settings(settings))
    assert indexer.run_once()["documents"]["new"] == 5
    worker = extract_worker._worker
    assert worker.pid is not None
    indexer.stop()
    assert worker.closed and worker.pid is None


def test_stopping_the_app_while_a_file_is_read_ends_the_pass_at_once(fresh_fake_ai, tmp_path):
    """Shutdown while the phase "Bildauswertung" waits for a slow model: the pass ends without waiting for
    it, the file stays stored with its page waiting (read again on the next start), and it is no indexing
    error. (The file itself is stored by the documents pass, before the model is asked.)"""
    from projektakte.ai.services import AIServices
    from projektakte.background import BackgroundIndexer

    root = tmp_path / "projekte"
    (root / "BS-2024-0008 Halle").mkdir(parents=True)
    (root / "BS-2024-0008 Halle" / "Scan.pdf").write_bytes(scan_pages_pdf())
    settings = fake_settings(fresh_fake_ai, tmp_path / "data", PROJECTS_DIR=root, EXTRACT_ISOLATION=1,
                             LLM_VISION=1, MAIL_DIR=tmp_path / "keine-mails", INDEX_INTERVAL=0)
    fresh_fake_ai.options["delay"] = 30.0
    indexer = BackgroundIndexer(settings, AIServices.from_settings(settings))
    indexer.start()
    wait_for(lambda: fresh_fake_ai.calls.get("chat"), timeout=120.0)  # (the embeddings wait first)
    started = time.monotonic()
    indexer.stop()
    assert not indexer.alive and time.monotonic() - started < 5
    assert indexer.status()["last_error"] is None
    conn = sqlite3.connect(settings.db_path)
    assert conn.execute("SELECT COUNT(*), SUM(vision_pending) FROM documents").fetchone() == (1, 1)
    assert conn.execute("SELECT COUNT(*) FROM page_texts WHERE state = 'pending'").fetchone()[0] == 1
    conn.close()


# -- soft masks ----------------------------------------------------------------------------------------------------
def masked_image(doc: Doc, mask_id: int, size=(10, 10), seed=0) -> int:
    w, h = size
    return doc.stream(b"/Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceGray "
                      b"/BitsPerComponent 8 /Filter /FlateDecode /SMask %d 0 R" % (w, h, mask_id),
                      zlib.compress(bytes([seed % 256]) * (w * h)))


def test_a_soft_mask_counts_against_the_page_limit(monkeypatch, decodes):
    """A 10×10 image with a 1200×1200 soft mask: before, the mask was decoded unseen by any limit (a
    13000×13000 mask took 1.3 GB, a chain of three 6 GB). Its real size counts."""
    monkeypatch.setattr(extract_mod, "MAX_PAGE_PIXELS", 1_000_000)
    doc = Doc()
    mask = doc.image(jpeg(scan((1200, 1200))), (10, 10), filt=b"/DCTDecode")  # declared small, really 1.4 MP
    doc.page(draw("Im1", (300, 0, 0, 300, 50, 50)), {"Im1": masked_image(doc, mask)})
    r = extract(Path("Maske.pdf"), doc.build(), llm=Vision())
    assert decodes == []
    assert (r.status, r.retry) == ("skipped", False)
    assert r.message == "Scan ohne Textebene – Bilder zu groß (über 1 Megapixel je Seite)"


def test_a_soft_mask_is_applied_onto_white():
    """Black ink whose shape is only in the soft mask (signatures, stamps): before, the alpha channel was
    dropped for large images and JPEGs – a black box. Now the image is put onto white, as on the page."""
    w, h = 400, 200
    shape = Image.new("L", (w, h), 0)
    ImageDraw.Draw(shape).rectangle([50, 50, 350, 150], fill=255)
    doc = Doc()
    mask = doc.image(flate(shape), (w, h))
    ink = doc.stream(b"/Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB /BitsPerComponent 8 "
                     b"/Filter /FlateDecode /SMask %d 0 R" % (w, h, mask), zlib.compress(b"\x00" * (w * h * 3)))
    doc.page(draw("Im1", (400, 0, 0, 200, 50, 50)), {"Im1": ink})
    llm = Vision()
    r = extract(Path("Stempel.pdf"), doc.build(), llm=llm)
    assert r.status == "ok"
    sent = llm.images[0].convert("L")
    assert sent.getpixel((10, 10)) > 240 and sent.getpixel((200, 100)) < 20  # white around, ink inside


def test_one_mask_shared_by_many_images_is_decoded_once(monkeypatch):
    """200 different images sharing one soft mask: before, the mask was decoded with each of them (82 s for
    one page with a 13000×13000 mask). Now it is counted once per page and decoded once."""
    doc = Doc()
    mask = doc.image(jpeg(scan((1600, 1600))), (1600, 1600), filt=b"/DCTDecode")
    names = {f"I{k}": masked_image(doc, mask, seed=k) for k in range(200)}
    doc.page(b"\n".join(draw(name, m) for name, m in zip(names, grid(200, 20))), names)
    sizes = []
    real = extract_mod._decode_stream
    monkeypatch.setattr(extract_mod, "_decode_stream",
                        lambda stream, size, *a: sizes.append(size) or real(stream, size, *a))
    llm = Vision()
    r = extract(Path("Geteilt.pdf"), doc.build(), llm=llm)
    assert (r.status, r.incomplete) == ("ok", False) and len(llm.images) == 1
    assert sizes.count((1600, 1600)) == 1 and len(sizes) == 201


def test_masks_of_masks_are_not_followed(monkeypatch):
    doc = Doc()
    inner = doc.image(jpeg(scan((2000, 2000))), (2000, 2000), filt=b"/DCTDecode")
    outer = doc.stream(b"/Type /XObject /Subtype /Image /Width 300 /Height 300 /ColorSpace /DeviceGray "
                       b"/BitsPerComponent 8 /Filter /FlateDecode /SMask %d 0 R" % inner,
                       zlib.compress(b"\xff" * 90_000))
    doc.page(draw("Im1", (300, 0, 0, 300, 50, 50)),
             {"Im1": masked_image(doc, outer, size=(300, 300))})
    sizes = []
    real = extract_mod._decode_stream
    monkeypatch.setattr(extract_mod, "_decode_stream",
                        lambda stream, size, *a: sizes.append(size) or real(stream, size, *a))
    r = extract(Path("Kette.pdf"), doc.build(), llm=Vision())
    assert r.status == "ok" and (2000, 2000) not in sizes


# -- reduced decoding -------------------------------------------------------------------------------------------
def _jpx(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG2000", irreversible=False)
    return buf.getvalue()


@pytest.mark.skipif(not features.check("jpg_2000"), reason="Pillow ohne OpenJPEG")
def test_jpeg2000_with_a_decode_array_is_decoded_at_reduced_scale_with_pypdfs_colours(monkeypatch):
    """A /Decode array sent JPEG 2000 through pypdf's full decoding (3.6 GB for 100 MP from 90 KB). Now it is
    decoded at reduced scale and the array applied afterwards – the colours are those of pypdf."""
    from pypdf import PdfReader
    from pypdf.generic._image_xobject import _xobj_to_image

    plan = scan((1200, 800), mode="RGB", text="Fluchtweg")
    doc = Doc()
    img = doc.stream(b"/Type /XObject /Subtype /Image /Width 1200 /Height 800 /ColorSpace /DeviceRGB "
                     b"/BitsPerComponent 8 /Filter /JPXDecode /Decode [1 0 1 0 1 0]", _jpx(plan))
    doc.page(draw("Im1", (400, 0, 0, 266, 50, 50)), {"Im1": img})
    data = doc.build()
    reduced = []
    real = extract_mod._decode_reduced
    monkeypatch.setattr(extract_mod, "_decode_reduced", lambda s, t: reduced.append(t) or real(s, t))
    monkeypatch.setattr(extract_mod, "MAX_IMAGE_PIXELS", 60_000)
    llm = Vision()
    assert extract(Path("x.pdf"), data, llm=llm).status == "ok"
    assert reduced == [(300, 200)] and llm.images[0].size == (300, 200)
    stream = PdfReader(io.BytesIO(data)).pages[0]["/Resources"]["/XObject"]["/Im1"]
    reference = _xobj_to_image(stream)[2].convert("RGB").resize((300, 200))
    from .test_daten_r3_scans import similar

    assert similar(llm.images[0], reference) < 6
    assert similar(llm.images[0], plan) > 100  # inverted, as the array says


def test_masked_jpeg_is_decoded_at_reduced_scale_not_reencoded(monkeypatch):
    """pypdf encodes a JPEG with a soft mask once more as JPEG 2000 (and decodes the mask in full); the
    image is now decoded at reduced scale and the mask applied to it."""
    calls = []
    import pypdf.generic._image_xobject as pypdf_images

    real = pypdf_images._xobj_to_image
    monkeypatch.setattr(pypdf_images, "_xobj_to_image", lambda s, *a, **k: calls.append(s) or real(s, *a, **k))
    monkeypatch.setattr(extract_mod, "MAX_IMAGE_PIXELS", 100_000)
    doc = Doc()
    mask = doc.image(flate(Image.new("L", (100, 100), 255)), (100, 100))
    img = doc.stream(b"/Type /XObject /Subtype /Image /Width 1200 /Height 800 /ColorSpace /DeviceRGB "
                     b"/BitsPerComponent 8 /Filter /DCTDecode /SMask %d 0 R" % mask,
                     jpeg(scan((1200, 800), mode="RGB")))
    doc.page(draw("Im1", (400, 0, 0, 266, 50, 50)), {"Im1": img})
    llm = Vision()
    assert extract(Path("x.pdf"), doc.build(), llm=llm).status == "ok"
    assert all(s.get("/Filter") != "/DCTDecode" for s in calls)  # only the small Flate mask went through pypdf
    assert llm.images[0].size[0] * llm.images[0].size[1] <= 100_000


# -- putting a page together -----------------------------------------------------------------------------------
_A0_BILEVEL_WITH_STAMP = r"""
import io, sys
from PIL import Image, ImageDraw
sys.path.insert(0, sys.argv[2])
from test_extract_r5 import Doc, jpeg, scan
w, h = 9933, 14043
plan = Image.new("1", (w, h), 1)
d = ImageDraw.Draw(plan)
for y in range(100, h, 400):
    d.text((100, y), "Brandschutzplan Erdgeschoss Achse %d" % y, fill=0)
    d.line([0, y, w, y], fill=0, width=3)
buf = io.BytesIO()
plan.save(buf, "TIFF", compression="group4", rowsperstrip=h)
tif = Image.open(io.BytesIO(buf.getvalue()))
g4 = buf.getvalue()[tif.tag_v2[273][0]:tif.tag_v2[273][0] + tif.tag_v2[279][0]]
doc = Doc()
cc = doc.stream(b"/Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceGray /BitsPerComponent 1 "
                b"/Filter /CCITTFaxDecode /DecodeParms << /K -1 /Columns %d /Rows %d /BlackIs1 false >>"
                % (w, h, w, h), g4)
stamps = [doc.image(jpeg(scan((200, 100), mode=m)), (200, 100), filt=b"/DCTDecode",
                    cs=b"/DeviceRGB" if m == "RGB" else b"/DeviceGray") for m in ("RGB", "L", "L")]
content = (b"q 595 0 0 742 0 100 cm /P Do Q\n" + b"".join(b"q 195 0 0 100 %d 0 cm /S%d Do Q\n" % (200 * k, k)
                                                          for k in range(3)))
doc.page(content, {"P": cc, "S0": stamps[0], "S1": stamps[1], "S2": stamps[2]})
open(sys.argv[1], "wb").write(doc.build())
"""


def test_bilevel_a0_plan_with_a_colour_stamp_is_scaled_before_it_turns_colour(tmp_path):
    """An A0 bilevel plan (CCITT, 140 MP) put together with a small colour stamp: before, the plan was
    converted to grey and then colour at its full size (1.1 GB); now it is scaled first."""
    path = tmp_path / "A0_Stempel.pdf"
    child(_A0_BILEVEL_WITH_STAMP, str(path), str(Path(__file__).parent))
    result = measure(path)
    assert (result["status"], result["incomplete"]) == ("ok", False) and len(result["sizes"]) == 1
    assert result["rss_mb"] < 600, result


# -- pages without images, images that cannot be read --------------------------------------------------------------
def vector_content(kb: int, k: int = 0) -> bytes:
    unit = b"0.5 w 10 10 m 500 800 l S 20 30 m 400 700 l S\n"
    return b"%% Plan %d\n" % k + unit * (kb * 1000 // len(unit))


def test_vector_drawings_are_blank_pages_and_use_no_content_bound(monkeypatch):
    """CAD plans with their text as outlines: no image the model could read. Before, a page with more
    content than the page bound ended as 'Seiteninhalt zu umfangreich' (incomplete), and such pages used up
    the document's content bound so that scans after them went unread."""
    monkeypatch.setattr(extract_mod, "MAX_PAGE_CONTENT_BYTES", 400_000)
    monkeypatch.setattr(extract_mod, "MAX_DOC_CONTENT_BYTES", 1_000_000)
    doc = Doc()
    doc.page(vector_content(500), {}, compress=True)
    r = extract(Path("CAD.pdf"), doc.build(), llm=Vision())
    assert (r.status, r.incomplete, r.message) == ("empty", False, "kein Text gefunden")

    doc = Doc()
    for k in range(4):  # 1.5 MB of vector content: over the document's content bound
        doc.page(vector_content(380, k), {}, compress=True)
    img = doc.image(jpeg(scan((400, 300))), (400, 300), filt=b"/DCTDecode")
    for k in range(3):
        doc.page(draw("Im1", (400, 0, 0, 300, 50 + k, 400)), {"Im1": img})
    llm = Vision()
    r = extract(Path("Gemischt.pdf"), doc.build(), llm=llm)
    assert (r.status, r.incomplete, r.message) == ("ok", False, None)
    assert [p.number for p in r.pages] == [5, 6, 7]


def test_large_content_with_an_inline_image_is_still_walked(monkeypatch):
    monkeypatch.setattr(extract_mod, "MAX_PAGE_CONTENT_BYTES", 400_000)
    small = scan((60, 40)).convert("L")
    inline = b"q 60 0 0 40 50 50 cm BI /W 60 /H 40 /CS /G /BPC 8 ID " + small.tobytes() + b" EI Q\n"
    doc = Doc()
    doc.page(vector_content(500) + inline, {}, compress=True)
    r = extract(Path("Inline.pdf"), doc.build(), llm=Vision())
    assert r.status == "skipped" and "Seiteninhalt zu umfangreich" in r.message  # not known to be blank


def test_an_image_without_size_is_named_as_such_in_the_note():
    doc = Doc()
    good = doc.image(jpeg(scan((400, 300))), (400, 300), filt=b"/DCTDecode")
    unsized = doc.image(zlib.compress(b"\x80" * 90_000), (None, None))
    doc.page(draw("A", (400, 0, 0, 300, 50, 400)) + b"\n" + draw("B", (300, 0, 0, 300, 50, 50)),
             {"A": good, "B": unsized})
    r = extract(Path("x.pdf"), doc.build(), llm=Vision())
    assert (r.status, r.incomplete, r.retry) == ("ok", True, False)
    assert r.message == "1 Seite(n) nur teilweise gelesen (1 Bild(er) ohne Größenangabe nicht ausgewertet)"


def test_image_over_pypdfs_data_limit_is_too_big_and_not_retried():
    """An indexed 1-bit A0 plan expands to 139 MB, beyond pypdf's 75 MB: before, 'nicht lesbar' with a retry
    every few hours; the file will not change."""
    w, h = 9933, 14043
    doc = Doc()
    img = doc.stream(b"/Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace [/Indexed /DeviceRGB 1 "
                     b"<FFFFFF000000>] /BitsPerComponent 1 /Filter /FlateDecode" % (w, h),
                     zlib.compress(b"\x00" * ((w + 7) // 8 * h)))
    doc.page(draw("Im1", (595, 0, 0, 842, 0, 0)), {"Im1": img})
    r = extract(Path("Plan.pdf"), doc.build(), llm=Vision())
    assert (r.status, r.incomplete, r.retry) == ("skipped", True, False)
    assert r.message == "Scan ohne Textebene – Bilder zu groß zum Lesen (Speicher- oder Datengrenze überschritten)"


def test_decodings_are_charged_by_their_work(monkeypatch, decodes):
    """Images declared 10×10 whose data inflates to 8 MB each (pypdf inflates it all): before, each was
    charged as a tiny image (25 s per page of 200). The excess data counts against the document."""
    monkeypatch.setattr(extract_mod, "MAX_DOC_PIXELS", 60_000_000)
    doc = Doc()
    names = {f"B{k}": doc.image(zlib.compress(bytes([k]) * 8_000_000, 9), (10, 10)) for k in range(30)}
    doc.page(b"\n".join(draw(n, m) for n, m in zip(names, grid(30))), names)
    r = extract(Path("Bombe.pdf"), doc.build(), llm=Vision())
    assert len(decodes) <= 8
    assert r.incomplete and not r.retry and "je Dokument" in r.message


def test_pages_with_the_same_content_are_read_once(monkeypatch):
    """200 pages sharing one content stream that draws a form 990 times: before, 232 s (pypdf's text
    extraction and the bounded walk on every page)."""
    doc = Doc()
    img = doc.image(jpeg(scan((100, 50))), (100, 50), filt=b"/DCTDecode")
    form = doc.form(b"q Q " * 499 + b"q 100 0 0 50 0 0 cm /I Do Q", {"I": img})
    content = doc.stream(b"/Filter /FlateDecode", zlib.compress(b"/F Do " * 990))
    for _ in range(200):
        doc.page_with(content, {"F": form})
    walks = []
    real = extract_mod._drawn_images
    monkeypatch.setattr(extract_mod, "_drawn_images", lambda page, d=None: walks.append(1) or real(page, d))
    llm = Vision()
    started = time.monotonic()
    r = extract(Path("Wiederholt.pdf"), doc.build(), llm=llm)
    assert time.monotonic() - started < 10
    assert (r.status, len(r.pages), len(llm.images), len(walks)) == ("ok", 200, 1, 1)


def test_memory_errors_are_too_big_to_read_without_retry(monkeypatch):
    def no_memory(*args, **kwargs):
        raise MemoryError

    monkeypatch.setattr(extract_mod, "_decode_stream", no_memory)
    r = extract(Path("Scan.pdf"), scan_pages_pdf(), llm=Vision())
    assert (r.status, r.incomplete, r.retry) == ("skipped", True, False)
    assert "Bilder zu groß zum Lesen" in r.message
    monkeypatch.setattr(extract_mod, "_docx", no_memory)
    r = extract(Path("Bericht.docx"), docx_bytes())
    assert (r.status, r.incomplete, r.retry, r.message) == ("skipped", True, False,
                                                            "Datei zu groß zum Lesen (nicht genug Speicher)")


def test_extraction_version_6_reads_incomplete_documents_again():
    # version 7 (two stages for scans, test_bildauswertung_stufen.py) is the next one after 6
    assert extract_mod.EXTRACT_VERSION >= 6
    assert extract_mod.extraction_profile(None, ocr=False) == f"v{extract_mod.EXTRACT_VERSION}:text"


@pytest.fixture()
def decodes(monkeypatch):
    seen: list[tuple] = []
    real = extract_mod._decode

    def counting(image, target):
        seen.append(image.ident)
        return real(image, target)

    monkeypatch.setattr(extract_mod, "_decode", counting)
    return seen


@pytest.mark.parametrize("mask_data", [b"kein JPEG", jpeg(scan((300, 300)))[:700]], ids=["header", "data"])
def test_a_broken_soft_mask_leaves_the_image_readable(mask_data):
    """A mask that is no image, or whose data breaks off after the header: the image is read without it."""
    doc = Doc()
    mask = doc.image(mask_data, (300, 300), filt=b"/DCTDecode")
    img = doc.stream(b"/Type /XObject /Subtype /Image /Width 300 /Height 300 /ColorSpace /DeviceGray "
                     b"/BitsPerComponent 8 /Filter /DCTDecode /SMask %d 0 R" % mask, jpeg(scan((300, 300))))
    doc.page(draw("Im1", (300, 0, 0, 300, 50, 50)), {"Im1": img})
    llm = Vision()
    r = extract(Path("x.pdf"), doc.build(), llm=llm)
    assert (r.status, r.incomplete, r.retry) == ("ok", False, False) and len(llm.images) == 1
