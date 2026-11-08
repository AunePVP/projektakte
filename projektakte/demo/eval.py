"""Evaluation set: questions with known answers in the demo data (written to tests/eval.json).

Fields of a question:

* ``sources`` – acceptable sources: ``file`` (path inside the project folder), ``mail_subject``,
  ``attachment`` (file name of a mail attachment) or only ``project``. Retrieval passes if one of
  them is among the top 5 hits; an answer passes only if it cites one of them.
* ``answer_contains`` – literal strings, any of them (case-insensitive) makes the answer pass; the
  first source must contain one of them (checked against the data). ``answer_regex`` adds regular
  expressions as alternatives (for real models that phrase differently), ``answer_all`` lists
  expressions that must all match (questions with two parts).
* ``not_found`` – there is no answer: the right result is "nicht gefunden".
* ``expected_projects`` – questions over all projects: every cited source must belong to one of these
  projects, and at least ``min_projects`` (default 1) of them must be cited.
* ``category`` – fact, paraphrase, abbreviation, stand (latest state), two_source, enumeration,
  cross (no project filter), negative, trap (near miss).

No answer string may appear in the question itself.
"""
from __future__ import annotations

BG = "01 Genehmigung/Baugenehmigung.pdf"
BK = "02 Konzept/Brandschutzkonzept.docx"
STAND = "{number} – aktueller Stand {name}"
LATEST = "Was ist der letzte Stand mit dem Bauherrn?"


def _q(id_: str, project: str | None, question: str, sources: list[dict], answer: list[str], category: str,
       **extra) -> dict:
    q = {"id": id_, "project": project, "question": question, "sources": sources, "answer_contains": answer,
         "category": category}
    q.update(extra)
    return q


def _neg(id_: str, project: str | None, question: str, category: str = "negative") -> dict:
    return _q(id_, project, question, [], [], category, not_found=True)


def _stand(id_: str, number: str, name: str, fact: str) -> dict:
    return _q(id_, number, LATEST, [{"mail_subject": STAND.format(number=number, name=name)}], [fact], "stand")


