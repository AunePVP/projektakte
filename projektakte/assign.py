"""Assign mails to projects.

1. **Certain** – a known project number in the subject, the body or an attachment file name
   (checked in this order). Unknown numbers do not count.
2. **Suggestion** – otherwise the mail is used as a search query (full text + vectors) against all
   sections of each project (project files and mails already assigned for certain). The top hits
   vote for their project; if the best project has at least ``ASSIGN_MIN_SHARE`` of the votes, leads
   by ``ASSIGN_MIN_LEAD`` and shares words with the mail, it is proposed. A human confirms or rejects
   it in the assignment inbox. A sender who so far only wrote about one project adds two votes.
3. Human decisions are stored per mail source key in ``assign_decisions`` and always win.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass

import numpy as np

from .ai import AIServiceError
from .documents import fts_title
from .registry import number_regex
from .search import fts_search, rrf
from .textutil import fold
from .vectors import VECTOR_INDEX, from_blob

log = logging.getLogger("projektakte.assign")

INBOX_STATUSES = ("suggested", "unassigned")
# manual mode: mails from plMail wait for a human decision *in plMail* (not in Projektakte's inbox)
PLMAIL_OPEN = "plmail_open"
SENDER_BONUS_VOTES = 2.0 / 61  # worth two top-ranked hits


@dataclass
class Suggestion:
    project: str | None
    score: float
    second: str | None
    second_score: float
    reason: str


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
    """Returns (project, method, reason) or (None, None, reason)."""
    unknown_all: list[str] = []
    for method, text, label in (("subject", subject, "Betreff"), ("body", body, "Text"),
                                ("attachment", "\n".join(attachment_names), "Anhangname")):
        found, unknown = numbers_in(text, rx, known)
        unknown_all += [u for u in unknown if u not in unknown_all]
        if len(found) == 1:
            return found[0], method, f"Projektnummer {found[0]} im {label}"
        if len(found) > 1:
            return None, "ambiguous", f"mehrere Projektnummern im {label}: {', '.join(found)}"
    if unknown_all:
        return None, "unknown", f"unbekannte Projektnummer {', '.join(unknown_all)}"
    return None, None, "keine Projektnummer gefunden"


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


EVIDENCE_SQL = ("c.project IS NOT NULL AND (c.mail_id IS NULL OR c.mail_id IN "
                "(SELECT id FROM mails WHERE assign_status IN ('assigned', 'confirmed') AND id != ?))")
VOTE_HITS = 15


def assignment_text(mail: sqlite3.Row, attachment_names: list[str]) -> str:
    """What a mail is about: subject, body and attachment names (no header boilerplate)."""
    return "\n".join([mail["subject"], mail["body"][:4000], " ".join(attachment_names)])


def vote_projects(conn: sqlite3.Connection, text: str, qvec, sender: str, vector_key: str, *,
                  exclude_mail_id: int = -1, exclude: set[str] = frozenset()):
    """Treat a text as a search query against everything known about each project (project files and
    mails assigned for certain): the top hits of full-text (BM25) and vector search vote for their
    project (Reciprocal Rank Fusion). Returns (ranking [(project, share)], projects with shared words,
    project that got the sender bonus)."""
    fts = fts_search(conn, text, k=VOTE_HITS, where=EVIDENCE_SQL, args=(exclude_mail_id,), max_terms=40,
                     keep_digits=False)
    vec = []
    if qvec is not None:
        evidence = {r[0] for r in conn.execute(f"SELECT c.id FROM chunks c WHERE {EVIDENCE_SQL}",
                                               (exclude_mail_id,))}
        if evidence:
            vec = VECTOR_INDEX.search(conn, vector_key, qvec, k=VOTE_HITS, chunk_ids=evidence)
    fused = rrf(fts, vec)
    if not fused:
        return [], set(), None
    project_of = dict(conn.execute(
        f"SELECT id, project FROM chunks WHERE id IN ({','.join(str(c) for c, _ in fused)})").fetchall())
    votes: dict[str, float] = {}
    for cid, score in fused:
        p = project_of.get(cid)
        if p and p not in exclude:
            votes[p] = votes.get(p, 0.0) + score
    lexical = {project_of.get(cid) for cid, _ in fts}
    hist = [r[0] for r in conn.execute(
        "SELECT DISTINCT project FROM mails WHERE sender = ? AND id != ? AND project IS NOT NULL "
        "AND assign_status IN ('assigned', 'confirmed')", (sender or "", exclude_mail_id))] if sender else []
    bonus_for = hist[0] if len(hist) == 1 and hist[0] not in exclude else None
    if bonus_for:
        votes[bonus_for] = votes.get(bonus_for, 0.0) + SENDER_BONUS_VOTES
    total = sum(votes.values())
    if not total:
        return [], lexical, None
    ranked = [(p, v / total) for p, v in sorted(votes.items(), key=lambda kv: -kv[1])]
    return ranked, lexical, bonus_for


def _judge(ranked, lexical, bonus_for, min_share: float, min_lead: float) -> Suggestion:
    if not ranked:
        return Suggestion(None, 0.0, None, 0.0, "keine ähnlichen Unterlagen gefunden")
    best, s1 = ranked[0]
    second, s2 = ranked[1] if len(ranked) > 1 else (None, 0.0)
    detail = f"{best} mit {s1:.0%} der ähnlichsten Abschnitte"
    if second:
        detail += f" (danach {second} mit {s2:.0%})"
    if bonus_for == best:
        detail += "; Absender schrieb bisher nur zu diesem Projekt"
    if best not in lexical:
        return Suggestion(None, s1, second, s2, f"kein eindeutiger Vorschlag: {detail}, "
                                                 "aber keine gemeinsamen Begriffe")
    if s1 >= min_share and s1 - s2 >= min_lead:
        return Suggestion(best, s1, second, s2, detail)
    return Suggestion(None, s1, second, s2, f"kein eindeutiger Vorschlag: {detail}")


def suggest(conn: sqlite3.Connection, mail: sqlite3.Row, attachment_names: list[str], vector_key: str,
            *, min_share: float, min_lead: float, exclude: set[str]) -> Suggestion | None:
    """Suggestion for an indexed mail (uses the mail's own vectors). None = no vectors yet."""
    vec_rows = conn.execute("SELECT embedding FROM chunks WHERE mail_id = ? AND doc_id IS NULL "
                            "AND embedding IS NOT NULL", (mail["id"],)).fetchall()
    if not vec_rows:
        return None  # no vectors yet (embedding service down) – try again later
    q = np.mean(np.vstack([from_blob(r[0]) for r in vec_rows]), axis=0)
    q /= (np.linalg.norm(q) or 1.0)
    ranked, lexical, bonus = vote_projects(conn, assignment_text(mail, attachment_names), q, mail["sender"],
                                           vector_key, exclude_mail_id=mail["id"], exclude=exclude)
    return _judge(ranked, lexical, bonus, min_share, min_lead)


def suggest_for_text(conn: sqlite3.Connection, ai, vector_key: str, pattern: str, *, subject: str, body: str,
                     attachments: list[str], sender: str = "", limit: int = 3, min_share: float = 0.5,
                     min_lead: float = 0.2) -> list[dict]:
    """Suggestions for a mail that is not (yet) indexed – used by plMail while a human reads a mail.

    Project numbers in subject, text or attachment names come first ("sicher"), then the content
    vote. Nothing is stored and nothing is assigned: the human decides."""
    known = known_projects(conn)
    names = dict(conn.execute("SELECT number, name FROM projects WHERE active = 1").fetchall())
    rx = number_regex(pattern)
    out: list[dict] = []
    for label, text in (("Betreff", subject), ("Text", body), ("Anhangname", "\n".join(attachments))):
        found, _ = numbers_in(text, rx, known)
        for n in found:
            if n not in {o["number"] for o in out}:
                out.append({"number": n, "name": names.get(n, ""), "confidence": "sicher", "score": 1.0,
                            "reason": f"Projektnummer {n} im {label}"})
    text = "\n".join([subject, body[:4000], " ".join(attachments)])
    qvec = None
    if ai is not None:
        try:
            qvec = ai.embed.embed([text])[0]
        except AIServiceError:
            qvec = None  # full text alone still votes
    ranked, lexical, bonus = vote_projects(conn, text, qvec, (sender or "").lower(), vector_key)
    verdict = _judge(ranked, lexical, bonus, min_share, min_lead)
    for p, share in ranked:
        if len(out) >= limit:
            break
        if p in {o["number"] for o in out} or p not in lexical:
            continue
        confidence = "hoch" if p == verdict.project else "niedrig"
        reason = f"{share:.0%} der ähnlichsten Abschnitte"
        if bonus == p:
            reason += "; Absender schrieb bisher nur zu diesem Projekt"
        out.append({"number": p, "name": names.get(p, ""), "confidence": confidence,
                    "score": round(share, 4), "reason": reason})
    return out[:limit]


def _external(m: sqlite3.Row) -> dict | None:
    try:
        return json.loads(m["external_assignment"]) if m["external_assignment"] else None
    except (TypeError, ValueError):
        return None


def _decision(conn: sqlite3.Connection, source_key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM assign_decisions WHERE source_key = ?", (source_key,)).fetchone()


def _set(conn, mail_id: int, project: str | None, status: str, method: str | None, reason: str) -> None:
    set_mail_project(conn, mail_id, project)
    conn.execute("UPDATE mails SET assign_status=?, assign_method=?, assign_reason=?, suggested_project=NULL, "
                 "suggestion_score=NULL, suggest_done=1 WHERE id=?", (status, method, reason, mail_id))


def apply_plmail_assignment(conn, m: sqlite3.Row, ext: dict, known: dict[str, str]) -> str:
    """Apply the assignment a human made in plMail. Returns the resulting status."""
    who = ext.get("assignedBy") or "Mensch"
    when = (ext.get("assignedAt") or "")[:10]
    suffix = f" am {when[8:10]}.{when[5:7]}.{when[0:4]}" if len(when) == 10 else ""
    if ext.get("status") == "none":
        _set(conn, m["id"], None, "none", "plmail", f"in plMail von {who}{suffix} als ohne Projektbezug markiert")
        return "none"
    number = known.get(str(ext.get("project") or "").lower())
    if not number:
        _set(conn, m["id"], None, PLMAIL_OPEN, "plmail",
             f"in plMail zugeordnetes Projekt {ext.get('project')} ist in Projektakte unbekannt")
        return PLMAIL_OPEN
    _set(conn, m["id"], number, "confirmed", "plmail", f"in plMail von {who}{suffix} zugeordnet")
    return "confirmed"


def assign_mails(conn: sqlite3.Connection, pattern: str, *, min_share: float, min_lead: float,
                 vector_key: str, mode: str = "auto") -> dict:
    """Process new mails and mails still waiting for a decision.

    Order of precedence: assignment made in plMail > decision made in Projektakte > automatic.
    In ``manual`` mode nothing is assigned automatically: mails from plMail wait for a decision in
    plMail, all other mails get suggestions (a project number only proposes) for the inbox."""
    from . import db

    if db.get_meta(conn, "assign_mode") != mode:  # mode switched: re-evaluate everything
        conn.execute("UPDATE mails SET assign_status = 'pending', suggest_done = 0")
        db.set_meta(conn, "assign_mode", mode)
    rx = number_regex(pattern)
    known = known_projects(conn)
    stats = {"assigned": 0, "suggested": 0, "unassigned": 0, "decided": 0, "plmail": 0, PLMAIL_OPEN: 0}
    rows = conn.execute(
        "SELECT * FROM mails WHERE deleted = 0 AND (assign_status = 'pending' OR "
        "(assign_status IN ('unassigned', 'suggested') AND suggest_done = 0) OR "
        "(assign_status = 'unassigned' AND assign_method = 'unknown'))").fetchall()
    for m in rows:
        ext = _external(m)
        if ext and ext.get("status") in ("assigned", "none"):
            status = apply_plmail_assignment(conn, m, ext, known)
            stats["plmail" if status != PLMAIL_OPEN else PLMAIL_OPEN] += 1
            continue
        if mode == "manual" and m["source"] == "jmap":
            _set(conn, m["id"], None, PLMAIL_OPEN, None, "wartet auf Zuordnung in plMail")
            stats[PLMAIL_OPEN] += 1
            continue
        dec = _decision(conn, m["source_key"])
        if dec and dec["action"] in ("assign", "none"):
            project = dec["project"] if dec["action"] == "assign" else None
            if project and project not in known.values():
                project = None  # project folder gone – back to the inbox
            else:
                set_mail_project(conn, m["id"], project)
                conn.execute("UPDATE mails SET assign_status=?, assign_method='manual', assign_reason=?, "
                             "suggested_project=NULL, suggestion_score=NULL, suggest_done=1 WHERE id=?",
                             ("confirmed" if project else "none",
                              f"von {dec['decided_by'] or 'Mensch'} entschieden", m["id"]))
                stats["decided"] += 1
                continue
        names = [r[0] for r in conn.execute("SELECT filename FROM documents WHERE mail_id = ?", (m["id"],))]
        project, method, reason = certain_project(m["subject"], m["body"], names, rx, known)
        if project and mode == "manual":  # a number only proposes – a human confirms
            set_mail_project(conn, m["id"], None)
            conn.execute("UPDATE mails SET assign_status='suggested', assign_method=?, assign_reason=?, "
                         "suggested_project=?, suggestion_score=1.0, suggest_done=1 WHERE id=?",
                         (method, f"{reason} – bitte bestätigen", project, m["id"]))
            stats["suggested"] += 1
            continue
        if project:
            set_mail_project(conn, m["id"], project)
            conn.execute("UPDATE mails SET assign_status='assigned', assign_method=?, assign_reason=?, "
                         "suggested_project=NULL, suggestion_score=NULL, suggest_done=1 WHERE id=?",
                         (method, reason, m["id"]))
            stats["assigned"] += 1
            continue
        rejected = set(filter(None, (dec["rejected"] or "").split(","))) if dec else set()
        sug = suggest(conn, m, names, vector_key, min_share=min_share, min_lead=min_lead, exclude=rejected)
        set_mail_project(conn, m["id"], None)
        if sug is None:
            conn.execute("UPDATE mails SET assign_status='unassigned', assign_method=?, assign_reason=?, "
                         "suggest_done=0 WHERE id=?",
                         (method, f"{reason}; Vorschlag folgt, sobald Vektoren vorliegen", m["id"]))
            stats["unassigned"] += 1
        elif sug.project:
            conn.execute("UPDATE mails SET assign_status='suggested', assign_method='similarity', "
                         "assign_reason=?, suggested_project=?, suggestion_score=?, suggest_done=1 WHERE id=?",
                         (f"{reason}; Vorschlag: {sug.reason}", sug.project, round(sug.score, 4), m["id"]))
            stats["suggested"] += 1
        else:
            conn.execute("UPDATE mails SET assign_status='unassigned', assign_method=?, assign_reason=?, "
                         "suggested_project=NULL, suggestion_score=?, suggest_done=1 WHERE id=?",
                         (method, f"{reason}; {sug.reason}", round(sug.score, 4), m["id"]))
            stats["unassigned"] += 1
    conn.commit()
    return stats


class DecisionError(ValueError):
    pass


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
    dec = _decision(conn, m["source_key"])
    rejected = set(filter(None, (dec["rejected"] or "").split(","))) if dec else set()
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
                     (f"von {user or 'Mensch'} zugeordnet", mail_id))
        stored = ("assign", canonical)
    elif action == "reject":
        if m["suggested_project"]:
            rejected.add(m["suggested_project"])
        set_mail_project(conn, mail_id, None)
        conn.execute("UPDATE mails SET assign_status='unassigned', assign_reason=?, suggested_project=NULL, "
                     "suggestion_score=NULL, suggest_done=1 WHERE id=?",
                     (f"Vorschlag {m['suggested_project']} von {user or 'Mensch'} verworfen", mail_id))
        stored = ("reject", None)
    elif action == "none":
        set_mail_project(conn, mail_id, None)
        conn.execute("UPDATE mails SET assign_status='none', assign_method='manual', assign_reason=?, "
                     "suggested_project=NULL, suggestion_score=NULL, suggest_done=1 WHERE id=?",
                     (f"von {user or 'Mensch'} als ohne Projektbezug markiert", mail_id))
        stored = ("none", None)
    else:
        raise DecisionError(f"Unbekannte Aktion: {action}")
    conn.execute(
        "INSERT INTO assign_decisions(source_key, action, project, rejected, decided_by, decided_at) "
        "VALUES(?,?,?,?,?,datetime('now')) ON CONFLICT(source_key) DO UPDATE SET action=excluded.action, "
        "project=excluded.project, rejected=excluded.rejected, decided_by=excluded.decided_by, "
        "decided_at=excluded.decided_at",
        (m["source_key"], stored[0], stored[1], ",".join(sorted(rejected)), user))
    conn.commit()
    row = conn.execute("SELECT id, project, assign_status, assign_reason FROM mails WHERE id = ?",
                       (mail_id,)).fetchone()
    return dict(row)
