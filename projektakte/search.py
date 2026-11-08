"""Retrieval: full text (FTS5/BM25) + vectors, fused with Reciprocal Rank Fusion, then reranked."""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date

from .ai import AIServiceError
import re

from .textutil import (
    QUESTION_WORDS,
    Synonyms,
    content_terms,
    de_date,
    find_identifiers,
    fold,
    identifier_in,
    load_synonyms,
    local_date,
    query_terms,
    strip_identifiers,
)
from .vectors import VECTOR_INDEX

log = logging.getLogger("projektakte.search")
RRF_K = 60
MAX_ALTERNATIVES = 8  # per question word: word forms, abbreviations, synonyms


def _fts_token(t: str) -> str:
    return f'"{t}"*' if len(t) >= 4 and not t.isdigit() else f'"{t}"'


def fts_query(text: str, max_terms: int = 24, keep_digits: bool = True, *, expand: bool = False,
              synonyms: Synonyms | None = None) -> str | None:
    """OR-query of stemmed, folded content words with prefix matching ("rettungsweg"*).

    With ``expand`` (questions typed by people) filler words are dropped and every word also matches
    its other word forms (Abnahme ↔ abgenommen) and synonyms/abbreviations (BMA ↔ Brandmeldeanlage,
    Treppenhaus ↔ Treppenraum); phrases become AND groups.
    """
    if expand:
        return _expanded_query(text, max_terms, keep_digits, synonyms)
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
    return " OR ".join(_fts_token(t) for t in terms)


def _expanded_query(text: str, max_terms: int, keep_digits: bool, synonyms: Synonyms | None) -> str | None:
    parts: dict[str, None] = {}
    n = 0
    for qt in query_terms(text, synonyms):
        t = qt.term
        if t.isdigit() and (not keep_digits or len(t) > 4):
            continue
        parts.setdefault(_fts_token(t), None)
        extra = 0
        for alt in qt.alts:  # synonyms first, then word forms
            if extra >= MAX_ALTERNATIVES:
                break
            if any(len(x) < 3 and not x.isdigit() for x in alt):
                continue
            if len(alt) == 1:
                parts.setdefault(_fts_token(alt[0]), None)
            else:
                parts.setdefault("(" + " AND ".join(_fts_token(x) for x in alt) + ")", None)
            extra += 1
        n += 1
        if n >= max_terms:
            break
    return " OR ".join(parts) if parts else None


def fts_search(conn: sqlite3.Connection, text: str, *, project: str | None = None, k: int = 50,
               where: str = "", args: tuple = (), max_terms: int = 24,
               keep_digits: bool = True, expand: bool = False,
               synonyms: Synonyms | None = None) -> list[tuple[int, float]]:
    """Chunk ids by BM25 (best first) with score = -bm25 (higher is better)."""
    q = fts_query(text, max_terms=max_terms, keep_digits=keep_digits, expand=expand, synonyms=synonyms)
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


def identifier_search(conn: sqlite3.Connection, identifiers: list[str], *, project: str | None = None,
                      k: int = 50) -> list[tuple[int, float]]:
    """Chunks that contain a reference code literally ("BS-2024-0233", "63-0137/24"): an FTS phrase of
    its parts, so the separators may be written differently."""
    phrases = []
    for ident in identifiers:
        parts = re.findall(r"[^\W_]+", fold(ident))
        if parts:
            phrases.append('"' + " ".join(parts) + '"')
    if not phrases:
        return []
    sql = ("SELECT c.id, bm25(chunks_fts, 2.0, 1.0) AS rank FROM chunks_fts "
           "JOIN chunks c ON c.id = chunks_fts.rowid WHERE chunks_fts MATCH ?")
    params: list = [" OR ".join(phrases)]
    if project:
        sql += " AND c.project = ?"
        params.append(project)
    sql += " ORDER BY rank LIMIT ?"
    params.append(k)
    try:
        return [(r[0], -float(r[1])) for r in conn.execute(sql, params)]
    except sqlite3.OperationalError:
        return []


def has_identifier(h: "Hit", identifiers: list[str]) -> bool:
    """Does the hit (text, file name or mail subject) contain one of the reference codes literally?"""
    return any(identifier_in(i, f"{h.title}\n{h.rel_path or ''}\n{h.text}") for i in identifiers)


def rrf(*rankings: list[tuple[int, float]], k: int = RRF_K) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion of several rankings of chunk ids."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, (cid, _) in enumerate(ranking):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


# --------------------------------------------------------------------------- hybrid search
CANDIDATES = 50
MAX_PER_SOURCE = 3  # chunks of one document page or mail among the results
MAX_PER_SOURCE_RERANK = 6  # … among the reranker candidates (the fused order is only a rough guess)


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
    ord: int | None = None  # position of the chunk inside its document/mail

    @property
    def local_date(self) -> date | None:
        """Date of the source in local time (mails are stored in UTC)."""
        return local_date(self.date)

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
    """TT.MM.JJJJ in local time (Europe/Berlin) – a mail at 00:30 belongs to that day, not the UTC one."""
    return de_date(iso)


