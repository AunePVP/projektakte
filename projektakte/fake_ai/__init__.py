"""Fake AI services (LLM, embeddings, reranker) for development and tests.

Start standalone with ``python -m projektakte.fake_ai --port 8999`` and point
``LLM_URL=http://127.0.0.1:8999/v1``, ``EMBED_URL=http://127.0.0.1:8999``,
``RERANK_URL=http://127.0.0.1:8999/rerank`` at it – or set ``AI_MODE=fake``.
"""
from .server import FakeAIServer, make_server, start_background

__all__ = ["FakeAIServer", "make_server", "start_background"]
