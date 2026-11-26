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


# where the text of a page is from (page_types.quelle)
LAYER, FROM_MODEL, STAGE1, HUMAN = "ebene", "modell", "stufe1", "mensch"


def compose(stage1: dict[int, str], rows: dict, segments: dict | None = None) -> tuple[dict[int, str], dict]:
    """The text of every page of a document, from its text of the first stage (``stage1``: text layer, or the OCR
    layer of a scan), what the page model said (``rows``: page → row of page_types) and, for pages with a real
    text layer, their lines with their place (``segments``: page → [[x0, y0, x1, y1, text], …]).

    - A page with a real text layer keeps the layer's words whatever its type; on a text page they are put into
      the model's blocks (paragraphs, headings, columns, headers and footers: seitentext.compose_layer) – unless
      the layer is not what the page shows (it has less than half of the words the model read): then the model's
      text.
    - A scan page: Text → the model's text; Leer, said by the model → no text; Plan, Foto, Unsicher and pages
      without a type → the text of the first stage. A page the app found blank when it rendered it (``source``
      render) keeps what the first stage read: only the model's word empties a page.
    - Headers and footers that come back on the pages of the document stay once (seitentext.texts_of_blocks).

    Returns the texts and, per typed page, (where its text is from, coverage of the model's words by the layer)."""
    from .seitentext import compose_layer, parse_blocks, texts_of_blocks

    out = dict(stage1)
    blocks: dict[int, list] = {}
    info: dict[int, tuple[str | None, float | None]] = {}
    for n, r in rows.items():
        raw = (r["raw"] or "").strip()
        has_text = bool((stage1.get(n) or "").strip())
        if r["layer"]:
            lines = (segments or {}).get(n)
            if r["typ"] == "Text" and lines:
                blocks[n], share, origin = compose_layer(lines, raw, schleife=bool(r["schleife"]), finish=r["finish"])
                info[n] = (origin, share)
            else:
                info[n] = (LAYER if has_text else None, None)
        elif r["typ"] == "Text" and raw:
            blocks[n] = parse_blocks(raw)
            info[n] = (FROM_MODEL, None)
        elif r["typ"] == "Leer" and r["source"] == MODEL:
            out[n] = ""
            info[n] = (FROM_MODEL, None)
        else:
            info[n] = (STAGE1 if has_text else None, None)
    for n, text in texts_of_blocks(blocks).items():
        if text.strip():
            out[n] = text
        elif not rows[n]["layer"]:  # the model wrote nothing usable: the text of the first stage stands
            info[n] = (STAGE1 if (stage1.get(n) or "").strip() else None, None)
    return out, info


def final_texts(stage1: dict[int, str], rows: dict, segments: dict | None = None) -> dict[int, str]:
    """The texts of :func:`compose` alone."""
    return compose(stage1, rows, segments)[0]


def note_sources(conn: sqlite3.Connection, doc_id: int, info: dict) -> None:
    """Record where each page's text is from and how well the text layer agrees with the model (no commit)."""
    conn.executemany("UPDATE page_types SET quelle = ?, deckung = ? WHERE doc_id = ? AND page = ?",
                     [(origin, share, doc_id, n) for n, (origin, share) in info.items()])


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
