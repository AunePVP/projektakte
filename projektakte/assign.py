"""Assign mails to projects.

1. **Certain** – a known project number in the subject, the body or an attachment file name
   (checked in this order). A number that is not (yet) a project folder never leads to a suggestion
   for another project: the mail is checked again on every run and assigned as soon as the folder
   exists.
2. **Suggestion** – otherwise the mail is used as a search query (full text + vectors) against all
   sections of each active project (project files and mails already assigned for certain). The top
   hits vote for their project. The best project is proposed when it has at least
   ``ASSIGN_MIN_SHARE`` of the votes, leads by ``ASSIGN_MIN_LEAD`` *and* the evidence is substantial
   on its own: enough shared specific words (more for senders without any project history) and, if
   configured, a minimum vector similarity (``ASSIGN_MIN_SIMILARITY``). Newsletters and advertising
   (List-Unsubscribe/Precedence header, mass-mail sender addresses, advertising phrases from unknown
   senders) get no content suggestion. A sender who so far only wrote about one project adds two
   votes. A human confirms or rejects suggestions in the inbox.
3. Human decisions are stored in ``assign_decisions`` per mail source key and per Message-ID
   (key ``msgid:<id>``, so they survive a moved or renamed .eml file); they always win.

A run has two phases with short write transactions: first everything that is certain (cheap), then
the content suggestions against an evidence set computed once per run. The expensive part of a
suggestion only reads, so the inbox never waits long for the database.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field

import numpy as np

from .ai import AIServiceError
from .documents import fts_title
from .mails.store import bulk_markers
from .registry import number_regex
from .search import fts_search, rrf
from .textutil import QUESTION_WORDS, content_terms, fold, stem, term_in
from .vectors import VECTOR_INDEX, from_blob

log = logging.getLogger("projektakte.assign")

INBOX_STATUSES = ("suggested", "unassigned")
# manual mode: mails from plMail wait for a human decision *in plMail* (not in Projektakte's inbox)
PLMAIL_OPEN = "plmail_open"
SENDER_BONUS_VOTES = 2.0 / 61  # worth two top-ranked hits
# Version of the rules below. When it changes, every mail is evaluated once more (new reasons,
# stricter suggestions); human decisions win as always, unchanged results are not written.
ASSIGN_RULES = "2"
COMMIT_EVERY = 50  # mails per write transaction at most …
COMMIT_SECONDS = 0.5  # … and never longer than this
VOTE_HITS = 15
MIN_SHARED_TERMS = 3  # specific words a mail must share with the proposed project's sections
MIN_SHARED_TERMS_NEW_SENDER = 4  # stricter for senders without any project history
CLEAR_LEAD = 0.4  # lead of the best project that is described as "deutlich vor"
SHOW_TERMS = 4
# Words found in more than this share of all sections have no weight in BM25 (FTS5 clamps their
# IDF to ~0) but make every query score nearly all sections – they are left out of the vote.
MAX_TERM_SHARE = 0.5
MIN_SECTIONS_FOR_STATS = 50  # below this, word statistics say nothing

# Words of everyday office mail that do not point to a project (not counted as shared evidence).
GENERIC_WORDS = frozenset(stem(fold(w)) for w in """
    unser unsere unseren ihr ihre ihrer ihren neu neue neuen zweite zweiten monat monate woche wochen
    jahr heute morgen gestern uhr datum termin termine frage fragen anfrage anbei danke dank gruss
    grusse gerne kurz info information informationen mail email betreff anhang anlage nachricht
    montag dienstag mittwoch donnerstag freitag samstag sonntag januar februar marz april mai juni
    juli august september oktober november dezember innerhalb passt bitte zusammen allen moglich
    mochte mochten wurde wurden ware hatte gerne leider bereits schon nochmal dazu darum hierzu deshalb
    aktuell aktuelle aktuellen nachste nachsten letzte letzten wahrend
""".split())
# Sender addresses of mass mailings (local part).
BULK_LOCAL_PARTS = frozenset("""
    newsletter news noreply no-reply no_reply donotreply do-not-reply marketing mailing werbung angebot
    angebote aktion aktionen promo promotion mailer-daemon bounce bounces
""".split())
# Phrases of newsletters and advertising (matched case- and accent-insensitively).
ADVERTISING = ("Rabatt", "Sonderpreis", "Sonderangebot", "Sonderaktion", "Gutschein", "Frühbucher",
               "jetzt bestellen", "jetzt anmelden", "jetzt sichern", "jetzt kaufen", "melden Sie sich jetzt",
               "nur diesen Monat", "nur für kurze Zeit", "Angebot gilt", "in dieser Ausgabe", "Newsletter",
               "abmelden", "abbestellen", "unsubscribe", "Webinar", "gratis", "kostenlos testen")
_ADVERTISING_FOLDED = tuple((fold(p), p) for p in ADVERTISING)
# Freemail domains: a known domain there says nothing about the sender.
FREEMAIL = frozenset("""
    gmail.com googlemail.com gmx.de gmx.net gmx.at gmx.ch web.de t-online.de outlook.com outlook.de
    hotmail.com hotmail.de live.com live.de yahoo.com yahoo.de icloud.com me.com aol.com freenet.de
    posteo.de mailbox.org arcor.de online.de protonmail.com proton.me
