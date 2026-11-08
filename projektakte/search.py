"""Retrieval building blocks: full-text (FTS5/BM25), vector search and Reciprocal Rank Fusion."""
from __future__ import annotations

import sqlite3

from .textutil import QUESTION_WORDS, content_terms

RRF_K = 60


def fts_query(text: str, max_terms: int = 24, keep_digits: bool = True) -> str | None:
    """OR-query of stemmed, folded content words with prefix matching ("rettungsweg"*)."""
    terms = []
    for t in content_terms(text, QUESTION_WORDS):
        if t.isdigit() and (not keep_digits or len(t) > 4):
            continue
        if len(t) < 2:
            continue
        terms.append(t)
        if len(terms) >= max_terms:
            break
    if not terms:
        return None
    return " OR ".join(f'"{t}"*' if len(t) >= 4 and not t.isdigit() else f'"{t}"' for t in terms)


def fts_search(conn: sqlite3.Connection, text: str, *, project: str | None = None, k: int = 50,
               where: str = "", args: tuple = (), max_terms: int = 24,
               keep_digits: bool = True) -> list[tuple[int, float]]:
    """Chunk ids by BM25 (best first) with score = -bm25 (higher is better)."""
    q = fts_query(text, max_terms=max_terms, keep_digits=keep_digits)
    if not q:
        return []
    sql = ("SELECT c.id, bm25(chunks_fts, 2.0, 1.0) AS rank FROM chunks_fts "
           "JOIN chunks c ON c.id = chunks_fts.rowid WHERE chunks_fts MATCH ?")
    params: list = [q]
    if project:
        sql += " AND c.project = ?"
        params.append(project)
    if where:
        sql += f" AND {where}"
        params += list(args)
    sql += " ORDER BY rank LIMIT ?"
    params.append(k)
    try:
        return [(r[0], -float(r[1])) for r in conn.execute(sql, params)]
    except sqlite3.OperationalError:
        return []


def rrf(*rankings: list[tuple[int, float]], k: int = RRF_K) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion of several rankings of chunk ids."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, (cid, _) in enumerate(ranking):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
