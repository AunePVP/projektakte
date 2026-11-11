"""Retrieval: full text (FTS5/BM25) + vectors, fused with Reciprocal Rank Fusion, then reranked."""
from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import date

import re

from . import db, dokumentarten, tippfehler
from .ai import AIServiceError

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


def _in_projects(project: str | None, projects: Sequence[str] | None) -> tuple[str, list]:
    """The SQL condition limiting sections to a project (``project``) or a scope of several (``projects``,
    see db.project_scope) – empty without either."""
    scope = list(projects) if projects else ([project] if project else [])
    if not scope:
        return "", []
    if len(scope) == 1:
        return " AND c.project = ?", scope
    return f" AND c.project IN ({','.join('?' * len(scope))})", scope


def fts_search(conn: sqlite3.Connection, text: str, *, project: str | None = None, k: int = 50,
               where: str = "", args: tuple = (), max_terms: int = 24,
               keep_digits: bool = True, expand: bool = False,
               synonyms: Synonyms | None = None, projects: Sequence[str] | None = None) -> list[tuple[int, float]]:
    """Chunk ids by BM25 (best first) with score = -bm25 (higher is better). ``projects`` (a scope of
    several numbers) replaces ``project``."""
    q = fts_query(text, max_terms=max_terms, keep_digits=keep_digits, expand=expand, synonyms=synonyms)
    if not q:
        return []
    sql = ("SELECT c.id, bm25(chunks_fts, 2.0, 1.0) AS rank FROM chunks_fts "
           "JOIN chunks c ON c.id = chunks_fts.rowid WHERE chunks_fts MATCH ?")
    params: list = [q]
    cond, scope = _in_projects(project, projects)
    sql += cond
    params += scope
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
                      k: int = 50, projects: Sequence[str] | None = None) -> list[tuple[int, float]]:
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
    cond, scope = _in_projects(project, projects)
    sql += cond
    params += scope
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
    project_kind: str | None = None  # "bauvorhaben" for a site's own files (db.BAUVORHABEN)
    parent: str | None = None  # the site (Bauvorhaben) of the hit's order, if any
    parent_name: str | None = None
    doc_type: str | None = None  # what the document is (dokumentarten.py), if known well enough
    type_match: str | None = None  # "art": of the type the query names, "verwandt": of a related type

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
    doc_types: list[str] = field(default_factory=list)  # document types the query names (ranked first)
    corrections: list[tippfehler.Correction] = field(default_factory=list)  # typing errors searched as corrected


def _de_date(iso: str | None) -> str:
    """TT.MM.JJJJ in local time (Europe/Berlin) – a mail at 00:30 belongs to that day, not the UTC one."""
    return de_date(iso)


