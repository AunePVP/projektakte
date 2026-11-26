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

from .pdfbau import Pdf, line, scan_pdf
from .seitenhilfe import OCR as STAGE1  # the OCR layer of the test scans: their text until the model read them
from .seitenhilfe import docs as _docs
from .seitenhilfe import setup_index as _setup
from .seitenhilfe import stage1 as _stage1
from .seitenhilfe import text_of as _text
from .test_seitenmodell_stufe1 import P1, P2, P3, Big, png, three_pages


def plain_scan(pages: int = 1) -> bytes:
    """Scanned pages with an OCR layer (STAGE1 on every page)."""
    return scan_pdf(pages, layer=STAGE1)


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
def test_scan_text_replaces_stage1_text(tmp_path):
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


def test_scan_leer_has_no_text(tmp_path):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan()})
    _stage1(conn, root, Big())
    run(conn, root, Stub(reading("Leer")))
    assert _text(conn, "Scan.pdf") == ""
    doc = _docs(conn)["Scan.pdf"]
    assert (doc["status"], doc["incomplete"], doc["vision_pending"]) == ("empty", 0, 0)
    assert types(conn, "Scan.pdf")[1]["typ"] == "Leer"


def test_plan_and_foto_keep_stage1_text(tmp_path):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan(2)})
    _stage1(conn, root, Big())
    run(conn, root, Stub(reading("Plan"), reading("Foto")))
    assert _text(conn, "Scan.pdf").count(STAGE1) == 2
    assert [r["typ"] for r in types(conn, "Scan.pdf").values()] == ["Plan", "Foto"]
    doc = _docs(conn)["Scan.pdf"]
    assert (doc["incomplete"], doc["vision_pending"], doc["method"]) == (0, 0, "text")


def test_unsicher_keeps_text_and_kopf(tmp_path):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan()})
    _stage1(conn, root, Big())
    run(conn, root, Stub(reading("Unsicher", kopf="Plan", sicherheit=0.41)))
    assert STAGE1 in _text(conn, "Scan.pdf")
    row = types(conn, "Scan.pdf")[1]
    assert (row["typ"], row["kopf"], row["sicherheit"]) == ("Unsicher", "Plan", 0.41)


def test_text_page_without_text_keeps_stage1_text(tmp_path):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan()})
    _stage1(conn, root, Big())
    run(conn, root, Stub(reading("Text", raw="")))
    assert STAGE1 in _text(conn, "Scan.pdf")


def test_loop_page_marked_no_second_call(tmp_path):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan()})
    _stage1(conn, root, Big())
    raw = block("Zeile A\nZeile A\n[Schleife des Modells: 37× wiederholt, Rest entfernt]")
    stub = Stub(reading(raw=raw, schleife=True))
    run(conn, root, stub)
    assert _text(conn, "Scan.pdf") == "Zeile A\nZeile A"
    assert types(conn, "Scan.pdf")[1]["schleife"] == 1
    assert len(stub.calls) == 1
    assert _docs(conn)["Scan.pdf"]["incomplete"] == 0  # not an error


def test_finish_length_marks_partial(tmp_path):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan()})
    _stage1(conn, root, Big())
    stub = Stub(reading(raw=block("Der Anfang der Seite.") + "\n\n<x_0.1><y_0.5>abgeschnittener Sat", finish="length"))
    run(conn, root, stub)
    doc = _docs(conn)["Scan.pdf"]
    assert "Der Anfang der Seite." in _text(conn, "Scan.pdf") and "abgeschnittener Sat" in _text(conn, "Scan.pdf")
    assert doc["incomplete"] == 1 and "abgeschnitten" in doc["message"]
    assert doc["retry_after"] is None and doc["vision_pending"] == 0
    assert len(stub.calls) == 1


def test_repeated_header_once_in_document(tmp_path):
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


def test_image_file_is_typed(tmp_path):
    conn, root = _setup(tmp_path, {"Skizze.png": png(), "Notiz.png": png()})
    _stage1(conn, root, Big())
    stub = Stub(reading("Foto"), reading(raw=block("Handskizze Treppenraum T2.")))
    run(conn, root, stub)
    found = sorted((types(conn, name)[1]["typ"], _text(conn, name)) for name in ("Skizze.png", "Notiz.png"))
    assert found == [("Foto", ""), ("Text", "Handskizze Treppenraum T2.")]
    assert stub.calls[0][0] == "image/png"


