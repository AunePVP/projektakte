"""Final check (round 4): question budget in every branch, format detection within one timeout,
refusals whose cited clause only names what the sources lack, images drawn several times in a scan."""
import io
import json
import re
import sqlite3
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from projektakte import ask as ask_mod
from projektakte import extract as extract_mod
from projektakte.ai.embeddings import EmbeddingClient
from projektakte.ai.http import AIServiceError, seconds_text, timeout_error
from projektakte.ai.rerank import RerankClient
from projektakte.ai.services import AIServices
from projektakte.ask import BUDGET_TEXT, NO_AI_TEXT, ask, is_not_found
from projektakte.config import ASK_DEADLINE_MIN, load_settings
from projektakte.demo import generate
from projektakte.extract import extract
from projektakte.indexer import run_index
from projektakte.startup import deadline_warnings

from .conftest import fake_settings


@pytest.fixture()
def demo(tmp_path, fake_ai):
    generate(tmp_path / "demo")
    s = fake_settings(fake_ai, tmp_path, DEMO_MODE=1)
    ai = AIServices.from_settings(s)
    run_index(s, ai)
    conn = sqlite3.connect(s.db_path)
    conn.row_factory = sqlite3.Row
    yield s, ai, conn
    conn.close()


# -- overview question without time for the overview search -------------------------------------------
def test_summary_without_time_for_the_overview_still_asks_the_model(demo, monkeypatch):
    """Before: the overview search was skipped and the question ended as "nichts gefunden" although
    the budget kept for the language model was untouched. Now the overview search runs within the
    remaining budget (falling back to the full text) and the model answers."""
    s, ai, conn = demo
    real = ai.rerank.rerank
    chats = []
    real_chat = ai.llm.chat

    def slow(*a, **kw):
        time.sleep(1.5)
        return real(*a, **kw)

    def chat(*a, **kw):
        chats.append(kw.get("timeout"))
        return real_chat(*a, **kw)

    question = "Worum geht es in dem Projekt?"
    monkeypatch.setattr(ai.rerank, "rerank", slow)
    monkeypatch.setattr(ai.llm, "chat", chat)
    started = time.monotonic()
    # budget 12 s: 6 s reserved for the model, the searches share the other 6 s
    r = ask(conn, ai, str(s.db_path), question, project="BS-2025-0019", deadline_s=12)
    elapsed = time.monotonic() - started
    assert chats and r.status == "answered" and r.sources
    assert "Kinder" in r.answer  # the project description from the overview search
    assert elapsed < 7


def test_exhausted_budget_says_so_instead_of_service_down(tmp_path, fresh_fake_ai):
    generate(tmp_path / "demo")
    s = fake_settings(fresh_fake_ai, tmp_path, DEMO_MODE=1)
    ai = AIServices.from_settings(s)
    run_index(s, ai)
    conn = sqlite3.connect(s.db_path)
    conn.row_factory = sqlite3.Row
    fresh_fake_ai.options["delay"] = 3.0
    try:
        r = ask(conn, ai, str(s.db_path), "Wie hoch ist der Löschwasserbedarf der Industriehalle?",
                project="BS-2023-0777", deadline_s=2.0)
    finally:
        fresh_fake_ai.options["delay"] = 0
    assert r.status == "llm_unavailable" and r.answer == BUDGET_TEXT and r.context
    assert len(r.notes) == len(set(r.notes))
    assert any("ASK_DEADLINE" in n for n in r.notes)
    assert not any(re.search(r"\d\.\d", n) for n in r.notes)  # German decimal comma, rounded


def test_ask_without_ai_services_returns_the_passages(demo):
    s, _ai, conn = demo
    for budget in (0, 180):
        r = ask(conn, None, str(s.db_path), "Wie hoch ist der Löschwasserbedarf der Industriehalle?",
                project="BS-2023-0777", deadline_s=budget)
        assert r.status == "llm_unavailable" and r.answer == NO_AI_TEXT and r.context


