"""``python -m projektakte.fake_ai --port 8999`` – run the fake AI services."""
from __future__ import annotations

import argparse

from .server import make_server


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Fake-KI-Dienste für Projektakte")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8999)
    ap.add_argument("--model", action="append", help="Modellname(n) für /v1/models")
    ap.add_argument("--embed-format", choices=["both", "openai", "tei"], default="both")
    ap.add_argument("--rerank-format", choices=["both", "tei", "cohere"], default="both")
    ap.add_argument("--no-think", action="store_true", help="keine <think>-Blöcke senden")
    ap.add_argument("--delay", type=float, default=0.0, help="künstliche Verzögerung in s")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args(argv)
    opts = {
        "embed_formats": ["openai", "tei"] if a.embed_format == "both" else [a.embed_format],
        "rerank_formats": ["tei", "cohere"] if a.rerank_format == "both" else [a.rerank_format],
        "think": not a.no_think,
        "delay": a.delay,
        "verbose": a.verbose,
    }
    if a.model:
        opts["models"] = a.model
    server = make_server(a.host, a.port, **opts)
    base = server.base_url
    print(f"Fake-KI-Dienste laufen auf {base}")
    print(f"  LLM_URL={base}/v1  EMBED_URL={base}  RERANK_URL={base}/rerank")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
