"""A report of numbers about the index: ``python -m projektakte.bericht`` (in the container:
``docker compose exec app python -m projektakte.bericht``).

What was read and how, where documents and pages failed (their messages grouped, numbers replaced by ``#``), how
the page model sorted the pages, where the texts are from and how well text layers agree with what the model saw.
Counts only – no file names, no text of any document: the output can be copied into a chat or a ticket. Reads the
database, changes nothing.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys

from . import db
from .config import load_settings

TOP = 12  # groups of messages shown
_NUMBER = re.compile(r"\d+")
_PATH = re.compile(r"(?:[A-Za-z]:)?[/\\][^\s:;,()]+")


def _group(message: str | None) -> str:
    """A message as a group: paths left out, every number a ``#`` (so the same problem counts together)."""
    text = _PATH.sub("…", message or "") if "http" not in (message or "") else (message or "")
    return _NUMBER.sub("#", text).strip()[:160] or "(ohne Meldung)"


def _top(rows) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for message, n in rows:
        key = _group(message)
        counts[key] = counts.get(key, 0) + n
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:TOP]


def collect(conn: sqlite3.Connection) -> dict:
    one = lambda sql, *args: conn.execute(sql, args).fetchone()[0]  # noqa: E731
    pairs = lambda sql: {k if k is not None else "–": n for k, n in conn.execute(sql)}  # noqa: E731
    docs = {
        "gesamt": one("SELECT COUNT(*) FROM documents"),
        "status": pairs("SELECT status, COUNT(*) FROM documents GROUP BY status"),
        "unvollstaendig": one("SELECT COUNT(*) FROM documents WHERE incomplete = 1"),
        "mit_offenen_seiten": one("SELECT COUNT(*) FROM documents WHERE vision_pending > 0"),
        "seiten_offen": one("SELECT COALESCE(SUM(vision_pending), 0) FROM documents"),
        "meldungen": _top(conn.execute(
            "SELECT message, COUNT(*) FROM documents WHERE status != 'ok' OR (incomplete = 1 AND vision_pending = 0) "
            "GROUP BY message")),
    }
    buckets = {"ab 95 %": (0.95, 2.0), "80–95 %": (0.8, 0.95), "50–80 %": (0.5, 0.8), "unter 50 %": (-1.0, 0.5)}
    pages = {
        "gesamt": one("SELECT COUNT(*) FROM page_types"),
        "typ": pairs("SELECT typ, COUNT(*) FROM page_types GROUP BY typ"),
        "eingeordnet_durch": pairs("SELECT source, COUNT(*) FROM page_types GROUP BY source"),
        "textquelle": pairs("SELECT quelle, COUNT(*) FROM page_types WHERE quelle IS NOT NULL GROUP BY quelle"),
        "mit_textebene": one("SELECT COUNT(*) FROM page_types WHERE layer = 1"),
        "schleife": one("SELECT COUNT(*) FROM page_types WHERE schleife = 1"),
        "abgeschnitten": one("SELECT COUNT(*) FROM page_types WHERE finish = 'length'"),
        "von_hand": one("SELECT COUNT(*) FROM page_types WHERE source = 'mensch'"),
        "deckung": {name: one("SELECT COUNT(*) FROM page_types WHERE layer = 1 AND deckung >= ? AND deckung < ?", lo, hi)
                    for name, (lo, hi) in buckets.items()},
        "wartend": pairs("SELECT state, COUNT(*) FROM page_texts WHERE state IN ('pending', 'failed') GROUP BY state"),
        "fehler": _top(conn.execute("SELECT note, COUNT(*) FROM page_texts WHERE state IN ('failed', 'skipped') "
                                    "GROUP BY note")),
    }
    chunks = one("SELECT COUNT(*) FROM chunks")
    sections = {
        "gesamt": chunks,
        "ohne_vektor": one("SELECT COUNT(*) FROM chunks WHERE embedding IS NULL"),
        "zeichen_mittel": round(one("SELECT COALESCE(AVG(LENGTH(text)), 0) FROM chunks")),
        "mit_ueberschrift": one("SELECT COUNT(*) FROM chunks WHERE heading IS NOT NULL AND heading != ''"),
        "aus_pdf_und_bildern": one("SELECT COUNT(*) FROM chunks WHERE page IS NOT NULL"),
    }
    return {"dokumente": docs, "seiten": pages, "abschnitte": sections}


def _n(value: int) -> str:
    return f"{value:,}".replace(",", ".")


def _line(pairs: dict) -> str:
    return ", ".join(f"{_n(n)} {key}" for key, n in pairs.items()) or "–"


def render(data: dict) -> str:
    d, p, c = data["dokumente"], data["seiten"], data["abschnitte"]
    out = [f"Dokumente: {_n(d['gesamt'])}",
           f"  Status: {_line(d['status'])}",
           f"  unvollständig: {_n(d['unvollstaendig'])} · mit offenen Seiten: {_n(d['mit_offenen_seiten'])} "
           f"({_n(d['seiten_offen'])} Seiten warten auf das Modell)"]
    if d["meldungen"]:
        out.append("  häufigste Meldungen nicht gelesener oder unvollständiger Dokumente:")
        out += [f"    {_n(n):>8} × {message}" for message, n in d["meldungen"]]
    out += ["", f"Seiten (vom Seitenmodell gesehen): {_n(p['gesamt'])}",
            f"  Typ: {_line(p['typ'])}",
            f"  eingeordnet durch: {_line(p['eingeordnet_durch'])}",
            f"  Text der Seite aus: {_line(p['textquelle'])}",
            f"  Schleife gekürzt: {_n(p['schleife'])} · Antwort abgeschnitten: {_n(p['abgeschnitten'])} · "
            f"von Hand zugeordnet: {_n(p['von_hand'])}",
            f"  Seiten mit echter Textebene: {_n(p['mit_textebene'])} – Deckung Textebene/Modell: {_line(p['deckung'])}",
            "    (unter 50 %: Die Textebene ist nicht, was die Seite zeigt – dort gilt der Text des Modells.)",
            f"  wartend: {_line(p['wartend'])}"]
    if p["fehler"]:
        out.append("  häufigste Meldungen fehlgeschlagener oder nicht auswertbarer Seiten (noch nicht fertige Dokumente):")
        out += [f"    {_n(n):>8} × {message}" for message, n in p["fehler"]]
    share = round(100 * c["mit_ueberschrift"] / c["gesamt"]) if c["gesamt"] else 0
    out += ["", f"Abschnitte: {_n(c['gesamt'])}",
            f"  ohne Vektor: {_n(c['ohne_vektor'])} · aus PDF/Bildern: {_n(c['aus_pdf_und_bildern'])} · "
            f"mit Überschrift: {_n(c['mit_ueberschrift'])} ({share} %) · im Mittel {_n(c['zeichen_mittel'])} Zeichen"]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m projektakte.bericht", description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", help="Datenbankdatei (Voreinstellung: die der Einstellungen, DATA_DIR)")
    args = ap.parse_args(argv)
    path = args.db or load_settings().db_path
    db.init_db(path)
    conn = db.connect(path)
    try:
        print(render(collect(conn)))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