def test_seconds_in_messages_are_rounded_with_a_german_comma():
    assert seconds_text(60) == "60" and seconds_text(37.46) == "37,5" and seconds_text(3.99416) == "4"
    assert "nach 4 s" in str(timeout_error(3.99416, "http://x"))
    assert ask_mod.llm_error_note(timeout_error(37.46, "http://x")) == (
        "Das Sprachmodell hat nicht rechtzeitig geantwortet (Zeitüberschreitung nach 37,5 s).")


# -- ASK_DEADLINE validation ------------------------------------------------------------------------------
def test_too_small_question_budget_is_raised_with_a_warning():
    s = load_settings({"ASK_DEADLINE": "1"})
    assert s.ask_deadline == ASK_DEADLINE_MIN == 10
    assert any("ASK_DEADLINE=1 ist zu knapp" in w for w in deadline_warnings(s))
    assert load_settings({"ASK_DEADLINE": "0"}).ask_deadline == 0  # off stays off
    assert load_settings({"ASK_DEADLINE": "-5"}).ask_deadline == 0
    assert deadline_warnings(load_settings({})) == []  # defaults fit together


def test_llm_timeout_above_the_budget_is_warned_about():
    s = load_settings({"LLM_TIMEOUT": "300"})
    (warning,) = deadline_warnings(s)
    assert "LLM_TIMEOUT=300 s" in warning and "ASK_DEADLINE=180 s" in warning
    assert deadline_warnings(load_settings({"LLM_TIMEOUT": "300", "ASK_DEADLINE": "0"})) == []


def test_evaluation_measures_with_the_question_budget(monkeypatch, demo):
    from projektakte import evaluate

    s, ai, conn = demo
    seen = {}
    real = evaluate.ask

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(evaluate, "ask", spy)
    q = {"id": "x", "question": "Wie hoch ist der Löschwasserbedarf der Industriehalle?", "project": "BS-2023-0777",
         "sources": [], "answer_contains": ["192"]}
    rec = evaluate.evaluate_question(conn, ai, str(s.db_path), q, s)
    assert seen["deadline_s"] == s.ask_deadline == 180 and rec.budget_hit is False


def test_demo_with_fake_services_reads_images_by_default():
    assert load_settings({"DEMO_MODE": "1", "AI_MODE": "fake"}).llm_vision is True
    assert load_settings({"DEMO_MODE": "1", "AI_MODE": "fake", "LLM_VISION": "0"}).llm_vision is False
    assert load_settings({"DEMO_MODE": "1"}).llm_vision is False  # real services: only on request
    assert load_settings({"AI_MODE": "fake"}).llm_vision is False


# -- refusals whose cited clause only names what the sources lack ----------------------------------------
@pytest.mark.parametrize("answer", [
    "Die Quellen enthalten keine Angabe zum Hersteller; weder [1] noch [2] nennen ihn.",
    "Die Quellen enthalten keine Angabe zum Hersteller – auch [3] erwähnt ihn nicht.",
    "Die Quellen enthalten keine Angabe zum Hersteller. Weder [1] noch [2] nennen ihn.",
    "Die Quellen nennen keinen Hersteller. Auch [2] enthält dazu keine Angaben.",
    "Den Unterlagen ist kein Hersteller zu entnehmen; in [1] und [2] ist er nicht genannt.",
])
def test_refusal_with_a_cited_negative_clause_stays_a_refusal(answer):
    assert is_not_found(answer, 3)


@pytest.mark.parametrize("answer", [
    "Die Quellen nennen kein genaues Datum; laut Protokoll ist die Abnahme für März vorgesehen [2].",
    "Die Quellen nennen keinen Termin. Laut [2] ist die Abnahme für März vorgesehen.",
    "Die Quellen nennen keinen Hersteller; weder [1] noch [2] nennen ihn, laut [3] wird er aber im August "
    "festgelegt.",
    "Es liegen keine Mängel mehr vor [1].",
    "Der Prüfbericht enthält keine Hinweise auf Mängel [2].",
])
def test_partial_answers_and_cited_negative_facts_stay_answers(answer):
    assert not is_not_found(answer, 3)