_HIT_SQL = """
SELECT c.id, c.project, p.name AS project_name, c.doc_id, c.mail_id, c.page, c.heading, c.text, c.ord,
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
            date=date, sender=(r["sender_name"] or r["sender"]) if r["mail_id"] else None, text=r["text"],
            ord=r["ord"])
    return out


def rerank_text(h: Hit) -> str:
    ctx = " | ".join(x for x in (h.title, h.project_name, h.heading) if x)
    return f"{ctx}\n{h.text}"


def default_synonyms_file() -> str:
    """SYNONYMS_FILE from the settings (callers may pass it explicitly instead)."""
    from .config import load_settings

    return getattr(load_settings(), "synonyms_file", "") or ""


def hybrid_search(conn: sqlite3.Connection, ai, vector_key: str, query: str, *, project: str | None = None,
                  k: int = 10, rerank_top_k: int = 30, use_reranker: bool = True,
                  synonyms_file: str | None = None) -> SearchResult:
    """Full text + vectors (RRF), then the reranker on the best candidates. Falls back gracefully:
    no embeddings → full text only; reranker down → order of the hybrid search.

    The full-text part expands abbreviations and synonyms (built-in list plus ``synonyms_file``,
    default: setting SYNONYMS_FILE). Reference codes in the query (project number, file number,
    invoice number) are also searched literally; those hits always reach the reranker, and for a query
    of reference codes alone they come first (a cross-encoder judges topics, not codes)."""
    result = SearchResult(query=query, project=project)
    synonyms = load_synonyms(default_synonyms_file() if synonyms_file is None else synonyms_file)
    identifiers = find_identifiers(query)
    literal = identifier_search(conn, identifiers, project=project, k=CANDIDATES) if identifiers else []
    codes_only = bool(identifiers) and not query_terms(strip_identifiers(query))
    fts = fts_search(conn, query, project=project, k=CANDIDATES, expand=True, synonyms=synonyms)
    vec: list[tuple[int, float]] = []
    if ai is not None:
        try:
            qvec = ai.embed.embed_query(query)
            vec = VECTOR_INDEX.search(conn, vector_key, qvec, project=project, k=CANDIDATES)
        except AIServiceError as exc:
            log.warning("Vektorsuche nicht verfügbar, nur Volltext: %s", exc)
            result.notes.append("Vektorsuche nicht verfügbar – nur Volltextsuche")
    fused = rrf(literal, fts, vec)
    if literal:  # literal hits of a reference code first, so they always reach the reranker
        first = {cid for cid, _ in literal}
        fused = [x for x in fused if x[0] in first] + [x for x in fused if x[0] not in first]
    hits_by_id = load_hits(conn, [cid for cid, _ in fused[: max(rerank_top_k, k) * 2]])
    candidates: list[Hit] = []
    per_source: dict[tuple, int] = {}
    for cid, score in fused:
        h = hits_by_id.get(cid)
        if h is None:
            continue
        key = (h.doc_id, h.mail_id if not h.doc_id else None)
        if per_source.get(key, 0) >= (MAX_PER_SOURCE_RERANK if use_reranker and ai is not None else MAX_PER_SOURCE):
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
    if codes_only:  # "BS-2024-0233", "63-0137/24": the documents that contain the code first
        candidates.sort(key=lambda h: not has_identifier(h, identifiers))
    result.hits = _cap_per_source(candidates, MAX_PER_SOURCE)[:k]
    return result


def _cap_per_source(hits: list[Hit], cap: int) -> list[Hit]:
    out, seen = [], {}
    for h in hits:
        key = (h.doc_id, h.mail_id if not h.doc_id else None)
        if seen.get(key, 0) < cap:
            seen[key] = seen.get(key, 0) + 1
            out.append(h)
    return out


# --------------------------------------------------------------------------- overlapping chunks
def _source_key(h: Hit) -> tuple:
    return ("doc", h.doc_id, h.page) if h.doc_id else ("mail", h.mail_id, None)


def _join_texts(first: str, second: str) -> str:
    """Concatenate two neighbouring chunks, dropping the paragraph(s) the second one repeats."""
    a, b = first.split("\n"), second.split("\n")
    for k in range(min(len(a), len(b)), 0, -1):
        if a[-k:] == b[:k]:
            return "\n".join(a + b[k:])
    return first + "\n" + second


def merge_adjacent(hits: list[Hit]) -> list[Hit]:
    """Join neighbouring chunks of the same document page or mail into one hit.

    Chunks overlap (the last paragraph of one starts the next), so numbered conditions often appear
    in two hits with the same label. The joined hit keeps the rank, id and scores of the better
    one; a hit whose text is contained in an earlier one is dropped.
    """
    out: list[Hit] = []
    spans: list[tuple[int, int] | None] = []
    for h in hits:
        for i, o in enumerate(out):
            if _source_key(o) != _source_key(h):
                continue
            if h.text.strip() in o.text:
                break
            span = spans[i]
            if span is None or h.ord is None or not (span[0] - 1 <= h.ord <= span[1] + 1):
                continue
            before = h.ord < span[0]
            text = _join_texts(h.text, o.text) if before else _join_texts(o.text, h.text)
            out[i] = Hit(**{**asdict(o), "text": text, "ord": min(span[0], h.ord),
                            "rerank": max((x for x in (o.rerank, h.rerank) if x is not None), default=None),
                            "fused": max(o.fused, h.fused)})
            spans[i] = (min(span[0], h.ord), max(span[1], h.ord))
            break
        else:
            out.append(h)
            spans.append((h.ord, h.ord) if h.ord is not None else None)
    return out
