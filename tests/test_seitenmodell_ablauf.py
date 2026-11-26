"""The phase "Bildauswertung" with the page model: every page gets a type; the model's text replaces the page's
text on scan pages only; a page with a real text layer keeps its text whatever the model says.

The page model is a small object here that returns fixed readings (no HTTP server, no fake service): what is
tested is what the app does with a reading. The service itself: tests/test_seitenmodell_real.py."""
from __future__ import annotations

import threading

import pytest

from projektakte import seitentypen
from projektakte.ai.http import AIServiceError
from projektakte.ai.seitenmodell import PageReading, PageRejected
from projektakte.demo.writers import pdf_bytes
from projektakte.vision import VisionLive, VisionPass, VisionWorker, run_vision

from .test_bildauswertung_stufen import _docs, _setup, _stage1, _text, plain_scan, png, tesseract  # noqa: F401
from .test_seitenmodell_stufe1 import P1, P2, P3, Big, three_pages
from .test_vektorseiten import outlined_and_blank

STAGE1 = "Seite gescannt Rauchabzug Treppenraum erkannt"  # what the faked Tesseract reads on every scan page


def block(text: str, klasse: str = "Text", y: float = 0.25) -> str:
    return f"<x_0.0918><y_{y}>{text}<x_0.8906><y_{round(y + 0.03, 4)}><class_{klasse}>"


def reading(typ: str = "Text", raw: str = "", **kw) -> PageReading:
    values = {"kopf": typ, "sicherheit": 0.98, "schleife": False, "finish": "stop", "ms": 400} | kw
    return PageReading(typ=typ, raw=raw, **values)