""".split())
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


# ------------------------------------------------------------------------------------------------
# project numbers
def known_projects(conn: sqlite3.Connection) -> dict[str, str]:
    """lower-case number → canonical number (active projects)."""
    return {r[0].lower(): r[0] for r in conn.execute("SELECT number FROM projects WHERE active = 1")}


def numbers_in(text: str, rx, known: dict[str, str]) -> tuple[list[str], list[str]]:
    """(known project numbers in order of appearance, unknown numbers)."""
    found, unknown = [], []
    for m in rx.finditer(text or ""):
        n = m.group(0).lower()
        if n in known:
            if known[n] not in found:
                found.append(known[n])
        elif m.group(0).upper() not in unknown:
            unknown.append(m.group(0).upper())
    return found, unknown


def certain_project(subject: str, body: str, attachment_names: list[str], rx, known: dict[str, str]):
    """Returns (project, method, reason) or (None, method, reason) with method "ambiguous",
    "unknown" or None (no number at all)."""
    unknown_all: list[str] = []
    for method, text, label in (("subject", subject, "Betreff"), ("body", body, "Text"),
                                ("attachment", "\n".join(attachment_names), "Anhangnamen")):
        found, unknown = numbers_in(text, rx, known)
        unknown_all += [u for u in unknown if u not in unknown_all]
        if len(found) == 1:
            return found[0], method, f"Projektnummer {found[0]} im {label}."
        if len(found) > 1:
            return None, "ambiguous", f"Mehrere Projektnummern im {label}: {', '.join(found)}."
    if len(unknown_all) == 1:
        return None, "unknown", f"Projektnummer {unknown_all[0]} ist in Projektakte (noch) nicht bekannt."
    if unknown_all:
        return None, "unknown", f"Projektnummern {', '.join(unknown_all)} sind in Projektakte (noch) nicht bekannt."
    return None, None, "Keine Projektnummer in der Mail."


def set_mail_project(conn: sqlite3.Connection, mail_id: int, project: str | None) -> None:
    """Set the project of a mail and everything derived from it (attachments, sections, FTS titles)."""
    conn.execute("UPDATE mails SET project = ? WHERE id = ?", (project, mail_id))
    conn.execute("UPDATE documents SET project = ? WHERE mail_id = ?", (project, mail_id))
    conn.execute("UPDATE chunks SET project = ? WHERE mail_id = ?", (project, mail_id))
    pname = None
    if project:
        r = conn.execute("SELECT name FROM projects WHERE number = ?", (project,)).fetchone()
        pname = r[0] if r else None
    rows = conn.execute(
        "SELECT c.id, c.heading, d.filename, m.sender_name FROM chunks c JOIN mails m ON m.id = c.mail_id "
        "LEFT JOIN documents d ON d.id = c.doc_id WHERE c.mail_id = ?", (mail_id,)).fetchall()
    for r in rows:
        if r["filename"]:
            title = fts_title(r["filename"], project, pname, r["heading"])
        else:
            title = fts_title("Mail", project, pname, r["heading"]) + " " + fold(r["sender_name"] or "")
        conn.execute("UPDATE chunks_fts SET title = ? WHERE rowid = ?", (title, r["id"]))


# ------------------------------------------------------------------------------------------------
# senders: history and newsletters/advertising
def _domain(address: str) -> str:
    local, at, domain = (address or "").rpartition("@")
    return domain.lower() if at and local else ""


class SenderHistory:
    """Projects of earlier mails (assigned for certain or confirmed) per sender address and per
    company domain (freemail domains do not count)."""

    def __init__(self, conn: sqlite3.Connection, only: str | None = None):
        sql = ("SELECT sender, project FROM mails WHERE deleted = 0 AND project IS NOT NULL "
               "AND assign_status IN ('assigned', 'confirmed')")
        args: tuple = ()
        if only is not None:  # just this sender and colleagues from the same company
            only = (only or "").lower()
            dom = _domain(only)
            if dom and dom not in FREEMAIL:
                sql += " AND (sender = ? OR substr(sender, -?) = ?)"
                args = (only, len(dom) + 1, "@" + dom)
            else:
                sql += " AND sender = ?"
                args = (only,)
        self.by_address: dict[str, set[str]] = {}
        self.by_domain: dict[str, set[str]] = {}
        for sender, project in conn.execute(sql, args):
            sender = (sender or "").lower()
            self.by_address.setdefault(sender, set()).add(project)
            dom = _domain(sender)
            if dom and dom not in FREEMAIL:
                self.by_domain.setdefault(dom, set()).add(project)

    def projects(self, address: str) -> set[str]:
        return self.by_address.get((address or "").lower(), set())

    def known(self, address: str) -> bool:
        """Did this sender – or a colleague from the same company – write about a project before?"""
        address = (address or "").lower()
        return bool(self.by_address.get(address)) or bool(self.by_domain.get(_domain(address)))


def _bulk_address(address: str) -> bool:
    local = (address or "").lower().partition("@")[0]
    return local in BULK_LOCAL_PARTS or re.split(r"[.+_-]", local)[0] in BULK_LOCAL_PARTS or \
        local.startswith(("newsletter", "noreply", "no-reply", "donotreply", "do-not-reply"))


def promotion(subject: str, body: str, sender: str, *, bulk: str | None, sender_known: bool) -> str | None:
    """Why a mail looks like a newsletter or advertising (German, for the reason), or None.

    A newsletter header or a mass-mail sender address is enough; advertising phrases count from a
    sender without project history (one phrase) or from anyone (two different phrases)."""
    if bulk:
        return f"Kopfzeile „{bulk}“"
    if _bulk_address(sender):
        return f"Absender {sender}"
    text = fold(f"{subject}\n{body}")
    found = [shown for folded, shown in _ADVERTISING_FOLDED if folded in text]
    if found and (not sender_known or len(found) >= 2):
        return "Wörter wie " + ", ".join(f"„{w}“" for w in found[:2])
    return None


# ------------------------------------------------------------------------------------------------
# content vote
EVIDENCE_SQL = ("c.project IN (SELECT number FROM projects WHERE active = 1) AND (c.mail_id IS NULL OR "
                "c.mail_id IN (SELECT id FROM mails WHERE assign_status IN ('assigned', 'confirmed') "
                "AND id != ?))")


def evidence_chunk_ids(conn: sqlite3.Connection) -> frozenset[int]:
    """Sections that may vote: files of active projects and mails assigned for certain or confirmed.
    Computed once per run (reading all ids per mail made long runs quadratic)."""
    return frozenset(r[0] for r in conn.execute(f"SELECT c.id FROM chunks c WHERE {EVIDENCE_SQL}", (-1,)))


def assignment_text(mail: sqlite3.Row, attachment_names: list[str]) -> str:
    """What a mail is about: subject, body and attachment names (no header boilerplate)."""
    return "\n".join([mail["subject"], mail["body"][:4000], " ".join(attachment_names)])


class TermFrequency:
    """In how many sections a word occurs (full-text index), cached for one run."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        self._sections: dict[str, int] = {}

    def sections(self, term: str) -> int:
        if term not in self._sections:
            expr = f'"{term}"*' if len(term) >= 4 and not term.isdigit() else f'"{term}"'
            try:
                n = self.conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH ?", (expr,)).fetchone()[0]
            except sqlite3.OperationalError:
                n = 0
            self._sections[term] = n
        return self._sections[term]

    def common(self, text: str) -> set[str]:
        """Stems of ``text`` that occur in more than MAX_TERM_SHARE of all sections."""
        if self.total < MIN_SECTIONS_FOR_STATS:
            return set()
        limit = self.total * MAX_TERM_SHARE
        return {t for t in content_terms(text or "", QUESTION_WORDS) if self.sections(t) > limit}


