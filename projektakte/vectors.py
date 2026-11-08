"""Embeddings per chunk (stored as float32 BLOBs in SQLite) and a cached in-memory matrix for
vector search. Similarity = dot product of L2-normalised vectors."""
from __future__ import annotations

import base64
import json
import logging
import sqlite3
import threading
import time

import numpy as np

from . import db
from .ai import AIServiceError
from .ai.embeddings import EmbeddingClient

log = logging.getLogger("projektakte.vectors")
PROBE_TEXT = "Projektakte Referenztext: Rauchableitung im notwendigen Treppenraum"


def to_blob(vec) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def embedding_text(filename: str | None, project_name: str | None, heading: str | None, text: str) -> str:
    """Document sections are embedded with a little context (file, project, heading), no instruction."""
    head = " – ".join(x for x in (filename, project_name, heading) if x)
    return f"{head}\n{text}" if head else text


def _bump_generation(conn: sqlite3.Connection) -> None:
    gen = int(db.get_meta(conn, "embed_generation", "0") or 0) + 1
    db.set_meta(conn, "embed_generation", str(gen))


def check_fingerprint(conn: sqlite3.Connection, client: EmbeddingClient) -> bool:
    """Detect a changed embedding service/model (e.g. fake → real Qwen3) and drop old vectors.

    Returns True if existing vectors were invalidated."""
    probe = np.asarray(client.embed([PROBE_TEXT])[0], dtype=np.float32)
    stored = db.get_meta(conn, "embed_fingerprint")
    current = {"model": client.model, "dim": int(probe.shape[0]),
               "probe": base64.b64encode(probe.tobytes()).decode("ascii")}
    if stored:
        old = json.loads(stored)
        old_probe = np.frombuffer(base64.b64decode(old["probe"]), dtype=np.float32)
        same = (old.get("model") == current["model"] and old_probe.shape == probe.shape
                and float(old_probe @ probe) > 0.999)
        if same:
            return False
        log.warning("Embedding-Dienst/-Modell hat sich geändert – alle Vektoren werden neu berechnet")
        conn.execute("UPDATE chunks SET embedding = NULL")
        _bump_generation(conn)
    db.set_meta(conn, "embed_fingerprint", json.dumps(current))
    return bool(stored)


# During a long embedding pass, new vectors become searchable at most this often (each publication
# makes every search process reload the whole matrix once).
PUBLISH_INTERVAL_S = 300.0
_PENDING_SQL = (
    "SELECT c.id, c.heading, c.text, d.filename, p.name AS project_name FROM chunks c "
    "LEFT JOIN documents d ON d.id = c.doc_id LEFT JOIN projects p ON p.number = c.project "
    "WHERE c.embedding IS NULL AND c.id > ? ORDER BY c.id LIMIT ?")


def embed_pending(conn: sqlite3.Connection, client: EmbeddingClient, limit: int | None = None, *,
                  publish_interval: float = PUBLISH_INTERVAL_S) -> dict:
    """Embed all chunks without a vector, in batches; commits after every batch (no transaction is
    open while the embedding service works). The embedding generation – which makes searches reload
    the matrix – is bumped once at the end, and during long passes at most every
    ``publish_interval`` seconds."""
    stats = {"embedded": 0, "pending": 0, "error": None, "reset": False}
    try:
        stats["reset"] = check_fingerprint(conn, client)
        conn.commit()
    except AIServiceError as exc:
        stats["error"] = str(exc)
        stats["pending"] = _count_pending(conn)
        log.warning("Embeddings nicht möglich: %s", exc)
        return stats
    batch = max(1, client.batch_size)
    last_id, unpublished, published_at = 0, 0, time.monotonic()
    while limit is None or stats["embedded"] < limit:
        size = batch if limit is None else min(batch, limit - stats["embedded"])
        part = conn.execute(_PENDING_SQL, (last_id, size)).fetchall()
        if not part:
            break
        last_id = part[-1]["id"]
        texts = [embedding_text(r["filename"] or ("Mail" if r["heading"] else None), r["project_name"],
                                r["heading"], r["text"]) for r in part]
        try:
            vecs = client.embed(texts)
        except AIServiceError as exc:
            stats["error"] = str(exc)
            log.warning("Embeddings abgebrochen: %s", exc)
            break
        conn.executemany("UPDATE chunks SET embedding = ? WHERE id = ?",
                         [(to_blob(v), r["id"]) for v, r in zip(vecs, part)])
        stats["embedded"] += len(part)
        unpublished += len(part)
        if time.monotonic() - published_at >= publish_interval:
            _bump_generation(conn)
            unpublished, published_at = 0, time.monotonic()
        conn.commit()
    if unpublished:
        _bump_generation(conn)
        conn.commit()
    stats["pending"] = _count_pending(conn)
    return stats


