"""Repetition loops of the vision model: found while the answer streams in (the call ends at once), asked once
more with VISION_RETRY_BODY, dropped if it loops again – the page keeps its text so far. Documents read by the
model before the guard are checked once and read again if their sections hold a loop (DB migration 16)."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from projektakte import db
from projektakte.ai.llm import DEFAULT_RETRY_BODY, PAGE_MIN_TOKENS, IncompleteAnswer, LLMClient, page_tokens
from projektakte.ai.repetition import before_loop, find_loop
from projektakte.config import load_settings
from projektakte.extract import vision_prompt
from projektakte.vision import DROPPED, VisionPass

from .test_bildauswertung_stufen import LAYER, _docs, _setup, _stage1, _text, ocr_layer_scan

LOOP_ROW = "| BSK 12 | geprüft | i.O. | Wartung 03/2024 |\n"


# -- finding loops --------------------------------------------------------------------------------------------
def test_a_repeated_row_is_a_loop_and_what_came_before_is_kept():
    text = "Prüfprotokoll Brandschutzklappen\n" + LOOP_ROW * 40
    assert find_loop(text) == len("Prüfprotokoll Brandschutzklappen\n")
    assert before_loop(text) == "Prüfprotokoll Brandschutzklappen"
    assert find_loop("Kopf " + "immer wieder dasselbe " * 200) is not None  # a loop inside a line


def test_real_pages_are_no_loop():
    # rows that differ in a number, a few equal marks, a short repeated head
    assert find_loop("\n".join(f"| BSK {i} | geprüft | i.O. |" for i in range(80))) is None
    assert find_loop("[handschriftlich: ✓] " * 5) is None
    assert find_loop("0,00 " * 30) is None  # under MIN_CHARS
    assert find_loop("") is None and before_loop("Text ohne Schleife") == "Text ohne Schleife"


def test_the_first_reading_gets_tokens_after_the_pages_text():
    assert page_tokens(vision_prompt(""), 4000) == PAGE_MIN_TOKENS
    assert page_tokens(vision_prompt("x" * 4000), 4000) > 2500
    assert page_tokens(vision_prompt("x" * 12000), 4000) == 4000
    assert page_tokens("kurz", 800) == 800


# -- a model server that streams ------------------------------------------------------------------------------
class _Server:
    """Answers chat calls from a list of scripts: ("stream", text, finish) streams ``text`` in small pieces,
    ("json", text, finish) answers in one body (a server that ignores "stream"). Records the payloads and how
    many pieces each streamed answer sent before the client went away."""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.payloads: list[dict] = []
        self.sent: list[int] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.payloads.append(body)
                kind, text, finish = owner.scripts.pop(0)
                if kind == "json":
                    data = json.dumps({"choices": [{"message": {"content": text}, "finish_reason": finish}]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                pieces = [text[i:i + 20] for i in range(0, len(text), 20)]
                sent = 0
                try:
                    for k, piece in enumerate(pieces):
                        last = k == len(pieces) - 1
                        event = {"choices": [{"delta": {"content": piece}, "finish_reason": finish if last else None}]}
                        self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
                        self.wfile.flush()
                        sent += 1
                        time.sleep(0.001)  # like a model: the client reads along
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    owner.sent.append(sent)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


@pytest.fixture()
def server():
    made = []

    def make(*scripts):
        s = _Server(scripts)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def _client(srv, **kw) -> LLMClient:
    return LLMClient(srv.url, "bildmodell", vision=True, timeout=10, extra_body={"top_k": 1}, **kw)


GOOD = "Prüfprotokoll BSK 12 Wartung bestanden [handschriftlich: erledigt]"
LOOP = "Prüfprotokoll\n" + LOOP_ROW * 400  # far beyond what is needed to see the loop


def test_a_loop_is_stopped_early_and_asked_again_with_the_retry_body(server):
    srv = server(("stream", LOOP, "length"), ("stream", GOOD, "stop"))
    res = _client(srv).describe_image(b"img", "image/png", vision_prompt(LAYER))
    assert res.text == GOOD and not res.loop
    first, second = srv.payloads
    assert first["stream"] is True and first["top_k"] == 1 and "presence_penalty" not in first
    assert first["max_tokens"] == page_tokens(vision_prompt(LAYER), 4000)
    # the second attempt: the full amount and the sampling against repetitions, over LLM_EXTRA_BODY
    assert second["max_tokens"] == 4000 and {k: second[k] for k in DEFAULT_RETRY_BODY} == DEFAULT_RETRY_BODY
    # the client went away long before the loop's end (the server stops generating)
    total = len(range(0, len(LOOP), 20))
    assert srv.sent[0] < total / 4


def test_a_page_that_loops_twice_is_dropped_with_what_came_before(server):
    srv = server(("stream", LOOP, "length"), ("stream", LOOP, "length"))
    with pytest.raises(IncompleteAnswer) as err:
        _client(srv).describe_image(b"img", "image/png", vision_prompt(LAYER))
    assert err.value.loop and err.value.text == "Prüfprotokoll"
    assert "Wiederholungsschleife" in str(err.value)


def test_an_answer_cut_short_is_asked_again_with_more_tokens_only(server):
    srv = server(("stream", "Anfang der Seite", "length"), ("stream", GOOD, "stop"))
    res = _client(srv, retry_body={"presence_penalty": 2}).describe_image(b"img", "image/png", vision_prompt(""))
    assert res.text == GOOD
    assert srv.payloads[0]["max_tokens"] == PAGE_MIN_TOKENS and srv.payloads[1]["max_tokens"] == 4000
    assert "presence_penalty" not in srv.payloads[1]  # no loop: the usual sampling
    srv = server(("stream", "Anfang", "length"), ("stream", "Anfang der langen Seite", "length"))
    with pytest.raises(IncompleteAnswer) as err:
        _client(srv).describe_image(b"img", "image/png", vision_prompt(""))
    assert not err.value.loop and err.value.text == "Anfang der langen Seite"


def test_a_server_without_streaming_answers_in_one_body(server):
    srv = server(("json", GOOD, "stop"), ("json", LOOP, "stop"), ("json", GOOD, "stop"))
    assert _client(srv).describe_image(b"img", "image/png", vision_prompt(LAYER)).text == GOOD
    # a loop in a whole answer is found as well
    assert _client(srv).describe_image(b"img", "image/png", vision_prompt(LAYER)).text == GOOD
    assert srv.payloads[2]["presence_penalty"] == DEFAULT_RETRY_BODY["presence_penalty"]


def test_the_retry_body_goes_with_the_client_into_the_worker():
    llm = LLMClient("http://x/v1", "m", vision=True, retry_body={})
    assert LLMClient.from_config(llm.config()).retry_body == {}
    assert LLMClient("http://x/v1", "m").retry_body == DEFAULT_RETRY_BODY


def test_vision_retry_body_setting():
    s = load_settings({"VISION_RETRY_BODY": '{"temperature": 0.6}'})
    assert s.vision_retry_body == {"temperature": 0.6}
    assert load_settings({}).vision_retry_body is None
    s = load_settings({"VISION_RETRY_BODY": "[1]"})
    assert s.vision_retry_body is None and any("VISION_RETRY_BODY" in w for w in s.config_warnings)


# -- the phase "Bildauswertung" -------------------------------------------------------------------------------
class _Looping:
    vision, model = True, "bildmodell"

    def __init__(self, text: str = ""):
        self.text = text
        self.calls = 0

    def describe_image(self, data, mime, prompt):
        self.calls += 1
        raise IncompleteAnswer("Modell-Antwort verworfen: Wiederholungsschleife", self.text, True)


def test_a_dropped_answer_leaves_the_text_so_far_and_says_so(tmp_path):
    conn, root = _setup(tmp_path, {"Protokoll.pdf": ocr_layer_scan()})
    llm = _Looping("Prüfprotokoll")
    _stage1(conn, root, llm)
    stats = VisionPass(conn, llm, projects_dir=root).run()
    doc = _docs(conn)["Protokoll.pdf"]
    assert LAYER in _text(conn, "Protokoll.pdf")  # the OCR layer stands, not the start of the loop
    assert (doc["vision_pending"], doc["incomplete"], doc["status"]) == (0, 1, "ok")
    assert DROPPED in doc["message"] and "Texterkennung behalten" in doc["message"]
    assert (stats["dropped"], stats["pages"], stats["failed"]) == (1, 1, 0)
    # not asked again and again: the next pass has nothing to do
    _stage1(conn, root, llm)
    VisionPass(conn, llm, projects_dir=root).run()
    assert llm.calls == 1


def test_stored_loops_of_earlier_answers_are_read_again(tmp_path):
    conn, _root = _setup(tmp_path, {})
    project = conn.execute("SELECT number FROM projects").fetchone()[0]
    for name, text in (("Schleife.pdf", "Kopf\n" + LOOP_ROW * 30), ("Gut.pdf", GOOD)):
        cur = conn.execute(
            "INSERT INTO documents (project, path, rel_path, filename, ext, sha256, status, method, incomplete, "
            "extract_profile, loop_check) VALUES (?, ?, ?, ?, '.pdf', 'x', 'ok', 'vision', 0, 'v99:p', 1)",
            (project, f"/x/{name}", name, name))
        conn.execute("INSERT INTO chunks (doc_id, project, ord, page, text) VALUES (?, ?, 0, 1, ?)",
                     (cur.lastrowid, project, text))
    conn.commit()
    stats = VisionPass(conn, _Looping()).run()
    docs = _docs(conn)
    assert (docs["Schleife.pdf"]["incomplete"], docs["Schleife.pdf"]["extract_profile"]) == (1, "")
    assert (docs["Gut.pdf"]["incomplete"], docs["Gut.pdf"]["extract_profile"]) == (0, "v99:p")
    assert docs["Schleife.pdf"]["loop_check"] == docs["Gut.pdf"]["loop_check"] == 0
    assert stats["loops_found"] == 1


def test_migration_16_marks_the_documents_the_model_read(tmp_path):
    path = tmp_path / "alt.sqlite3"
    conn = db.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    for i, script in enumerate(db.MIGRATIONS[:15], start=1):
        conn.executescript("BEGIN;" + script + f"PRAGMA user_version = {i}; COMMIT;")
    conn.execute("INSERT INTO documents (path, rel_path, filename, ext, method) VALUES ('/a', 'a', 'a', '.pdf', 'vision'), ('/b', 'b', 'b', '.pdf', 'text')")
    conn.commit()
    conn.close()
    db.init_db(path)
    conn = db.connect(path)
    assert dict(conn.execute("SELECT rel_path, loop_check FROM documents").fetchall()) == {"a": 1, "b": 0}
