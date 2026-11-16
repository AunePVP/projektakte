"""The target for reading a page (agreed with the office, see DECISIONS "Was auf eine Seite gehört"): the page type in
the first line, then what is to be written for that type. One prompt for drafts (ocr_entwurf.py), the correction tool
(ocr_korrektur.py), training (the exported JSONL) and later operation – a fine-tuned model is asked with it as well.
"""
from __future__ import annotations

TYPES = ("Text", "Plan", "Foto", "Leer")

PROMPT = (
    "Du liest eine einzelne Seite aus der Projektakte eines Brandschutz-Ingenieurbüros. Schreibe in die erste Zeile "
    "den Seitentyp: „TYP: Text“, „TYP: Plan“, „TYP: Foto“ oder „TYP: Leer“. Danach:\n"
    "- Text (Brief, Bericht, Bescheid, Prüfbericht, Gutachten, Formular, Datenblatt): den gesamten sichtbaren Text "
    "wörtlich in Lesereihenfolge, auch Kopf- und Fußzeilen. Tabellen als Markdown-Tabellen, Ankreuzfelder als ☒ oder ☐ "
    "mit ihrer Beschriftung, Handschrift als [handschriftlich: …] an ihrer Stelle, Unterschriften als [Unterschrift], "
    "inhaltlich bedeutsame Stempel (Genehmigung, Prüfvermerk, Eingang mit Datum) wörtlich als [Stempel: …].\n"
    "- Plan: nur das Schriftfeld (Planart, Objekt und Adresse, Geschoss oder Bereich, Plan-Nr., Index, Datum, Maßstab, "
    "Verfasser), die Legende und Genehmigungsstempel – keine Raumnamen, Maße, Achsen oder Flurstücke.\n"
    "- Foto: nur Text, der im Foto zu sehen ist (Typenschilder, Zulassungsnummern, Beschriftungen) – keine "
    "Beschreibung des Fotos.\n"
    "- Leer (kein lesbarer Text, nur Grafik, Logo, Hintergrund oder Produktbild): nach der Typzeile nichts.\n"
    "Schreibe nur, was auf der Seite zu sehen ist. Unleserliches als [unleserlich]. Erfinde nichts, beschreibe nichts, "
    "fasse nichts zusammen. Logos und dekorative Firmenstempel lässt du weg."
)


def split(answer: str) -> tuple[str | None, str]:
    """(type, text) of an answer in the target form; type None if the first line names none of TYPES."""
    first, _sep, rest = (answer or "").strip().partition("\n")
    head = first.strip().lstrip("*#").strip()
    if head.upper().startswith("TYP:"):
        name = head[4:].strip().strip("*„“\"").strip()
        for t in TYPES:
            if name.lower().startswith(t.lower()):
                return t, rest.strip()
    return None, (answer or "").strip()


def join(page_type: str, text: str) -> str:
    """The answer in the target form."""
    body = (text or "").strip()
    return f"TYP: {page_type}" + (f"\n{body}" if body and page_type != "Leer" else "")