def _count_pending(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM chunks WHERE embedding IS NULL").fetchone()[0]


def load_matrix(conn: sqlite3.Connection, fetch: int = 1024) -> tuple[np.ndarray, np.ndarray]:
    """All vectors as (chunk ids ascending, float32 matrix). The matrix is allocated once and filled
    row by row, so loading needs little more memory than the matrix itself (no list of all blobs,
    no stacking copy). Vectors of another dimension than the first (model change in progress) are
    left out."""
    first = conn.execute("SELECT length(embedding) FROM chunks WHERE embedding IS NOT NULL "
                         "ORDER BY id LIMIT 1").fetchone()
    if not first or not first[0]:
        return np.zeros(0, dtype=np.int64), np.zeros((0, 0), dtype=np.float32)
    nbytes = int(first[0])
    where = "embedding IS NOT NULL AND length(embedding) = ?"
    n = conn.execute(f"SELECT COUNT(*) FROM chunks WHERE {where}", (nbytes,)).fetchone()[0]
    ids = np.empty(n, dtype=np.int64)
    mat = np.empty((n, nbytes // 4), dtype=np.float32)
    i = 0
    cur = conn.execute(f"SELECT id, embedding FROM chunks WHERE {where} ORDER BY id", (nbytes,))
    while rows := cur.fetchmany(fetch):
        if i + len(rows) > len(ids):  # vectors were added since counting
            grow = max(len(rows), len(ids) // 8)
            ids = np.concatenate([ids, np.empty(grow, dtype=np.int64)])
            mat = np.concatenate([mat, np.empty((grow, mat.shape[1]), dtype=np.float32)])
        for cid, blob in rows:
            ids[i] = cid
            mat[i] = np.frombuffer(blob, dtype=np.float32)
            i += 1
    return ids[:i], mat[:i]


def _rows_for(ids: np.ndarray, wanted) -> np.ndarray:
    """Matrix rows (ascending) of the given chunk ids; ids without a vector are ignored."""
    w = np.fromiter(wanted, dtype=np.int64)
    if w.size == 0 or ids.size == 0:
        return np.zeros(0, dtype=np.int64)
    pos = np.searchsorted(ids, w)
    inside = pos < ids.size
    pos, w = pos[inside], w[inside]
    return np.unique(pos[ids[pos] == w])


def _scores(mat: np.ndarray, q: np.ndarray, rows: np.ndarray | None, block: int = 1024) -> np.ndarray:
    """Similarity of ``q`` to all rows, or only to ``rows`` (in blocks, so a filtered search never
    copies a large part of the matrix)."""
    if rows is None:
        return mat @ q
    if rows.size > mat.shape[0] // 4:
        return (mat @ q)[rows]
    out = np.empty(rows.size, dtype=np.float32)
    for s in range(0, rows.size, block):
        out[s : s + block] = mat[rows[s : s + block]] @ q
    return out


class VectorIndex:
    """All chunk vectors as one matrix, reloaded only when the embedding generation changes.

    Project membership is deliberately not cached: it changes without new vectors (mail assignment,
    decisions in the inbox, renamed projects), so a project filter is resolved per search in SQL."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cache: dict[str, tuple] = {}

    def clear(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._cache.clear()
            else:
                self._cache.pop(key, None)

    def _load(self, conn: sqlite3.Connection, key: str):
        gen = db.get_meta(conn, "embed_generation", "0")
        cached = self._cache.get(key)
        if cached and cached[0] == gen:
            return cached
        self._cache.pop(key, None)  # release the old matrix before the new one is built
        ids, mat = load_matrix(conn)
        entry = (gen, ids, mat)
        self._cache[key] = entry
        return entry

    def search(self, conn: sqlite3.Connection, key: str, qvec, *, project: str | None = None,
               k: int = 50, chunk_ids: set[int] | None = None) -> list[tuple[int, float]]:
        with self._lock:
            _, ids, mat = self._load(conn, key)
        if mat.size == 0 or k <= 0:
            return []
        q = np.asarray(qvec, dtype=np.float32)
        if q.shape[0] != mat.shape[1]:
            return []  # vectors from another model – wait for re-embedding
        rows = None
        if project:
            rows = _rows_for(ids, (r[0] for r in conn.execute(
                "SELECT id FROM chunks WHERE project = ? AND embedding IS NOT NULL", (project,))))
        if chunk_ids is not None:
            selected = _rows_for(ids, chunk_ids)
            rows = selected if rows is None else np.intersect1d(rows, selected, assume_unique=True)
        if rows is not None and rows.size == 0:
            return []
        scores = _scores(mat, q, rows)
        order = np.argsort(-scores, kind="stable")
        if rows is not None:  # candidates were just read from the database – they exist
            top = order[:k]
            return [(int(c), float(sc)) for c, sc in zip(ids[rows[top]], scores[top])]
        return _top_existing(conn, ids[order], scores[order], k)


def _top_existing(conn: sqlite3.Connection, ranked_ids: np.ndarray, ranked_scores: np.ndarray,
                  k: int) -> list[tuple[int, float]]:
    """The first ``k`` ranked sections that still exist (sections deleted since the matrix was
    loaded – removed files, re-indexed documents – are skipped)."""
    out: list[tuple[int, float]] = []
    step = max(2 * k, 64)
    for start in range(0, ranked_ids.size, step):
        part = [int(c) for c in ranked_ids[start : start + step]]
        alive = {r[0] for r in conn.execute(
            f"SELECT id FROM chunks WHERE id IN ({','.join(map(str, part))})")}
        for cid, sc in zip(part, ranked_scores[start : start + step]):
            if cid in alive:
                out.append((cid, float(sc)))
                if len(out) == k:
                    return out
    return out


VECTOR_INDEX = VectorIndex()
