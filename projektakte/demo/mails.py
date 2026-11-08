"""Demo e-mails (.eml) for the fictional projects.

Categories (VISION.md section 6):

* ``subject``     – project number in the subject (the majority)
* ``body``        – number only in the body text
* ``attachment``  – number only in an attachment file name
* ``content``     – no number at all, but clearly about one project (address, building, people)
* ``unrelated``   – no project reference (newsletter, office supplies …), must stay unassigned
"""
from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime

from . import data
from .documents import de
from .writers import docx_bytes, pdf_bytes

CET, CEST = timezone(timedelta(hours=1)), timezone(timedelta(hours=2))


def _last_sunday(year: int, month: int) -> int:
    d = datetime(year, month + 1, 1) - timedelta(days=1) if month < 12 else datetime(year, 12, 31)
    return d.day - (d.weekday() + 1) % 7


def berlin(d: datetime) -> datetime:
    """Attach the German offset (CET/CEST, EU rule) to a naive local time – deterministic without a
    time zone database, so the demo files are identical on every system."""
    start = datetime(d.year, 3, _last_sunday(d.year, 3), 2)
    end = datetime(d.year, 10, _last_sunday(d.year, 10), 3)
    return d.replace(tzinfo=CEST if start <= d < end else CET)

# Per-project mail facts. FACT markers are referenced by the evaluation set.
MAILFACTS: dict[str, dict] = {
    "BS-2024-0137": {
        "feuerwehr": "Die Aufstellfläche für die Drehleiter an der Westseite ist ausreichend. Die "
                     "Feuerwehrzufahrt über den Schulhof ist mit Pollern zu sichern, die mit dem "
                     "Feuerwehr-Dreikant geöffnet werden können.",
        "architekt": "Die Außentreppe an der Ostseite würden wir gern als verzinkte Stahltreppe mit "
                     "Gitterroststufen ausführen. Ist das aus Brandschutzsicht in Ordnung?",
        "bauherr": "Die Stadt hat die Mittel für die Sprachalarmanlage freigegeben. Der Auftrag an den "
                   "Errichter geht in dieser Woche raus.",
        "special": [
            # FACT gs-abweichung
            {"role": "pruefing", "date": "2024-06-18",
             "subject": "{number} Zustimmung zur Abweichung Flurlänge Obergeschoss",
             "body": "Sehr geehrter Herr Brenner,\n\nnach Prüfung der nachgereichten Unterlagen stimme ich "
                     "der Abweichung zur Länge des notwendigen Flurs im Obergeschoss der Grundschule "
                     "(38 m statt 30 m) zu. Voraussetzung ist die Rauchschutztür in Flurmitte mit "
                     "Feststellanlage, die bei Raucherkennung schließt.\n\nDie Zustimmung wird "
                     "Bestandteil meines nächsten Prüfberichts."},
        ],
        "body_only": ("Rückfrage Rauchableitung Treppenraum",
                      "bei unserem Projekt BS-2024-0137 ist noch offen, welcher Antrieb für die "
                      "Öffnung zur Rauchableitung im Treppenraum Nord verwendet wird. Elektrisch mit "
                      "Notstromversorgung oder pneumatisch?"),
        "content_only": ("architekt", "Lindenhofweg 4 – Bemusterung Stahltreppe",
                         "wie besprochen bringen wir die Muster der Gitterroststufen für die neue "
                         "Außentreppe der Grundschule am Lindenhofweg 4 am Donnerstag mit. Die "
                         "Schulleitung möchte die Treppe vorher sehen."),
    },
    "BS-2023-0412": {
        "feuerwehr": "Gegen die horizontale Evakuierung in den benachbarten Rauchabschnitt bestehen keine "
                     "Bedenken. Die Laufkarten der Brandmeldeanlage sind vor der Abnahme vorzulegen.",
        "architekt": "Können die Rauchschutztüren im Wohnbereich C als Glastüren ausgeführt werden, damit "
                     "der Flur heller wird?",
        # FACT ph-bauherr
        "bauherr": "Die Abnahme der Feststellanlage im Wohnbereich B ist am 12.12.2024 erfolgt. Das "
                   "Abnahmeprotokoll des Türherstellers liegt bei.",
        "bauherr_attachment": ("Abnahmeprotokoll Feststellanlage WB B.pdf",
                               ["# Abnahmeprotokoll Feststellanlage",
                                "Objekt: Pflegeheim Sonnenhang, Wohnbereich B, 2. Obergeschoss",
                                "Tür: Rauchschutztür T30-RS, Feststellanlage mit Rauchschalter",
                                "Datum der Abnahme: 12.12.2024",
                                "Ergebnis: Die Feststellanlage löst bei Raucherkennung zuverlässig aus. "
                                "Keine Mängel."]),
        "attach_only": ("Unterlagen wie besprochen",
                        "anbei die Unterlagen wie besprochen.",
                        "BS-2023-0412_Brandschutzordnung_Teil_B.pdf",
                        ["# Brandschutzordnung Teil B",
                         "Für alle Beschäftigten der Seniorenresidenz. Im Brandfall Ruhe bewahren, "
                         "Brandmeldeanlage auslösen, Bewohner in den benachbarten Rauchabschnitt bringen.",
                         "Türen schließen, Aufzüge nicht benutzen. Sammelplatz ist der Parkplatz an der "
                         "Zufahrt."]),
        "content_only": ("bauherr", "Seniorenresidenz am Sonnenhang – Termin Räumungsübung",
                         "für die Räumungsübung in der Nachtschicht schlagen wir den 23. Januar vor. "
                         "Unsere Pflegedienstleitung möchte, dass Sie als Beobachter dabei sind. "
                         "Matthias Kortmann, Sonnenhang Seniorenresidenz gGmbH"),
    },
    "BS-2024-0058": {
        "feuerwehr": "Das Feuerwehrschlüsseldepot ist am Haupteingang zur Hafenkante vorzusehen. Die "
                     "Druckbelüftungsanlage muss von der Feuerwehr manuell abgeschaltet werden können.",
        "architekt": "Der Mieter im 4. OG wünscht ein Großraumbüro ohne Flurtrennwände. Passt das zum "
                     "Konzept?",
        "bauherr": "Der Mietvertrag für das 4. OG ist unterschrieben, der Ausbau beginnt im Februar.",
        "body_only": ("Termin Abnahme Druckbelüftung",
                      "für das Projekt BS-2024-0058 schlage ich als Termin für die Abnahme der "
                      "Druckbelüftungsanlage durch den Prüfsachverständigen den 11. März vor."),
        "content_only": ("bauherr", "Hafenkante 7 – Mieterausbau viertes Obergeschoss",
                         "unser Mieter für das vierte Obergeschoss am Gebäude Hafenkante 7 hat die "
                         "Pläne für das Großraumbüro geschickt. Bitte prüfen Sie diese im Hinblick auf "
                         "den Sicherheitstreppenraum. Dr. Frederik Lanz"),
    },
    "BS-2022-0901": {
        "feuerwehr": "Die Einfahrtshöhe von 2,10 m ist für unsere Einsatzfahrzeuge nicht ausreichend; "
                     "ein Einsatz erfolgt zu Fuß über die Treppenräume. Die Feuerwehrpläne sind "
                     "entsprechend zu ergänzen.",
        "architekt": "Wir planen die Ventilatoren der Entrauchung auf der Ebene -1. Reicht die "
                     "Zuluftnachströmung über die Rampe?",
        "bauherr": "Der Probebetrieb der Entrauchung ist gut gelaufen, die Eröffnung der Tiefgarage ist "
                   "für den 1. September geplant.",
        "body_only": ("Frage zur CO-Warnanlage",
                      "zur Tiefgarage (unser Zeichen BS-2022-0901): In welcher Höhe sollen die "
                      "CO-Sensoren montiert werden?"),
        "content_only": ("bauherr", "Marktplatz-Garage: Eröffnung und Feuerwehrpläne",
                         "die Garage unter dem Marktplatz in Beispielstadt soll im September eröffnen. "
                         "Fehlen aus Ihrer Sicht noch Unterlagen für die Entrauchungsanlage? "
                         "Ingrid Vogt, Parkhausgesellschaft"),
    },
    "BS-2024-0233": {
        "feuerwehr": "Die Saugstelle am See ist ganzjährig zugänglich zu halten und zu beschildern. Die "
                     "Sprachalarmanlage soll auch im Restaurant zu hören sein.",
        "architekt": "Für den Stichflur im 3. OG (17 m) bereiten wir den Abweichungsantrag vor. Welche "
                     "Kompensation halten Sie für tragfähig?",
        "bauherr": "Der Hotelbetrieb im Altbau läuft weiter. Bitte die Bauabschnitte so planen, dass die "
                   "Rettungswege der Gäste immer frei bleiben.",
        "special": [
            # FACT ho-abweichung
            {"role": "pruefing", "date": "2024-10-15",
             "subject": "Abweichungsantrag Stichflur 3. OG – {number} {name}",
             "body": "Sehr geehrte Damen und Herren,\n\nder Abweichung zur Länge des Stichflurs im 3. "
                     "Obergeschoss (17 m statt 15 m) kann ich nicht zustimmen; der Antrag wird "
                     "abgelehnt. Die vorgeschlagene Kompensation durch zusätzliche Rauchmelder reicht "
                     "nicht aus. Ich empfehle einen zusätzlichen Ausgang über einen Fluchtbalkon am "
                     "Ende des Stichflurs."},
        ],
        "attach_only": ("Fluchtbalkon Entwurf",
                        "anbei der Entwurf für den Fluchtbalkon, bitte um kurze Rückmeldung.",
                        "BS-2024-0233_Fluchtbalkon_Entwurf.docx",
                        ["Entwurf Fluchtbalkon 3. Obergeschoss",
                         "Der Fluchtbalkon schließt an das Ende des Stichflurs an und führt über eine "
                         "Stahltreppe in den Garten an der Uferpromenade. Nutzbare Breite 1,0 m."]),
        "content_only": ("bauherr", "Uferpromenade 15: Bauabschnitte während des Hotelbetriebs",
                         "da unser Haus an der Uferpromenade 15 während des Umbaus geöffnet bleibt, "
                         "brauchen wir einen Plan, wie die Gäste im Altbau sicher ins Freie kommen. "
                         "Konstantin Rehberg"),
    },
    "BS-2023-0777": {
        "feuerwehr": "Der Löschwasserteich ist mit einer Saugstelle nach DIN 14210 zu versehen und muss "
                     "über eine befestigte Zufahrt erreichbar sein.",
        "architekt": "Die Brandwand führen wir 0,5 m über Dach. Das Detail schicke ich anbei.",
        "bauherr": "Der Aushub für den Löschwasserteich ist beauftragt, Beginn im Mai.",
        "body_only": ("Rauchabzugsgeräte Dach",
                      "zum Vorhaben BS-2023-0777: Der Dachdecker fragt, ob die Rauchabzugsgeräte "
                      "auch in der Lichtbandebene sitzen dürfen."),
        "attach_only": ("Brandwanddetail",
                        "wie telefonisch besprochen.",
                        "BS-2023-0777_Brandwand_Detail.pdf",
                        ["# Detail Brandwand über Dach",
                         "Die Brandwand wird 0,5 m über Dach geführt. Die Dachabdichtung wird "
                         "seitlich mit nichtbrennbarem Material angeschlossen."]),
        "content_only": ("bauherr", "Halle am Nordring – Löschwasserteich",
                         "für unsere neue Logistikhalle am Nordring 88 ist der Bagger für den "
                         "Löschwasserteich bestellt. Muss die Saugstelle vor der Abnahme fertig sein? "
                         "Svenja Holt, Nordring Logistik AG"),
    },
    "BS-2025-0019": {
        "feuerwehr": "Die Balkonanlage als zweiter Rettungsweg wird mitgetragen. Für die Kinder ist eine "
                     "Evakuierungsübung je Halbjahr sinnvoll.",
        "architekt": "Welcher Handlauf ist an der Balkontreppe nötig, damit auch kleine Kinder sicher "
                     "laufen?",
        "bauherr": "Das Jugendamt möchte die funkvernetzten Rauchwarnmelder im Rahmenvertrag bestellen.",
        "body_only": ("Handlauf Balkontreppe",
                      "zum Projekt BS-2025-0019 (Kita Regenbogen): Der kindgerechte Handlauf an der "
                      "Balkontreppe soll in 60 cm Höhe montiert werden."),
        "attach_only": ("Ausschreibung Rauchwarnmelder",
                        "anbei der Entwurf des Leistungsverzeichnisses.",
                        "BS-2025-0019_LV_Rauchwarnmelder.txt",
                        ["Leistungsverzeichnis Rauchwarnmelder",
                         "Position 1: Funkvernetzter Rauchwarnmelder nach DIN 14676 mit Q-Kennzeichnung, "
                         "Stück 24.",
                         "Position 2: Funkmodul zur Alarmweiterleitung in das Leitungsbüro, Stück 1."]),
        "content_only": ("bauherr", "Regenbogenweg 3 – Rauchwarnmelder im Schlafraum",
                         "für die Kindertagesstätte am Regenbogenweg 3 fragt die Leitung, ob die "
                         "Rauchwarnmelder im Schlafraum auch im Büro der Leitung Alarm geben. "
                         "Petra Lindqvist, Jugendamt"),
    },
    "BS-2024-0311": {
        # FACT wh-aufstell
        "feuerwehr": "Die Aufstellfläche für das Hubrettungsfahrzeug an der Parkallee muss mindestens "
                     "12 m lang sein und darf nicht durch Baumpflanzungen eingeschränkt werden. Der "
                     "Feuerwehraufzug ist vor Nutzungsaufnahme gemeinsam zu erproben.",
        "architekt": "Der Vorraum des Feuerwehraufzugs ist in den oberen Geschossen nur 5,5 m² groß. "
                     "Reicht das?",
        "bauherr": "Die Genossenschaft möchte die Baumreihe an der Parkallee erhalten. Gibt es eine "
                   "Lösung für die Aufstellfläche?",
        "body_only": ("Druckerhöhungsanlage Steigleitung",
                      "betrifft BS-2024-0311: Die Druckerhöhungsanlage für die nassen Steigleitungen "
                      "wird redundant mit zwei Pumpen ausgeführt."),
        "attach_only": ("Unterlagen Aufzug",
                        "anbei die Unterlagen des Aufzugsherstellers.",
                        "BS-2024-0311_Feuerwehraufzug_Steuerung.pdf",
                        ["# Steuerung Feuerwehraufzug",
                         "Die Steuerung für den Feuerwehrbetrieb erfüllt DIN EN 81-72. Der Aufzug fährt "
                         "bei Brandalarm in die Zugangsebene und wird dort von der Feuerwehr übernommen."]),
        "content_only": ("bauherr", "Parkallee 50 – Bäume und Aufstellfläche",
                         "die Mitglieder unserer Genossenschaft möchten die Bäume vor dem Hochhaus an der "
                         "Parkallee 50 behalten. Können wir die Aufstellfläche für die Feuerwehr "
                         "verschieben? Benedikt Sauer, Wohnbau Stadtpark eG"),
    },
}

