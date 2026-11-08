"""Fictional demo projects. All names, companies, addresses and towns are invented.

E-mail addresses use the reserved ``.example`` domain (RFC 2606), so none can belong to anyone.
Facts marked in comments are referenced by the evaluation set (tests/eval.json).
"""
from __future__ import annotations

OFFICE = {
    "name": "Ingenieurbüro Feuerfest",
    "domain": "ib-feuerfest.example",
    "staff": [("Tobias Brenner", "t.brenner"), ("Lea Funke", "l.funke"), ("Murat Yilmaz", "m.yilmaz")],
}

BAUAMT = {
    "Musterstadt": ("Bauaufsichtsamt Musterstadt", "Jens Pfeiffer", "bauaufsicht@musterstadt.example"),
    "Beispielstadt": ("Untere Bauaufsicht Beispielstadt", "Karin Obermeier",
                      "bauaufsicht@beispielstadt.example"),
    "Fiktivhausen": ("Bauordnungsamt Fiktivhausen", "Ralf Dreesen", "bauordnung@fiktivhausen.example"),
}

FEUERWEHR = {
    "Musterstadt": ("Feuerwehr Musterstadt, Vorbeugender Brandschutz", "Brandoberinspektor Uwe Kessler",
                    "vb@feuerwehr-musterstadt.example"),
    "Beispielstadt": ("Feuerwehr Beispielstadt, Abteilung Gefahrenvorbeugung", "Brandamtsrätin Nina Brück",
                      "gefahrenvorbeugung@feuerwehr-beispielstadt.example"),
    "Fiktivhausen": ("Freiwillige Feuerwehr Fiktivhausen", "Stadtbrandmeister Holger Lamm",
                     "wehrfuehrung@feuerwehr-fiktivhausen.example"),
}

PRUEFING = ("Prüfingenieurbüro Wendehals", "Dipl.-Ing. Sabine Wendehals", "pruefung@wendehals-pi.example")

ARCHITEKTEN = {
    "nordlicht": ("Atelier Nordlicht Architekten", "Jonas Feld", "feld@nordlicht-architekten.example"),
    "meier": ("Baukunst Meier & Partner", "Clara Meier", "c.meier@baukunst-meier.example"),
    "strebewerk": ("Ingenieurbüro Strebewerk", "Paul Hinz", "hinz@strebewerk.example"),
}

# Generic conditions used to fill the numbered list of each building permit.
GENERIC_AUFLAGEN = [
    ("Kennzeichnung der Rettungswege",
     "Die Rettungswege sind ständig freizuhalten und mit Sicherheitszeichen nach DIN ISO 7010 zu "
     "kennzeichnen. Die Zeichen müssen beleuchtet oder langnachleuchtend sein."),
    ("Feuerwehrzufahrt",
     "Zufahrten, Aufstellflächen und Bewegungsflächen für die Feuerwehr sind nach der Muster-Richtlinie "
     "über Flächen für die Feuerwehr herzustellen, dauerhaft zu kennzeichnen und freizuhalten."),
    ("Feuerlöscher",
     "Feuerlöscher sind nach ASR A2.2 in ausreichender Anzahl gut sichtbar und leicht erreichbar "
     "bereitzustellen und alle zwei Jahre durch einen Sachkundigen zu prüfen."),
    ("Brandschutzordnung",
     "Vor Nutzungsaufnahme ist eine Brandschutzordnung nach DIN 14096 (Teile A bis C) aufzustellen "
     "und den Beschäftigten bekannt zu machen."),
    ("Feuerwehrpläne",
     "Feuerwehrpläne nach DIN 14095 sind vor Nutzungsaufnahme mit der Brandschutzdienststelle "
     "abzustimmen und in zweifacher Ausfertigung zu übergeben."),
    ("Abschottungen",
     "Leitungsdurchführungen durch raumabschließende Bauteile mit Feuerwiderstand sind mit "
     "zugelassenen Abschottungssystemen zu verschließen und zu kennzeichnen."),
    ("Prüfung sicherheitstechnischer Anlagen",
     "Die sicherheitstechnischen Anlagen sind vor der ersten Inbetriebnahme und danach wiederkehrend "
     "durch Prüfsachverständige nach der Prüfverordnung zu prüfen."),
    ("Brandschutztüren",
     "Türen in Brandwänden sind feuerbeständig, dicht- und selbstschließend auszuführen (T90-RS). "
     "Keile und Haken zum Offenhalten sind unzulässig."),
    ("Blitzschutz",
     "Das Gebäude ist mit einer Blitzschutzanlage nach DIN EN 62305 auszustatten."),
    ("Brandschutzbeauftragter",
     "Der Betreiber hat einen Brandschutzbeauftragten zu bestellen und der Bauaufsicht zu benennen."),
]

