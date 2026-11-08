"""verify3 (round 3, operations): file names stay out of the access log and the Caddy error log, the
favicon also loads on the sandboxed viewer pages, and the documented proxy timeout covers the
longest question (several searches – summary and role questions)."""
import logging
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from projektakte.config import load_settings
from projektakte.logsetup import StripQueryString, strip_path
from projektakte.main import create_app
from projektakte.startup import ask_worst_case_seconds, log_startup

ROOT = Path(__file__).resolve().parent.parent
CADDYFILE = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
DEPLOY_README = (ROOT / "deploy" / "README.md").read_text(encoding="utf-8")
ENV_EXAMPLE = (ROOT / "deploy" / "projektakte.env.example").read_text(encoding="utf-8")


# -- file names in the access log ------------------------------------------------------------------------
def access_record(path: str) -> logging.LogRecord:
    return logging.LogRecord("uvicorn.access", logging.INFO, "h11_impl.py", 1, '%s - "%s %s HTTP/%s" %d',
                             ("10.0.0.5:51234", "GET", path, "1.1", 200), None)


@pytest.mark.parametrize("path,expected", [
    ("/dokumente/4/datei/2023-06-22%20Besprechungsprotokoll%20Nr.%205.txt", "/dokumente/4/datei/"),
    ("/dokumente/1/datei/Gehaltsliste%20Mueller.pdf?download=1", "/dokumente/1/datei/"),
    ("/dokumente/12/datei/Anhang/Vertrag%20Meier.pdf", "/dokumente/12/datei/"),  # {filename:path}
    ("/dokumente/7/datei", "/dokumente/7/datei"),  # no name: nothing to cut
    ("/dokumente/7", "/dokumente/7"),
    ("/root/dokumente/3/datei/Name.png", "/root/dokumente/3/datei/"),  # behind a root path
    ("/suche?q=/dokumente/3/datei/Name.png", "/suche"),
])
def test_access_log_without_file_names(path, expected):
    record = access_record(path)
    assert StripQueryString().filter(record)
    assert record.getMessage() == f'10.0.0.5:51234 - "GET {expected} HTTP/1.1" 200'
    assert strip_path(path) == expected


def test_file_names_cut_on_other_message_shapes():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "x.py", 1,
                               '10.0.0.5:1 - "GET /dokumente/4/datei/Protokoll Meier.pdf?x=1 HTTP/1.1" 200', None, None)
    assert StripQueryString().filter(record)
    assert record.getMessage() == '10.0.0.5:1 - "GET /dokumente/4/datei/ HTTP/1.1" 200'


# -- the same in the Caddy error log -----------------------------------------------------------------------
def caddy_uri_filter() -> tuple[str, str]:
    m = re.search(r"^\s*request>uri regexp (\S+) (\S+)\s*$", CADDYFILE, re.M)
    assert m, "Caddyfile: request>uri regexp filter missing"
    return m.group(1), m.group(2)


@pytest.mark.parametrize("uri,expected", [
    ("/suche?q=Gehaltsliste+Mueller", "/suche"),
    ('/api/search?q=abc"Rohzitat', "/api/search"),
    ("/dokumente/1/datei/Gehaltsliste%20Mueller.pdf", "/dokumente/1/datei/"),
    ("/dokumente/12/datei/Anhang/Mueller%20Vertrag.pdf?download=Geheim", "/dokumente/12/datei/"),
    ("/dokumente/7/datei", "/dokumente/7/datei"),
    ("/projekte/BS-2024-0137", "/projekte/BS-2024-0137"),
])
def test_caddy_log_filter_cuts_query_and_file_name(uri, expected):
    """The pattern is RE2 (Go); for these constructs Python's re behaves the same (checked against
    caddy:2.8.4-alpine with a missing upstream: the 502 log lines show exactly these URIs)."""
    pattern, replacement = caddy_uri_filter()
    assert replacement == "$1"
    assert re.sub(pattern, r"\1", uri) == expected
    assert "request>headers delete" in CADDYFILE


# -- favicon on the sandboxed viewer pages -----------------------------------------------------------------
@pytest.fixture()
def app(tmp_path):
    return create_app(load_settings({"DATA_DIR": str(tmp_path), "APP_ENV": "test", "AUTH_DISABLED": "1"}),
                      background=False)


def test_favicon_loads_from_opaque_origins(app):
    client = TestClient(app)
    r = client.get("/favicon.ico")
    assert r.status_code == 200 and r.headers["cross-origin-resource-policy"] == "cross-origin"
    assert r.headers["x-content-type-options"] == "nosniff"  # the other security headers stay
    # everything else keeps same-origin
    assert client.get("/health").headers["cross-origin-resource-policy"] == "same-origin"
    assert client.get("/static/app.css").headers["cross-origin-resource-policy"] == "same-origin"


# -- proxy timeout ----------------------------------------------------------------------------------------
def test_worst_case_counts_every_search_of_a_question():
    # without an overall budget: 4 searches x (embedding + one reranker call) + language model
    assert ask_worst_case_seconds(load_settings({"ASK_DEADLINE": "0"})) == 4 * (60 + 30) + 120 == 480
    custom = load_settings({"EMBED_TIMEOUT": "30", "RERANK_TIMEOUT": "10", "ASK_DEADLINE": "0"})
    assert ask_worst_case_seconds(custom) == 4 * (30 + 10) + 120 == 280
    # default: ASK_DEADLINE=180 caps every question
    assert ask_worst_case_seconds(load_settings({})) == 180


