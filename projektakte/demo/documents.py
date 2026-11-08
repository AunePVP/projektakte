"""Builds the demo project documents (building permit, fire-safety concept, review report,
meeting minutes, optional scanned plan) from the data in ``data.py``."""
from __future__ import annotations

import random
from datetime import date, timedelta

from . import data
from .writers import docx_bytes, pdf_bytes, plan_png


def de(d: str | date) -> str:
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return d.strftime("%d.%m.%Y")


def auflagen(p: dict, rng: random.Random) -> list[tuple[int, str, str]]:
    """Numbered list of 10 conditions: the project's own at fixed numbers, generic ones around."""
    specific = p["auflagen"]
    generic = list(data.GENERIC_AUFLAGEN)
    rng.shuffle(generic)
    out = []
    for n in range(1, 11):
        topic, text = specific[n] if n in specific else generic.pop()
        out.append((n, topic, text))
    return out


def baugenehmigung_pdf(p: dict, rng: random.Random) -> bytes:
    amt, sachbearbeiter, _ = data.BAUAMT[p["town"]]
    org, contact, _ = p["bauherr"]
    paras = [
        f"# {amt}",
        f"{p['town']}, den {de(p['permit_date'])}",
        f"Aktenzeichen: {p['az']}    Ihr Zeichen: {p['number']}",
        "# Baugenehmigung",
        f"Bauherr: {org}, vertreten durch {contact}",
        f"Bauvorhaben: {p['vorhaben']}",
        f"Grundstück: {data.address(p)}",
        f"Brandschutzplanung: {data.OFFICE['name']}, Projekt {p['number']} {p['name']}",
        "Sehr geehrte Damen und Herren, auf Ihren Antrag wird die Baugenehmigung für das oben genannte "
        "Bauvorhaben nach Maßgabe der geprüften Bauvorlagen erteilt. Der Brandschutznachweis ist "
        "Bestandteil dieser Genehmigung. Die Genehmigung ergeht unter den folgenden Auflagen zum "
        "Brandschutz.",
        "\f",
        f"# Auflagen zum Brandschutz – {p['name']}",
    ]
    for n, topic, text in auflagen(p, rng):
        paras.append(f"Auflage {n} ({topic}): {text}")
    paras += [
        "\f",
        "# Hinweise",
        "Der Beginn der Bauarbeiten ist der Bauaufsicht mindestens eine Woche vorher anzuzeigen. Die "
        "Fertigstellung des Rohbaus und die abschließende Fertigstellung sind ebenfalls anzuzeigen.",
        "Die Baugenehmigung erlischt, wenn innerhalb von drei Jahren nach ihrer Erteilung mit der "
        "Ausführung nicht begonnen wurde.",
        "# Rechtsbehelfsbelehrung",
        "Gegen diesen Bescheid kann innerhalb eines Monats nach Bekanntgabe Widerspruch erhoben werden.",
        "Im Auftrag",
        sachbearbeiter,
    ]
    return pdf_bytes(paras)


def konzept_date(p: dict) -> date:
    return date.fromisoformat(p["permit_date"]) - timedelta(days=63)


def brandschutzkonzept_docx(p: dict, author: str) -> bytes:
    k = p["konzept"]
    blocks: list[tuple[str, object]] = [
        ("title", "Brandschutzkonzept"),
        ("p", f"Projekt {p['number']} – {p['name']}"),
        ("p", f"Grundstück: {data.address(p)}"),
        ("p", f"Verfasser: {data.OFFICE['name']}, {author}. Stand: {de(konzept_date(p))}"),
        ("h1", "1 Allgemeines"),
        ("p", p["vorhaben"]),
        ("p", k["allgemein"]),
        ("h1", "2 Kennwerte"),
        ("table", [("Kennwert", "Wert")] + list(p["kennwerte"])),
        ("h1", "3 Rettungswege"),
        ("p", k["rettungswege"]),
        ("h1", "4 Anlagentechnischer Brandschutz"),
        ("p", k["anlagen"]),
        ("h1", "5 Löschwasserversorgung"),
        ("p", k["loeschwasser"]),
        ("h1", "6 Abweichungen"),
        ("p", k["abweichungen"]),
        ("h1", "7 Organisatorischer Brandschutz"),
        ("p", k["organisation"]),
        ("h1", "8 Zusammenfassung"),
        ("p", "Bei Umsetzung der beschriebenen Maßnahmen bestehen aus Sicht des Brandschutzes keine "
              "Bedenken gegen das Bauvorhaben. Änderungen der Planung sind mit dem Verfasser "
              "abzustimmen."),
    ]
    return docx_bytes(blocks)