# fmt: off
PROJECTS = [
    {
        "number": "BS-2024-0137",
        "name": "Grundschule Am Lindenhof",
        "kind": "Schule",
        "street": "Lindenhofweg 4", "zip": "31999", "town": "Musterstadt",
        "bauherr": ("Stadt Musterstadt, Amt für Gebäudewirtschaft", "Renate Albers",
                    "r.albers@musterstadt.example"),
        "architekt": "nordlicht",
        "az": "63-0137/24", "permit_date": "2024-03-12",
        "vorhaben": "Neubau einer dreizügigen Grundschule mit Einfeld-Sporthalle, zwei Vollgeschosse, "
                    "Gebäudeklasse 3, Sonderbau nach der Schulbaurichtlinie, rund 300 Schülerinnen und Schüler.",
        "auflagen": {
            3: ("Brandmeldeanlage",
                "Das Schulgebäude ist mit einer Brandmeldeanlage der Kategorie 2 (Teilschutz in Fluren, "
                "Treppenräumen und Technikräumen) nach DIN 14675 auszustatten. Die Alarmierung erfolgt "
                "über eine Sprachalarmanlage."),
            # FACT gs-rauch
            4: ("Rauchableitung",
                "Die notwendigen Treppenräume erhalten an oberster Stelle eine Öffnung zur Rauchableitung "
                "mit mindestens 1,0 m² freiem Querschnitt. Die Öffnung muss vom Erdgeschoss und vom "
                "obersten Treppenabsatz aus geöffnet werden können."),
            # FACT gs-auflage7
            7: ("Rettungswege",
                "Für die Unterrichtsräume im Obergeschoss ist ein zweiter baulicher Rettungsweg über eine "
                "Außentreppe an der Ostseite des Gebäudes herzustellen. Die Außentreppe ist aus "
                "nichtbrennbaren Baustoffen auszuführen."),
            9: ("Abweichungen",
                "Die beantragte Abweichung von der Schulbaurichtlinie zur Länge des notwendigen Flurs im "
                "Obergeschoss (38 m statt 30 m) wird nur zugelassen, wenn der Prüfingenieur der "
                "Kompensation durch eine Rauchschutztür in Flurmitte zustimmt."),
        },
        "konzept": {
            "allgemein": "Die Grundschule Am Lindenhof wird als zweigeschossiger Massivbau errichtet. "
                         "Die Sporthalle bildet einen eigenen Brandabschnitt.",
            "rettungswege": "Jeder Unterrichtsraum hat zwei voneinander unabhängige Rettungswege. Der erste "
                            "führt über den notwendigen Flur in die beiden Treppenräume, der zweite über "
                            "die neue Außentreppe an der Ostseite.",
            "anlagen": "Brandmeldeanlage Kategorie 2 mit Sprachalarmanlage; Rauchableitung der Treppenräume "
                       "über Öffnungen mit 1,0 m² freiem Querschnitt; Sicherheitsbeleuchtung in Fluren.",
            "loeschwasser": "Der Grundschutz von 96 m³/h über zwei Stunden wird aus dem öffentlichen Netz "
                            "über zwei Hydranten im Lindenhofweg sichergestellt.",
            "abweichungen": "Der Flur im Obergeschoss ist 38 m lang. Kompensation: Rauchschutztür in "
                            "Flurmitte, Brandmeldeanlage im Flur, zweiter Rettungsweg über die Außentreppe.",
            "organisation": "Zwei Räumungsübungen je Schuljahr; Hausmeister als Brandschutzhelfer.",
        },
        "kennwerte": [("Gebäudeklasse", "3"), ("Geschosse", "2"), ("Nutzer", "ca. 330 Personen"),
                      ("Löschwasser", "96 m³/h über 2 h")],
        "pruefbericht": {
            "date": "2024-06-03", "nr": 1,
            "findings": [
                "Die Kompensation der Flurlängenüberschreitung durch eine Rauchschutztür in Flurmitte ist "
                "schlüssig und wird mitgetragen.",
                "Die Außentreppe an der Ostseite ist in den Ausführungsplänen mit 1,20 m nutzbarer Breite "
                "darzustellen.",
                "Die Ansteuerung der Rauchableitung durch die Brandmeldeanlage ist in der Steuermatrix zu "
                "ergänzen.",
            ],
        },
        "protokoll": {
            "date": "2025-02-20", "nr": 4,
            "items": [
                # FACT gs-bma-abnahme
                ("Brandmeldeanlage", "Die Abnahme der Brandmeldeanlage durch den Prüfsachverständigen ist "
                                     "für den 18.06.2025 geplant. Der Errichter liefert vorher die "
                                     "Steuermatrix."),
                ("Außentreppe", "Die Stahlbauarbeiten an der Außentreppe beginnen in KW 14."),
                ("Feuerwehrpläne", "Entwurf der Feuerwehrpläne wird bis Ende April mit der Feuerwehr "
                                   "Musterstadt abgestimmt."),
            ],
        },
        "image": True,
    },
    {
        "number": "BS-2023-0412",
        "name": "Pflegeheim Sonnenhang",
        "kind": "Pflegeheim",
        "street": "Am Sonnenhang 21", "zip": "31888", "town": "Beispielstadt",
        "bauherr": ("Sonnenhang Seniorenresidenz gGmbH", "Matthias Kortmann",
                    "kortmann@sonnenhang-senioren.example"),
        "architekt": "meier",
        "az": "BA-412/2023", "permit_date": "2023-09-28",
        "vorhaben": "Neubau eines Pflegeheims mit 96 Pflegeplätzen in vier Wohnbereichen, drei Vollgeschosse, "
                    "Gebäudeklasse 4, Sonderbau.",
        "auflagen": {
            2: ("Rauchabschnitte",
                "Die Flure der Wohnbereiche sind durch Rauchschutztüren in Rauchabschnitte von höchstens "
                "30 m Länge zu unterteilen."),
            4: ("Rauchableitung",
                "Die Treppenräume erhalten an oberster Stelle Öffnungen zur Rauchableitung mit mindestens "
                "1,0 m² freiem Querschnitt, die von der Brandmeldeanlage angesteuert werden."),
            5: ("Brandmeldeanlage",
                "Es ist eine flächendeckende Brandmeldeanlage der Kategorie 1 zu errichten. Die Rauchmelder "
                "in den Bewohnerzimmern sind auf die Brandmeldeanlage aufzuschalten; das Personal wird "
                "still über Pager alarmiert."),
            6: ("Feststellanlagen",
                "Rauchschutztüren in Fluren, die offen gehalten werden sollen, erhalten bauaufsichtlich "
                "zugelassene Feststellanlagen, die bei Raucherkennung selbsttätig auslösen."),
            # FACT ph-uebung
            8: ("Räumungsübungen",
                "Räumungsübungen sind mindestens zweimal jährlich durchzuführen, davon eine während der "
                "Nachtschicht. Die Übungen sind zu dokumentieren."),
        },
        "konzept": {
            "allgemein": "Das Pflegeheim Sonnenhang ist ein dreigeschossiger Bau mit vier Wohnbereichen "
                         "zu je 24 Plätzen.",
            "rettungswege": "Horizontale Evakuierung in den benachbarten Rauchabschnitt; jeder Wohnbereich "
                            "hat zwei Treppenräume.",
            "anlagen": "Brandmeldeanlage Kategorie 1, Feststellanlagen an Rauchschutztüren, "
                       "Sicherheitsbeleuchtung, Rauchableitung der Treppenräume.",
            "loeschwasser": "Löschwasserbedarf 96 m³/h über zwei Stunden aus dem Netz der Stadtwerke.",
            "abweichungen": "Keine Abweichungen beantragt.",
            "organisation": "Nachts mindestens drei Pflegekräfte im Haus; Evakuierungshilfen (Rettungstücher) "
                            "in jedem Zimmer.",
        },
        "kennwerte": [("Gebäudeklasse", "4"), ("Pflegeplätze", "96"), ("Rauchabschnitt", "max. 30 m"),
                      ("Löschwasser", "96 m³/h über 2 h")],
        "pruefbericht": {
            "date": "2024-11-14", "nr": 3,
            "findings": [
                # FACT ph-feststell
                "Mangel: Die Feststellanlage der Rauchschutztür T30-RS im 2. Obergeschoss (Wohnbereich B) "
                "wurde ohne Abnahmeprüfung in Betrieb genommen. Die Abnahme durch eine Fachkraft ist "
                "nachzuholen.",
                "Die Aufschaltung der Zimmerrauchmelder auf die Brandmeldeanlage wurde stichprobenartig "
                "geprüft und funktioniert.",
                "Die Rettungstücher sind in allen Zimmern vorhanden.",
            ],
        },
        "protokoll": {
            "date": "2024-12-05", "nr": 7,
            "items": [
                ("Feststellanlage", "Der Türhersteller führt die Abnahme der Feststellanlage im Wohnbereich B "
                                    "am 12.12.2024 durch."),
                ("Räumungsübung", "Die nächste Räumungsübung in der Nachtschicht findet im Januar statt."),
                ("Pager", "Die Pager-Alarmierung wird um den Wohnbereich D erweitert."),
            ],
        },
    },
    {
        "number": "BS-2024-0058",
        "name": "Bürogebäude Hafenkante 7",
        "kind": "Bürogebäude",
        "street": "Hafenkante 7", "zip": "31999", "town": "Musterstadt",
        "bauherr": ("Hafenkante Immobilien GmbH", "Dr. Frederik Lanz", "lanz@hafenkante-immobilien.example"),
        "architekt": "nordlicht",
        "az": "63-0058/24", "permit_date": "2024-05-21",
        "vorhaben": "Neubau eines achtgeschossigen Bürogebäudes mit Tiefgarage, Gebäudehöhe 21,5 m, "
                    "Gebäudeklasse 5, ein innenliegender Sicherheitstreppenraum.",
        "auflagen": {
            3: ("Sicherheitstreppenraum",
                "Der einzige Treppenraum ist als Sicherheitstreppenraum mit Druckbelüftungsanlage "
                "auszuführen."),
            # FACT bg-druck
            5: ("Druckbelüftungsanlage",
                "Die Druckbelüftungsanlage des Sicherheitstreppenraums ist vor Inbetriebnahme durch einen "
                "Prüfsachverständigen für Rauchabzugsanlagen abzunehmen. Der Prüfbericht ist der "
                "Bauaufsicht vorzulegen."),
            7: ("Brandmeldeanlage",
                "Eine Brandmeldeanlage der Kategorie 1 mit Ansteuerung der Druckbelüftungsanlage ist zu "
                "errichten und auf die Leitstelle aufzuschalten."),
        },
        "konzept": {
            "allgemein": "Das Bürogebäude Hafenkante 7 hat acht Geschosse mit offenen Bürobereichen.",
            "rettungswege": "Erster Rettungsweg über den Sicherheitstreppenraum; ein zweiter Rettungsweg ist "
                            "wegen des Sicherheitstreppenraums nicht erforderlich.",
            "anlagen": "Die Druckbelüftungsanlage hält im Sicherheitstreppenraum einen Überdruck von 15 bis "
                       "50 Pa; die Abströmung erfolgt über Schächte in den Geschossen.",
            "loeschwasser": "Löschwasserbedarf 96 m³/h; trockene Steigleitung im Sicherheitstreppenraum.",
            "abweichungen": "Keine.",
            "organisation": "Brandschutzhelfer: mindestens 5 % der Beschäftigten je Mieteinheit.",
        },
        "kennwerte": [("Gebäudeklasse", "5"), ("Geschosse", "8"), ("Höhe", "21,5 m"),
                      ("Überdruck Treppenraum", "15–50 Pa")],
        "pruefbericht": {
            "date": "2024-09-09", "nr": 2,
            "findings": [
                "Die Auslegung der Druckbelüftungsanlage ist nachvollziehbar; die Türöffnungskraft von "
                "100 N wird eingehalten.",
                "Die Abströmschächte sind in den Grundrissen aller Geschosse darzustellen.",
            ],
        },
        "protokoll": {
            "date": "2025-01-15", "nr": 3,
            "items": [
                ("Mieterausbau", "Der Mieterausbau im 4. OG als Großraumbüro ist brandschutztechnisch "
                                 "unkritisch, sofern die Flurtrennwände entfallen."),
                ("Druckbelüftung", "Die Abnahme der Druckbelüftungsanlage ist für März vorgesehen."),
            ],
        },
    },
    {
        "number": "BS-2022-0901",
        "name": "Tiefgarage Marktplatz",
        "kind": "Tiefgarage",
        "street": "Marktplatz 1", "zip": "31888", "town": "Beispielstadt",
        "bauherr": ("Parkhausgesellschaft Beispielstadt mbH", "Ingrid Vogt", "vogt@parken-beispielstadt.example"),
        "architekt": "strebewerk",
        "az": "BA-901/2022", "permit_date": "2022-11-30",
        "vorhaben": "Neubau einer Großgarage mit zwei Untergeschossen und 240 Einstellplätzen unter dem "
                    "Marktplatz.",
        "auflagen": {
            # FACT tg-rettungsweg
            2: ("Rettungswege",
                "Von jeder Stelle eines Einstellplatzes muss in höchstens 30 m Entfernung ein Ausgang zu "
                "einem Treppenraum oder ins Freie erreichbar sein."),
            # FACT tg-entrauchung
            4: ("Rauch- und Wärmeabzug",
                "Die Tiefgarage erhält eine maschinelle Rauch- und Wärmeabzugsanlage, die einen mindestens "
                "12-fachen Luftwechsel je Stunde sicherstellt. Die Zuluft wird über die Rampe nachgeführt."),
            6: ("CO-Warnanlage",
                "Eine CO-Warnanlage mit Ansteuerung der Lüftung und optischer Warnung an den Einfahrten ist "
                "einzubauen."),
        },
        "konzept": {
            "allgemein": "Die Tiefgarage Marktplatz hat zwei Ebenen mit je rund 3.100 m² Nutzfläche.",
            "rettungswege": "Vier Treppenräume, Rettungsweglänge höchstens 30 m bis zum nächsten Ausgang.",
            "anlagen": "Maschinelle Entrauchung mit 12-fachem Luftwechsel, CO-Warnanlage, "
                       "Sicherheitsbeleuchtung, Brandmeldeanlage in den Technikräumen.",
            "loeschwasser": "Löschwasserbedarf 96 m³/h; trockene Steigleitungen in allen Treppenräumen.",
            "abweichungen": "Die lichte Einfahrtshöhe von 2,10 m ist mit der Feuerwehr abgestimmt.",
            "organisation": "Videoüberwachung mit Aufschaltung auf die Parkhauszentrale.",
        },
        "kennwerte": [("Einstellplätze", "240"), ("Ebenen", "2"), ("Luftwechsel Entrauchung", "12-fach"),
                      ("Rettungsweglänge", "max. 30 m")],
        "pruefbericht": {
            "date": "2023-04-17", "nr": 1,
            "findings": [
                "Die Zuluftnachströmung über die Rampe ist rechnerisch nachgewiesen.",
                "Die Ventilatoren der Entrauchung sind für 300 °C über 60 Minuten auszulegen.",
            ],
        },
        "protokoll": {
            "date": "2023-06-22", "nr": 5,
            "items": [
                ("Entrauchung", "Der Probebetrieb der Entrauchungsanlage ist erfolgreich verlaufen."),
                ("CO-Warnanlage", "Die Sensoren werden auf 1,5 m Höhe montiert."),
            ],
        },
    },
    {
        "number": "BS-2024-0233",
        "name": "Hotel Seeblick",
        "kind": "Hotel",
        "street": "Uferpromenade 15", "zip": "31777", "town": "Fiktivhausen",
        "bauherr": ("Seeblick Hotelbetriebe GmbH", "Konstantin Rehberg", "rehberg@seeblick-hotels.example"),
        "architekt": "meier",
        "az": "BO-233/24", "permit_date": "2024-07-02",
        "vorhaben": "Umbau und Erweiterung eines Hotels auf 120 Gastbetten, vier Obergeschosse, "
                    "Beherbergungsstätte nach der Beherbergungsstättenverordnung.",
        "auflagen": {
            # FACT ho-sicherheitsbeleuchtung
            4: ("Sicherheitsbeleuchtung",
                "In allen Fluren, Treppenräumen und im Restaurant ist eine Sicherheitsbeleuchtung mit einer "
                "Betriebsdauer von mindestens 8 Stunden vorzusehen."),
            5: ("Rauchableitung",
                "Beide Treppenräume erhalten an oberster Stelle eine Öffnung zur Rauchableitung mit mindestens "
                "1,0 m² freiem Querschnitt, auslösbar über Rauchmelder und Handtaster im Erdgeschoss."),
            6: ("Brandmeldeanlage",
                "Die Gästezimmer erhalten Rauchmelder mit Aufschaltung auf die Brandmeldeanlage. Die "
                "Alarmierung erfolgt über eine Sprachalarmanlage in deutscher und englischer Sprache."),
            9: ("Abweichungen",
                "Über den Antrag auf Abweichung zur Länge des Stichflurs im 3. Obergeschoss (17 m statt "
                "15 m) wird nach Vorlage der Stellungnahme des Prüfingenieurs entschieden."),
        },
        "konzept": {
            "allgemein": "Das Hotel Seeblick liegt direkt an der Uferpromenade und wird um einen Flügel "
                         "erweitert.",
            "rettungswege": "Zwei Treppenräume; der Stichflur im 3. Obergeschoss ist 17 m lang.",
            "anlagen": "Brandmeldeanlage mit Zimmerrauchmeldern, Sprachalarmanlage zweisprachig, "
                       "Sicherheitsbeleuchtung mit 8 Stunden Betriebsdauer, Rauchableitung der Treppenräume.",
            "loeschwasser": "Löschwasserbedarf 96 m³/h, zusätzlich Saugstelle am See.",
            "abweichungen": "Für den Stichflur im 3. OG (17 m) wird eine Abweichung beantragt.",
            "organisation": "Nachtportier als Brandschutzhelfer; Fluchtpläne in jedem Gästezimmer.",
        },
        "kennwerte": [("Gastbetten", "120"), ("Obergeschosse", "4"), ("Stichflur 3. OG", "17 m"),
                      ("Sicherheitsbeleuchtung", "8 h")],
        "pruefbericht": {
            "date": "2024-10-01", "nr": 1,
            "findings": [
                "Die Länge des Stichflurs im 3. Obergeschoss überschreitet das zulässige Maß von 15 m.",
                "Die zweisprachige Sprachalarmanlage ist in der Steuermatrix zu berücksichtigen.",
            ],
        },
        "protokoll": {
            "date": "2024-11-06", "nr": 2,
            "items": [
                ("Stichflur", "Als Ersatz für die abgelehnte Abweichung wird ein Fluchtbalkon am Ende des "
                              "Stichflurs im 3. OG geplant."),
                ("Bauzeit", "Der Hotelbetrieb läuft während der Bauzeit im Altbau weiter."),
            ],
        },
    },
    {
        "number": "BS-2023-0777",
        "name": "Industriehalle Nordring",
        "kind": "Industriehalle",
        "street": "Nordring 88", "zip": "31999", "town": "Musterstadt",
        "bauherr": ("Nordring Logistik AG", "Svenja Holt", "holt@nordring-logistik.example"),
        "architekt": "strebewerk",
        "az": "63-0777/23", "permit_date": "2023-08-15",
        "vorhaben": "Neubau einer Lager- und Produktionshalle mit 7.200 m² Grundfläche nach der "
                    "Industriebaurichtlinie, eingeschossig.",
        "auflagen": {
            2: ("Brandabschnitte",
                "Die Halle ist durch eine Brandwand in zwei Brandabschnitte von je höchstens 3.600 m² zu "
                "unterteilen."),
            4: ("Rauchableitung",
                "Die Halle erhält natürliche Rauchabzugsgeräte mit einer aerodynamisch wirksamen Fläche von "
                "mindestens 72 m² im Dach, gleichmäßig auf beide Brandabschnitte verteilt."),
            5: ("Löschwasserversorgung",
                "Der Löschwasserbedarf ist durch die öffentliche Versorgung und einen Löschwasserteich "
                "nachzuweisen."),
            7: ("Wandhydranten",
                "An den Hallentoren sind Wandhydranten Typ F vorzusehen."),
        },
        "konzept": {
            "allgemein": "Die Industriehalle Nordring wird als Stahlhalle mit zwei Brandabschnitten errichtet.",
            "rettungswege": "Rettungsweglänge höchstens 35 m bis zu einem Ausgang ins Freie.",
            "anlagen": "Natürliche Rauchabzugsgeräte mit einer aerodynamisch wirksamen Fläche von insgesamt "
                       "72 m² im Dach; Brandmeldeanlage in den Lagerbereichen.",
            # FACT in-loeschwasser
            "loeschwasser": "Der Löschwasserbedarf beträgt 192 m³/h über 2 Stunden. Er wird zu 96 m³/h aus "
                            "dem öffentlichen Netz und zu 96 m³/h aus einem Löschwasserteich mit 200 m³ "
                            "gedeckt.",
            "abweichungen": "Keine.",
            "organisation": "Werkfeuerwehr ist nicht erforderlich; Brandschutzbeauftragter wird bestellt.",
        },
        "kennwerte": [("Grundfläche", "7.200 m²"), ("Brandabschnitte", "2 × 3.600 m²"),
                      ("Löschwasserbedarf", "192 m³/h über 2 h"), ("Rauchabzugsfläche", "72 m²")],
        "pruefbericht": {
            "date": "2024-01-22", "nr": 2,
            "findings": [
                "Die Brandwand ist mindestens 0,5 m über Dach zu führen.",
                "Der Löschwasserteich ist mit einer Saugstelle nach DIN 14210 auszustatten.",
            ],
        },
        "protokoll": {
            "date": "2024-03-07", "nr": 6,
            "items": [
                ("Brandwand", "Die Brandwand wird 0,5 m über Dach geführt; Detail liegt vor."),
                ("Löschwasserteich", "Der Teich wird im Mai ausgehoben."),
            ],
        },
    },
    {
        "number": "BS-2025-0019",
        "name": "Kita Regenbogen",
        "kind": "Kita",
        "street": "Regenbogenweg 3", "zip": "31888", "town": "Beispielstadt",
        "bauherr": ("Stadt Beispielstadt, Jugendamt", "Petra Lindqvist", "p.lindqvist@beispielstadt.example"),
        "architekt": "nordlicht",
        "az": "BA-019/2025", "permit_date": "2025-03-18",
        "vorhaben": "Neubau einer fünfgruppigen Kindertagesstätte mit Erdgeschoss und Obergeschoss.",
        "auflagen": {
            3: ("Rettungswege",
                "Jeder Gruppenraum im Obergeschoss erhält einen direkten Ausgang auf einen umlaufenden Balkon "
                "mit Treppe ins Freie als zweiten Rettungsweg."),
            4: ("Rauchableitung",
                "Der Treppenraum erhält an oberster Stelle eine Öffnung zur Rauchableitung mit mindestens "
                "1,0 m² freiem Querschnitt."),
            # FACT kt-rauchwarnmelder
            5: ("Rauchwarnmelder",
                "In den Schlafräumen und Gruppenräumen sind funkvernetzte Rauchwarnmelder nach DIN 14676 zu "
                "installieren, die zusätzlich einen Alarm im Büro der Leitung auslösen."),
        },
        "konzept": {
            "allgemein": "Die Kita Regenbogen bietet Platz für fünf Gruppen mit insgesamt 95 Kindern.",
            "rettungswege": "Zweiter Rettungsweg aus dem Obergeschoss über den umlaufenden Balkon mit Treppe.",
            "anlagen": "Die Rauchwarnmelder in den Schlafräumen werden funkvernetzt ausgeführt und lösen "
                       "auch im Leitungsbüro Alarm aus. Der Treppenraum erhält eine Öffnung zur Rauchableitung.",
            "loeschwasser": "Grundschutz 48 m³/h aus dem öffentlichen Netz.",
            "abweichungen": "Keine.",
            "organisation": "Brandschutzerziehung und eine Evakuierungsübung mit den Kindern je Halbjahr.",
        },
        "kennwerte": [("Gruppen", "5"), ("Kinder", "95"), ("Geschosse", "2"),
                      ("Löschwasser", "48 m³/h")],
        "pruefbericht": {
            "date": "2025-06-10", "nr": 1,
            "findings": [
                "Die Balkonanlage ist als zweiter Rettungsweg geeignet; die Treppe benötigt einen "
                "kindgerechten Handlauf.",
                "Die Funkvernetzung der Rauchwarnmelder ist in der Ausschreibung zu fordern.",
            ],
        },
        "protokoll": {
            "date": "2025-07-01", "nr": 2,
            "items": [
                ("Balkon", "Der kindgerechte Handlauf wird in 60 cm Höhe ergänzt."),
                ("Rauchwarnmelder", "Hersteller der funkvernetzten Melder wird bis August festgelegt."),
            ],
        },
    },
    {
        "number": "BS-2024-0311",
        "name": "Wohnhochhaus Am Stadtpark",
        "kind": "Wohnhochhaus",
        "street": "Parkallee 50", "zip": "31999", "town": "Musterstadt",
        "bauherr": ("Wohnbau Stadtpark eG", "Benedikt Sauer", "sauer@wohnbau-stadtpark.example"),
        "architekt": "meier",
        "az": "63-0311/24", "permit_date": "2024-10-08",
        "vorhaben": "Neubau eines Wohnhochhauses mit 22 Geschossen und 140 Wohnungen, Höhe 68 m, "
                    "Hochhaus nach der Muster-Hochhaus-Richtlinie.",
        "auflagen": {
            # FACT wh-steigleitung
            4: ("Steigleitungen",
                "In beiden Sicherheitstreppenräumen sind nasse Steigleitungen mit Wandhydranten in jedem "
                "Geschoss vorzusehen."),
            # FACT wh-aufzug
            6: ("Feuerwehraufzug",
                "Es ist ein Feuerwehraufzug mit eigenem Vorraum in jedem Geschoss zu errichten. Der Vorraum "
                "ist durch eine Druckbelüftung gegen Raucheintritt zu schützen."),
            8: ("Außenwand",
                "Die Außenwandbekleidung einschließlich Dämmung ist aus nichtbrennbaren Baustoffen "
                "herzustellen."),
        },
        "konzept": {
            "allgemein": "Das Wohnhochhaus Am Stadtpark hat 22 Geschosse mit 140 Wohnungen.",
            "rettungswege": "Zwei Sicherheitstreppenräume mit Druckbelüftung.",
            "anlagen": "Nasse Steigleitungen mit Wandhydranten in jedem Geschoss, Feuerwehraufzug mit "
                       "Vorraum, Brandmeldeanlage in den Fluren und Technikräumen.",
            "loeschwasser": "Löschwasserbedarf 192 m³/h; Druckerhöhungsanlage für die nassen Steigleitungen.",
            "abweichungen": "Keine.",
            "organisation": "Hausmeisterdienst mit Brandschutzunterweisung; jährliche Begehung mit der "
                            "Feuerwehr.",
        },
        "kennwerte": [("Geschosse", "22"), ("Höhe", "68 m"), ("Wohnungen", "140"),
                      ("Steigleitung", "nass")],
        "pruefbericht": {
            "date": "2025-02-12", "nr": 1,
            "findings": [
                "Die Druckerhöhungsanlage der nassen Steigleitungen ist redundant auszuführen.",
                "Der Vorraum des Feuerwehraufzugs muss in allen Geschossen mindestens 6 m² groß sein.",
            ],
        },
        "protokoll": {
            "date": "2025-04-03", "nr": 3,
            "items": [
                ("Feuerwehraufzug", "Der Aufzugshersteller liefert die Steuerung für den Feuerwehrbetrieb."),
                ("Aufstellfläche", "Die Abstimmung zur Aufstellfläche an der Parkallee läuft noch."),
            ],
        },
    },
]
# fmt: on


def project_by_number(number: str) -> dict:
    return next(p for p in PROJECTS if p["number"] == number)


def folder_name(p: dict) -> str:
    return f"{p['number']} {p['name']}"


def address(p: dict) -> str:
    return f"{p['street']}, {p['zip']} {p['town']}"
