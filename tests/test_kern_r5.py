"""Round 5 (cross-check of the final check), core: the question budget in every branch and every
setting, wall-clock bounded AI calls, invalid timeouts, refusals versus partial answers."""
import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from projektakte.ai.embeddings import EmbeddingClient
from projektakte.ai.http import AIServiceError, CallBudget, post_json, seconds_text, timeout_error
from projektakte.ai.rerank import RerankClient
from projektakte.ai.services import AIServices
from projektakte.ask import BUDGET_TEXT, ask, is_not_found
from projektakte.config import load_settings
from projektakte.demo import generate
from projektakte.indexer import run_index
from projektakte.startup import deadline_warnings, log_startup

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


def _hanging_embedding(monkeypatch, ai):
    """An embedding service that hangs until whatever timeout it is given."""
    def embed_query(query, *, timeout=None):
        time.sleep(timeout or 0)
        raise timeout_error(timeout or 0, "http://embed/v1/embeddings")
    monkeypatch.setattr(ai.embed, "embed_query", embed_query)


# -- overview question without passages of its own under time pressure ---------------------------------
def test_summary_with_a_hanging_embedding_service_is_answered_from_the_overview(demo, monkeypatch):
    """Before: the overview search was skipped for lack of time, the question had no passages of its own
    and ended as "nichts gefunden" without asking the model – with its reserve untouched."""
    s, ai, conn = demo
    _hanging_embedding(monkeypatch, ai)
    started = time.monotonic()
    r = ask(conn, ai, str(s.db_path), "Worum geht es?", project="BS-2025-0019", deadline_s=4)
    elapsed = time.monotonic() - started
    assert r.status == "answered" and "Kindertagesstätte" in r.answer, (r.status, r.answer, r.notes)
    assert elapsed < 4.3
    (note,) = [n for n in r.notes if "Zeitbudget" in n]
    assert note.startswith("Zeitbudget der Frage (ASK_DEADLINE = 4 s) erreicht – nicht abgewartet: Vektorsuche")


@pytest.mark.parametrize("question", ["Worum geht es?", "Wer ist der Bauherr?"])
def test_smallest_budget_with_fast_services_runs_every_search(demo, question):
    """Before: at ASK_DEADLINE=10 (the minimum) the overview and party searches were always skipped,
    even with services answering in milliseconds."""
    s, ai, conn = demo
    calls = {"embed": 0, "rerank": 0}
    real_embed, real_rerank = ai.embed.embed_query, ai.rerank.rerank

    def embed(*a, **kw):
        calls["embed"] += 1
        return real_embed(*a, **kw)

    def rerank(*a, **kw):
        calls["rerank"] += 1
        return real_rerank(*a, **kw)

    ai.embed.embed_query, ai.rerank.rerank = embed, rerank
    try:
        r = ask(conn, ai, str(s.db_path), question, project="BS-2025-0019", deadline_s=10)
    finally:
        ai.embed.embed_query, ai.rerank.rerank = real_embed, real_rerank
    assert calls["embed"] >= 2 and calls["rerank"] >= 2
    assert not any("Zeitbudget" in n for n in r.notes)


def test_budget_notes_are_merged_into_one(tmp_path, fresh_fake_ai):
    generate(tmp_path / "demo")
    s = fake_settings(fresh_fake_ai, tmp_path, DEMO_MODE=1)
    ai = AIServices.from_settings(s)
    run_index(s, ai)
    conn = sqlite3.connect(s.db_path)
    conn.row_factory = sqlite3.Row
    fresh_fake_ai.options["delay"] = 3.0
    try:
        r = ask(conn, ai, str(s.db_path), "Worum geht es?", project="BS-2025-0019", deadline_s=2.0)
    finally:
        fresh_fake_ai.options["delay"] = 0
    assert r.answer == BUDGET_TEXT
    budget = [n for n in r.notes if "Zeitbudget" in n or "Zeitüberschreitung" in n or "nicht verfügbar" in n
              or "nicht erreichbar" in n]
    assert budget == ["Zeitbudget der Frage (ASK_DEADLINE = 2 s) erreicht – nicht abgewartet: Vektorsuche, "
                      "Reranker, Sprachmodell. Die KI-Dienste antworten zu langsam."]