def without_terms(text: str, stems: set[str]) -> str:
    """``text`` without the words whose stem is in ``stems``."""
    if not stems:
        return text
    return " ".join(tok for tok in _TOKEN.findall(text or "") if stem(fold(tok)) not in stems)


def specific_terms(text: str, common: set[str] = frozenset()) -> dict[str, str]:
    """Words of a mail that can identify a project: stem → word as written (first occurrence).
    Stop words, everyday office words, words found almost everywhere (``common``), short words and
    plain numbers are left out."""
    shown: dict[str, str] = {}
    for tok in _TOKEN.findall(text or ""):
        shown.setdefault(stem(fold(tok)), tok)
    out: dict[str, str] = {}
    for t in content_terms(text or "", QUESTION_WORDS):
        if len(t) < 4 or t.isdigit() or t in GENERIC_WORDS or t in common:
            continue
        out[t] = shown.get(t, t)
    return out


@dataclass
class Evidence:
    """Result of the content vote."""
    ranked: list[tuple[str, float]] = field(default_factory=list)  # (project, share of the votes)
    shared: dict[str, list[str]] = field(default_factory=dict)  # project → shared specific words
    similarity: dict[str, float] = field(default_factory=dict)  # project → best vector similarity
    bonus_for: str | None = None  # project that got the sender bonus


