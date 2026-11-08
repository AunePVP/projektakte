"""HTTP server that imitates LLM, embedding and reranker services on one port.

Routes (same shapes as the real services, see docs/ki-dienste.md):

* ``GET  /v1/models``, ``POST /v1/chat/completions``  – OpenAI-compatible LLM (optionally with the
  quirks of real servers: cut-off answers, reasoning-only output, no model list)
* ``POST /v1/embeddings`` (OpenAI) and ``POST /embed`` (TEI)  – embeddings
* ``POST /rerank``  – ``texts`` → TEI list, ``documents`` → Cohere/Jina ``results``
* ``GET  /health``

Behaviour can be changed at runtime through ``server.options`` (used by tests to simulate
other formats, slow responses or outages).
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import core

DEFAULT_OPTIONS = {
    "models": ["qwen3.8-flash-next"],
    "embed_formats": ["openai", "tei"],
    "rerank_formats": ["tei", "cohere"],
    "think": True,  # wrap answers in <think>…</think> like Qwen3 does
    "delay": 0.0,  # seconds to sleep before answering (timeout tests)
    "fail": [],  # endpoints answering 500: "chat", "embed", "rerank", "models"
    # behave like real servers in edge cases (see core.CHAT_QUIRKS): "length" (answer cut off at
    # max_tokens), "reasoning_only" (vLLM reasoning parser: content null, reasoning_content set),
    # "unterminated_think" (cut off inside <think>)
    "chat_quirk": None,
    "models_404": False,  # no GET /v1/models (some llama.cpp/proxy setups)
    # synonym file for reranker and chat (same format as SYNONYMS_FILE); None = SYNONYMS_FILE from the
    # environment, as the app's full-text search uses it
    "synonyms_file": None,
}


class _Handler(BaseHTTPRequestHandler):
    server_version = "projektakte-fake-ai/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet unless verbose
        if self.server.options.get("verbose"):
            super().log_message(fmt, *args)

    # -- helpers --
    def _send(self, status: int, obj) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw.decode("utf-8") or "null")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def _prelude(self, name: str) -> bool:
        with self.server.calls_lock:
            self.server.calls[name] = self.server.calls.get(name, 0) + 1
        opts = self.server.options
        if opts.get("delay"):
            time.sleep(float(opts["delay"]))
        if name in opts.get("fail", []):
            self._send(500, {"error": f"fake {name} failure"})
            return False
        return True

    # -- routes --
    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0].rstrip("/")
        if path == "/health":
            return self._send(200, {"status": "ok"})
        if path == "/v1/models":
            if not self._prelude("models"):
                return
            if self.server.options.get("models_404"):
                return self._send(404, {"detail": "Not Found"})
            models = self.server.options.get("models") or ["fake-llm"]
            return self._send(200, {"object": "list", "data": [
                {"id": m, "object": "model", "owned_by": "projektakte-fake"} for m in models]})
        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0].rstrip("/")
        payload = self._read_json()
        opts = self.server.options
        if not isinstance(payload, (dict, list)):
            return self._send(400, {"error": "invalid JSON"})
        if path == "/v1/chat/completions":
            if not self._prelude("chat"):
                return
            if not isinstance(payload, dict) or not payload.get("messages"):
                return self._send(400, {"error": "messages required"})
            return self._send(200, core.chat_completion(payload, think=bool(opts.get("think")),
                                                        quirk=opts.get("chat_quirk"),
                                                        synonyms=core.active_synonyms(opts.get("synonyms_file"))))
        if path == "/v1/embeddings" and "openai" in opts["embed_formats"]:
            if not self._prelude("embed"):
                return
            inputs = payload.get("input") if isinstance(payload, dict) else None
            if isinstance(inputs, str):
                inputs = [inputs]
            if not isinstance(inputs, list):
                return self._send(422, {"error": "input required"})
            data = [{"object": "embedding", "index": i, "embedding": core.embed_text(str(t))}
                    for i, t in enumerate(inputs)]
            n = sum(len(str(t).split()) for t in inputs)
            return self._send(200, {"object": "list", "data": data,
                                    "model": payload.get("model", "fake-embed"),
                                    "usage": {"prompt_tokens": n, "total_tokens": n}})
        if path == "/embed" and "tei" in opts["embed_formats"]:
            if not self._prelude("embed"):
                return
            inputs = payload.get("inputs") if isinstance(payload, dict) else None
            if isinstance(inputs, str):
                inputs = [inputs]
            if not isinstance(inputs, list):
                return self._send(422, {"error": "inputs required"})
            return self._send(200, [core.embed_text(str(t)) for t in inputs])
        if path in ("/rerank", "/v1/rerank"):
            if not self._prelude("rerank"):
                return
            query = payload.get("query") if isinstance(payload, dict) else None
            if not isinstance(query, str):
                return self._send(422, {"error": "query required"})
            syn = core.active_synonyms(opts.get("synonyms_file"))
            if isinstance(payload.get("texts"), list) and "tei" in opts["rerank_formats"]:
                scores = [core.rerank_score(query, str(t), syn) for t in payload["texts"]]
                ranked = sorted(range(len(scores)), key=lambda i: (-scores[i], i))
                return self._send(200, [{"index": i, "score": scores[i]} for i in ranked])
            if isinstance(payload.get("documents"), list) and "cohere" in opts["rerank_formats"]:
                docs = [d.get("text", "") if isinstance(d, dict) else str(d)
                        for d in payload["documents"]]
                scores = [core.rerank_score(query, d, syn) for d in docs]
                ranked = sorted(range(len(scores)), key=lambda i: (-scores[i], i))
                top_n = payload.get("top_n") or len(ranked)
                return self._send(200, {
                    "model": payload.get("model", "fake-rerank"),
                    "results": [{"index": i, "relevance_score": scores[i]}
                                for i in ranked[: int(top_n)]]})
            return self._send(422, {"error": "unsupported rerank request format"})
        self._send(404, {"error": "not found"})


class FakeAIServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, options: dict | None = None):
        super().__init__(addr, _Handler)
        self.options = {**DEFAULT_OPTIONS, **(options or {})}
        self.calls: dict[str, int] = {}  # requests per endpoint (tests check batching)
        self.calls_lock = threading.Lock()

    @property
    def base_url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"


def make_server(host: str = "127.0.0.1", port: int = 8999, **options) -> FakeAIServer:
    return FakeAIServer((host, port), options)


def start_background(host: str = "127.0.0.1", port: int = 0, **options) -> FakeAIServer:
    """Start the fake services in a daemon thread (tests and AI_MODE=fake)."""
    server = make_server(host, port, **options)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                     name="fake-ai", daemon=True).start()
    return server