# -- invalid numbers and timeouts ------------------------------------------------------------------------
@pytest.mark.parametrize("raw", ["0", "-5", "inf", "1e999", "nan", "abc"])
def test_invalid_service_timeouts_fall_back_with_a_warning(raw):
    s = load_settings({"LLM_TIMEOUT": raw, "EMBED_TIMEOUT": raw})
    assert (s.llm_timeout, s.embed_timeout) == (120.0, 60.0)
    warnings = deadline_warnings(s)
    assert any(w.startswith(f"LLM_TIMEOUT={raw} ist kein gültiges Zeitlimit") for w in warnings)
    assert any(w.startswith(f"EMBED_TIMEOUT={raw} ist kein gültiges Zeitlimit") for w in warnings)


def test_startup_survives_odd_values_and_names_them(caplog, tmp_path):
    import logging

    caplog.set_level(logging.INFO, logger="projektakte.startup")
    s = load_settings({"DATA_DIR": str(tmp_path), "APP_ENV": "test", "LLM_TIMEOUT": "inf", "ASK_DEADLINE": "nan"})

    class NoAuth:
        disabled, users, cookie_secure = True, {}, False

    log_startup(s, NoAuth())
    assert "Konfiguration: LLM_TIMEOUT=inf ist kein gültiges Zeitlimit" in caplog.text
    assert "Konfiguration: ASK_DEADLINE=nan ist keine gültige Zahl" in caplog.text
    assert s.ask_deadline == 180.0


def test_too_small_budget_warning_names_the_configured_value():
    (warning, _llm) = deadline_warnings(load_settings({"ASK_DEADLINE": "9.99"}))
    assert warning.startswith("ASK_DEADLINE=9.99 ist zu knapp") and "verwendet werden 10 s" in warning


def test_seconds_text_never_crashes():
    assert seconds_text(float("inf")) == "unbegrenzt" and seconds_text(1e9) == "1000000000"


# -- wall-clock bounded calls ------------------------------------------------------------------------------
class _Trickle:
    """Sends status and headers at once, then the body one byte every ``pause`` seconds."""

    def __init__(self, body: bytes, pause: float):
        outer = self
        self.body, self.pause = body, pause

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(outer.body)))
                    self.end_headers()
                    for b in outer.body:
                        self.wfile.write(bytes([b]))
                        self.wfile.flush()
                        time.sleep(outer.pause)
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


def test_trickling_answer_is_cut_at_the_timeout():
    srv = _Trickle(json.dumps([{"index": 0, "score": 0.5}]).encode(), 0.1)  # ~2.8 s for the whole body
    try:
        started = time.monotonic()
        with pytest.raises(AIServiceError, match="Zeitüberschreitung nach 0,6 s"):
            post_json(srv.url + "/rerank", {"query": "q", "texts": ["t"]}, timeout=0.6)
        assert time.monotonic() - started < 0.9
        client = RerankClient(srv.url + "/rerank", fmt="tei", timeout=0.6)
        started = time.monotonic()
        with pytest.raises(AIServiceError):
            client.rerank("Frage", ["Text"])
        assert time.monotonic() - started < 0.9
    finally:
        srv.close()


def test_normal_answers_are_read_completely():
    srv = _Trickle(json.dumps([{"index": 0, "score": 0.5}]).encode(), 0.0)
    try:
        assert post_json(srv.url + "/rerank", {"query": "q", "texts": ["t"]}, timeout=5) == [{"index": 0, "score": 0.5}]
    finally:
        srv.close()


def test_detection_timeout_names_the_configured_timeout():
    budget = CallBudget(2.0, "http://x/embed")
    exc = budget.own(timeout_error(0.5, "http://x/embed"))
    assert str(exc) == "Zeitüberschreitung nach 2 s: http://x/embed"
    other = AIServiceError("nicht erreichbar: http://x (refused)")
    assert budget.own(other) is other


def test_embedding_detection_reports_the_configured_timeout():
    class Srv:
        def __init__(self):
            outer = self
            self.n = 0

            class Handler(BaseHTTPRequestHandler):
                def do_POST(self):  # noqa: N802
                    self.rfile.read(int(self.headers.get("Content-Length", 0)))
                    outer.n += 1
                    if outer.n == 1:
                        body = json.dumps({"data": [{"index": 0, "embedding": [0.1, 0.2]}]}).encode()
                        status = 200
                    elif outer.n == 2:
                        body, status = b"gone", 404
                    else:
                        time.sleep(3)
                        body, status = b"[]", 200
                    try:
                        self.send_response(status)
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

    srv = Srv()
    try:
        client = EmbeddingClient(srv.url, "m", timeout=1.0)
        client.embed_query("Frage")
        with pytest.raises(AIServiceError) as err:
            client.embed_query("Frage")
        assert "Zeitüberschreitung nach 1 s" in str(err.value)
    finally:
        srv.httpd.shutdown()
        srv.httpd.server_close()