# -- pages with a real text layer ------------------------------------------------------------------------------
def test_layer_page_keeps_layer_text(tmp_path):
    conn, root = _setup(tmp_path, {"Konzept.pdf": three_pages()})
    _stage1(conn, root, Big())
    before = _text(conn, "Konzept.pdf")
    raw = block("Brandschutzkonzcpt für die Grundschulc")  # the model misreads: the text layer is exact
    run(conn, root, Stub(reading(raw=raw), reading("Plan"), reading(raw=block(P3.replace("Fenster", "Fcnster")))))
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
def test_rejected_scan_page_is_final(tmp_path):
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


def test_service_error_retries_later(tmp_path):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan()})
    _stage1(conn, root, Big())
    run(conn, root, Stub(AIServiceError("HTTP 500 von …: GPU-Fehler")))
    row = conn.execute("SELECT state, retry_after, note FROM page_texts").fetchone()
    assert row["state"] == "failed" and row["retry_after"] and "GPU-Fehler" in row["note"]
    doc = _docs(conn)["Scan.pdf"]
    assert (doc["vision_pending"], doc["incomplete"]) == (1, 1)
    assert STAGE1 in _text(conn, "Scan.pdf")


def test_changed_file_drops_answer(tmp_path):
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


def test_blank_render_is_leer(tmp_path):
    """A page without anything on it is found blank when it is rendered: "Leer" without asking the model."""
    conn, root = _setup(tmp_path, {"Auszug.pdf": Pdf().page(line(60, 700, "Stellplätze und Garagen im Hof.")).page().build()})
    _stage1(conn, root, Big())
    assert _docs(conn)["Auszug.pdf"]["vision_pending"] == 2
    stub = Stub(reading(raw=block("Stellplätze und Garagen im Hof.")))
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
def test_big_model_gets_no_image_and_no_loop_check(tmp_path):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan(), "Konzept.pdf": three_pages()})
    _stage1(conn, root, Big())
    conn.execute("UPDATE documents SET loop_check = 1")
    conn.commit()
    stats = run(conn, root, Stub())  # Big.describe_image raises if it is called
    assert stats["pages"] == 4 and stats["checked"] == 0 and stats["loops_found"] == 0
    assert conn.execute("SELECT COUNT(*) FROM documents WHERE loop_check = 1").fetchone()[0] == 2


def test_page_texts_dropped_page_types_stay(tmp_path):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan(2)})
    _stage1(conn, root, Big())
    run(conn, root, Stub(reading(raw=block("Eins.")), reading(raw=block("Zwei."))))
    assert conn.execute("SELECT COUNT(*) FROM page_texts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM page_types").fetchone()[0] == 2


def test_read_again_takes_the_texts_from_the_types(tmp_path):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan(2)})
    _stage1(conn, root, Big())
    run(conn, root, Stub(reading(raw=block("Rauchabzug im Treppenraum geprüft.")), reading("Leer")))
    conn.execute("UPDATE documents SET incomplete = 1, retry_after = '2000-01-01 00:00:00'")
    conn.commit()
    _stage1(conn, root, Big())  # the same file read again: the model is not needed
    doc = _docs(conn)["Scan.pdf"]
    assert doc["vision_pending"] == 0 and doc["method"] == "vision"
    assert _text(conn, "Scan.pdf") == "Rauchabzug im Treppenraum geprüft."


def test_live_status_counts_the_types(tmp_path):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan(3)})
    _stage1(conn, root, Big())
    live = VisionLive()
    run(conn, root, Stub(reading(raw=block("Text.")), reading("Plan"), reading("Plan")), live=live)
    assert live.snapshot()["types"] == {"Text": 1, "Plan": 2}


def test_run_vision_and_worker_want_the_page_model(tmp_path):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan()})
    _stage1(conn, root, Big())
    settings = type("S", (), {"projects_dir": root, "vision_concurrency": 1, "vision_pass_seconds": 60.0})()
    assert run_vision(conn, Big(), settings)["note"].startswith("ausgeschaltet")
    stats = run_vision(conn, Big(), settings, page_model=Stub(reading("Plan")))
    assert stats["pages"] == 1
    assert VisionWorker.wanted(Big()) is False
    assert VisionWorker.wanted(Big(), Stub()) is True


