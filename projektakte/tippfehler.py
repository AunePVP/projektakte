"""Typing errors in search queries: "Brandschutzkozept", "Branschutzkonzept", "Rettungswek".

A query word that finds (almost) nothing in the full-text index is compared with the vocabulary of the index
(the words that really occur, from FTS5's ``fts5vocab``); the closest frequent word (edit distance 1 from
6 letters, 2 from 9 letters on, swapped neighbours count as one edit) is searched as well.

The vocabulary may hold millions of words, so it is read once in the background (and again after the index
grew, at most every REFRESH_SECONDS) and kept compact in memory: only words of at least MIN_LEN letters that
occur in at least MIN_DOCS sections, at most MAX_TERMS of the most frequent ones, grouped by length, each with
a letter histogram (uint8). A query word is compared only with words whose length differs by at most the
allowed edits and whose histograms differ by at most twice that (one edit changes at most two counts) – a
NumPy comparison of a few thousand rows – and the few left are measured exactly.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass

import numpy as np

from .dokumentarten import osa_distance, typo_limit
from .textutil import QUESTION_WORDS, QUERY_FILLER, STOPWORDS, fold, stem

log = logging.getLogger("projektakte.tippfehler")

MIN_LEN = 6  # shorter words are not corrected (too many neighbours)
MIN_DOCS = 2  # a word must occur in this many sections to be offered as the correction
MAX_TERMS = 400_000  # most frequent words kept in memory (about 15 MB plus the strings)
FEW_HITS = 2  # a query word with at most this many sections counts as (nearly) unknown
MIN_GAIN = 10  # … and is corrected only to a word found this many times more often
REFRESH_SECONDS = 600.0
SYNC_BUILD_CHUNKS = 50_000  # smaller indexes are read at once on the first search, larger in the background
_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
_INDEX = {c: i for i, c in enumerate(_ALPHABET)}
_DIMS = len(_ALPHABET) + 1  # last one: any other character
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
# code point → histogram slot (anything beyond ASCII: the last slot)
_LUT = np.full(129, _DIMS - 1, dtype=np.uint8)
for _c, _i in _INDEX.items():
    _LUT[ord(_c)] = _i


def histogram(word: str) -> np.ndarray:
    h = np.zeros(_DIMS, dtype=np.int16)
    for c in word:
        h[_INDEX.get(c, _DIMS - 1)] += 1
    return h


class Vocabulary:
    """The frequent words of the full-text index, grouped by length (see the module docstring)."""

    def __init__(self, terms: list[tuple[str, int]]):
        by_len: dict[int, list[tuple[str, int]]] = {}
        for term, n in terms:
            by_len.setdefault(len(term), []).append((term, n))
        self.size = len(terms)
        self._groups: dict[int, tuple[list[str], np.ndarray, np.ndarray]] = {}
        for length, items in by_len.items():
            words = [t for t, _ in items]
            # code points of all words of this length at once (UTF-32), mapped to histogram slots
            codes = np.frombuffer("".join(words).encode("utf-32-le"), dtype=np.uint32).reshape(len(words), length)
            slots = _LUT[np.minimum(codes, len(_LUT) - 1)]
            hist = np.zeros((len(words), _DIMS), dtype=np.uint8)
            for d in range(_DIMS):
                hist[:, d] = np.minimum((slots == d).sum(axis=1), 255)
            self._groups[length] = (words, hist, np.array([n for _, n in items], dtype=np.int64))
        self._freq = dict(terms)

    def frequency(self, word: str) -> int:
        return self._freq.get(word, 0)

    def closest(self, word: str, limit: int | None = None) -> list[tuple[str, int, int]]:
        """Words within the allowed edits, best first: (word, edits, sections) by edits, then frequency."""
        limit = typo_limit(word) if limit is None else limit
        if not limit:
            return []
        q = histogram(word)
        out: list[tuple[str, int, int]] = []
        for length in range(len(word) - limit, len(word) + limit + 1):
            group = self._groups.get(length)
            if group is None:
                continue
            words, hist, freq = group
            diff = np.abs(hist.astype(np.int16) - q).sum(axis=1)
            for i in np.nonzero(diff <= 2 * limit)[0]:
                w = words[i]
                if w == word:
                    continue
                d = osa_distance(word, w, limit)
                if d <= limit:
                    out.append((w, d, int(freq[i])))
        out.sort(key=lambda x: (x[1], -x[2], x[0]))
        return out


def read_vocabulary(conn: sqlite3.Connection, *, max_terms: int = MAX_TERMS, min_docs: int = MIN_DOCS) -> Vocabulary:
    """Read the frequent words from the full-text index (fts5vocab, a temporary table of this connection)."""
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.chunks_vocab USING fts5vocab(main, chunks_fts, row)")
    rows = conn.execute("SELECT term, doc FROM temp.chunks_vocab WHERE doc >= ? AND length(term) >= ? "
                        "ORDER BY doc DESC LIMIT ?", (min_docs, MIN_LEN, max_terms)).fetchall()
    return Vocabulary([(t, n) for t, n in rows if not t.isdigit()])


@dataclass
class _Entry:
    vocab: Vocabulary | None = None
    built_at: float = 0.0
    chunks_seen: int = -1
    building: bool = False


class VocabularyCache:
    """One vocabulary per database file, read again in the background when the index changed."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def get(self, conn: sqlite3.Connection) -> Vocabulary | None:
        path = _db_path(conn)
        if path == ":memory:":
            return read_vocabulary(conn)
        marker = _marker(conn)
        with self._lock:
            entry = self._entries.setdefault(path, _Entry())
            fresh = entry.vocab is not None and (entry.chunks_seen == marker
                                                 or time.monotonic() - entry.built_at < REFRESH_SECONDS)
            if fresh or entry.building:
                return entry.vocab
            entry.building = True
            small = marker < SYNC_BUILD_CHUNKS
        if small:
            self._build(path, marker)
            return self._entries[path].vocab
        threading.Thread(target=self._build, args=(path, marker), name="wortschatz", daemon=True).start()
        return entry.vocab

    def _build(self, path: str, marker: int) -> None:
        started = time.monotonic()
        vocab = None
        try:
            conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
            try:
                vocab = read_vocabulary(conn)
            finally:
                conn.close()
            log.info("Wortschatz der Suche gelesen: %d Wörter in %.1f s", vocab.size, time.monotonic() - started)
        except Exception as exc:  # noqa: BLE001 – no corrections rather than a failed search
            log.warning("Wortschatz der Suche nicht lesbar: %s", exc)
        with self._lock:
            entry = self._entries.setdefault(path, _Entry())
            entry.building = False
            if vocab is not None:
                entry.vocab, entry.built_at, entry.chunks_seen = vocab, time.monotonic(), marker


