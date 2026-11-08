import math
import sqlite3

import numpy as np
import pytest

from projektakte import db
from projektakte.ai.services import AIServices
from projektakte.demo import generate
from projektakte.indexer import run_index
from projektakte.vectors import VECTOR_INDEX, embed_pending, from_blob, load_matrix, to_blob

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


def _gen(conn):
    return int(conn.execute("SELECT value FROM meta WHERE key = 'embed_generation'").fetchone()[0])


def test_project_filter_follows_reassignment_without_new_vectors(setup):
    """BUG-1: the project of a section changes (mail assigned, decision in the inbox) without a new
    vector – the project filter of the vector search must see it at once."""
    s, ai, _ = setup
    run_index(s, ai)
    conn = db.connect(s.db_path)
    key = str(s.db_path)
    mail_chunk = conn.execute("SELECT id, project, embedding FROM chunks WHERE mail_id IS NOT NULL "
                              "AND doc_id IS NULL AND project IS NOT NULL ORDER BY id LIMIT 1").fetchone()
    vec = from_blob(mail_chunk["embedding"])
    assert mail_chunk["id"] in [c for c, _ in VECTOR_INDEX.search(conn, key, vec, project=mail_chunk["project"])]
    gen = _gen(conn)
    other = conn.execute("SELECT number FROM projects WHERE number != ? LIMIT 1",
                         (mail_chunk["project"],)).fetchone()[0]
    mail_id = conn.execute("SELECT mail_id FROM chunks WHERE id = ?", (mail_chunk["id"],)).fetchone()[0]
    # what assignment does to the sections of a mail (assign.set_mail_project) – no new vectors
    conn.execute("UPDATE chunks SET project = ? WHERE mail_id = ?", (other, mail_id))
    conn.commit()
    assert _gen(conn) == gen
    assert mail_chunk["id"] in [c for c, _ in VECTOR_INDEX.search(conn, key, vec, project=other)]
    assert mail_chunk["id"] not in [c for c, _ in VECTOR_INDEX.search(conn, key, vec, project=mail_chunk["project"])]
    # combined filters
    hits = VECTOR_INDEX.search(conn, key, vec, project=other, chunk_ids={mail_chunk["id"], 1})
    assert [c for c, _ in hits] == [mail_chunk["id"]]
    assert VECTOR_INDEX.search(conn, key, vec, project="BS-1999-0000") == []


def test_deleted_sections_are_not_returned(setup):
    s, ai, _ = setup
    run_index(s, ai)
    conn = db.connect(s.db_path)
    key = str(s.db_path)
    row = conn.execute("SELECT id, embedding FROM chunks ORDER BY id LIMIT 1").fetchone()
    vec = from_blob(row["embedding"])
    assert VECTOR_INDEX.search(conn, key, vec, k=1)[0][0] == row["id"]
    conn.execute("DELETE FROM chunks WHERE id = ?", (row["id"],))
    conn.commit()
    hits = VECTOR_INDEX.search(conn, key, vec, k=5)
    assert len(hits) == 5 and row["id"] not in [c for c, _ in hits]


def test_generation_is_bumped_once_per_pass(setup):
    """ops-07: searches reload the matrix once per embedding pass, not after every batch."""
    s, ai, _ = setup
    run_index(s, ai)
    conn = db.connect(s.db_path)
    n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert n > 3 * s.embed_batch
    gen = _gen(conn)
    conn.execute("UPDATE chunks SET embedding = NULL")
    conn.commit()
    stats = embed_pending(conn, ai.embed)
    assert stats["embedded"] == n and _gen(conn) == gen + 1
    assert embed_pending(conn, ai.embed)["embedded"] == 0 and _gen(conn) == gen + 1  # nothing new
    # long passes publish progress in between (here: after every batch)
    conn.execute("UPDATE chunks SET embedding = NULL")
    conn.commit()
    embed_pending(conn, ai.embed, publish_interval=0)
    assert _gen(conn) == gen + 1 + math.ceil(n / s.embed_batch)


def test_embed_pending_respects_limit(setup):
    s, ai, _ = setup
    run_index(s, ai)
    conn = db.connect(s.db_path)
    conn.execute("UPDATE chunks SET embedding = NULL")
    conn.commit()
    stats = embed_pending(conn, ai.embed, limit=10)
    assert stats["embedded"] == 10
    assert stats["pending"] == conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] - 10


def test_load_matrix_is_filled_in_place(setup):
    s, ai, _ = setup
    run_index(s, ai)
    conn = db.connect(s.db_path)
    ids, mat = load_matrix(conn, fetch=7)
    rows = conn.execute("SELECT id, embedding FROM chunks WHERE embedding IS NOT NULL ORDER BY id").fetchall()
    assert ids.tolist() == [r["id"] for r in rows] and mat.dtype == np.float32
    assert np.array_equal(mat[3], from_blob(rows[3]["embedding"]))
    # a vector of another dimension (model change in progress) is left out
    conn.execute("UPDATE chunks SET embedding = ? WHERE id = ?", (to_blob([1.0, 0.0]), rows[-1]["id"]))
    ids2, mat2 = load_matrix(conn)
    assert rows[-1]["id"] not in ids2.tolist() and mat2.shape == (len(rows) - 1, mat.shape[1])


def test_empty_index(tmp_path):
    from projektakte import db as _db
    dbp = tmp_path / "e.sqlite3"
    _db.init_db(dbp)
    conn = _db.connect(dbp)
    ids, mat = load_matrix(conn)
    assert ids.size == 0 and mat.size == 0
    assert VECTOR_INDEX.search(conn, str(dbp), [1.0, 0.0]) == []
