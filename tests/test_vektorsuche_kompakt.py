"""Vector search with VECTOR_SEARCH=kompakt: sign codes in memory, exact scores of the candidates from
SQLite – the same hits as the full matrix within a scope, nearly the same over everything."""
from __future__ import annotations

import numpy as np
import pytest

from projektakte import db
from projektakte.config import load_settings
from projektakte.vectors import (RESCORE_MIN, VECTOR_INDEX, VectorIndex, load_codes, to_blob)


def _anisotropic(rng: np.random.Generator, n: int, dim: int, topics: int = 200) -> tuple[np.ndarray, np.ndarray]:
    """Unit vectors like sentence embeddings: a strong common direction, topic clusters, noise."""
    common = rng.normal(size=dim)
    common /= np.linalg.norm(common)
    centres = rng.normal(size=(topics, dim))
    centres /= np.linalg.norm(centres, axis=1, keepdims=True)
    topic = rng.integers(0, topics, n)
    v = 0.8 * common + 0.5 * centres[topic] + 0.35 * rng.normal(size=(n, dim)) / np.sqrt(dim) * 3
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    return v.astype(np.float32), centres


def _db(tmp_path, vecs: np.ndarray, projects: list[str]):
    path = tmp_path / "v.sqlite3"
    db.init_db(path)
    conn = db.connect(path)
    for p in sorted(set(projects)):
        conn.execute("INSERT INTO projects (number, name, path) VALUES (?, ?, ?)", (p, p, f"/x/{p}"))
    conn.executemany("INSERT INTO chunks (id, project, ord, text, embedding) VALUES (?, ?, 0, 't', ?)",
                     [(i + 1, projects[i], to_blob(v)) for i, v in enumerate(vecs)])
    conn.commit()
    return path, conn


@pytest.fixture()
def corpus(tmp_path):
    rng = np.random.default_rng(7)
    vecs, centres = _anisotropic(rng, 12_000, 384)
    projects = [f"P{i % 40:02d}" for i in range(len(vecs))]  # 300 sections per project
    path, conn = _db(tmp_path, vecs, projects)
    queries = centres[:40] * 0.6 + vecs[rng.integers(0, len(vecs), 40)] * 0.4
    yield str(path), conn, vecs, queries.astype(np.float32)
    conn.close()


def _search(mode: str, conn, key: str, q, **kw):
    index = VectorIndex(mode)
    return index.search(conn, key, q, **kw)


def test_compact_finds_nearly_the_same_as_the_matrix(corpus):
    key, conn, _, queries = corpus
    recall = []
    for q in queries:
        exact = [c for c, _ in _search("matrix", conn, key, q, k=50)]
        compact = _search("kompakt", conn, key, q, k=50)
        recall.append(len(set(exact) & {c for c, _ in compact}) / 50)
        # scores are the exact ones (from the full vectors), best first
        scores = [s for _, s in compact]
        assert scores == sorted(scores, reverse=True)
    assert np.mean(recall) >= 0.95, np.mean(recall)
    top1 = [_search("matrix", conn, key, q, k=1)[0][0] == _search("kompakt", conn, key, q, k=1)[0][0]
            for q in queries]
    assert np.mean(top1) >= 0.95


def test_compact_is_exact_inside_a_project(corpus):
    key, conn, _, queries = corpus
    for q in queries[:10]:
        exact = _search("matrix", conn, key, q, project="P03", k=50)
        compact = _search("kompakt", conn, key, q, project="P03", k=50)
        assert [c for c, _ in compact] == [c for c, _ in exact]
        assert np.allclose([s for _, s in compact], [s for _, s in exact], atol=1e-5)
    several = _search("kompakt", conn, key, queries[0], projects=["P01", "P02"], k=20)
    assert {(c - 1) % 40 for c, _ in several} <= {1, 2}
    chosen = {5, 45, 85, 9999}
    assert {c for c, _ in _search("kompakt", conn, key, queries[0], chunk_ids=chosen, k=10)} == chosen


def test_hidden_and_deleted_sections_are_skipped_and_the_search_looks_further(corpus):
    key, conn, vecs, queries = corpus
    index = VectorIndex("kompakt")
    q = queries[0]
    first = index.search(conn, key, q, k=50)  # codes loaded now
    # most projects disappear (inactive) and the best hit is deleted after loading
    conn.execute("UPDATE projects SET active = 0 WHERE number <> 'P07'")
    conn.execute("DELETE FROM chunks WHERE id = ?", (first[0][0],))
    conn.commit()
    hits = index.search(conn, key, q, k=50)
    assert len(hits) == 50 and all((c - 1) % 40 == 7 for c, _ in hits)
    assert first[0][0] not in {c for c, _ in hits}
    exact = _search("matrix", conn, key, q, k=50)
    assert {c for c, _ in hits} == {c for c, _ in exact}


def test_codes_of_odd_dimensions_and_the_memory_they_take(tmp_path):
    rng = np.random.default_rng(1)
    vecs, _ = _anisotropic(rng, RESCORE_MIN * 3, 70)  # 70 dimensions: 2 words of 64 bits
    path, conn = _db(tmp_path, vecs, ["P1"] * len(vecs))
    ids, codes, mean = load_codes(conn, fetch=13)
    assert ids.tolist() == list(range(1, len(vecs) + 1)) and codes.shape == (len(vecs), 2)
    assert codes.dtype == np.uint64 and mean.shape == (70,)
    hits = _search("kompakt", conn, str(path), vecs[17], k=5)
    assert hits[0][0] == 18 and abs(hits[0][1] - 1.0) < 1e-5
    assert _search("kompakt", conn, str(path), np.ones(3), k=5) == []  # another model's dimension
    # 2560 dimensions: 320 bytes of code per section instead of 10 240 bytes of float32
    assert (2560 + 63) // 64 * 8 == 320


def test_mode_setting_and_switch(tmp_path, corpus):
    key, conn, _, queries = corpus
    assert load_settings({}).vector_search == "kompakt"
    assert load_settings({"VECTOR_SEARCH": "Matrix"}).vector_search == "matrix"
    s = load_settings({"VECTOR_SEARCH": "schnell"})
    assert s.vector_search == "kompakt" and any("VECTOR_SEARCH" in w for w in s.config_warnings)
    index = VectorIndex("matrix")
    index.search(conn, key, queries[0], k=5)
    assert index._cache[key][1] == "matrix"
    index.configure("kompakt")
    assert not index._cache
    index.search(conn, key, queries[0], k=5)
    assert index._cache[key][1] == "kompakt"
    index.configure("unbekannt")
    assert index.mode == "kompakt"
    assert VECTOR_INDEX.mode in ("kompakt", "matrix")