def _db_path(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA database_list").fetchone()
    return str(row[2] or ":memory:")


def _marker(conn: sqlite3.Connection) -> int:
    """Highest section id – grows with every document written (cheap: the rowid's end)."""
    row = conn.execute("SELECT MAX(id) FROM chunks").fetchone()
    return int(row[0] or 0)


VOCABULARY = VocabularyCache()


@dataclass
class Correction:
    typed: str  # the word as typed
    word: str  # the correction as written in the documents ("Brandschutzkonzept")
    unknown: bool  # the typed word finds nothing at all ("Meinten Sie …?") – else only rarely


def _hits(conn: sqlite3.Connection, term: str, limit: int) -> int:
    try:
        return conn.execute("SELECT COUNT(*) FROM (SELECT 1 FROM chunks_fts WHERE chunks_fts MATCH ? LIMIT ?)",
                            (f'"{term}"*', limit)).fetchone()[0]
    except sqlite3.OperationalError:
        return limit


def _as_written(conn: sqlite3.Connection, term: str) -> str:
    """The word as the documents write it (capitals, umlauts): "prufbericht" → "Prüfbericht"."""
    try:
        rows = conn.execute("SELECT c.text FROM chunks_fts f JOIN chunks c ON c.id = f.rowid "
                            "WHERE chunks_fts MATCH ? LIMIT 3", (f'"{term}"',)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for (text,) in rows:
        for w in _WORD.findall(text or ""):
            if fold(w) == term:
                return w
    return term


def corrections(conn: sqlite3.Connection, query: str, *, max_words: int = 4) -> list[Correction]:
    """Corrections for the words of a query that find nothing or nearly nothing (see the module docstring).
    Words with digits, short words, question and filler words are never corrected."""
    out: list[Correction] = []
    vocab: Vocabulary | None = None
    seen: set[str] = set()
    for typed in _WORD.findall(query or ""):
        word = fold(typed)
        if (len(word) < MIN_LEN or any(c.isdigit() for c in word) or word in seen or word in STOPWORDS
                or word in QUESTION_WORDS or word in QUERY_FILLER):
            continue
        seen.add(word)
        found = _hits(conn, stem(word), FEW_HITS + 1)
        if found > FEW_HITS:
            continue
        if vocab is None:
            vocab = VOCABULARY.get(conn)
            if vocab is None:
                return out  # read in the background – corrections from the next search on
        for cand, _, freq in vocab.closest(word):
            if stem(cand) == stem(word) or (found and freq < MIN_GAIN * found):
                continue
            out.append(Correction(typed, _as_written(conn, cand), found == 0))
            break
        if len(out) >= max_words:
            break
    return out


def corrected_query(query: str, found: list[Correction]) -> str:
    """The query with the unknown words replaced by their corrections (for the reranker and the vectors)."""
    for c in found:
        if c.unknown:
            query = re.sub(rf"(?<!\w){re.escape(c.typed)}(?!\w)", c.word, query)
    return query