def vote_projects(conn: sqlite3.Connection, text: str, qvec, sender_projects: set[str], vector_key: str, *,
                  exclude_mail_id: int = -1, exclude: set[str] = frozenset(),
                  evidence: frozenset[int] | None = None, freq: TermFrequency | None = None) -> Evidence:
    """Treat a text as a search query against everything known about each project: the top hits of
    full-text (BM25) and vector search vote for their project (Reciprocal Rank Fusion).

    ``evidence`` (sections that may vote) and ``freq`` (word statistics) are computed once per run
    by the caller; words found almost everywhere are left out of the full-text query."""
    common = (freq or TermFrequency(conn)).common(text)
    fts = fts_search(conn, without_terms(text, common), k=VOTE_HITS, where=EVIDENCE_SQL, args=(exclude_mail_id,),
                     max_terms=40, keep_digits=False)
    vec = []
    if qvec is not None:
        if evidence is None:
            evidence = evidence_chunk_ids(conn)
        if exclude_mail_id != -1:
            own = {r[0] for r in conn.execute("SELECT id FROM chunks WHERE mail_id = ?", (exclude_mail_id,))}
            evidence = evidence - own if own & evidence else evidence
        if evidence:
            vec = VECTOR_INDEX.search(conn, vector_key, qvec, k=VOTE_HITS, chunk_ids=evidence)
    fused = rrf(fts, vec)
    if not fused:
        return Evidence()
    ids = {c for c, _ in fused}
    rows = conn.execute(f"SELECT id, project, text FROM chunks WHERE id IN ({','.join(map(str, ids))})").fetchall()
    project_of = {r[0]: r[1] for r in rows}
    votes: dict[str, float] = {}
    for cid, score in fused:
        p = project_of.get(cid)
        if p and p not in exclude:
            votes[p] = votes.get(p, 0.0) + score
    hist = sorted(sender_projects)
    bonus_for = hist[0] if len(hist) == 1 and hist[0] not in exclude else None
    if bonus_for:
        votes[bonus_for] = votes.get(bonus_for, 0.0) + SENDER_BONUS_VOTES
    total = sum(votes.values())
    if not total:
        return Evidence()
    ev = Evidence(ranked=[(p, v / total) for p, v in sorted(votes.items(), key=lambda kv: -kv[1])],
                  bonus_for=bonus_for)
    # shared words: what the mail and each project's full-text hits have in common
    text_of = {r[0]: r[2] for r in rows}
    project_terms: dict[str, set[str]] = {}
    for cid, _ in fts:
        p = project_of.get(cid)
        if p:
            project_terms.setdefault(p, set()).update(content_terms(text_of.get(cid, "")))
    mail_terms = specific_terms(text, common)
    for p, terms in project_terms.items():
        ev.shared[p] = [shown for t, shown in mail_terms.items() if term_in(t, terms)]
    for cid, score in vec:
        p = project_of.get(cid)
        if p:
            ev.similarity[p] = max(ev.similarity.get(p, 0.0), float(score))
    return ev


@dataclass
class Suggestion:
    project: str | None
    score: float
    second: str | None
    second_score: float
    reason: str
    shared: list[str] = field(default_factory=list)


def _label(project: str, names: dict[str, str]) -> str:
    name = names.get(project)
    return f"{project} {name}" if name else project


def judge(ev: Evidence, *, min_share: float, min_lead: float, min_similarity: float = 0.0,
          new_sender: bool = False, promo: str | None = None,
          names: dict[str, str] | None = None) -> Suggestion:
    """Decide on a suggestion and explain it in plain German."""
    names = names or {}
    if promo:
        return Suggestion(None, 0.0, None, 0.0, f"Sieht nach Newsletter oder Werbung aus ({promo}) – darum "
                                                "kein inhaltlicher Vorschlag.")
    if not ev.ranked:
        return Suggestion(None, 0.0, None, 0.0, "Keine inhaltlich ähnlichen Unterlagen gefunden.")
    best, s1 = ev.ranked[0]
    second, s2 = ev.ranked[1] if len(ev.ranked) > 1 else (None, 0.0)
    shared = ev.shared.get(best, [])
    need = MIN_SHARED_TERMS_NEW_SENDER if new_sender else MIN_SHARED_TERMS
    if len(shared) < need:
        why = "Kein Vorschlag: zu wenige gemeinsame Fachbegriffe mit den Projektunterlagen"
        why += f" (am ehesten {_label(best, names)})." if shared else "."
        if new_sender:
            why += " Der Absender schrieb bisher zu keinem Projekt."
        return Suggestion(None, s1, second, s2, why, shared)
    if min_similarity and ev.similarity.get(best, 0.0) < min_similarity:
        return Suggestion(None, s1, second, s2, f"Kein Vorschlag: Die inhaltliche Ähnlichkeit zu "
                                                f"{_label(best, names)} ist zu gering.", shared)
    if s1 < min_share or s1 - s2 < min_lead:
        if second and s1 - s2 < min_lead:
            why = (f"Kein eindeutiger Vorschlag: am ähnlichsten {_label(best, names)}, aber {second} "
                   "kommt ebenso in Frage.")
        else:
            why = (f"Kein eindeutiger Vorschlag: am ähnlichsten {_label(best, names)}, aber die "
                   "ähnlichen Unterlagen verteilen sich auf mehrere Projekte.")
        return Suggestion(None, s1, second, s2, why, shared)
    why = f"Inhaltlich am ähnlichsten: {_label(best, names)}"
    if second:
        why += f", {'deutlich ' if s1 - s2 >= CLEAR_LEAD else ''}vor {second}"
    why += f". Gemeinsame Begriffe: {', '.join(shared[:SHOW_TERMS])}."
    if ev.bonus_for == best:
        why += " Der Absender schrieb bisher nur zu diesem Projekt."
    elif new_sender:
        why += " Der Absender schrieb bisher zu keinem Projekt."
    return Suggestion(best, s1, second, s2, why, shared)


