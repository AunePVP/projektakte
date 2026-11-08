"""Retrieval: full text (FTS5/BM25) + vectors, fused with Reciprocal Rank Fusion, then reranked."""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import asdict, dataclass, field

from .ai import AIServiceError
from .textutil import QUESTION_WORDS, content_terms
from .vectors import VECTOR_INDEX

log = logging.getLogger("projektakte.search")
RRF_K = 60


def fts_query(text: str, max_terms: int = 24, keep_digits: bool = True) -> str | None:
    """OR-query of stemmed, folded content words with prefix matching ("rettungsweg"*)."""
    terms = []
    for t in content_terms(text, QUESTION_WORDS):
        if t.isdigit() and (not keep_digits or len(t) > 4):
            continue
        if len(t) < 2 and not t.isdigit():
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


# --------------------------------------------------------------------------- hybrid search
CANDIDATES = 50
MAX_PER_SOURCE = 3


@dataclass
class Hit:
    chunk_id: int
    project: str | None
    project_name: str | None
    kind: str  # document | mail | attachment
    title: str  # file name or mail subject
    doc_id: int | None
    mail_id: int | None
    rel_path: str | None
    page: int | None
    heading: str | None
    date: str | None
    sender: str | None
    text: str
    fused: float = 0.0
    rerank: float | None = None

    @property
    def label(self) -> str:
        """Human-readable source: 'Baugenehmigung.pdf, S. 2' or 'Mail vom 18.06.2024: Betreff'."""
        if self.kind == "mail":
            return f"Mail vom {_de_date(self.date)} ({self.sender}): {self.title}"
        base = self.rel_path or self.title
        if self.kind == "attachment":
            base = f"{self.title} (Anhang einer Mail vom {_de_date(self.date)})"
        return f"{base}, S. {self.page}" if self.page else base

    def to_dict(self) -> dict:
        d = asdict(self)
        d["label"] = self.label
        return d


@dataclass
class SearchResult:
    query: str
    project: str | None
    hits: list[Hit] = field(default_factory=list)
    reranked: bool = False
    notes: list[str] = field(default_factory=list)


def _de_date(iso: str | None) -> str:
    if not iso or len(iso) < 10:
        return "?"
    return f"{iso[8:10]}.{iso[5:7]}.{iso[0:4]}"


_HIT_SQL = """
SELECT c.id, c.project, p.name AS project_name, c.doc_id, c.mail_id, c.page, c.heading, c.text,
       d.filename, d.rel_path, d.doc_date, m.subject, m.date AS mail_date, m.sender_name, m.sender
FROM chunks c
LEFT JOIN documents d ON d.id = c.doc_id
LEFT JOIN mails m ON m.id = c.mail_id
LEFT JOIN projects p ON p.number = c.project
WHERE c.id IN ({ids})
"""


def load_hits(conn: sqlite3.Connection, ids: list[int]) -> dict[int, Hit]:
    if not ids:
        return {}
    out = {}
    for r in conn.execute(_HIT_SQL.format(ids=",".join(str(int(i)) for i in ids))):
        if r["doc_id"] and r["mail_id"]:
            kind, title, date = "attachment", r["filename"], r["mail_date"] or r["doc_date"]
        elif r["doc_id"]:
            kind, title, date = "document", r["filename"], r["doc_date"]
        else:
            kind, title, date = "mail", r["subject"], r["mail_date"]
        out[r["id"]] = Hit(
            chunk_id=r["id"], project=r["project"], project_name=r["project_name"], kind=kind,
            title=title or "", doc_id=r["doc_id"], mail_id=r["mail_id"],
            rel_path=r["rel_path"] if kind == "document" else None, page=r["page"], heading=r["heading"],
            date=date, sender=(r["sender_name"] or r["sender"]) if r["mail_id"] else None, text=r["text"])
    return out


def rerank_text(h: Hit) -> str:
    ctx = " | ".join(x for x in (h.title, h.project_name, h.heading) if x)
    return f"{ctx}\n{h.text}"


def hybrid_search(conn: sqlite3.Connection, ai, vector_key: str, query: str, *, project: str | None = None,
                  k: int = 10, rerank_top_k: int = 30, use_reranker: bool = True) -> SearchResult:
    """Full text + vectors (RRF), then the reranker on the best candidates. Falls back gracefully:
    no embeddings → full text only; reranker down → order of the hybrid search."""
    result = SearchResult(query=query, project=project)
    fts = fts_search(conn, query, project=project, k=CANDIDATES)
    vec: list[tuple[int, float]] = []
    if ai is not None:
        try:
            qvec = ai.embed.embed_query(query)
            vec = VECTOR_INDEX.search(conn, vector_key, qvec, project=project, k=CANDIDATES)
        except AIServiceError as exc:
            result.notes.append(f"Vektorsuche nicht verfügbar ({exc}); nur Volltext")
    fused = rrf(fts, vec)
    hits_by_id = load_hits(conn, [cid for cid, _ in fused[: max(rerank_top_k, k) * 2]])
    candidates: list[Hit] = []
    per_source: dict[tuple, int] = {}
    for cid, score in fused:
        h = hits_by_id.get(cid)
        if h is None:
            continue
        key = (h.doc_id, h.mail_id if not h.doc_id else None)
        if per_source.get(key, 0) >= MAX_PER_SOURCE:
            continue
        per_source[key] = per_source.get(key, 0) + 1
        h.fused = score
        candidates.append(h)
        if len(candidates) >= rerank_top_k:
            break
    if candidates and use_reranker and ai is not None:
        try:
            scores = ai.rerank.rerank(query, [rerank_text(h) for h in candidates])
            for h, s in zip(candidates, scores):
                h.rerank = s
            order = sorted(range(len(candidates)), key=lambda i: (-scores[i], i))
            candidates = [candidates[i] for i in order]
            result.reranked = True
        except AIServiceError as exc:
            log.warning("Reranker nicht verfügbar, Reihenfolge der hybriden Suche: %s", exc)
            result.notes.append("Reranker nicht erreichbar – Reihenfolge der hybriden Suche")
    result.hits = candidates[:k]
    return result