# (organisation, address, subject, body, html, local send time) – dates fit the texts
UNRELATED = [
    ("Fachverband Brandschutz e. V.", "newsletter@fachverband-brandschutz.example",
     "Newsletter November: neue Muster-Richtlinien und Termine",
     "Liebe Mitglieder,\n\nin dieser Ausgabe: Überarbeitung der Muster-Richtlinien, Erfahrungen mit "
     "Rauchableitung in Treppenräumen, Termine der Regionalgruppen und unsere Jahrestagung im März. "
     "Außerdem berichten wir über Rettungswege in Bestandsgebäuden.\n\nIhr Fachverband", True,
     "2024-11-05 09:12"),
    ("Papeterie Kugelschreiber GmbH", "rechnung@papeterie-kugel.example",
     "Rechnung Nr. 2024-1187 Bürobedarf",
     "Sehr geehrte Damen und Herren,\n\nanbei unsere Rechnung über Druckerpapier, Toner und "
     "Aktenordner. Zahlbar innerhalb von 14 Tagen ohne Abzug.\n\nMit freundlichen Grüßen\nIhre "
     "Papeterie", False, "2024-12-03 10:21"),
    ("Lea Funke", "l.funke@ib-feuerfest.example", "Einladung Sommerfest",
     "Hallo zusammen,\n\nunser Sommerfest findet am Freitag ab 16 Uhr im Hof statt. Bitte tragt euch "
     "bis Mittwoch in die Liste ein, ob ihr Salat oder Kuchen mitbringt.\n\nLea", False, "2025-06-16 08:47"),
    ("Hausverwaltung Bürohaus", "service@hv-buero.example", "Wartung der Kaffeemaschine am Freitag",
     "Die Kaffeemaschine in der Küche im zweiten Stock wird am Freitag zwischen 9 und 11 Uhr entkalkt "
     "und gewartet.", False, "2025-01-13 07:58"),
    ("CAD-Software Vertrieb", "lizenz@cad-vertrieb.example", "Ihr Software-Update: neue Lizenzdatei",
     "Ihre Wartungsverlängerung ist eingegangen. Die neue Lizenzdatei finden Sie im Kundenportal. "
     "Bitte installieren Sie das Update vor dem 31. des Monats.", False, "2025-03-11 14:03"),
    ("Akademie für Sicherheitstechnik", "seminare@akademie-sicherheit.example",
     "Seminarangebot: Fortbildung Brandschutzbeauftragte 2025",
     "Unsere Fortbildung für Brandschutzbeauftragte umfasst 16 Unterrichtseinheiten zu "
     "Brandschutzordnung, Feuerlöschern und Räumungsübungen. Jetzt anmelden und Frühbucherrabatt "
     "sichern.", False, "2024-11-18 11:34"),
    ("Löschtechnik Sonderangebot", "angebot@loeschtechnik-billig.example",
     "Feuerlöscher-Wartung zum Sonderpreis",
     "Nur diesen Monat: Prüfung Ihrer Feuerlöscher nach ASR A2.2 für 4,90 Euro je Gerät. "
     "Antworten Sie einfach auf diese Nachricht.", False, "2025-04-08 16:12"),
]