def _mail_vector(conn: sqlite3.Connection, mail_id: int):
    """Mean of a mail's body vectors (normalised), or None while they are missing."""
    rows = conn.execute("SELECT embedding FROM chunks WHERE mail_id = ? AND doc_id IS NULL "
                        "AND embedding IS NOT NULL", (mail_id,)).fetchall()
    if not rows:
        return None
    q = np.mean(np.vstack([from_blob(r[0]) for r in rows]), axis=0)
    q /= (np.linalg.norm(q) or 1.0)
    return q


def suggest(conn: sqlite3.Connection, mail: sqlite3.Row, attachment_names: list[str], vector_key: str,
            *, min_share: float, min_lead: float, exclude: set[str], min_similarity: float = 0.0,
            history: SenderHistory | None = None, bulk: str | None = None,
            evidence: frozenset[int] | None = None, names: dict[str, str] | None = None,
            freq: TermFrequency | None = None) -> Suggestion | None:
    """Suggestion for an indexed mail (uses the mail's own vectors). None = no vectors yet."""
    history = history or SenderHistory(conn, only=mail["sender"])
    known = history.known(mail["sender"])
    promo = promotion(mail["subject"], mail["body"], mail["sender"], bulk=bulk, sender_known=known)
    if promo:
        return judge(Evidence(), min_share=min_share, min_lead=min_lead, promo=promo)
    q = _mail_vector(conn, mail["id"])
    if q is None:
        return None  # no vectors yet (embedding service down) – try again later
    ev = vote_projects(conn, assignment_text(mail, attachment_names), q, history.projects(mail["sender"]),
                       vector_key, exclude_mail_id=mail["id"], exclude=exclude, evidence=evidence, freq=freq)
    return judge(ev, min_share=min_share, min_lead=min_lead, min_similarity=min_similarity,
                 new_sender=not known, names=names if names is not None else _names(conn))


def _names(conn: sqlite3.Connection) -> dict[str, str]:
    return dict(conn.execute("SELECT number, name FROM projects WHERE active = 1").fetchall())


def _bulk_from_headers(headers: dict | None) -> str | None:
    """Newsletter header among headers passed by plMail (List-Unsubscribe, Precedence: bulk/junk)."""
    lower = {str(k).strip().lower(): str(v or "").strip() for k, v in (headers or {}).items()}
    if lower.get("list-unsubscribe"):
        return "List-Unsubscribe"
    if lower.get("precedence", "").lower() in ("bulk", "junk"):
        return f"Precedence: {lower['precedence'].lower()}"
    return None


def suggest_for_text(conn: sqlite3.Connection, ai, vector_key: str, pattern: str, *, subject: str, body: str,
                     attachments: list[str], sender: str = "", limit: int = 3, min_share: float = 0.5,
                     min_lead: float = 0.2, min_similarity: float = 0.0,
                     headers: dict | None = None) -> list[dict]:
    """Suggestions for a mail that is not (yet) indexed – used by plMail while a human reads a mail.

    Project numbers in subject, text or attachment names come first ("sicher"), then the content
    vote ("hoch" for a clear suggestion, else "niedrig"). Newsletters and advertising get no content
    suggestions. Nothing is stored and nothing is assigned: the human decides."""
    known = known_projects(conn)
    names = _names(conn)
    rx = number_regex(pattern)
    out: list[dict] = []
    for label, text in (("Betreff", subject), ("Text", body), ("Anhangnamen", "\n".join(attachments))):
        found, _ = numbers_in(text, rx, known)
        for n in found:
            if n not in {o["number"] for o in out}:
                out.append({"number": n, "name": names.get(n, ""), "confidence": "sicher", "score": 1.0,
                            "reason": f"Projektnummer {n} im {label}."})
    sender = (sender or "").strip().lower()
    history = SenderHistory(conn, only=sender)
    if promotion(subject, body, sender, bulk=_bulk_from_headers(headers), sender_known=history.known(sender)):
        return out[:limit]
    text = "\n".join([subject, body[:4000], " ".join(attachments)])
    qvec = None
    if ai is not None:
        try:
            qvec = ai.embed.embed([text])[0]
        except AIServiceError:
            qvec = None  # full text alone still votes
    ev = vote_projects(conn, text, qvec, history.projects(sender), vector_key)
    verdict = judge(ev, min_share=min_share, min_lead=min_lead, min_similarity=min_similarity,
                    new_sender=not history.known(sender), names=names)
    for p, share in ev.ranked:
        if len(out) >= limit:
            break
        shared = ev.shared.get(p, [])
        if p in {o["number"] for o in out} or not shared:
            continue
        if p == verdict.project:
            confidence, reason = "hoch", "Inhaltlich am ähnlichsten"
        else:
            confidence, reason = "niedrig", "Inhaltlich ähnlich, aber nicht eindeutig"
        reason += f". Gemeinsame Begriffe: {', '.join(shared[:SHOW_TERMS])}."
        if ev.bonus_for == p:
            reason += " Der Absender schrieb bisher nur zu diesem Projekt."
        out.append({"number": p, "name": names.get(p, ""), "confidence": confidence,
                    "score": round(share, 4), "reason": reason})
    return out[:limit]


