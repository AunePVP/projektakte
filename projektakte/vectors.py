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
    "SELECT c.id, c.heading, c.text, c.project, d.filename, p.name AS project_name FROM chunks c "
    "LEFT JOIN documents d ON d.id = c.doc_id LEFT JOIN projects p ON p.number = c.project "
    "WHERE c.embedding IS NULL AND c.id > ? ORDER BY c.id LIMIT ?")


# A vector is stored only for the section it was made from: a document written anew meanwhile (the
# documents pass, the Bildauswertung) deletes its sections, and SQLite may give a new one the same id.
STORE_SQL = "UPDATE chunks SET embedding = ? WHERE id = ? AND embedding IS NULL AND text = ?"
EMBED_LOCK = threading.Lock()  # one embedding loop at a time in this process (no section embedded twice)
# Set when sections are written (documents.write_chunks): wakes the background embedding (embed_worker.py).
CHUNKS_WAITING = threading.Event()


def pending_rows(conn: sqlite3.Connection, after: int, size: int) -> list[sqlite3.Row]:
    """Sections without a vector after the id ``after``, in id order (the partial index of migration 13)."""
    return conn.execute(_PENDING_SQL, (after, size)).fetchall()


def store_vectors(conn: sqlite3.Connection, rows, vecs) -> int:
    """Store the vectors of ``rows`` (no commit); returns how many sections still had that text."""
    n = 0
    for v, r in zip(vecs, rows):
        n += conn.execute(STORE_SQL, (to_blob(v), r["id"], r["text"])).rowcount
    return n


def bump_generation(conn: sqlite3.Connection) -> None:
    """New vectors become searchable (every search process reloads the matrix once)."""
    _bump_generation(conn)


def count_pending(conn: sqlite3.Connection) -> int:
    return _count_pending(conn)


def embed_pending(conn: sqlite3.Connection, client: EmbeddingClient, limit: int | None = None, *,
                  publish_interval: float = PUBLISH_INTERVAL_S) -> dict:
    """Embed all chunks without a vector (see :func:`_embed_pending`); waits while the background embedding
    of the app embeds a batch, so no section is embedded twice."""
    with EMBED_LOCK:
        return _embed_pending(conn, client, limit, publish_interval=publish_interval)


