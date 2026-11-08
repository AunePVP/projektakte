"""Embeddings per chunk (stored as float32 BLOBs in SQLite) and a cached in-memory matrix for
vector search. Similarity = dot product of L2-normalised vectors."""
from __future__ import annotations

import base64
import json
import logging
import sqlite3
import threading

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


def embed_pending(conn: sqlite3.Connection, client: EmbeddingClient, limit: int | None = None) -> dict:
    """Embed all chunks without a vector, in batches; commits after every batch."""
    stats = {"embedded": 0, "pending": 0, "error": None, "reset": False}
    try:
        stats["reset"] = check_fingerprint(conn, client)
        conn.commit()
    except AIServiceError as exc:
        stats["error"] = str(exc)
        stats["pending"] = conn.execute("SELECT COUNT(*) FROM chunks WHERE embedding IS NULL").fetchone()[0]
        log.warning("Embeddings nicht möglich: %s", exc)
        return stats
    sql = ("SELECT c.id, c.heading, c.text, d.filename, p.name AS project_name FROM chunks c "
           "LEFT JOIN documents d ON d.id = c.doc_id LEFT JOIN projects p ON p.number = c.project "
           "WHERE c.embedding IS NULL ORDER BY c.id")
    rows = conn.execute(sql + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
    batch = max(1, client.batch_size)
    for i in range(0, len(rows), batch):
        part = rows[i : i + batch]
        texts = [embedding_text(r["filename"], r["project_name"], r["heading"], r["text"]) for r in part]
        try:
            vecs = client.embed(texts)
        except AIServiceError as exc:
            stats["error"] = str(exc)
            log.warning("Embeddings abgebrochen: %s", exc)
            break
        conn.executemany("UPDATE chunks SET embedding = ? WHERE id = ?",
                         [(to_blob(v), r["id"]) for v, r in zip(vecs, part)])
        _bump_generation(conn)
        conn.commit()
        stats["embedded"] += len(part)
    stats["pending"] = conn.execute("SELECT COUNT(*) FROM chunks WHERE embedding IS NULL").fetchone()[0]
    return stats


class VectorIndex:
    """All chunk vectors as one matrix, reloaded only when the embedding generation changes."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cache: dict[str, tuple] = {}

    def _load(self, conn: sqlite3.Connection, key: str):
        gen = db.get_meta(conn, "embed_generation", "0")
        cached = self._cache.get(key)
        if cached and cached[0] == gen:
            return cached
        rows = conn.execute("SELECT id, project, embedding FROM chunks WHERE embedding IS NOT NULL").fetchall()
        if rows:
            dim = len(rows[0]["embedding"]) // 4
            rows = [r for r in rows if len(r["embedding"]) // 4 == dim]
            mat = np.vstack([from_blob(r["embedding"]) for r in rows])
        else:
            mat = np.zeros((0, 0), dtype=np.float32)
        ids = np.array([r["id"] for r in rows], dtype=np.int64)
        projects = np.array([r["project"] or "" for r in rows], dtype=object)
        entry = (gen, ids, projects, mat)
        self._cache[key] = entry
        return entry

    def search(self, conn: sqlite3.Connection, key: str, qvec, *, project: str | None = None,
               k: int = 50, chunk_ids: set[int] | None = None) -> list[tuple[int, float]]:
        with self._lock:
            _, ids, projects, mat = self._load(conn, key)
        if mat.size == 0:
            return []
        q = np.asarray(qvec, dtype=np.float32)
        if q.shape[0] != mat.shape[1]:
            return []  # vectors from another model – wait for re-embedding
        scores = mat @ q
        mask = np.ones(len(ids), dtype=bool)
        if project:
            mask &= projects == project
        if chunk_ids is not None:
            mask &= np.isin(ids, np.fromiter(chunk_ids, dtype=np.int64))
        idx = np.nonzero(mask)[0]
        if idx.size == 0:
            return []
        order = idx[np.argsort(-scores[idx], kind="stable")[:k]]
        return [(int(ids[i]), float(scores[i])) for i in order]


VECTOR_INDEX = VectorIndex()