# ------------------------------------------------------------------------------------------------
# decisions
def _external(m: sqlite3.Row) -> dict | None:
    try:
        return json.loads(m["external_assignment"]) if m["external_assignment"] else None
    except (TypeError, ValueError):
        return None


def _msgid_key(message_id: str | None) -> str | None:
    message_id = (message_id or "").strip()
    return f"msgid:{message_id}" if message_id else None


def _decision(conn: sqlite3.Connection, m: sqlite3.Row) -> sqlite3.Row | None:
    """The human decision for a mail: by source key, else by Message-ID (file moved or renamed)."""
    for key in (m["source_key"], _msgid_key(m["message_id"])):
        if key:
            row = conn.execute("SELECT * FROM assign_decisions WHERE source_key = ?", (key,)).fetchone()
            if row:
                return row
    return None


def _rejected(dec: sqlite3.Row | None) -> set[str]:
    return set(filter(None, (dec["rejected"] or "").split(","))) if dec else set()


# ------------------------------------------------------------------------------------------------
# one run
@dataclass
class Outcome:
    """What the assignment of one mail results in (the columns of ``mails`` it sets)."""
    stat: str
    status: str
    method: str | None
    reason: str
    project: str | None = None
    suggested: str | None = None
    score: float | None = None
    suggest_done: int = 1


def _apply(conn: sqlite3.Connection, m: sqlite3.Row, o: Outcome) -> bool:
    """Write an outcome; nothing is written when it equals the stored state. True if changed."""
    score = round(o.score, 4) if o.score is not None else None
    current = (m["project"], m["assign_status"], m["assign_method"], m["assign_reason"], m["suggested_project"],
               m["suggestion_score"], m["suggest_done"])
    if current == (o.project, o.status, o.method, o.reason, o.suggested, score, o.suggest_done):
        return False
    if m["project"] != o.project:
        set_mail_project(conn, m["id"], o.project)
    conn.execute("UPDATE mails SET assign_status=?, assign_method=?, assign_reason=?, suggested_project=?, "
                 "suggestion_score=?, suggest_done=? WHERE id=?",
                 (o.status, o.method, o.reason, o.suggested, score, o.suggest_done, m["id"]))
    return True


def _plmail_outcome(ext: dict, known: dict[str, str]) -> Outcome:
    """The assignment a human made in plMail."""
    who = ext.get("assignedBy") or "Mensch"
    when = (ext.get("assignedAt") or "")[:10]
    suffix = f" am {when[8:10]}.{when[5:7]}.{when[0:4]}" if len(when) == 10 else ""
    if ext.get("status") == "none":
        return Outcome("plmail", "none", "plmail", f"In plMail von {who}{suffix} als ohne Projektbezug markiert.")
    number = known.get(str(ext.get("project") or "").lower())
    if not number:
        return Outcome(PLMAIL_OPEN, PLMAIL_OPEN, "plmail",
                       f"Das in plMail gewählte Projekt {ext.get('project')} ist in Projektakte (noch) nicht "
                       "bekannt. Die Zuordnung wird übernommen, sobald es den Projektordner gibt.")
    return Outcome("plmail", "confirmed", "plmail", f"In plMail von {who}{suffix} zugeordnet.", project=number)


def _decided_reason(action: str, who: str | None) -> str:
    who = who or "Mensch"
    return f"Von {who} zugeordnet." if action == "assign" else f"Von {who} als ohne Projektbezug markiert."


@dataclass
class _Pending:
    """A mail that needs a content suggestion (phase 2)."""
    row: sqlite3.Row
    method: str | None
    reason: str
    names: list[str]
    rejected: set[str]