class Stub:
    """Stands in for the client of the page model: answers the pages in the order given."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, int]] = []
        self._lock = threading.Lock()

    def read_page(self, image: bytes, mime: str) -> PageReading:
        with self._lock:
            self.calls.append((mime, len(image)))
            answer = self.answers.pop(0) if self.answers else reading("Plan")
        if isinstance(answer, Exception):
            raise answer
        return answer


def run(conn, root, stub, **kw):
    return VisionPass(conn, Big(), projects_dir=root, page_model=stub, concurrency=1, **kw).run()


def types(conn, name: str) -> dict:
    doc = _docs(conn)[name]
    return {n: dict(r) for n, r in seitentypen.known(conn, doc["id"]).items()}


# -- scan pages -------------------------------------------------------------------------------------------------
def test_scan_text_replaces_stage1_text(tmp_path, tesseract):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan()})
    _stage1(conn, root, Big())
    assert STAGE1 in _text(conn, "Scan.pdf")
    raw = block("# Prüfbericht", "Section-header", 0.1) + "\n\n" + block("Rauchabzug im Treppenraum **geprüft**.")
    stub = Stub(reading(raw=raw))
    stats = run(conn, root, stub)
    text = _text(conn, "Scan.pdf")
    assert "Rauchabzug im Treppenraum **geprüft**." in text and "Prüfbericht" in text
    assert STAGE1 not in text and "<x_" not in text and "<class_" not in text
    assert conn.execute("SELECT heading FROM chunks").fetchone()[0] == "Prüfbericht"  # Markdown headings work
    doc = _docs(conn)["Scan.pdf"]
    assert (doc["status"], doc["incomplete"], doc["vision_pending"], doc["method"]) == ("ok", 0, 0, "vision")
    assert doc["message"] is None
    row = types(conn, "Scan.pdf")[1]
    assert (row["typ"], row["raw"], row["layer"], row["source"]) == ("Text", raw, 0, "modell")
    assert len(stub.calls) == 1 and stub.calls[0][0] in ("image/jpeg", "image/png")
    assert stats["pages"] == 1 and stats["types"] == {"Text": 1}


def test_scan_leer_has_no_text(tmp_path, tesseract):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan()})
    _stage1(conn, root, Big())
    run(conn, root, Stub(reading("Leer")))
    assert _text(conn, "Scan.pdf") == ""
    doc = _docs(conn)["Scan.pdf"]
    assert (doc["status"], doc["incomplete"], doc["vision_pending"]) == ("empty", 0, 0)
    assert types(conn, "Scan.pdf")[1]["typ"] == "Leer"


def test_plan_and_foto_keep_stage1_text(tmp_path, tesseract):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan(2)})
    _stage1(conn, root, Big())
    run(conn, root, Stub(reading("Plan"), reading("Foto")))
    assert _text(conn, "Scan.pdf").count(STAGE1) == 2
    assert [r["typ"] for r in types(conn, "Scan.pdf").values()] == ["Plan", "Foto"]
    doc = _docs(conn)["Scan.pdf"]
    assert (doc["incomplete"], doc["vision_pending"], doc["method"]) == (0, 0, "text")


def test_unsicher_keeps_text_and_kopf(tmp_path, tesseract):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan()})
    _stage1(conn, root, Big())
    run(conn, root, Stub(reading("Unsicher", kopf="Plan", sicherheit=0.41)))
    assert STAGE1 in _text(conn, "Scan.pdf")
    row = types(conn, "Scan.pdf")[1]
    assert (row["typ"], row["kopf"], row["sicherheit"]) == ("Unsicher", "Plan", 0.41)


def test_text_page_without_text_keeps_stage1_text(tmp_path, tesseract):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan()})
    _stage1(conn, root, Big())
    run(conn, root, Stub(reading("Text", raw="")))
    assert STAGE1 in _text(conn, "Scan.pdf")


def test_loop_page_marked_no_second_call(tmp_path, tesseract):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan()})
    _stage1(conn, root, Big())
    raw = block("Zeile A\nZeile A\n[Schleife des Modells: 37× wiederholt, Rest entfernt]")
    stub = Stub(reading(raw=raw, schleife=True))
    run(conn, root, stub)
    assert _text(conn, "Scan.pdf") == "Zeile A\nZeile A"
    assert types(conn, "Scan.pdf")[1]["schleife"] == 1
    assert len(stub.calls) == 1
    assert _docs(conn)["Scan.pdf"]["incomplete"] == 0  # not an error


def test_finish_length_marks_partial(tmp_path, tesseract):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan()})
    _stage1(conn, root, Big())
    stub = Stub(reading(raw=block("Der Anfang der Seite.") + "\n\n<x_0.1><y_0.5>abgeschnittener Sat", finish="length"))
    run(conn, root, stub)
    doc = _docs(conn)["Scan.pdf"]
    assert "Der Anfang der Seite." in _text(conn, "Scan.pdf") and "abgeschnittener Sat" in _text(conn, "Scan.pdf")
    assert doc["incomplete"] == 1 and "abgeschnitten" in doc["message"]
    assert doc["retry_after"] is None and doc["vision_pending"] == 0
    assert len(stub.calls) == 1


def test_repeated_header_once_in_document(tmp_path, tesseract):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan(4)})
    _stage1(conn, root, Big())
    answers = [reading(raw="\n\n".join([block("Musterbüro GmbH · Beispielstadt", "Page-header", 0.0),
                                        block(f"Inhalt der Seite {n} zum Rauchabzug."),
                                        block(f"Seite {n} von 4", "Page-footer", 0.95)])) for n in (1, 2, 3, 4)]
    run(conn, root, Stub(*answers))
    text = _text(conn, "Scan.pdf")
    assert text.count("Musterbüro GmbH") == 1 and text.count("von 4") == 1
    assert all(f"Inhalt der Seite {n}" in text for n in (1, 2, 3, 4))
    assert "Page-footer" in types(conn, "Scan.pdf")[3]["raw"]  # the raw text stays whole


def test_image_file_is_typed(tmp_path, tesseract):
    conn, root = _setup(tmp_path, {"Skizze.png": png()})
    _stage1(conn, root, Big())
    stub = Stub(reading("Foto"))
    run(conn, root, stub)
    assert types(conn, "Skizze.png")[1]["typ"] == "Foto"
    assert stub.calls[0][0] == "image/png"
    assert STAGE1 in _text(conn, "Skizze.png")


# -- pages with a real text layer ------------------------------------------------------------------------------
def test_layer_page_keeps_layer_text(tmp_path):
    conn, root = _setup(tmp_path, {"Konzept.pdf": three_pages()})
    _stage1(conn, root, Big())
    before = _text(conn, "Konzept.pdf")
    raw = block("Brandschutzkonzcpt für die Grundschulc")  # the model misreads: the text layer is exact
    run(conn, root, Stub(reading(raw=raw), reading("Plan"), reading(raw=block("Etwas anderes."))))
    assert _text(conn, "Konzept.pdf") == before
    assert "Brandschutzkonzcpt" not in before and P2 in before
    rows = types(conn, "Konzept.pdf")
    assert [rows[n]["typ"] for n in (1, 2, 3)] == ["Text", "Plan", "Text"]
    assert all(r["layer"] == 1 for r in rows.values()) and rows[1]["raw"] == raw
    doc = _docs(conn)["Konzept.pdf"]
    assert (doc["status"], doc["incomplete"], doc["vision_pending"], doc["method"]) == ("ok", 0, 0, "text")
    assert doc["message"] is None


def test_layer_page_typed_leer_keeps_text(tmp_path):
    conn, root = _setup(tmp_path, {"Konzept.pdf": three_pages()})
    _stage1(conn, root, Big())
    run(conn, root, Stub(reading("Leer"), reading("Leer"), reading("Leer")))
    text = _text(conn, "Konzept.pdf")
    assert P1 in text and P2 in text and P3 in text


def test_rejected_layer_page_leaves_the_document_complete(tmp_path):
    conn, root = _setup(tmp_path, {"Konzept.pdf": three_pages()})
    _stage1(conn, root, Big())
    run(conn, root, Stub(reading(), PageRejected("HTTP 400: kaputtes Bild"), reading()))
    doc = _docs(conn)["Konzept.pdf"]
    assert (doc["status"], doc["incomplete"], doc["vision_pending"]) == ("ok", 0, 0)
    assert doc["message"] is None
    assert sorted(types(conn, "Konzept.pdf")) == [1, 3]
    assert P2 in _text(conn, "Konzept.pdf")


def test_failed_layer_page_waits_but_the_document_is_complete(tmp_path):
    conn, root = _setup(tmp_path, {"Konzept.pdf": three_pages()})
    _stage1(conn, root, Big())
    run(conn, root, Stub(reading(), AIServiceError("HTTP 504 von …: Zeitlimit"), reading()))
    doc = _docs(conn)["Konzept.pdf"]
    assert (doc["incomplete"], doc["vision_pending"]) == (0, 1)
    assert doc["message"] == "Seitentyp ausstehend: 1 Seite(n)"
    state = conn.execute("SELECT state, retry_after FROM page_texts WHERE page = 2").fetchone()
    assert state["state"] == "failed" and state["retry_after"]


# -- failures ---------------------------------------------------------------------------------------------------
def test_rejected_scan_page_is_final(tmp_path, tesseract):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan()})
    _stage1(conn, root, Big())
    stub = Stub(PageRejected("HTTP 400: kaputtes Bild"))
    run(conn, root, stub)
    doc = _docs(conn)["Scan.pdf"]
    assert (doc["vision_pending"], doc["incomplete"]) == (0, 1)
    assert "nicht ausgewertet" in doc["message"]
    assert STAGE1 in _text(conn, "Scan.pdf") and types(conn, "Scan.pdf") == {}
    run(conn, root, stub)
    assert len(stub.calls) == 1  # not asked again


def test_service_error_retries_later(tmp_path, tesseract):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan()})
    _stage1(conn, root, Big())
    run(conn, root, Stub(AIServiceError("HTTP 500 von …: GPU-Fehler")))
    row = conn.execute("SELECT state, retry_after, note FROM page_texts").fetchone()
    assert row["state"] == "failed" and row["retry_after"] and "GPU-Fehler" in row["note"]
    doc = _docs(conn)["Scan.pdf"]
    assert (doc["vision_pending"], doc["incomplete"]) == (1, 1)
    assert STAGE1 in _text(conn, "Scan.pdf")


def test_changed_file_drops_answer(tmp_path, tesseract):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan()})
    _stage1(conn, root, Big())

    class Changing(Stub):
        def read_page(self, image, mime):
            conn.execute("UPDATE documents SET sha256 = 'neu'")  # the documents pass read a changed file meanwhile
            conn.commit()
            return super().read_page(image, mime)

    stats = run(conn, root, Changing(reading(raw=block("Alter Inhalt."))))
    assert stats["discarded"] == 1
    assert conn.execute("SELECT COUNT(*) FROM page_types").fetchone()[0] == 0
    assert "Alter Inhalt" not in _text(conn, "Scan.pdf")


def test_blank_render_is_leer(tmp_path, tesseract, monkeypatch):
    monkeypatch.delenv("VISION_SKIP_CAD", raising=False)
    conn, root = _setup(tmp_path, {"Auszug.pdf": outlined_and_blank()})
    _stage1(conn, root, Big())
    doc = _docs(conn)["Auszug.pdf"]
    # as if stage 1 had not seen that page 2 is blank: it waits for the model and is found blank when rendered
    conn.execute("DELETE FROM page_types WHERE doc_id = ? AND page = 2", (doc["id"],))
    conn.execute("INSERT OR REPLACE INTO page_texts(doc_id, page, text, state) VALUES(?, 2, '', 'pending')", (doc["id"],))
    conn.execute("UPDATE documents SET vision_pending = 2 WHERE id = ?", (doc["id"],))
    conn.commit()
    stub = Stub(reading(raw=block("Stellplätze und Garagen.")))
    run(conn, root, stub)
    rows = types(conn, "Auszug.pdf")
    assert (rows[1]["typ"], rows[1]["source"]) == ("Text", "modell")
    assert (rows[2]["typ"], rows[2]["source"]) == ("Leer", "render")
    assert len(stub.calls) == 1


def test_page_without_image_and_with_text_layer_is_sent_despite_skip_cad(tmp_path, monkeypatch):
    monkeypatch.setenv("VISION_SKIP_CAD", "1")
    conn, root = _setup(tmp_path, {"Konzept.pdf": three_pages()})
    _stage1(conn, root, Big())
    stub = Stub(reading(), reading(), reading())
    run(conn, root, stub)
    assert len(stub.calls) == 3 and sorted(types(conn, "Konzept.pdf")) == [1, 2, 3]


# -- around it --------------------------------------------------------------------------------------------------
def test_big_model_gets_no_image_and_no_loop_check(tmp_path, tesseract):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan(), "Konzept.pdf": three_pages()})
    _stage1(conn, root, Big())
    conn.execute("UPDATE documents SET loop_check = 1")
    conn.commit()
    stats = run(conn, root, Stub())  # Big.describe_image raises if it is called
    assert stats["pages"] == 4 and stats["checked"] == 0 and stats["loops_found"] == 0
    assert conn.execute("SELECT COUNT(*) FROM documents WHERE loop_check = 1").fetchone()[0] == 2


def test_page_texts_dropped_page_types_stay(tmp_path, tesseract):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan(2)})
    _stage1(conn, root, Big())
    run(conn, root, Stub(reading(raw=block("Eins.")), reading(raw=block("Zwei."))))
    assert conn.execute("SELECT COUNT(*) FROM page_texts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM page_types").fetchone()[0] == 2


def test_read_again_takes_the_texts_from_the_types(tmp_path, tesseract):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan(2)})
    _stage1(conn, root, Big())
    run(conn, root, Stub(reading(raw=block("Rauchabzug im Treppenraum geprüft.")), reading("Leer")))
    conn.execute("UPDATE documents SET incomplete = 1, retry_after = '2000-01-01 00:00:00'")
    conn.commit()
    _stage1(conn, root, Big())  # the same file read again: the model is not needed
    doc = _docs(conn)["Scan.pdf"]
    assert doc["vision_pending"] == 0 and doc["method"] == "vision"
    assert _text(conn, "Scan.pdf") == "Rauchabzug im Treppenraum geprüft."


def test_live_status_counts_the_types(tmp_path, tesseract):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan(3)})
    _stage1(conn, root, Big())
    live = VisionLive()
    run(conn, root, Stub(reading(raw=block("Text.")), reading("Plan"), reading("Plan")), live=live)
    assert live.snapshot()["types"] == {"Text": 1, "Plan": 2}


def test_run_vision_and_worker_want_the_page_model(tmp_path, tesseract):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan()})
    _stage1(conn, root, Big())
    settings = type("S", (), {"projects_dir": root, "vision_concurrency": 1, "vision_pass_seconds": 60.0})()
    assert run_vision(conn, Big(), settings)["note"].startswith("ausgeschaltet")
    stats = run_vision(conn, Big(), settings, page_model=Stub(reading("Plan")))
    assert stats["pages"] == 1
    assert VisionWorker.wanted(Big()) is False
    assert VisionWorker.wanted(Big(), Stub()) is True