_HIT_SQL = """
SELECT c.id, c.project, p.name AS project_name, c.doc_id, c.mail_id, c.page, c.heading, c.text, c.ord,
       d.filename, d.rel_path, d.doc_date, m.subject, m.date AS mail_date, m.sender_name, m.sender,
       p.kind AS project_kind, p.parent AS parent, pp.name AS parent_name,
       d.doc_type, d.doc_type_confidence
FROM chunks c
LEFT JOIN documents d ON d.id = c.doc_id
LEFT JOIN mails m ON m.id = c.mail_id
LEFT JOIN projects p ON p.number = c.project
LEFT JOIN projects pp ON pp.number = p.parent
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
            ord=r["ord"], project_kind=r["project_kind"], parent=r["parent"], parent_name=r["parent_name"],
            doc_type=r["doc_type"] if (r["doc_type_confidence"] or 0) >= dokumentarten.MIN_RANKED else None)
    return out


def rerank_text(h: Hit) -> str:
    ctx = " | ".join(x for x in (h.title, h.project_name, h.parent_name, h.heading) if x)
    return f"{ctx}\n{h.text}"


def default_synonyms_file() -> str:
    """SYNONYMS_FILE from the settings (callers may pass it explicitly instead)."""
    from .config import load_settings

    return getattr(load_settings(), "synonyms_file", "") or ""


MIN_CALL_SECONDS = 1.0  # less time left than this: skip the AI call instead of starting it
# notes when the question's time budget (ASK_DEADLINE) cut a step short – ask.py merges them into one
BUDGET_VECTOR_NOTE = "Zeitbudget der Frage erschöpft – nur Volltextsuche"
BUDGET_RERANK_NOTE = "Zeitbudget der Frage erschöpft – Reihenfolge der hybriden Suche"


def time_left(deadline: float | None, timeout: float) -> float | None:
    """Timeout for the next AI call: the client's own (None) without a deadline, otherwise at most the
    remaining time, 0 when too little is left to be worth a call."""
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining < MIN_CALL_SECONDS:
        return 0
    return min(timeout, remaining)


def _cut_by_budget(exc: Exception, used: float | None, own: float) -> bool:
    """Did a call time out because the question's budget shortened its timeout (not the service's own)?"""
    return used is not None and used < own and "Zeitüberschreitung" in str(exc)


TYPE_CANDIDATES = 6  # sections of documents of the named type added to the reranker candidates (questions)
# how the document types a query names are used (hybrid_search ``doc_types``)
TYPES_TOP = "top"  # search page: documents of the type first, newest first, then everything else
TYPES_CANDIDATES = "candidates"  # questions: their best sections join the reranker candidates
TYPES_OFF = "off"


def hybrid_search(conn: sqlite3.Connection, ai, vector_key: str, query: str, *, project: str | None = None,
                  k: int = 10, rerank_top_k: int = 30, use_reranker: bool = True,
                  synonyms_file: str | None = None, deadline: float | None = None,
                  doc_types: str = TYPES_OFF, typos: bool = True) -> SearchResult:
    """Full text + vectors (RRF), then the reranker on the best candidates. Falls back gracefully:
    no embeddings → full text only; reranker down → order of the hybrid search.

    The full-text part expands abbreviations and synonyms (built-in list plus ``synonyms_file``,
    default: setting SYNONYMS_FILE). Reference codes in the query (project number, file number,
    invoice number) are also searched literally; those hits always reach the reranker, and for a query
    of reference codes alone they come first (a cross-encoder judges topics, not codes).

    ``typos``: a word that finds (nearly) nothing is also searched as the closest frequent word of the index
    (tippfehler.py, ``result.corrections``); the reranker and the vectors get the corrected query.
    ``doc_types``: a document type named in the query ("Brandschutzkonzepte", "BS-Konzept",
    "Brandschutzkozept") – TYPES_TOP ranks the documents of that type first (newest first; with further words
    those matching them first), then those of related types, then everything else; nothing is left out.
    TYPES_CANDIDATES only lets their best sections reach the reranker (questions).

    ``deadline`` (``time.monotonic()`` value): the AI calls get at most the remaining time; without
    time left the search falls back to full text / hybrid order instead of waiting."""
    result = SearchResult(query=query, project=project)
    # a site (Bauvorhaben) covers its orders, an order only itself (db.project_scope)
    scope = db.project_scope(conn, project) if project else None
    synonyms = load_synonyms(default_synonyms_file() if synonyms_file is None else synonyms_file)
    fts_text = semantic = query
    if typos:
        result.corrections = tippfehler.corrections(conn, query)
        fts_text = " ".join([query, *(c.word for c in result.corrections)])
        semantic = tippfehler.corrected_query(query, result.corrections)
    tq = dokumentarten.query_types(conn, semantic, synonyms) if doc_types != TYPES_OFF else None
    if tq:
        result.doc_types = tq.names
    identifiers = find_identifiers(query)
    literal = identifier_search(conn, identifiers, projects=scope, k=CANDIDATES) if identifiers else []
    codes_only = bool(identifiers) and not query_terms(strip_identifiers(query))
    fts = fts_search(conn, fts_text, projects=scope, k=CANDIDATES, expand=True, synonyms=synonyms)
    vec: list[tuple[int, float]] = []
    embed_timeout = time_left(deadline, ai.embed.timeout) if ai is not None else None
    if ai is not None and embed_timeout == 0:
        result.notes.append(BUDGET_VECTOR_NOTE)
    elif ai is not None:
        try:
            qvec = ai.embed.embed_query(semantic, timeout=embed_timeout)
            vec = VECTOR_INDEX.search(conn, vector_key, qvec, projects=scope, k=CANDIDATES)
        except AIServiceError as exc:
            log.warning("Vektorsuche nicht verfügbar, nur Volltext: %s", exc)
            result.notes.append(BUDGET_VECTOR_NOTE if _cut_by_budget(exc, embed_timeout, ai.embed.timeout)
                                else "Vektorsuche nicht verfügbar – nur Volltextsuche")
    fused = rrf(literal, fts, vec)
    if literal:  # literal hits of a reference code first, so they always reach the reranker
        first = {cid for cid, _ in literal}
        fused = [x for x in fused if x[0] in first] + [x for x in fused if x[0] not in first]
    typed = type_ranking(conn, tq, scope, synonyms, limit=max(k, TYPE_CANDIDATES)) if tq else []
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
    if typed and doc_types == TYPES_CANDIDATES:  # their best sections are judged by the reranker as well
        have = {h.chunk_id for h in candidates}
        extra = [t.chunk_id for t in typed if t.matches and t.chunk_id not in have][:TYPE_CANDIDATES]
        extra_hits = load_hits(conn, extra)
        candidates += [extra_hits[c] for c in extra if c in extra_hits]
    rerank_timeout = time_left(deadline, ai.rerank.timeout) if ai is not None else None
    if candidates and use_reranker and ai is not None and rerank_timeout == 0:
        result.notes.append(BUDGET_RERANK_NOTE)
    elif candidates and use_reranker and ai is not None:
        try:
            scores = ai.rerank.rerank(semantic, [rerank_text(h) for h in candidates], timeout=rerank_timeout)
            for h, s in zip(candidates, scores):
                h.rerank = s
            order = sorted(range(len(candidates)), key=lambda i: (-scores[i], i))
            candidates = [candidates[i] for i in order]
            result.reranked = True
        except AIServiceError as exc:
            log.warning("Reranker nicht verfügbar, Reihenfolge der hybriden Suche: %s", exc)
            result.notes.append(BUDGET_RERANK_NOTE if _cut_by_budget(exc, rerank_timeout, ai.rerank.timeout)
                                else "Reranker nicht erreichbar – Reihenfolge der hybriden Suche")
    if codes_only:  # "BS-2024-0233", "63-0137/24": the documents that contain the code first
        candidates.sort(key=lambda h: not has_identifier(h, identifiers))
    hits = _cap_per_source(candidates, MAX_PER_SOURCE)
    if typed and doc_types == TYPES_TOP:
        hits = _types_first(conn, hits, typed)
    result.hits = hits[:k]
    return result


@dataclass
class TypedDocument:
    """A document of a type the query names (search.type_ranking)."""

    doc_id: int
    chunk_id: int  # its best section for the other words of the query, else its beginning
    match: str  # "art" (the named type) or "verwandt" (a related type)
    matches: bool  # the other words of the query occur in it (or there are none)


def type_ranking(conn: sqlite3.Connection, tq: "dokumentarten.TypeQuery", scope: list[str] | None,
                 synonyms: Synonyms | None, *, limit: int = 20) -> list[TypedDocument]:
    """The documents of the types a query names, in the order they are shown first: the named type before
    related ones, those matching the other words of the query before the rest, newest first within."""
    exact = dokumentarten.type_documents(conn, tq.exact, projects=scope, limit=limit)
    seen = {d[0] for d in exact}
    related = [d for d in dokumentarten.type_documents(conn, tq.related, projects=scope, limit=limit)
               if d[0] not in seen] if tq.related else []
    docs = [(d, "art") for d in exact] + [(d, "verwandt") for d in related]
    if not docs:
        return []
    best: dict[int, int] = {}
    rest = fts_query(tq.rest, expand=True, synonyms=synonyms) if query_terms(tq.rest, synonyms) else None
    if rest:
        ids = ",".join(str(int(d[0])) for d, _ in docs)
        cond, args = _in_projects(None, scope)
        try:
            for cid, doc_id in conn.execute(
                    "SELECT c.id, c.doc_id FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid "
                    f"WHERE chunks_fts MATCH ? AND c.doc_id IN ({ids}){cond} "
                    "ORDER BY bm25(chunks_fts, 2.0, 1.0) LIMIT 500", [rest, *args]):
                best.setdefault(doc_id, cid)
        except sqlite3.OperationalError:
            pass
    out = [TypedDocument(d[0], best.get(d[0], d[1]), how, rest is None or d[0] in best) for d, how in docs]
    # high-confidence types before weak guesses (a type from the file name alone), stable otherwise
    conf = {d[0]: d[3] for d, _ in docs}
    out.sort(key=lambda t: (not t.matches, t.match != "art", conf[t.doc_id] < dokumentarten.CLEAR))
    return out


def _types_first(conn: sqlite3.Connection, hits: list[Hit], typed: list[TypedDocument]) -> list[Hit]:
    """The documents of the named type first (search page), then the other hits in their order, then the
    documents of the type the other words of the query do not occur in. One hit per typed document."""
    by_chunk = {h.chunk_id: h for h in hits}
    missing = [t.chunk_id for t in typed if t.chunk_id not in by_chunk]
    loaded = load_hits(conn, missing) if missing else {}
    placed: set[int] = set()
    first: list[Hit] = []
    last: list[Hit] = []
    other_docs = {h.doc_id for h in hits}
    for t in typed:
        h = by_chunk.get(t.chunk_id) or loaded.get(t.chunk_id)
        if h is None:
            continue
        h.type_match = t.match
        if t.matches:
            first.append(h)
            placed.add(t.doc_id)
        elif t.doc_id not in other_docs:
            last.append(h)
    how = {t.doc_id: t.match for t in typed}
    middle = [h for h in hits if h.doc_id not in placed or h.doc_id is None]
    for h in middle:
        if h.doc_id in how:
            h.type_match = how[h.doc_id]
    return first + middle + last


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