# -- found by the review of the whole branch --------------------------------------------------------------------
def invisible_text_pdf() -> bytes:
    """A page with a real text layer that a viewer shows blank (text render mode 3: neither filled nor stroked)."""
    return Pdf().page(line(50, 780, "Rauchabzug im Treppenraum laut Pruefbericht vorhanden", mode=3)).build()


def test_layer_page_rendered_blank_keeps_its_text(tmp_path):
    conn, root = _setup(tmp_path, {"Bericht.pdf": invisible_text_pdf()})
    _stage1(conn, root, Big())
    assert "Rauchabzug im Treppenraum" in _text(conn, "Bericht.pdf")
    stub = Stub()
    run(conn, root, stub)
    assert stub.calls == []  # rendered blank: typed without the model
    assert "Rauchabzug im Treppenraum" in _text(conn, "Bericht.pdf")
    doc = _docs(conn)["Bericht.pdf"]
    assert (doc["status"], doc["incomplete"], doc["vision_pending"]) == ("ok", 0, 0)
    row = types(conn, "Bericht.pdf")[1]
    assert (row["typ"], row["source"], row["layer"]) == ("Leer", "render", 1)


def test_scan_page_rendered_blank_keeps_its_stage1_text(tmp_path):
    """Only the model's "Leer" empties a scan page – a page the app could not draw keeps what stage 1 read."""
    from projektakte.extract import PageImages

    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan()})
    _stage1(conn, root, Big())
    stub = Stub()
    VisionPass(conn, Big(), projects_dir=root, page_model=stub, concurrency=1,
               prepare=lambda data, pages, name, **kw: [PageImages(n, problem="Seite leer") for n in pages]).run()
    assert stub.calls == [] and STAGE1 in _text(conn, "Scan.pdf")
    assert types(conn, "Scan.pdf")[1]["source"] == "render"


def test_sections_stay_when_no_text_changed(tmp_path):
    """Typing the pages of a digital PDF must not write its sections anew: their vectors would be made again."""
    conn, root = _setup(tmp_path, {"Konzept.pdf": three_pages()})
    _stage1(conn, root, Big())
    conn.execute("UPDATE chunks SET embedding = x'00112233'")  # as if the vectors were made already
    conn.commit()
    before = conn.execute("SELECT id, page, text, embedding FROM chunks ORDER BY id").fetchall()
    run(conn, root, Stub(reading(), reading("Plan"), reading()))
    after = conn.execute("SELECT id, page, text, embedding FROM chunks ORDER BY id").fetchall()
    assert [tuple(r) for r in after] == [tuple(r) for r in before] and before
    assert _docs(conn)["Konzept.pdf"]["vision_pending"] == 0


def test_sections_are_written_anew_when_a_scan_page_got_text(tmp_path):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan()})
    _stage1(conn, root, Big())
    conn.execute("UPDATE chunks SET embedding = x'00112233'")
    conn.commit()
    run(conn, root, Stub(reading(raw=block("Neuer Text vom Modell."))))
    assert [r[0] for r in conn.execute("SELECT embedding FROM chunks")] == [None]  # a vector for the new text
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'Modell'").fetchone()[0] == 1


def test_types_go_when_the_file_changes_without_the_page_model(tmp_path):
    conn, root = _setup(tmp_path, {"Konzept.pdf": three_pages()})
    _stage1(conn, root, Big())
    run(conn, root, Stub(reading(), reading("Plan"), reading()))
    assert len(types(conn, "Konzept.pdf")) == 3
    (root / "BS-2024-0001 Halle" / "Konzept.pdf").write_bytes(pdf_bytes([P1 + " Neue Fassung.", "\f", P2]))
    _stage1(conn, root, None)  # VISION_URL removed meanwhile
    assert types(conn, "Konzept.pdf") == {}