def test_worst_case_respects_an_overall_question_deadline():
    from dataclasses import dataclass

    @dataclass
    class WithDeadline:  # an ASK_DEADLINE setting (package antworten) caps the value
        embed_timeout: float = 60
        rerank_timeout: float = 30
        llm_timeout: float = 120
        rerank_format: str = "auto"
        ask_deadline: float = 200

    assert ask_worst_case_seconds(WithDeadline()) == 200
    assert ask_worst_case_seconds(WithDeadline(ask_deadline=0)) == 480


def test_role_and_summary_questions_run_at_most_four_searches():
    """The formula's 4: the question's own search, the overview for a summary and up to two parties.
    Counted on the real ask() with the fake services – a summary naming two parties."""
    import projektakte.ask as ask_mod
    from projektakte import db
    from projektakte.ai.services import AIServices, resolve_settings
    from projektakte.demo import generate
    from projektakte.indexer import run_index

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        generate(Path(tmp) / "demo")
        s = load_settings({"DATA_DIR": tmp, "DEMO_MODE": "1", "AI_MODE": "fake", "APP_ENV": "test"})
        run_index(s)
        s = resolve_settings(s)
        ai = AIServices.from_settings(s)
        calls = {"embed": 0, "rerank": 0}
        real_embed, real_rerank = ai.embed.embed_query, ai.rerank.rerank

        def embed(*a, **kw):
            calls["embed"] += 1
            return real_embed(*a, **kw)

        def rerank(*a, **kw):
            calls["rerank"] += 1
            return real_rerank(*a, **kw)

        ai.embed.embed_query, ai.rerank.rerank = embed, rerank
        worst = 0
        with db.session(s.db_path) as conn:
            for question in ("Fasse zusammen, was Bauherr und Architektin zur Rauchableitung vereinbart haben.",
                             "Was hat der Bauherr zur Rauchableitung geschrieben?",
                             "Welche Auflagen zur Rauchableitung stehen in der Baugenehmigung?"):
                calls.update(embed=0, rerank=0)
                ask_mod.ask(conn, ai, str(s.db_path), question, project="BS-2024-0137")
                worst = max(worst, calls["embed"], calls["rerank"])
        assert 1 < worst <= getattr(ask_mod, "MAX_SEARCHES", 4)


def test_documented_proxy_timeouts_cover_the_worst_case():
    worst = ask_worst_case_seconds(load_settings({}))
    caddy = int(re.search(r"response_header_timeout (\d+)s", CADDYFILE).group(1))
    nginx = int(re.search(r"proxy_read_timeout (\d+)s", DEPLOY_README).group(1))
    assert caddy >= worst + 30 and nginx >= worst + 30
    assert "ASK_DEADLINE" in DEPLOY_README and "4 × (EMBED_TIMEOUT + RERANK_TIMEOUT) + LLM_TIMEOUT" in DEPLOY_README
    assert f"response_header_timeout {caddy}s" in ENV_EXAMPLE and "ASK_DEADLINE" in ENV_EXAMPLE
    assert worst == 180 and caddy < 300  # below the browser's own 5-minute limit


def test_startup_line_states_the_longest_question(caplog, tmp_path):
    caplog.set_level(logging.INFO, logger="projektakte.startup")
    s = load_settings({"DATA_DIR": str(tmp_path), "APP_ENV": "test", "AUTH_DISABLED": "1",
                       "EMBED_TIMEOUT": "30", "RERANK_TIMEOUT": "10", "ASK_DEADLINE": "0"})

    class NoAuth:
        disabled, users, cookie_secure = True, {}, False

    log_startup(s, NoAuth())
    assert "längste Frage bis 280s (Proxy-Zeitlimit darüber)" in caplog.text  # 4 x (30 + 10) + 120


# -- shared demo account ----------------------------------------------------------------------------------
def test_deploy_readme_asks_for_one_account_per_person():
    assert "## Benutzerkonten: eines je Person" in DEPLOY_README
    assert "Abmelden beendet alle\nSitzungen dieses Kontos auf allen Geräten" in DEPLOY_README


def test_hung_reranker_costs_one_timeout_also_with_auto_format():
    """Once the format is detected, a timeout must not trigger a second format detection: a hung
    reranker costs 1 x RERANK_TIMEOUT per search, with RERANK_FORMAT=auto as with a fixed format
    (ask_worst_case_seconds, deploy/README.md and the Caddyfile rely on it)."""
    import json
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from projektakte.ai.http import AIServiceError
    from projektakte.ai.rerank import RerankClient

    state = {"hang": False, "requests": 0}
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            state["requests"] += 1
            if state["hang"]:
                release.wait(5)
                return
            body = json.dumps([{"index": 0, "score": 0.9}]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/rerank"
    try:
        timings = {}
        for fmt in ("auto", "tei"):
            client = RerankClient(url, fmt=fmt, timeout=0.3)
            state["hang"] = False
            assert client.rerank("Frage", ["Text"]) == [0.9]  # format known now
            state["hang"], state["requests"] = True, 0
            started = time.monotonic()
            with pytest.raises(AIServiceError):
                client.rerank("Frage", ["Text"])
            timings[fmt] = (state["requests"], time.monotonic() - started)
        assert timings["auto"][0] == 1 and timings["auto"][1] < 0.55
        assert timings["tei"][0] == 1 and timings["tei"][1] < 0.55
    finally:
        release.set()
        server.shutdown()
        server.server_close()