def test_refusal_with_cited_negative_clause_is_not_shown_as_answer(demo, monkeypatch):
    s, ai, conn = demo

    class Chat:
        text = "Die Quellen enthalten keine Angabe zum Hersteller; weder [1] noch [2] nennen ihn."
        usage, latency_ms, finish_reason, truncated = {}, 1.0, "stop", False

    calls = []
    monkeypatch.setattr(ai.llm, "chat", lambda *a, **kw: calls.append(1) or Chat())
    r = ask(conn, ai, str(s.db_path), "Welcher Hersteller liefert die Brandmeldeanlage im Pflegeheim?",
            project="BS-2023-0412")
    assert calls and len(r.context) >= 2
    assert (r.status, r.found, r.sources) == ("not_found", False, [])


# -- format detection stays within one timeout --------------------------------------------------------------
class _Server:
    """Answers the first request correctly, later ones after ``delay`` with ``later``."""

    def __init__(self, first: bytes, later: tuple[int, bytes], delay: float):
        self.requests = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.requests += 1
                status, body = (200, first) if outer.requests == 1 else later
                if outer.requests > 1:
                    time.sleep(delay)
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_reranker_redetection_stays_within_one_timeout():
    srv = _Server(json.dumps([{"index": 0, "score": 0.9}]).encode(), (200, b"<html>Wartung</html>"), 0.25)
    try:
        client = RerankClient(srv.url + "/rerank", timeout=0.4)
        assert client.rerank("Frage", ["Text"]) == [0.9]
        started = time.monotonic()
        with pytest.raises(AIServiceError):
            client.rerank("Frage", ["Text"])
        assert time.monotonic() - started < 0.55  # before: 3 calls of up to 0.4 s each
    finally:
        srv.close()


def test_embedding_redetection_stays_within_one_timeout():
    first = json.dumps({"data": [{"index": 0, "embedding": [0.1, 0.2]}]}).encode()
    srv = _Server(first, (404, b"not found"), 0.25)
    try:
        client = EmbeddingClient(srv.url, "m", timeout=0.4)
        assert len(client.embed_query("Frage")) == 2
        started = time.monotonic()
        with pytest.raises(AIServiceError):
            client.embed_query("Frage")
        assert time.monotonic() - started < 0.55
    finally:
        srv.close()


# -- one image drawn many times in a scan --------------------------------------------------------------------
def _page_image(size=(1200, 1200)) -> Image.Image:
    img = Image.new("L", size, 255)
    d = ImageDraw.Draw(img)
    for y in range(40, size[1] - 40, 60):
        d.text((40, y), "Grundriss EG – Rauchabzug", fill=0)
    return img


def _repeat_pdf(img: Image.Image, placements: list[tuple[float, ...]], *, declared=None, data=None) -> bytes:
    """One page drawing the same image XObject at every placement."""
    w, h = declared or img.size
    raw = data if data is not None else zlib.compress(img.tobytes())
    image = (b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceGray "
             b"/BitsPerComponent 8 /Filter /FlateDecode /Length %d >>\nstream\n" % (w, h, len(raw)) + raw
             + b"\nendstream")
    ops = b"\n".join(b"q %s cm /Im1 Do Q" % " ".join(f"{v:g}" for v in m).encode() for m in placements)
    content = b"<< /Length %d >>\nstream\n" % len(ops) + ops + b"\nendstream"
    objects = [image, content,
               b"<< /Type /Page /Parent 4 0 R /MediaBox [0 0 595 842] /Contents 2 0 R "
               b"/Resources << /XObject << /Im1 1 0 R >> >> >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Catalog /Pages 4 0 R >>"]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 5 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


class _Vision:
    vision = True
    model = "vision-r4"

    def __init__(self):
        self.calls = 0

    def describe_image(self, data, mime, prompt):
        Image.open(io.BytesIO(data)).load()
        self.calls += 1

        class Result:
            text = "Grundriss EG"
        return Result()