def _contact(p: dict, role: str) -> tuple[str, str, str]:
    """(organisation, person, address) for a role."""
    if role == "bauamt":
        return data.BAUAMT[p["town"]]
    if role == "feuerwehr":
        return data.FEUERWEHR[p["town"]]
    if role == "pruefing":
        return data.PRUEFING
    if role == "architekt":
        return data.ARCHITEKTEN[p["architekt"]]
    if role == "bauherr":
        return p["bauherr"]
    raise ValueError(role)


def _greeting(person: str) -> str:
    role = "\nPrüfingenieurin für Brandschutz" if person == data.PRUEFING[1] else ""
    return f"Mit freundlichen Grüßen\n{person}{role}"


def _attachment(filename: str, lines: list[str]) -> tuple[str, bytes, str]:
    if filename.endswith(".pdf"):
        return filename, pdf_bytes(lines), "application/pdf"
    if filename.endswith(".docx"):
        blocks = [("h1", lines[0])] + [("p", line) for line in lines[1:]]
        return (filename, docx_bytes(blocks),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    return filename, ("\n".join(lines) + "\n").encode("utf-8"), "text/plain"


def build_mails(rng: random.Random) -> list[dict]:
    """All demo mails as dicts (deterministic for a given rng state)."""
    mails: list[dict] = []
    office = data.OFFICE

    def staff_addr(i: int) -> tuple[str, str]:
        name, local = office["staff"][i % len(office["staff"])]
        return name, f"{local}@{office['domain']}"

    def when(base: str, lo: int, hi: int) -> datetime:
        d = datetime.fromisoformat(base) + timedelta(days=rng.randint(lo, hi))
        return berlin(d.replace(hour=rng.randint(7, 17), minute=rng.choice([3, 12, 21, 34, 47, 58])))

    def add(category, project, sender, to, subject, body, date, attachments=(), html=False,
            charset="utf-8", reply_to=None):
        mails.append({
            "category": category, "project": project, "from": sender, "to": to,
            "subject": subject, "body": body, "date": date, "attachments": list(attachments),
            "html": html, "charset": charset, "in_reply_to": reply_to,
        })
        return len(mails) - 1

    for pi, p in enumerate(data.PROJECTS):
        f = MAILFACTS[p["number"]]
        fmt = {"number": p["number"], "name": p["name"]}
        lead = staff_addr(pi)
        nr, name = p["number"], p["name"]

        # --- subject mails ---------------------------------------------------------------
        org, person, addr = _contact(p, "bauamt")
        add("subject", nr, (person, addr), lead, f"Baugenehmigung {p['az']} – {name} ({nr})",
            f"Sehr geehrte Damen und Herren,\n\ndie Baugenehmigung zum Bauvorhaben {name}, "
            f"{data.address(p)}, ist am {de(p['permit_date'])} erteilt worden. Bitte beachten Sie "
            f"insbesondere die Auflagen zum Brandschutz.\n\n{_greeting(person)}\n{org}",
            when(p["permit_date"], 1, 3))

        org, person, addr = _contact(p, "architekt")
        first = add("subject", nr, (person, addr), lead,
                    f"{nr} {name}: Frage zum Brandschutz",
                    f"Hallo {lead[0].split()[0]},\n\n{f['architekt']}\n\nViele Grüße\n{person}\n{org}",
                    when(p["permit_date"], 10, 40))
        add("subject", nr, lead, (person, addr), f"AW: {nr} {name}: Frage zum Brandschutz",
            f"Hallo {person.split()[0]},\n\ndanke für die Nachfrage. Ich sehe mir das bis Ende der "
            f"Woche an und melde mich mit einer Stellungnahme.\n\nViele Grüße\n{lead[0]}",
            mails[first]["date"] + timedelta(hours=26), reply_to=first)

        org, person, addr = _contact(p, "feuerwehr")
        add("subject", nr, (person, addr), lead,
            f"Stellungnahme Brandschutzdienststelle – {nr} {name}",
            f"Sehr geehrte Damen und Herren,\n\nzum Bauvorhaben {name} nehmen wir wie folgt Stellung: "
            f"{f['feuerwehr']}\n\n{_greeting(person)}\n{org}",
            when(p["permit_date"], -20, -5),
            attachments=[_attachment("Stellungnahme Feuerwehr.pdf", [
                f"# Stellungnahme der Brandschutzdienststelle",
                f"Bauvorhaben: {name}, {data.address(p)}",
                f["feuerwehr"],
                "Gegen das Bauvorhaben bestehen bei Beachtung dieser Hinweise keine Bedenken.",
                person])])

        pb = p["pruefbericht"]
        org, person, addr = _contact(p, "pruefing")
        add("subject", nr, (person, addr), lead, f"Prüfbericht Nr. {pb['nr']} – {nr} {name}",
            f"Sehr geehrte Damen und Herren,\n\nmein Prüfbericht Nr. {pb['nr']} vom {de(pb['date'])} "
            f"liegt vor. Wesentliche Feststellung: {pb['findings'][0]}\n\n{_greeting(person)}",
            when(pb["date"], 0, 2))

        org, person, addr = _contact(p, "bauherr")
        atts = []
        if f.get("bauherr_attachment"):
            atts = [_attachment(*f["bauherr_attachment"])]
        add("subject", nr, (person, addr), lead, f"{nr} – aktueller Stand {name}",
            f"Guten Tag,\n\n{f['bauherr']}\n\n{_greeting(person)}\n{org}",
            when(p["protokoll"]["date"], 5, 20), attachments=atts)

        for sp in f.get("special", []):
            org, person, addr = _contact(p, sp["role"])
            d = berlin(datetime.fromisoformat(sp["date"]).replace(hour=10, minute=15))
            add("subject", nr, (person, addr), lead, sp["subject"].format(**fmt),
                sp["body"] + f"\n\n{_greeting(person)}", d)

        # --- number only in the body -----------------------------------------------------
        if f.get("body_only"):
            subj, text = f["body_only"]
            org, person, addr = _contact(p, "architekt")
            add("body", nr, (person, addr), lead, subj,
                f"Hallo {lead[0].split()[0]},\n\n{text}\n\nViele Grüße\n{person}",
                when(p["protokoll"]["date"], -30, -3))

        # --- number only in an attachment name ---------------------------------------------
        if f.get("attach_only"):
            subj, text, fname, lines = f["attach_only"]
            org, person, addr = _contact(p, "architekt")
            add("attachment", nr, (person, addr), lead, subj, f"Hallo,\n\n{text}\n\nGruß\n{person}",
                when(p["protokoll"]["date"], -10, 10), attachments=[_attachment(fname, lines)])

        # --- no number, but clearly about this project ----------------------------------------
        role, subj, text = f["content_only"]
        org, person, addr = _contact(p, role)
        add("content", nr, (person, addr), lead, subj, f"Guten Tag,\n\n{text}",
            when(p["protokoll"]["date"], 15, 45), charset="iso-8859-1" if pi == 3 else "utf-8")

    # --- unrelated -----------------------------------------------------------------------------
    for i, (org, addr, subj, body, html, sent) in enumerate(UNRELATED):
        add("unrelated", None, (org, addr), staff_addr(i), subj, body,
            berlin(datetime.fromisoformat(sent)), html=html,
            attachments=[_attachment("Rechnung_2024-1187.pdf", [
                "# Rechnung Nr. 2024-1187", "Druckerpapier A4, 10 Pakete", "Toner schwarz, 2 Stück",
                "Aktenordner breit, 20 Stück", "Gesamtbetrag 312,40 Euro"])] if "Rechnung" in subj
            else [])
    return mails


def _html(body: str) -> str:
    paras = "".join(f"<p>{line}</p>" for line in body.split("\n") if line.strip())
    return f"<html><body><h1>Newsletter</h1>{paras}<p><a href=\"#\">Abmelden</a></p></body></html>"


def to_eml(m: dict, idx: int) -> tuple[str, bytes]:
    """Render one mail as RFC 5322 bytes (deterministic boundary and Message-ID)."""
    msg = EmailMessage()
    sender_name, sender_addr = m["from"]
    to_name, to_addr = m["to"]
    msg["From"] = f"{sender_name} <{sender_addr}>"
    msg["To"] = f"{to_name} <{to_addr}>"
    msg["Subject"] = m["subject"]
    msg["Date"] = format_datetime(m["date"])
    digest = hashlib.sha256(f"{idx}{m['subject']}".encode()).hexdigest()[:16]
    msg["Message-ID"] = f"<{digest}@{sender_addr.split('@')[1]}>"
    m["message_id"] = msg["Message-ID"]
    if m.get("in_reply_to_id"):
        msg["In-Reply-To"] = m["in_reply_to_id"]
        msg["References"] = m["in_reply_to_id"]
    if m["html"]:
        msg.set_content(_html(m["body"]), subtype="html", charset=m["charset"])
    else:
        msg.set_content(m["body"] + "\n", charset=m["charset"])
    for fname, content, mime in m["attachments"]:
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(content, maintype=maintype, subtype=subtype, filename=fname)
    if msg.is_multipart():
        msg.set_boundary(f"==projektakte-demo-{idx:03d}==")
    filename = f"{m['date'].strftime('%Y-%m-%d_%H%M')}_{idx:03d}.eml"
    return filename, msg.as_bytes()


def render_all(rng: random.Random) -> list[tuple[str, bytes, dict]]:
    mails = build_mails(rng)
    out = []
    for idx, m in enumerate(mails, start=1):
        if m["in_reply_to"] is not None:
            m["in_reply_to_id"] = mails[m["in_reply_to"]].get("message_id")
        fname, raw = to_eml(m, idx)
        out.append((fname, raw, m))
    return out