def pruefbericht_md(p: dict) -> str:
    pi_org, pi_name, _ = data.PRUEFING
    pb = p["pruefbericht"]
    lines = [
        f"# Prüfbericht Nr. {pb['nr']} zum Brandschutznachweis",
        "",
        "| Angabe | Inhalt |",
        "|---|---|",
        f"| Projekt | {p['number']} {p['name']} |",
        f"| Grundstück | {data.address(p)} |",
        f"| Prüfingenieur | {pi_name}, {pi_org} |",
        f"| Datum | {de(pb['date'])} |",
        "",
        "## Gegenstand",
        "",
        f"Geprüft wurde das Brandschutzkonzept des {data.OFFICE['name']} für das Bauvorhaben "
        f"{p['name']} einschließlich der Ausführungspläne.",
        "",
        "## Feststellungen",
        "",
    ]
    for i, f in enumerate(pb["findings"], start=1):
        lines.append(f"{i}. {f}")
    lines += [
        "",
        "## Ergebnis",
        "",
        "Der Brandschutznachweis wird unter Beachtung der Feststellungen als vollständig und richtig "
        "bescheinigt. Die Bauüberwachung wird fortgesetzt.",
        "",
    ]
    return "\n".join(lines)


def protokoll_txt(p: dict, author: str) -> str:
    pr = p["protokoll"]
    _, bh_contact, _ = p["bauherr"]
    _, ar_contact, _ = data.ARCHITEKTEN[p["architekt"]]
    lines = [
        f"Besprechungsprotokoll Nr. {pr['nr']}",
        f"Projekt: {p['number']} {p['name']}",
        f"Datum: {de(pr['date'])}, Ort: Baustelle {p['street']}",
        f"Teilnehmer: {bh_contact} (Bauherr), {ar_contact} (Architektur), {author} "
        f"({data.OFFICE['name']})",
        "",
    ]
    for i, (topic, text) in enumerate(pr["items"], start=1):
        lines += [f"TOP {i} – {topic}", text, ""]
    nxt = date.fromisoformat(pr["date"]) + timedelta(days=28)
    lines += [f"Nächste Besprechung: {de(nxt)}", f"Aufgestellt: {author}", ""]
    return "\n".join(lines)


def project_files(p: dict, rng: random.Random) -> list[tuple[str, bytes, date]]:
    """(relative path inside the project folder, content, document date)."""
    author = rng.choice(data.OFFICE["staff"])[0]
    pb, pr = p["pruefbericht"], p["protokoll"]
    files = [
        ("01 Genehmigung/Baugenehmigung.pdf", baugenehmigung_pdf(p, rng),
         date.fromisoformat(p["permit_date"])),
        ("02 Konzept/Brandschutzkonzept.docx", brandschutzkonzept_docx(p, author), konzept_date(p)),
        (f"03 Prüfung/Prüfbericht Nr. {pb['nr']}.md", pruefbericht_md(p).encode("utf-8"),
         date.fromisoformat(pb["date"])),
        (f"04 Protokolle/{pr['date']} Besprechungsprotokoll Nr. {pr['nr']}.txt",
         protokoll_txt(p, author).encode("utf-8"), date.fromisoformat(pr["date"])),
    ]
    if p.get("image"):
        files.append(("05 Pläne/Fluchtwegplan EG.png", plan_png(), date.fromisoformat(pr["date"])))
    return files