def _embed_pending(conn: sqlite3.Connection, client: EmbeddingClient, limit: int | None = None, *,
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
        store_vectors(conn, part, vecs)
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


# Sections that may be found: mails and their attachments always, project files only while their
# project is active – the files of a project whose folder is missing are kept for a grace period
# (registry.MISSING_GRACE) but must not show up in search and answers meanwhile.
_SEARCHABLE = "(c.mail_id IS NOT NULL OR c.project IN (SELECT number FROM projects WHERE active = 1))"


# VECTOR_SEARCH: how the vectors are held for searching.
# "kompakt" (default): in memory only one bit per dimension (sign of the vector minus the mean vector,
#   2560 dimensions = 320 bytes per section); a search compares these codes (Hamming distance), then
#   scores the best RESCORE candidates exactly with their full vectors read from SQLite. A scope with
#   at most RESCORE sections (a project, an order) is scored exactly.
# "matrix": all vectors as one float32 matrix in memory (sections × dimensions × 4 bytes), exact.
VECTOR_SEARCH_MODES = ("kompakt", "matrix")
DEFAULT_VECTOR_SEARCH = "kompakt"
RESCORE_MIN = 2000  # candidates scored exactly with their full vectors (at least, k × RESCORE_FACTOR)
RESCORE_FACTOR = 10
WIDEN_MAX = 64  # at most this many times the candidates are read when most of them are hidden
MEAN_SAMPLE = 20_000  # vectors sampled for the mean that centres the codes
_HAMMING_BLOCK = 65_536  # rows per block of the code comparison (bounds the temporary memory)
_FETCH_IDS = 900  # ids per SQL statement when reading full vectors


def _pack(vecs: np.ndarray, words: int) -> np.ndarray:
    """Sign bits of the rows of ``vecs`` (already centred) as ``words`` uint64 per row."""
    bits = np.packbits(vecs > 0, axis=1)
    pad = words * 8 - bits.shape[1]
    if pad:
        bits = np.pad(bits, ((0, 0), (0, pad)))
    return np.ascontiguousarray(bits).view(np.uint64)


def _mean_vector(conn: sqlite3.Connection, nbytes: int, n: int, sample: int = MEAN_SAMPLE) -> np.ndarray:
    """The mean of a sample of the vectors (every n/sample-th id) – sentence vectors share a common
    direction; without centring most sign bits would be the same for every section."""
    step = max(1, n // max(1, sample))
    total = np.zeros(nbytes // 4, dtype=np.float64)
    count = 0
    cur = conn.execute("SELECT embedding FROM chunks WHERE embedding IS NOT NULL AND length(embedding) = ? "
                       "AND id % ? = 0", (nbytes, step))
    while rows := cur.fetchmany(1024):
        total += np.vstack([np.frombuffer(r[0], dtype=np.float32) for r in rows]).sum(axis=0, dtype=np.float64)
        count += len(rows)
    return (total / count).astype(np.float32) if count else np.zeros(nbytes // 4, dtype=np.float32)


def load_codes(conn: sqlite3.Connection, fetch: int = 1024) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """All vectors as (chunk ids ascending, sign codes (n × words uint64), mean vector). Like
    load_matrix, but the full vectors are only read, never kept; vectors of another dimension than the
    first are left out."""
    first = conn.execute("SELECT length(embedding) FROM chunks WHERE embedding IS NOT NULL "
                         "ORDER BY id LIMIT 1").fetchone()
    if not first or not first[0]:
        return np.zeros(0, dtype=np.int64), np.zeros((0, 0), dtype=np.uint64), np.zeros(0, dtype=np.float32)
    nbytes = int(first[0])
    dim = nbytes // 4
    words = (dim + 63) // 64
    where = "embedding IS NOT NULL AND length(embedding) = ?"
    n = conn.execute(f"SELECT COUNT(*) FROM chunks WHERE {where}", (nbytes,)).fetchone()[0]
    mean = _mean_vector(conn, nbytes, n)
    ids = np.empty(n, dtype=np.int64)
    codes = np.empty((n, words), dtype=np.uint64)
    i = 0
    cur = conn.execute(f"SELECT id, embedding FROM chunks WHERE {where} ORDER BY id", (nbytes,))
    while rows := cur.fetchmany(fetch):
        if i + len(rows) > len(ids):  # vectors were added since counting
            grow = max(len(rows), len(ids) // 8)
            ids = np.concatenate([ids, np.empty(grow, dtype=np.int64)])
            codes = np.concatenate([codes, np.empty((grow, words), dtype=np.uint64)])
        block = np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in rows]) - mean
        ids[i : i + len(rows)] = [r[0] for r in rows]
        codes[i : i + len(rows)] = _pack(block, words)
        i += len(rows)
    return ids[:i], codes[:i], mean


def _hamming(codes: np.ndarray, qcode: np.ndarray, rows: np.ndarray | None) -> np.ndarray:
    """Differing bits between ``qcode`` and all rows of ``codes`` (or only ``rows``), in blocks."""
    n = codes.shape[0] if rows is None else rows.size
    out = np.empty(n, dtype=np.uint32)
    for s in range(0, n, _HAMMING_BLOCK):
        part = codes[s : s + _HAMMING_BLOCK] if rows is None else codes[rows[s : s + _HAMMING_BLOCK]]
        out[s : s + part.shape[0]] = np.bitwise_count(part ^ qcode).sum(axis=1, dtype=np.uint32)
    return out


def _full_vectors(conn: sqlite3.Connection, ids, dim: int, searchable: bool) -> tuple[list[int], np.ndarray]:
    """The full vectors of ``ids`` from SQLite (sections deleted meanwhile, of another dimension or –
    with ``searchable`` – not to be found are left out)."""
    got_ids: list[int] = []
    got: list[np.ndarray] = []
    ids = [int(c) for c in ids]
    extra = f" AND {_SEARCHABLE}" if searchable else ""
    for s in range(0, len(ids), _FETCH_IDS):
        part = ",".join(map(str, ids[s : s + _FETCH_IDS]))
        for cid, blob in conn.execute(f"SELECT c.id, c.embedding FROM chunks c WHERE c.id IN ({part}) "
                                      f"AND c.embedding IS NOT NULL{extra}"):
            if len(blob) == dim * 4:
                got_ids.append(cid)
                got.append(np.frombuffer(blob, dtype=np.float32))
    return got_ids, (np.vstack(got) if got else np.zeros((0, dim), dtype=np.float32))


class VectorIndex:
    """The vectors of all sections, held for searching as set by ``mode`` (VECTOR_SEARCH: "kompakt" or
    "matrix", see above), reloaded only when the embedding generation changes.

    Project membership and visibility are deliberately not cached: they change without new vectors
    (mail assignment, decisions in the inbox, renamed projects, a project folder missing for a
    while), so they are resolved per search in SQL."""

    def __init__(self, mode: str = DEFAULT_VECTOR_SEARCH):
        self._lock = threading.Lock()
        self._cache: dict[str, tuple] = {}
        self.mode = mode

    def configure(self, mode: str) -> None:
        """Switch the way vectors are held (VECTOR_SEARCH); loaded data of the other way is dropped."""
        mode = mode if mode in VECTOR_SEARCH_MODES else DEFAULT_VECTOR_SEARCH
        with self._lock:
            if mode != self.mode:
                self._cache.clear()
            self.mode = mode

    def clear(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._cache.clear()
            else:
                self._cache.pop(key, None)

    def _load(self, conn: sqlite3.Connection, key: str):
        gen = db.get_meta(conn, "embed_generation", "0")
        cached = self._cache.get(key)
        if cached and cached[0] == gen and cached[1] == self.mode:
            return cached
        self._cache.pop(key, None)  # release the old data before the new is built
        if self.mode == "matrix":
            ids, mat = load_matrix(conn)
            entry = (gen, self.mode, ids, mat, None)
        else:
            ids, codes, mean = load_codes(conn)
            entry = (gen, self.mode, ids, codes, mean)
        self._cache[key] = entry
        return entry

    def search(self, conn: sqlite3.Connection, key: str, qvec, *, project: str | None = None,
               k: int = 50, chunk_ids: set[int] | None = None,
               projects: list[str] | tuple[str, ...] | None = None) -> list[tuple[int, float]]:
        """The ``k`` most similar sections, of ``project`` or of a scope of several (``projects``, see
        db.project_scope) if given."""
        scope = list(projects) if projects else ([project] if project else [])
        with self._lock:
            _, mode, ids, data, mean = self._load(conn, key)
        if data.size == 0 or k <= 0:
            return []
        q = np.asarray(qvec, dtype=np.float32)
        dim = data.shape[1] if mode == "matrix" else mean.shape[0]
        if q.shape[0] != dim:
            return []  # vectors from another model – wait for re-embedding
        rows = None
        if scope:
            rows = _rows_for(ids, (r[0] for r in conn.execute(
                f"SELECT c.id FROM chunks c WHERE c.project IN ({','.join('?' * len(scope))}) "
                f"AND c.embedding IS NOT NULL AND {_SEARCHABLE}", scope)))
        if chunk_ids is not None:
            selected = _rows_for(ids, chunk_ids)
            rows = selected if rows is None else np.intersect1d(rows, selected, assume_unique=True)
        if rows is not None and rows.size == 0:
            return []
        if mode == "matrix":
            return self._search_matrix(conn, ids, data, q, rows, k)
        return self._search_codes(conn, ids, data, mean, q, rows, k)

    @staticmethod
    def _search_matrix(conn, ids, mat, q, rows, k) -> list[tuple[int, float]]:
        scores = _scores(mat, q, rows)
        order = np.argsort(-scores, kind="stable")
        if rows is not None:  # candidates were just read from the database – they exist
            top = order[:k]
            return [(int(c), float(sc)) for c, sc in zip(ids[rows[top]], scores[top])]
        return _top_existing(conn, ids[order], scores[order], k)

    @staticmethod
    def _search_codes(conn, ids, codes, mean, q, rows, k) -> list[tuple[int, float]]:
        """Pre-selection by sign codes, then exact scores of the candidates from their full vectors. A
        scope no larger than the number of candidates is scored exactly. Without a scope the candidates
        must still be searchable; while fewer than the usual number of candidates are (projects missing,
        sections deleted since loading), the pre-selection is widened – up to WIDEN_MAX times."""
        pool = rows if rows is not None else np.arange(ids.size)
        n = pool.size
        target = want = max(RESCORE_MIN, RESCORE_FACTOR * k)
        order = None
        if n > want:
            qcode = _pack((q - mean)[None, :], codes.shape[1])[0]
            dist = _hamming(codes, qcode, rows)
            order = np.argsort(dist, kind="stable")
        while True:
            take = pool if order is None else pool[order[:want]]
            got_ids, vecs = _full_vectors(conn, ids[take], q.shape[0], searchable=rows is None)
            if len(got_ids) >= target or order is None or want >= n or want >= WIDEN_MAX * target:
                break
            want *= 4  # many candidates were hidden or deleted since loading: look further
        if not got_ids:
            return []
        scores = vecs @ q
        top = np.argsort(-scores, kind="stable")[:k]
        return [(int(got_ids[i]), float(scores[i])) for i in top]


def _top_existing(conn: sqlite3.Connection, ranked_ids: np.ndarray, ranked_scores: np.ndarray,
                  k: int) -> list[tuple[int, float]]:
    """The first ``k`` ranked sections that still exist and may be found (sections deleted since the
    matrix was loaded – removed files, re-indexed documents – and hidden files are skipped)."""
    out: list[tuple[int, float]] = []
    step = max(2 * k, 64)
    for start in range(0, ranked_ids.size, step):
        part = [int(c) for c in ranked_ids[start : start + step]]
        alive = {r[0] for r in conn.execute(
            f"SELECT c.id FROM chunks c WHERE c.id IN ({','.join(map(str, part))}) AND {_SEARCHABLE}")}
        for cid, sc in zip(part, ranked_scores[start : start + step]):
            if cid in alive:
                out.append((cid, float(sc)))
                if len(out) == k:
                    return out
    return out


VECTOR_INDEX = VectorIndex()