def _certain(conn: sqlite3.Connection, m: sqlite3.Row, rx, known: dict[str, str], mode: str):
    """Phase 1: everything that needs no content vote. Returns the Outcome, or a _Pending when the
    mail needs a content suggestion (phase 2)."""
    ext = _external(m)
    if ext and ext.get("status") in ("assigned", "none"):
        return _plmail_outcome(ext, known)
    if mode == "manual" and m["source"] == "jmap":
        return Outcome(PLMAIL_OPEN, PLMAIL_OPEN, None, "Wartet auf Zuordnung in plMail.")
    dec = _decision(conn, m)
    if dec and dec["action"] in ("assign", "none"):
        project = dec["project"] if dec["action"] == "assign" else None
        if not project or project in known.values():  # else: project folder gone – back to the inbox
            return Outcome("decided", "confirmed" if project else "none", "manual",
                           _decided_reason(dec["action"], dec["decided_by"]), project=project)
    names = [r[0] for r in conn.execute("SELECT filename FROM documents WHERE mail_id = ?", (m["id"],))]
    project, method, reason = certain_project(m["subject"], m["body"], names, rx, known)
    if project and mode == "manual":  # a number only proposes – a human confirms
        return Outcome("suggested", "suggested", method, f"{reason[:-1]} – bitte bestätigen.",
                       suggested=project, score=1.0)
    if project:
        return Outcome("assigned", "assigned", method, reason, project=project)
    if method == "unknown":  # no suggestion for another project while the number is unknown
        then = "automatisch zugeordnet" if mode != "manual" else "zur Bestätigung vorgeschlagen"
        return Outcome("unassigned", "unassigned", "unknown",
                       f"{reason} Die Mail wird {then}, sobald es den Projektordner gibt – bis dahin "
                       "kein Vorschlag für ein anderes Projekt.")
    return _Pending(m, method, reason, names, _rejected(dec))


class _Commits:
    """Short write transactions: commit after ``every`` mails or ``seconds``, whichever comes first."""

    def __init__(self, conn: sqlite3.Connection, every: int = COMMIT_EVERY, seconds: float = COMMIT_SECONDS):
        self.conn, self.every, self.seconds = conn, every, seconds
        self.pending, self.since = 0, 0.0

    def wrote(self) -> None:
        """Call after each mail whose result was written."""
        if not self.pending:
            self.since = time.monotonic()
        self.pending += 1
        if self.pending >= self.every:
            self.flush()
        self.tick()

    def tick(self) -> None:
        """Call after each mail, written or not: an open transaction never lasts longer than ``seconds``."""
        if self.pending and time.monotonic() - self.since >= self.seconds:
            self.flush()

    def flush(self) -> None:
        if self.conn.in_transaction:
            self.conn.commit()
        self.pending = 0


def _selection(conn: sqlite3.Connection, mode: str) -> str:
    """Which mails this run looks at (SQL condition). After a mode switch or a rule change every
    mail is evaluated again – the stored results stay visible meanwhile and are replaced one by one
    (an unchanged result is not written). An interrupted full pass continues on the next run."""
    from . import db

    if db.get_meta(conn, "assign_mode") != mode or db.get_meta(conn, "assign_rules") != ASSIGN_RULES:
        db.set_meta(conn, "assign_mode", mode)
        db.set_meta(conn, "assign_rules", ASSIGN_RULES)
        db.set_meta(conn, "assign_pass", "all")
        conn.commit()
    if db.get_meta(conn, "assign_pass") == "all":
        return "1 = 1"
    return ("assign_status = 'pending' OR (assign_status IN ('unassigned', 'suggested') AND suggest_done = 0) "
            "OR (assign_status = 'unassigned' AND assign_method = 'unknown') "  # number not known yet
            f"OR (assign_status = '{PLMAIL_OPEN}' AND external_assignment IS NOT NULL)")  # project not known yet


def assign_mails(conn: sqlite3.Connection, pattern: str, *, min_share: float, min_lead: float,
                 vector_key: str, mode: str = "auto", min_similarity: float = 0.0) -> dict:
    """Process new mails and mails still waiting for a decision.

    Order of precedence: assignment made in plMail > decision made in Projektakte > automatic.
    In ``manual`` mode nothing is assigned automatically: mails from plMail wait for a decision in
    plMail, all other mails get suggestions (a project number only proposes) for the inbox.
    ``min_similarity`` is the minimum vector similarity of a content suggestion (0 = off)."""
    from . import db

    where = _selection(conn, mode)
    rx = number_regex(pattern)
    known = known_projects(conn)
    stats = {"assigned": 0, "suggested": 0, "unassigned": 0, "decided": 0, "plmail": 0, PLMAIL_OPEN: 0,
             "unchanged": 0}
    commits = _Commits(conn)
    ids = [r[0] for r in conn.execute(f"SELECT id FROM mails WHERE deleted = 0 AND ({where}) ORDER BY id")]

    def count(m: sqlite3.Row, o: Outcome) -> None:
        if _apply(conn, m, o):
            stats[o.stat] += 1
            commits.wrote()
        else:
            stats["unchanged"] += 1
            commits.tick()

    # phase 1: certain cases (cheap)
    pending: list[_Pending] = []
    for mail_id in ids:
        m = conn.execute("SELECT * FROM mails WHERE id = ? AND deleted = 0", (mail_id,)).fetchone()
        if m is None:
            continue
        result = _certain(conn, m, rx, known, mode)
        if isinstance(result, _Pending):
            pending.append(result)
        else:
            count(m, result)  # e.g. a plMail project that is still unknown: unchanged, nothing written
    commits.flush()

    # phase 2: content suggestions – the expensive part only reads, each result is committed soon
    if pending:
        evidence = evidence_chunk_ids(conn)
        history = SenderHistory(conn)
        bulk = bulk_markers(conn)
        names = _names(conn)
        freq = TermFrequency(conn)
        for p in pending:
            m = conn.execute("SELECT * FROM mails WHERE id = ? AND deleted = 0", (p.row["id"],)).fetchone()
            if m is None or (m["assign_status"], m["assign_reason"]) != (p.row["assign_status"],
                                                                        p.row["assign_reason"]):
                continue  # gone, or a human decided meanwhile (the next run sees the decision)
            commits.flush()  # nothing uncommitted while computing
            sug = suggest(conn, m, p.names, vector_key, min_share=min_share, min_lead=min_lead,
                          exclude=p.rejected, min_similarity=min_similarity, history=history,
                          bulk=bulk.get(m["source_key"]), evidence=evidence, names=names, freq=freq)
            if sug is None:
                o = Outcome("unassigned", "unassigned", p.method,
                            f"{p.reason} Ein inhaltlicher Vorschlag folgt, sobald die Ähnlichkeitssuche für "
                            "diese Mail bereit ist.", suggest_done=0)
            elif sug.project:
                o = Outcome("suggested", "suggested", "similarity", f"{p.reason} {sug.reason}",
                            suggested=sug.project, score=sug.score)
            else:
                o = Outcome("unassigned", "unassigned", p.method, f"{p.reason} {sug.reason}", score=sug.score)
            count(m, o)
    commits.flush()
    if db.get_meta(conn, "assign_pass"):
        conn.execute("DELETE FROM meta WHERE key = 'assign_pass'")
        conn.commit()
    return stats