# -- status page: the page model's own numbers ------------------------------------------------------------------
def test_page_model_has_its_own_page_times(tmp_path):
    """The pages Qwen read and its 50 s a page must not count for the page model (status page: pages read,
    rough time left)."""
    from projektakte import zeiten

    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan(2)})
    zeiten.add(conn, zeiten.PAGE_KEY, 50.0)
    conn.commit()
    _stage1(conn, root, Big())
    run(conn, root, Stub(reading("Plan"), reading("Plan")))
    assert zeiten.read(conn, zeiten.PAGE_KEY).samples == 1
    assert zeiten.read(conn, zeiten.SEITENMODELL_KEY).samples == 2
    assert zeiten.page_key(True) == zeiten.SEITENMODELL_KEY and zeiten.page_key(False) == zeiten.PAGE_KEY


def test_status_shows_no_recheck_with_the_page_model(tmp_path):
    from fastapi.testclient import TestClient

    from projektakte import db
    from projektakte.config import load_settings
    from projektakte.main import create_app

    def counts(extra: dict) -> dict:
        env = {"DATA_DIR": str(tmp_path / "data"), "APP_ENV": "test", "AUTH_DISABLED": "1",
               "LLM_URL": "http://127.0.0.1:9/v1", "EMBED_URL": "http://127.0.0.1:9",
               "RERANK_URL": "http://127.0.0.1:9/rerank"} | extra
        with TestClient(create_app(load_settings(env), background=False)) as client:
            return client.get("/api/status").json()["counts"]

    s = load_settings({"DATA_DIR": str(tmp_path / "data")})
    db.init_db(s.db_path)
    with db.session(s.db_path) as conn:  # a complete PDF read by an older version: the old re-check wants it
        conn.execute("INSERT INTO documents(path, rel_path, filename, ext, sha256, status, incomplete, method, "
                     "extract_profile) VALUES('/p/a.pdf', 'a.pdf', 'a.pdf', '.pdf', 'x', 'ok', 0, 'text', 'v7:text')")
    assert counts({})["vision_recheck"] == 1
    assert counts({"VISION_URL": "http://127.0.0.1:9/v1"})["vision_recheck"] == 0


# -- pages with a real text layer: the layer's words in the model's blocks --------------------------------------
HEAD = "Musterbüro GmbH · Beispielstadt · Telefon 01234 5678"


def report(pages: int = 4) -> bytes:
    """A digital report: letterhead and page number on every page, a heading, a paragraph of two lines."""
    pdf = Pdf()
    for n in range(1, pages + 1):
        pdf.page(line(60, 800, HEAD, size=8),
                 line(60, 740, f"Abschnitt {n} Rettungswege", size=14, bold=True),
                 line(60, 700, f"{n}. Der zweite Rettungsweg aus dem Geschoss {n} führt über die Außen-"),
                 line(60, 686, "treppe an der Nordseite; die Tür hat die Feuerwiderstandsklasse T30."),
                 line(60, 40, f"Seite {n} von {pages}", size=8))
    return pdf.build()


def as_the_model_saw(data: bytes, number: int, *, misread: bool = True) -> str:
    """The raw text the page model would give for a page of :func:`report`: one block per part of the page, drawn
    around the lines the reader found, the paragraph's two lines as one block – and "T30" misread as "T3O"."""
    from projektakte import pdfseiten

    lines = pdfseiten.read(data).pages[number - 1].segments

    def around(*segs) -> tuple:
        return (round(min(s.x0 for s in segs) - 0.01, 4), round(min(s.y0 for s in segs) - 0.004, 4),
                round(max(s.x1 for s in segs) + 0.01, 4), round(max(s.y1 for s in segs) + 0.004, 4))

    def mblock(text, klasse, box) -> str:
        return f"<x_{box[0]}><y_{box[1]}>{text}<x_{box[2]}><y_{box[3]}><class_{klasse}>"

    head, title, first, second, foot = lines
    body = f"{first.text[:-1]}{second.text}"
    return "\n\n".join([mblock(head.text, "Page-header", around(head)),
                        mblock("## " + title.text, "Section-header", around(title)),
                        mblock(body.replace("T30", "T3O") if misread else body, "List-item", around(first, second)),
                        mblock(foot.text, "Page-footer", around(foot))])