# -- refusals versus partial answers -------------------------------------------------------------------------
REFUSALS = [
    "Die Quellen enthalten keine Angabe zum Hersteller; weder [1] noch [2] nennen ihn.",
    "In den Quellen ist kein Hersteller genannt – [1] und [2] beschreiben nur die Anlage.",
    "Die Quellen enthalten keine Angaben zu den Kosten; in [1] geht es nur um den Brandschutz.",
    "In den Unterlagen finde ich dazu nichts - auch [3] schweigt dazu.",
    "In den Unterlagen finde ich dazu nichts.",
    "Die Quellen nennen keinen Hersteller. Sowohl [1] als auch [2] behandeln ausschließlich die Prüfung der Anlage.",
    "Die Quellen enthalten keine Angaben zum Hersteller der Brandmeldeanlage. [1] und [2] befassen sich lediglich "
    "mit der Abnahme.",
    "Aus den Quellen geht nicht hervor, wer der Hersteller ist. Weder im Prüfbericht [2] noch im Protokoll [3] wird "
    "er genannt.",
    "Dazu enthalten die Quellen keine Angaben. Auch in [2] findet sich kein Hinweis auf den Hersteller.",
    "Die Quellen nennen keinen Hersteller. [1] erwähnt nur die Anlagenart, nicht den Hersteller.",
    "Leider lässt sich die Frage anhand der Quellen nicht beantworten: Weder das Brandschutzkonzept [1] noch der "
    "Prüfbericht [2] nennen einen Hersteller.",
    "Die vorliegenden Unterlagen enthalten keine Informationen zum Hersteller der Brandmeldeanlage. Die Quellen [1] "
    "bis [3] beziehen sich auf die Abnahme und die Mängelbeseitigung.",
    "Die Quellen enthalten keine Angaben zum Hersteller. Sie beschreiben lediglich die Anforderungen an die Anlage "
    "[1, 2].",
    "Nicht gefunden. Die Quellen [1]–[3] betreffen andere Themen.",
    "Nicht gefunden. [1] und [2] behandeln die Rettungswege, nicht die Brandmeldeanlage.",
    "Dazu habe ich in den Quellen keine Informationen gefunden. Die Quellen [1] und [2] behandeln die Rettungswege, "
    "nicht die Brandmeldeanlage.",
    "Die Quellen enthalten keine Angaben zur Anzahl der Rauchmelder. In [1] werden die Rauchmelder zwar erwähnt, "
    "aber keine Anzahl genannt.",
    "Leider kann ich diese Frage nicht beantworten. Die Quellen [1]–[3] enthalten keine Angaben zum Hersteller.",
    "Die Quellen enthalten keine Angaben zum Hersteller. Möglicherweise ist er im Leistungsverzeichnis [4] aufgeführt.",
    "Die Quellen enthalten keine Angaben zum Hersteller. Bitte prüfen Sie dazu das Leistungsverzeichnis [4].",
    "Der Hersteller wird in den Quellen nicht genannt – [2] spricht nur allgemein von einer Brandmeldeanlage.",
    "Die Quellen nennen keinen Hersteller (siehe [1], [2]).",
    "Die Quellen nennen keinen Hersteller; [1] und [2] erwähnen ihn mit keinem Wort.",
    "Die Quellen nennen keinen Hersteller; in [1] und [2] taucht er nicht auf.",
    "Die Quellen nennen keinen Hersteller; [1] und [2] äußern sich dazu nicht.",
    "Die Quellen nennen keinen Hersteller; aus [1] und [2] ergibt sich dazu nichts.",
    "Die Quellen nennen keinen Hersteller. Auch im Prüfbericht [2] fehlt eine entsprechende Angabe.",
    "Die Quellen nennen keinen Hersteller; [1] und [2] sind dazu unergiebig.",
    "Die Quellen nennen keinen Hersteller; Angaben dazu fehlen in [1] und [2].",
    "Ein Hersteller ist in den Quellen nicht angegeben; [1] nennt lediglich die Kategorie der Anlage, nicht den "
    "Hersteller.",
    "Die Frage nach dem Hersteller lässt sich mit den Quellen nicht beantworten. Die Dokumente [1] und [2] stammen "
    "aus der Planungsphase.",
]
ANSWERS = [
    "Die Quellen nennen kein genaues Datum; laut Protokoll ist die Abnahme für März vorgesehen [2].",
    "Ein genauer Termin ist in den Quellen nicht genannt. Laut [2] gibt es aber keine offenen Mängel mehr.",
    "Die Quellen nennen keinen Abnahmetermin. Laut Prüfbericht [2] gibt es keine offenen Mängel mehr.",
    "Den Unterlagen ist kein Termin zu entnehmen. Die Feuerwehr hat laut [3] keine Bedenken gegen die Planung.",
    "Die Quellen enthalten keine Angaben zu Sprinklern. Laut Brandschutzkonzept [1] ist eine Sprinkleranlage nicht "
    "erforderlich.",
    "Ob die Anlage abgenommen wurde, geht aus den Quellen nicht eindeutig hervor; der Prüfsachverständige nennt in "
    "[2] keine offenen Mängel.",
    "Ein Abnahmedatum nennen die Unterlagen nicht – der Prüfbericht [2] führt jedoch keine offenen Mängel mehr auf.",
    "Die Quellen nennen keinen Termin. Laut [2] sind weder Brandschutztüren noch Rauchmelder mangelhaft.",
    "Ein Datum nennen die Quellen nicht. Der Prüfsachverständige sagt in [2] aber, dass keine Mängel mehr bestehen.",
    "Die Quellen nennen keinen Wartungsvertrag. Laut [3] führt der Betreiber die monatliche Sichtprüfung nicht "
    "selbst durch, sondern die Firma Schmidt.",
    "Zum Hersteller enthalten die Quellen keine Angaben. Die Feuerwehr macht laut [3] keine Einwände gegen das "
    "Fabrikat geltend.",
    "Ein konkretes Datum nennen die Unterlagen nicht – die Abnahme ist laut [2] für März vorgesehen.",
    "Die Unterlagen enthalten keinen Hinweis auf einen offenen Mangel; das Abnahmeprotokoll vom 12.12.2024 meldet "
    "„Keine Mängel“ [1].",
    "Die Quellen nennen keinen Hersteller; weder [1] noch [2] nennen ihn, laut [3] wird er aber im August festgelegt.",
    "Die Quellen nennen keinen Termin. Laut [2] ist die Abnahme für März vorgesehen.",
    "Der Hersteller ist nicht genannt. Laut [2] gibt die Feuerwehr keine Freigabe, solange die Aufschaltung fehlt.",
    "Die Quellen enthalten keinen genauen Termin. Laut [2] geht der Bauherr nicht von einer Abnahme vor Juni aus.",
    "Die Quellen enthalten keine Angaben zur Uhrzeit. Laut Protokoll [2] fand die Begehung am 12.03.2024 statt; "
    "Mängel wurden nicht festgestellt.",
    "Ein Termin ist nicht genannt. Laut [2] wurden keine Mängel an der RWA festgestellt.",
    "Die Quellen nennen keinen Prüfer. Laut [2] gibt es im 2. OG keine notwendigen Flure.",
    "Ob die Nachprüfung stattfand, geht aus den Quellen nicht hervor. Der Bauherr nennt in seiner Mail vom "
    "03.04.2024 [5] keinen neuen Termin, sagt aber zu, die Türen bis Ende April zu tauschen.",
    "Eine Kostenangabe enthalten die Quellen nicht; die Brandschutztüren sind laut [1] nicht als T30, sondern als "
    "T90 auszuführen.",
    "Die Quellen nennen keinen Hersteller. Laut Leistungsverzeichnis [4] ist kein bestimmtes Fabrikat vorgeschrieben.",
    # from the correctness review: cited negative facts with generic verbs
    "Die Unterlagen nennen kein genaues Abnahmedatum. Laut Prüfbericht [2] gibt es keine offenen Mängel mehr, die "
    "Abnahme erfolgte am 12.03.2025.",
    "Die Quellen nennen keinen Termin. Laut Protokoll [2] gibt es keine Einwände gegen eine Abnahme im März.",
    "Die Quellen nennen keinen Termin. Die Brandschutzdienststelle äußert laut [3] keine Bedenken gegen die Planung.",
    "Die Quellen nennen keine Kosten. Laut [2] führt die Planänderung nicht zu zusätzlichen Brandschutzmaßnahmen.",
    "Die Quellen nennen keinen Termin. Laut Stellungnahme [1] geht die Feuerwehr nicht von einer Gefährdung aus.",
    "Die Quellen nennen keinen genauen Termin – [2] nennt nur den Monat März.",
]


@pytest.mark.parametrize("answer", REFUSALS)
def test_refusals_with_meta_statements_about_the_sources(answer):
    assert is_not_found(answer, 5)


@pytest.mark.parametrize("answer", ANSWERS)
def test_partial_answers_keep_their_cited_facts(answer):
    assert not is_not_found(answer, 5)