# ------------------------------------------------------------------------------------------------
# human decisions from the inbox
class DecisionError(ValueError):
    pass


def _store_decision(conn: sqlite3.Connection, key: str, action: str, project: str | None, rejected: set[str],
                    user: str | None) -> None:
    conn.execute(
        "INSERT INTO assign_decisions(source_key, action, project, rejected, decided_by, decided_at) "
        "VALUES(?,?,?,?,?,datetime('now')) ON CONFLICT(source_key) DO UPDATE SET action=excluded.action, "
        "project=excluded.project, rejected=excluded.rejected, decided_by=excluded.decided_by, "
        "decided_at=excluded.decided_at",
        (key, action, project, ",".join(sorted(rejected)), user))


def decide(conn: sqlite3.Connection, mail_id: int, action: str, project: str | None = None,
           user: str | None = None, mode: str = "auto") -> dict:
    """Apply a human decision: confirm | assign | reject | none."""
    m = conn.execute("SELECT * FROM mails WHERE id = ? AND deleted = 0", (mail_id,)).fetchone()
    if not m:
        raise LookupError("Mail nicht gefunden")
    if m["source"] == "jmap" and (mode == "manual" or _external(m)):
        raise DecisionError("Diese Mail wird in plMail zugeordnet – bitte dort beim Lesen der Mail das "
                            "Projekt wählen.")
    known = known_projects(conn)
    rejected = _rejected(_decision(conn, m))
    if action == "confirm":
        if not m["suggested_project"]:
            raise DecisionError("Es gibt keinen Vorschlag zum Bestätigen")
        action, project = "assign", m["suggested_project"]
    if action == "assign":
        canonical = known.get((project or "").lower())
        if not canonical:
            raise DecisionError(f"Unbekanntes Projekt: {project}")
        set_mail_project(conn, mail_id, canonical)
        conn.execute("UPDATE mails SET assign_status='confirmed', assign_method='manual', assign_reason=?, "
                     "suggested_project=NULL, suggestion_score=NULL, suggest_done=1 WHERE id=?",
                     (_decided_reason("assign", user), mail_id))
        stored = ("assign", canonical)
    elif action == "reject":
        if m["suggested_project"]:
            rejected.add(m["suggested_project"])
        set_mail_project(conn, mail_id, None)
        conn.execute("UPDATE mails SET assign_status='unassigned', assign_reason=?, suggested_project=NULL, "
                     "suggestion_score=NULL, suggest_done=1 WHERE id=?",
                     (f"Vorschlag {m['suggested_project']} von {user or 'Mensch'} verworfen.", mail_id))
        stored = ("reject", None)
    elif action == "none":
        set_mail_project(conn, mail_id, None)
        conn.execute("UPDATE mails SET assign_status='none', assign_method='manual', assign_reason=?, "
                     "suggested_project=NULL, suggestion_score=NULL, suggest_done=1 WHERE id=?",
                     (_decided_reason("none", user), mail_id))
        stored = ("none", None)
    else:
        raise DecisionError(f"Unbekannte Aktion: {action}")
    for key in (m["source_key"], _msgid_key(m["message_id"])):
        if key:  # by Message-ID too: the decision survives a moved or renamed .eml file
            _store_decision(conn, key, stored[0], stored[1], rejected, user)
    conn.commit()
    row = conn.execute("SELECT id, project, assign_status, assign_reason FROM mails WHERE id = ?",
                       (mail_id,)).fetchone()
    return dict(row)