def test_digital_pdf_gets_structure_from_the_model_and_words_from_the_layer(tmp_path):
    data = report()
    conn, root = _setup(tmp_path, {"Konzept.pdf": data})
    _stage1(conn, root, Big())
    run(conn, root, Stub(*[reading(raw=as_the_model_saw(data, n)) for n in (1, 2, 3, 4)]))
    text = _text(conn, "Konzept.pdf")
    # the layer's exact words (T30, not the model's T3O), the two lines one paragraph, the divided word whole
    assert "über die Außentreppe an der Nordseite; die Tür hat die Feuerwiderstandsklasse T30." in text
    assert "T3O" not in text
    # letterhead and page number of every page once
    assert text.count("Musterbüro GmbH") == 1 and text.count("von 4") == 1
    # the model's headings are the sections' headings
    headings = [r[0] for r in conn.execute("SELECT DISTINCT heading FROM chunks ORDER BY ord")]
    assert headings == [None] + [f"Abschnitt {n} Rettungswege" for n in (1, 2, 3, 4)]  # None: the letterhead
    rows = types(conn, "Konzept.pdf")
    assert all(r["quelle"] == "ebene" and r["deckung"] >= 0.8 and r["layer"] == 1 for r in rows.values())
    doc = _docs(conn)["Konzept.pdf"]
    assert (doc["status"], doc["incomplete"], doc["vision_pending"], doc["method"]) == ("ok", 0, 0, "text")


def test_text_layer_that_is_not_what_the_page_shows_gives_way_to_the_model(tmp_path):
    """Hidden text under a page that shows something else: the model read the page, the layer has other words."""
    data = Pdf().page(line(60, 700, "Verborgener Text einer alten Fassung ohne jeden Bezug zum Inhalt.", mode=3),
                      line(60, 600, "X")).build()
    conn, root = _setup(tmp_path, {"Bescheid.pdf": data})
    _stage1(conn, root, Big())
    seen = block("Die Baugenehmigung wird mit folgenden Auflagen zum Brandschutz und zur Rauchableitung erteilt.")
    run(conn, root, Stub(reading(raw=seen)))
    assert "Auflagen zum Brandschutz" in _text(conn, "Bescheid.pdf")
    assert "Verborgener Text" not in _text(conn, "Bescheid.pdf")
    row = types(conn, "Bescheid.pdf")[1]
    assert row["quelle"] == "modell" and row["deckung"] < 0.5


def test_read_again_composes_without_the_model(tmp_path):
    """Everything after the model's answer can be made anew from what is stored: a later version of the reader or
    of these rules reads the file again, the model is not asked."""
    data = report()
    conn, root = _setup(tmp_path, {"Konzept.pdf": data})
    _stage1(conn, root, Big())
    run(conn, root, Stub(*[reading(raw=as_the_model_saw(data, n)) for n in (1, 2, 3, 4)]))
    first = _text(conn, "Konzept.pdf")
    conn.execute("UPDATE documents SET extract_profile = 'v8:seitenmodell:nemotron-kmw'")  # read by the old reader
    conn.execute("DELETE FROM chunks")
    conn.commit()
    _stage1(conn, root, Big())
    doc = _docs(conn)["Konzept.pdf"]
    assert doc["vision_pending"] == 0 and _text(conn, "Konzept.pdf") == first


def test_read_again_takes_the_new_readers_word_on_text_layers(tmp_path):
    """Types stored by an earlier reader say "scan" for a page the reader now knows to have a real text layer (or
    the other way round): what the reader says now counts, and the page's text follows."""
    data = report(3)
    conn, root = _setup(tmp_path, {"Konzept.pdf": data})
    _stage1(conn, root, Big())
    run(conn, root, Stub(*[reading(raw=as_the_model_saw(data, n)) for n in (1, 2, 3)]))
    conn.execute("UPDATE page_types SET layer = 0")  # as stored by a reader that took the pages for scans
    conn.execute("UPDATE documents SET extract_profile = 'v8:seitenmodell:nemotron-kmw'")
    conn.commit()
    _stage1(conn, root, Big())
    rows = types(conn, "Konzept.pdf")
    assert all(r["layer"] == 1 and r["quelle"] == "ebene" for r in rows.values())
    assert "T30" in _text(conn, "Konzept.pdf") and "T3O" not in _text(conn, "Konzept.pdf")