EVAL = [
    # --- BS-2024-0137 Grundschule Am Lindenhof ---------------------------------------------------
    _q("gs-auflage7", "BS-2024-0137", "Was fordert Auflage 7 der Baugenehmigung für BS-2024-0137?",
       [{"file": BG}], ["Außentreppe"], "fact"),
    _q("gs-rauch", "BS-2024-0137", "Welche Auflagen zur Rauchableitung stehen in der Baugenehmigung?",
       [{"file": BG}], ["1,0 m²"], "fact"),
    _q("gs-rauch-syn", "BS-2024-0137", "Wie groß muss die Entrauchungsöffnung im Treppenhaus sein?",
       [{"file": BG}, {"file": BK}], ["1,0 m²"], "paraphrase"),
    _q("gs-fluchtweg", "BS-2024-0137", "Was muss laut Genehmigung für den zweiten Fluchtweg aus dem OG gebaut werden?",
       [{"file": BG}], ["Außentreppe"], "paraphrase"),
    _q("gs-abweichung", "BS-2024-0137", "Hat der Prüfingenieur dem Abweichungsantrag zur Flurlänge zugestimmt?",
       [{"mail_subject": "BS-2024-0137 Zustimmung zur Abweichung Flurlänge Obergeschoss"}],
       ["stimme ich der Abweichung"], "fact", answer_regex=[r"stimm\w*[^.]{0,160}\bzu\b", r"\bja\b", r"Zustimmung"]),
    _q("gs-bma-abnahme", "BS-2024-0137", "Wann ist die Abnahme der Brandmeldeanlage geplant?",
       [{"file": "04 Protokolle/2025-02-20 Besprechungsprotokoll Nr. 4.txt"}], ["18.06.2025"], "fact"),
    _q("gs-bma-abk", "BS-2024-0137", "Wann wird die BMA abgenommen?",
       [{"file": "04 Protokolle/2025-02-20 Besprechungsprotokoll Nr. 4.txt"}], ["18.06.2025"], "abbreviation"),
    _q("gs-treppe-breite", "BS-2024-0137", "Welche Breite braucht die Außentreppe?",
       [{"file": "03 Prüfung/Prüfbericht Nr. 1.md"}], ["1,20 m"], "paraphrase"),
    _q("gs-flur-og", "BS-2024-0137", "wie lang ist der flur im og",
       [{"file": BK}, {"file": BG}, {"mail_subject": "BS-2024-0137 Zustimmung zur Abweichung Flurlänge Obergeschoss"}],
       ["38 m"], "abbreviation"),
    _q("gs-feuerwehr", "BS-2024-0137", "Hat die Feuerwehr Einwände gegen das Bauvorhaben?",
       [{"attachment": "Stellungnahme Feuerwehr.pdf"},
        {"mail_subject": "Stellungnahme Brandschutzdienststelle – BS-2024-0137 Grundschule Am Lindenhof"}],
       ["keine Bedenken"], "paraphrase"),
    _stand("gs-stand", "BS-2024-0137", "Grundschule Am Lindenhof", "freigegeben"),
    # --- BS-2023-0412 Pflegeheim Sonnenhang ------------------------------------------------------
    _q("ph-feststell", "BS-2023-0412", "Welcher Mangel an der Feststellanlage wurde im Prüfbericht festgestellt?",
       [{"file": "03 Prüfung/Prüfbericht Nr. 3.md"}], ["ohne Abnahmeprüfung"], "fact"),
    _q("ph-uebung", "BS-2023-0412", "Wie oft müssen Räumungsübungen im Pflegeheim durchgeführt werden?",
       [{"file": BG}], ["zweimal jährlich"], "fact"),
    _q("ph-uebung-para", "BS-2023-0412", "Wie häufig muss geübt werden, das Heim zu evakuieren?",
       [{"file": BG}], ["zweimal jährlich"], "paraphrase"),
    _q("ph-bauherr", "BS-2023-0412", "Was ist der letzte Stand mit dem Bauherrn zur Abnahme der Feststellanlage?",
       [{"mail_subject": "BS-2023-0412 – aktueller Stand Pflegeheim Sonnenhang"}], ["12.12.2024"], "stand"),
    _stand("ph-stand", "BS-2023-0412", "Pflegeheim Sonnenhang", "12.12.2024"),
    _q("ph-neuigkeiten", "BS-2023-0412", "Gibt es Neuigkeiten vom Bauherrn?",
       [{"mail_subject": "BS-2023-0412 – aktueller Stand Pflegeheim Sonnenhang"}], ["12.12.2024"], "stand"),
    _q("ph-behoben", "BS-2023-0412", "Ist der Mangel an der Feststellanlage inzwischen behoben?",
       [{"attachment": "Abnahmeprotokoll Feststellanlage WB B.pdf"},
        {"mail_subject": "BS-2023-0412 – aktueller Stand Pflegeheim Sonnenhang"}],
       ["Keine Mängel", "12.12.2024"], "two_source"),
    _q("ph-abgenommen", "BS-2023-0412", "Wurde die Feststellanlage schon abgenommen?",
       [{"mail_subject": "BS-2023-0412 – aktueller Stand Pflegeheim Sonnenhang"},
        {"attachment": "Abnahmeprotokoll Feststellanlage WB B.pdf"},
        {"file": "04 Protokolle/2024-12-05 Besprechungsprotokoll Nr. 7.txt"}], ["12.12.2024"], "stand"),
    _q("ph-bma-kat", "BS-2023-0412", "Welche Kategorie hat die BMA im Pflegeheim?",
       [{"file": BG}, {"file": BK}], ["Kategorie 1"], "abbreviation", answer_regex=[r"Kategorie\s*1\b"]),
    _q("ph-keine-abw", "BS-2023-0412", "Wurden für das Pflegeheim Abweichungen beantragt?",
       [{"file": BK}], ["Keine Abweichungen"], "fact", answer_regex=[r"\bkeine\b", r"\bnein\b"]),
    _q("ph-genehmigung", "BS-2023-0412", "Wann wurde die Baugenehmigung erteilt?",
       [{"file": BG}, {"mail_subject": "Baugenehmigung BA-412/2023 – Pflegeheim Sonnenhang (BS-2023-0412)"}],
       ["28.09.2023"], "fact"),
    # --- BS-2024-0058 Bürogebäude Hafenkante 7 ---------------------------------------------------
    _q("bg-druck", "BS-2024-0058", "Wer muss die Druckbelüftungsanlage des Sicherheitstreppenraums abnehmen?",
       [{"file": BG}], ["Prüfsachverständigen"], "fact", answer_regex=[r"Prüfsachverständig"]),
    _q("bg-druck-para", "BS-2024-0058", "Wer nimmt die Überdruckanlage im Treppenhaus ab?",
       [{"file": BG}, {"mail_subject": "Termin Abnahme Druckbelüftung"}], ["Prüfsachverständigen"], "paraphrase",
       answer_regex=[r"Prüfsachverständig"]),
    _q("bg-rda-termin", "BS-2024-0058", "Wann soll die RDA abgenommen werden?",
       [{"file": "04 Protokolle/2025-01-15 Besprechungsprotokoll Nr. 3.txt"},
        {"mail_subject": "Termin Abnahme Druckbelüftung"}], ["März"], "abbreviation"),
    _q("bg-ueberdruck", "BS-2024-0058", "Wie hoch ist der Überdruck im Sicherheitstreppenraum?",
       [{"file": BK}], ["15 bis 50 Pa"], "paraphrase", answer_regex=[r"15\s*(bis|–|-)\s*50\s*Pa"]),
    _q("bg-zweiter-rw", "BS-2024-0058", "Braucht das Bürogebäude einen zweiten Rettungsweg?",
       [{"file": BK}], ["nicht erforderlich"], "fact", answer_regex=[r"\bnein\b", r"nicht (nötig|notwendig)"]),
    _q("bg-mieterausbau", "BS-2024-0058", "Wie wird der Mieterausbau als Großraumbüro im 4. OG bewertet?",
       [{"file": "04 Protokolle/2025-01-15 Besprechungsprotokoll Nr. 3.txt"}], ["unkritisch"], "abbreviation"),
    _stand("bg-stand", "BS-2024-0058", "Bürogebäude Hafenkante 7", "unterschrieben"),
    # --- BS-2022-0901 Tiefgarage Marktplatz -----------------------------------------------------
    _q("tg-entrauchung", "BS-2022-0901",
       "Welcher Luftwechsel ist für die maschinelle Entrauchung der Tiefgarage gefordert?",
       [{"file": BG}, {"file": BK}], ["12-fach"], "fact"),
    _q("tg-rettungsweg", "BS-2022-0901", "Wie lang darf der Rettungsweg bis zum nächsten Ausgang höchstens sein?",
       [{"file": BG}, {"file": BK}], ["30 m"], "fact"),
    _q("tg-rauch-auflagen", "BS-2022-0901", "Welche Auflagen zur Rauchableitung stehen in der Baugenehmigung?",
       [{"file": BG}], ["12-fach"], "paraphrase", answer_regex=[r"Rauch- und Wärmeabzug"]),
    _q("tg-ventilatoren", "BS-2022-0901", "Wie heiß dürfen die Entrauchungsventilatoren werden?",
       [{"file": "03 Prüfung/Prüfbericht Nr. 1.md"}], ["300 °C"], "paraphrase"),
    _stand("tg-stand", "BS-2022-0901", "Tiefgarage Marktplatz", "September"),
    _neg("tg-loeschanlage", "BS-2022-0901", "Welche Löschanlage ist in der Tiefgarage eingebaut?", "trap"),
    _neg("tg-raeumung", "BS-2022-0901", "Wann findet die nächste Räumungsübung in der Tiefgarage Marktplatz statt?",
         "trap"),
    # --- BS-2024-0233 Hotel Seeblick -----------------------------------------------------------
    _q("ho-abweichung", "BS-2024-0233", "Hat der Prüfingenieur der Abweichung zur Länge des Stichflurs zugestimmt?",
       [{"mail_subject": "Abweichungsantrag Stichflur 3. OG – BS-2024-0233 Hotel Seeblick"}],
       ["abgelehnt", "nicht zustimmen"], "fact", answer_regex=[r"\bnein\b"]),
    _q("ho-sicherheitsbeleuchtung", "BS-2024-0233", "Welche Betriebsdauer muss die Sicherheitsbeleuchtung haben?",
       [{"file": BG}, {"file": BK}], ["8 Stunden"], "fact", answer_regex=[r"\b8\s*(Stunden|h)\b"]),
    _q("ho-notlicht", "BS-2024-0233", "Wie lange muss das Notlicht funktionieren?",
       [{"file": BG}, {"file": BK}], ["8 Stunden"], "paraphrase", answer_regex=[r"\b8\s*(Stunden|h)\b"]),
    _q("ho-wendehals", "BS-2024-0233", "Hat Frau Wendehals den Antrag zum Stichflur abgelehnt?",
       [{"mail_subject": "Abweichungsantrag Stichflur 3. OG – BS-2024-0233 Hotel Seeblick"}],
       ["nicht zustimmen"], "paraphrase", answer_regex=[r"\bja\b", r"kann ich nicht"]),
    _q("ho-fluchtbalkon", "BS-2024-0233", "Warum wird ein Fluchtbalkon geplant und wie breit wird er?",
       [{"attachment": "BS-2024-0233_Fluchtbalkon_Entwurf.docx"},
        {"file": "04 Protokolle/2024-11-06 Besprechungsprotokoll Nr. 2.txt"},
        {"mail_subject": "Abweichungsantrag Stichflur 3. OG – BS-2024-0233 Hotel Seeblick"}],
       ["1,0 m"], "two_source", answer_all=[r"1,0\s*m", r"abgelehnt|Ersatz|empfehle|nicht zustimm"]),
    _q("ho-sprachen", "BS-2024-0233", "In welchen Sprachen wird im Hotel alarmiert?",
       [{"file": BG}, {"file": BK}], ["englisch"], "paraphrase"),
    _stand("ho-stand", "BS-2024-0233", "Hotel Seeblick", "Altbau"),
    _neg("ho-fahrrad", "BS-2024-0233", "Wie viele Fahrradstellplätze mit Ladestation sind geplant?"),
    _neg("ho-sprinkler", "BS-2024-0233", "Welche Sprinkleranlage ist im Hotel Seeblick vorgesehen?"),
    # --- BS-2023-0777 Industriehalle Nordring ---------------------------------------------------
    _q("in-loeschwasser", "BS-2023-0777", "Wie hoch ist der Löschwasserbedarf der Industriehalle Nordring?",
       [{"file": BK}], ["192 m³/h"], "fact"),
    _q("in-wasser-para", "BS-2023-0777", "Wie viel Wasser zum Löschen wird für die Halle benötigt?",
       [{"file": BK}], ["192 m³/h"], "paraphrase"),
    _q("in-rwa", "BS-2023-0777", "Wie groß ist die RWA-Fläche im Dach der Halle?",
       [{"file": BK}, {"file": BG}], ["72 m²"], "abbreviation"),
    _stand("in-stand", "BS-2023-0777", "Industriehalle Nordring", "Aushub"),
    # --- BS-2025-0019 Kita Regenbogen ------------------------------------------------------------
    _q("kt-rauchwarnmelder", "BS-2025-0019", "Welche Rauchwarnmelder sind in den Schlafräumen der Kita vorgesehen?",
       [{"file": BG}, {"file": BK}], ["funkvernetzt"], "fact"),
    _q("kt-rauchmelder-para", "BS-2025-0019", "Was für Rauchmelder kommen in die Schlafräume?",
       [{"file": BG}, {"file": BK}], ["funkvernetzt"], "paraphrase"),
    _q("kt-handlauf", "BS-2025-0019", "Wie hoch soll der Handlauf für die Kinder sein?",
       [{"file": "04 Protokolle/2025-07-01 Besprechungsprotokoll Nr. 2.txt"},
        {"mail_subject": "Handlauf Balkontreppe"}], ["60 cm"], "paraphrase"),
    _q("kt-zweiter-rw", "BS-2025-0019", "Wie ist der zweite Rettungsweg aus dem OG der Kita gelöst?",
       [{"file": BG}, {"file": BK}], ["Balkon"], "abbreviation"),
    _q("kt-evakuierung", "BS-2025-0019", "Wie oft wird mit den Kindern die Evakuierung geübt?",
       [{"file": BK}, {"mail_subject": "Stellungnahme Brandschutzdienststelle – BS-2025-0019 Kita Regenbogen"}],
       ["je Halbjahr"], "paraphrase", answer_regex=[r"halbj"]),
    _stand("kt-stand", "BS-2025-0019", "Kita Regenbogen", "Rahmenvertrag"),
    # --- BS-2024-0311 Wohnhochhaus Am Stadtpark -------------------------------------------------
    _q("wh-aufzug", "BS-2024-0311", "Was gilt für den Feuerwehraufzug im Wohnhochhaus?",
       [{"file": BG}, {"file": BK}, {"file": "03 Prüfung/Prüfbericht Nr. 1.md"},
        {"mail_subject": "Stellungnahme Brandschutzdienststelle – BS-2024-0311 Wohnhochhaus Am Stadtpark"},
        {"attachment": "BS-2024-0311_Feuerwehraufzug_Steuerung.pdf"}],
       ["Vorraum"], "fact", answer_regex=[r"erprob", r"Feuerwehrbetrieb"]),
    _q("wh-aufstell", "BS-2024-0311", "Was hat die Feuerwehr zur Aufstellfläche an der Parkallee angemerkt?",
       [{"mail_subject": "Stellungnahme Brandschutzdienststelle – BS-2024-0311 Wohnhochhaus Am Stadtpark"}],
       ["12 m"], "fact"),
    _q("wh-drehleiter", "BS-2024-0311", "Was sagt die Feuerwehr zur Drehleiterfläche an der Parkallee?",
       [{"mail_subject": "Stellungnahme Brandschutzdienststelle – BS-2024-0311 Wohnhochhaus Am Stadtpark"}],
       ["12 m"], "paraphrase"),
    _q("wh-vorraum", "BS-2024-0311", "Wie groß muss der Vorraum des Feuerwehraufzugs mindestens sein?",
       [{"file": "03 Prüfung/Prüfbericht Nr. 1.md"}], ["6 m²"], "fact"),
    _q("wh-steigleitung", "BS-2024-0311", "Welche Art von Steigleitung bekommt das Hochhaus?",
       [{"file": BG}, {"file": BK}], ["nass"], "paraphrase"),
    _q("wh-druckerhoehung", "BS-2024-0311", "Was hat der Prüfingenieur zur Druckerhöhungsanlage gefordert?",
       [{"file": "03 Prüfung/Prüfbericht Nr. 1.md"},
        {"mail_subject": "Prüfbericht Nr. 1 – BS-2024-0311 Wohnhochhaus Am Stadtpark"}], ["redundant"], "fact"),
    _stand("wh-stand", "BS-2024-0311", "Wohnhochhaus Am Stadtpark", "Baumreihe"),
    # --- over all projects ------------------------------------------------------------------------
    _q("x-druckbelueftung", None,
       "In welchem Projekt ist eine Druckbelüftungsanlage für einen Sicherheitstreppenraum gefordert?",
       [{"project": "BS-2024-0058", "file": BG}, {"project": "BS-2024-0058", "file": BK},
        {"project": "BS-2024-0311", "file": BK}, {"project": "BS-2024-0311", "file": BG}],
       ["BS-2024-0058", "Hafenkante", "BS-2024-0311", "Stadtpark"], "cross",
       expected_projects=["BS-2024-0058", "BS-2024-0311"]),
    _q("x-loeschwasserteich", None, "Für welches Bauvorhaben wird ein Löschwasserteich mit Saugstelle gebaut?",
       [{"project": "BS-2023-0777", "file": "03 Prüfung/Prüfbericht Nr. 2.md"},
        {"project": "BS-2023-0777", "file": BK}, {"project": "BS-2023-0777", "file": BG},
        {"project": "BS-2023-0777",
         "mail_subject": "Stellungnahme Brandschutzdienststelle – BS-2023-0777 Industriehalle Nordring"}],
       ["BS-2023-0777", "Nordring"], "cross", expected_projects=["BS-2023-0777"]),
    _q("x-wo-teich", None, "Wo wird ein Löschwasserteich angelegt?",
       [{"project": "BS-2023-0777", "file": "03 Prüfung/Prüfbericht Nr. 2.md"},
        {"project": "BS-2023-0777", "file": BK}, {"project": "BS-2023-0777", "file": BG},
        {"project": "BS-2023-0777", "file": "04 Protokolle/2024-03-07 Besprechungsprotokoll Nr. 6.txt"},
        {"project": "BS-2023-0777", "mail_subject": STAND.format(number="BS-2023-0777", name="Industriehalle Nordring")}],
       ["BS-2023-0777", "Nordring"], "cross", expected_projects=["BS-2023-0777"]),
    _q("x-sprachalarm", None, "In welchen Projekten ist eine Sprachalarmanlage vorgesehen?",
       [{"project": "BS-2024-0137", "file": BG}, {"project": "BS-2024-0233", "file": BG},
        {"project": "BS-2024-0137", "file": BK}, {"project": "BS-2024-0233", "file": BK}],
       ["BS-2024-0137"], "enumeration", expected_projects=["BS-2024-0137", "BS-2024-0233"], min_projects=2,
       answer_all=[r"BS-2024-0137|Grundschule", r"BS-2024-0233|Hotel Seeblick"]),
    _q("x-wendehals", None, "Welche Projekte hat Frau Wendehals geprüft?",
       [{"project": p, "file": f"03 Prüfung/Prüfbericht Nr. {nr}.md"} for p, nr in (
           ("BS-2024-0137", 1), ("BS-2023-0412", 3), ("BS-2024-0058", 2), ("BS-2022-0901", 1),
           ("BS-2024-0233", 1), ("BS-2023-0777", 2), ("BS-2025-0019", 1), ("BS-2024-0311", 1))],
       ["Prüfbericht"], "enumeration",
       expected_projects=["BS-2024-0137", "BS-2023-0412", "BS-2024-0058", "BS-2022-0901", "BS-2024-0233",
                          "BS-2023-0777", "BS-2025-0019", "BS-2024-0311"], min_projects=8,
       answer_all=[r"BS-20\d\d-\d{4}"]),
    _q("x-nasse-steigleitung", None, "In welchen Projekten sind nasse Steigleitungen gefordert?",
       [{"project": "BS-2024-0311", "file": BG}, {"project": "BS-2024-0311", "file": BK}],
       ["BS-2024-0311", "Stadtpark"], "trap", expected_projects=["BS-2024-0311"]),
    _q("x-bma-abnahme", None, "Wann ist die Abnahme der Brandmeldeanlage geplant?",
       [{"project": "BS-2024-0137", "file": "04 Protokolle/2025-02-20 Besprechungsprotokoll Nr. 4.txt"}],
       ["18.06.2025"], "cross", expected_projects=["BS-2024-0137"]),
    _q("x-loeschwasser-schule", None, "Wie viel Löschwasser braucht die Grundschule?",
       [{"project": "BS-2024-0137", "file": BK}], ["96 m³/h"], "cross", expected_projects=["BS-2024-0137"]),
    _q("x-pruefing-hotel", None, "Wer ist der Prüfingenieur im Hotel Seeblick?",
       [{"project": "BS-2024-0233", "file": "03 Prüfung/Prüfbericht Nr. 1.md"},
        {"project": "BS-2024-0233", "mail_subject": "Prüfbericht Nr. 1 – BS-2024-0233 Hotel Seeblick"}],
       ["Wendehals"], "cross", expected_projects=["BS-2024-0233"]),
    _q("x-rettungsweg-halle", None, "Wie lang darf der Rettungsweg in der Industriehalle höchstens sein?",
       [{"project": "BS-2023-0777", "file": BK}], ["35 m"], "trap", expected_projects=["BS-2023-0777"]),
    _q("x-feuerwehr-einfahrt", None, "Kann die Feuerwehr mit ihren Fahrzeugen in die Tiefgarage einfahren?",
       [{"project": "BS-2022-0901",
         "mail_subject": "Stellungnahme Brandschutzdienststelle – BS-2022-0901 Tiefgarage Marktplatz"}],
       ["nicht ausreichend", "zu Fuß"], "cross", expected_projects=["BS-2022-0901"]),
    _neg("x-fahrrad-hotel", None, "Wie viele Fahrradstellplätze hat das Hotel Seeblick?"),
    _neg("x-sprinkler", None, "Welche Projekte haben eine Sprinkleranlage?"),
    _neg("x-kantine", None, "Welche Kantinenpreise gelten für Mitarbeitende im Sommer?"),
    _neg("x-fussball", None, "Wer hat die Fußball-WM 2014 gewonnen?"),
    _neg("x-feuerwiderstand", None, "Welchen Feuerwiderstand müssen die tragenden Wände der Grundschule haben?",
         "trap"),
    _neg("x-honorar", None, "Wie hoch ist das Honorar für das Brandschutzkonzept der Kita Regenbogen?"),
]