def test_image_drawn_many_times_is_decoded_and_sent_once(monkeypatch):
    img = _page_image()
    pdf = _repeat_pdf(img, [(100, 0, 0, 100, 20 + (k % 5) * 110, 20 + (k // 5) * 20) for k in range(200)])
    assert len(pdf) < 100_000
    decodes = {"n": 0}
    real = extract_mod._page_payloads

    def counting(images, drawn, *a):
        class Counted:
            def __getitem__(self, key):
                decodes["n"] += 1
                return images[key]
        return real(Counted(), drawn, *a)

    monkeypatch.setattr(extract_mod, "_page_payloads", counting)
    llm = _Vision()
    started = time.monotonic()
    r = extract(Path("Plan.pdf"), pdf, llm=llm)
    assert time.monotonic() - started < 5
    assert (r.status, r.incomplete) == ("ok", False)
    assert llm.calls == 1 and decodes["n"] == 1
    assert r.text.count("Grundriss EG") == 1


def test_same_image_at_three_places_is_sent_once():
    llm = _Vision()
    r = extract(Path("Logo.pdf"), _repeat_pdf(_page_image((300, 300)), [(100, 0, 0, 100, 50 + 150 * k, 600)
                                                                        for k in range(3)]), llm=llm)
    assert llm.calls == 1 and r.text.count("Grundriss EG") == 1


def test_declared_huge_image_is_not_decoded():
    """A few bytes may declare a gigapixel image: it is left out with a note, never decoded."""
    llm = _Vision()
    pdf = _repeat_pdf(_page_image((10, 10)), [(500, 0, 0, 500, 50, 50)], declared=(40_000, 40_000),
                      data=zlib.compress(b"\x00" * 100))
    r = extract(Path("Bombe.pdf"), pdf, llm=llm)
    assert llm.calls == 0
    assert (r.status, r.incomplete, r.retry) == ("skipped", True, False)
    assert "Bilder zu groß (über 150 Megapixel je Seite)" in r.message


def test_more_different_images_than_the_page_limit(monkeypatch):
    from .test_daten_r3_scans import Vision, jpeg, page_scan, pdf, strips

    monkeypatch.setattr(extract_mod, "MAX_PAGE_IMAGES", 4)
    original = page_scan()
    llm = Vision()
    r = extract(Path("Streifen.pdf"), pdf([{"images": [(jpeg(s), m) for s, m in strips(original, 6)]}]), llm=llm)
    assert r.status == "ok" and r.incomplete and not r.retry
    assert len(llm.images) == 1  # the first four strips put together
    assert "2 Bild(er) nicht ausgewertet (mehr als 4 Bilder bzw. 150 Megapixel je Seite)" in r.message


# -- a building type is no project name when the question names another project -----------------------
@pytest.mark.parametrize("question", [
    "Welche Türöffnungskraft wird in der Tiefgarage der Hafenkante eingehalten?",  # Hafenkante has none
    "Wie viele Stellplätze hat die Tiefgarage im Wohnhochhaus?",  # the Wohnhochhaus has no garage
])
def test_type_word_of_another_project_stays_a_topic_word(demo, question):
    """Before: "Tiefgarage" counted as the name of Tiefgarage Marktplatz and was dropped from the topic –
    a green answer from the wrong project or about the wrong place."""
    s, ai, conn = demo
    r = ask(conn, ai, str(s.db_path), question)
    assert r.status == "not_found" and not r.sources


@pytest.mark.parametrize("question,fact", [
    ("Wie viele Stellplätze hat die Tiefgarage Marktplatz?", "240"),
    ("Wie viele Stellplätze hat die Tiefgarage?", "240"),
    ("Wie hoch ist das Wohnhochhaus?", "68 m"),
])
def test_project_named_by_its_type_or_name_is_still_answered(demo, question, fact):
    s, ai, conn = demo
    r = ask(conn, ai, str(s.db_path), question)
    assert r.status == "answered" and fact in r.answer
