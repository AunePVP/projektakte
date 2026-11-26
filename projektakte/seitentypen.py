"""The page type of a page (table ``page_types``): what the page model said a page of a PDF or image file is –
Text, Plan, Foto, Leer or Unsicher – kept for good, with the model's raw text for text pages. Pages without a row
were not seen by the model (Word, ODF, mails, folders left out, pages still waiting): "nicht eingeordnet".
"""
from __future__ import annotations

import sqlite3

TYPES = ("Text", "Plan", "Foto", "Leer", "Unsicher")  # ai.seitenmodell.TYPES
LOOP = "Schleife"
NONE = "ohne"  # the search filter's value for pages without a type
# how a page got its type
MODEL, CAD, RENDER = "modell", "cad", "render"


def store(conn: sqlite3.Connection, doc_id: int, page: int, *, typ: str, kopf: str | None = None,
          sicherheit: float | None = None, schleife: bool = False, finish: str | None = None,
          source: str = MODEL, layer: bool = False, raw: str | None = None) -> None:
    """Record the type of one page (replaces what was known of it). No commit."""
    if typ not in TYPES:
        raise ValueError(f"unbekannter Seitentyp: {typ!r}")
    conn.execute(
        "INSERT OR REPLACE INTO page_types(doc_id, page, typ, kopf, sicherheit, schleife, finish, source, layer, raw) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (doc_id, page, typ, kopf, sicherheit, int(bool(schleife)), finish, source, int(bool(layer)), raw))


def clear(conn: sqlite3.Connection, doc_id: int) -> None:
    conn.execute("DELETE FROM page_types WHERE doc_id = ?", (doc_id,))


def known(conn: sqlite3.Connection, doc_id: int) -> dict[int, sqlite3.Row]:
    """Page number → row for the pages of a document that have a type."""
    return {r["page"]: r for r in conn.execute("SELECT * FROM page_types WHERE doc_id = ?", (doc_id,))}


def final_texts(stage1: dict[int, str], rows: dict) -> dict[int, str]:
    """The text of every page of a document, from its text of the first stage (``stage1``: text layer or
    Tesseract) and what the page model said (``rows``: page → row of page_types). A page with a real text layer
    keeps its text whatever its type. A scan page: Text → the model's text made from the raw text (headers and
    footers that come back kept once, seitentext.document_texts); Leer, said by the model → no text; Plan, Foto,
    Unsicher and pages without a type → the text of the first stage. A page the app found blank when it rendered
    it (``source`` render) keeps what the first stage read: only the model's word empties a page."""
    from .seitentext import document_texts

    written = document_texts({n: r["raw"] for n, r in rows.items()
                              if r["typ"] == "Text" and not r["layer"] and (r["raw"] or "").strip()})
    out = dict(stage1)
    for n, r in rows.items():
        if r["layer"]:
            continue
        if n in written and written[n].strip():
            out[n] = written[n]
        elif r["typ"] == "Leer" and r["source"] == MODEL:
            out[n] = ""
    return out


def counts(conn: sqlite3.Connection, doc_id: int) -> dict[str, int]:
    """Pages of a document per type, and under "Schleife" those the service cut a loop short on."""
    out = {typ: n for typ, n in conn.execute(
        "SELECT typ, COUNT(*) FROM page_types WHERE doc_id = ? GROUP BY typ", (doc_id,))}
    loops = conn.execute("SELECT COUNT(*) FROM page_types WHERE doc_id = ? AND schleife = 1", (doc_id,)).fetchone()[0]
    if loops:
        out[LOOP] = loops
    return out


_WORDS = {"Text": "Text", "Plan": "Plan", "Foto": "Foto", "Leer": "leer", "Unsicher": "unsicher", LOOP: LOOP}


def summary(counts: dict[str, int]) -> str:
    """"12 Text, 3 Plan, 1 Foto, 2 leer, 1 unsicher, 1 Schleife" – what is not there is left out."""
    return ", ".join(f"{counts[key]} {word}" for key, word in _WORDS.items() if counts.get(key))


def label(typ: str | None, *, kopf: str | None = None, schleife: bool = False) -> str | None:
    """What is shown beside a page number; None for a plain text page and for a page without a type."""
    if typ is None:
        return None
    if typ == "Unsicher":
        return f"Typ unsicher (vermutlich {kopf})" if kopf and kopf != "Unsicher" else "Typ unsicher"
    if schleife:
        return LOOP
    if typ == "Text":
        return None
    return _WORDS.get(typ, typ)
