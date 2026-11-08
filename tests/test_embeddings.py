import math
import sqlite3

import pytest

from projektakte.ai.services import AIServices
from projektakte.demo import generate
from projektakte.indexer import run_index
from projektakte.vectors import VECTOR_INDEX, from_blob

from .conftest import fake_settings


@pytest.fixture()
def setup(tmp_path, fresh_fake_ai):
    generate(tmp_path / "demo")
    s = fake_settings(fresh_fake_ai, tmp_path, DEMO_MODE=1, EMBED_BATCH=8)
    return s, AIServices.from_settings(s), fresh_fake_ai


def test_all_chunks_get_normalised_vectors_in_batches(setup):
    s, ai, server = setup
    stats = run_index(s, ai)
    conn = sqlite3.connect(s.db_path)
    n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert stats["embeddings"]["embedded"] == n and stats["embeddings"]["pending"] == 0
    vecs = [from_blob(b) for (b,) in conn.execute("SELECT embedding FROM chunks")]
    assert all(v.shape == (256,) and abs(float(v @ v) - 1) < 1e-4 for v in vecs)
    # one probe call + ceil(n / 8) batches
    assert server.calls["embed"] == 1 + math.ceil(n / 8)
    # second run: nothing to do
    assert run_index(s, ai)["embeddings"]["embedded"] == 0


def test_outage_keeps_fulltext_and_catches_up_later(setup):
    s, ai, server = setup
    server.options["fail"] = ["embed"]
    stats = run_index(s, ai)
    assert stats["embeddings"]["error"] and stats["embeddings"]["pending"] > 0
    conn = sqlite3.connect(s.db_path)
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] > 0  # FTS still works
    server.options["fail"] = []
    stats = run_index(s, ai)
    assert stats["embeddings"]["pending"] == 0 and stats["embeddings"]["embedded"] > 0


def test_model_change_triggers_reembedding(setup, tmp_path):
    s, ai, server = setup
    run_index(s, ai)
    s2 = fake_settings(server, tmp_path, DEMO_MODE=1, EMBED_MODEL="anderes-modell")
    stats = run_index(s2, AIServices.from_settings(s2))
    assert stats["embeddings"]["reset"] is True
    conn = sqlite3.connect(s.db_path)
    assert stats["embeddings"]["embedded"] == conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]


def test_vector_search_finds_relevant_section(setup):
    s, ai, _ = setup
    run_index(s, ai)
    conn = sqlite3.connect(s.db_path)
    conn.row_factory = sqlite3.Row
    q = ai.embed.embed_query("zweiter baulicher Rettungsweg über eine Außentreppe")
    hits = VECTOR_INDEX.search(conn, str(s.db_path), q, project="BS-2024-0137", k=3)
    texts = [conn.execute("SELECT text FROM chunks WHERE id=?", (cid,)).fetchone()[0] for cid, _ in hits]
    assert any("Auflage 7" in t for t in texts)
    assert hits[0][1] >= hits[-1][1]
    # project filter
    all_projects = {conn.execute("SELECT project FROM chunks WHERE id=?", (cid,)).fetchone()[0]
                    for cid, _ in hits}
    assert all_projects == {"BS-2024-0137"}
