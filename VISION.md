# Vision: Projektakte

> Dieses Dokument ist die **Spezifikation** für den Maintainer. Es beschreibt, *was* gebaut werden
> soll und *warum*. Das *Wie* entscheidet der Maintainer selbst – innerhalb der Leitplanken unten.
> Die Reihenfolge der Umsetzung steht in [ROADMAP.md](ROADMAP.md).

## 1. Ausgangslage

Ein Ingenieurbüro für **Brandschutz** arbeitet an vielen Bauvorhaben gleichzeitig. Jedes Vorhaben ist
ein **Projekt** mit einer **eindeutigen Projektnummer**. Zu jedem Projekt gehören viele Dokumente:
Baugenehmigungen mit Auflagen, Brandschutzkonzepte, Prüfberichte, Stellungnahmen, Besprechungs-
protokolle, Pläne, Rechnungen – und sehr viele **E-Mails** mit Bauherren, Architekten, Behörden,
Prüfingenieuren und Fachplanern.

Heute liegt das verstreut: Dateien in Projektordnern, Mails im Mailprogramm (plMail). Wer eine
Frage zu einem Projekt hat („Welche Auflagen zur Rauchableitung stehen in der Baugenehmigung?“,
„Hat der Prüfingenieur dem Abweichungsantrag zugestimmt?“, „Was ist der letzte Stand mit dem
Bauherrn?“), muss in Ordnern und Postfächern suchen.

## 2. Ziel

**Projektakte** ist ein internes, selbst gehostetes KI-Werkzeug, das **Mails und Dateien je Projekt
zusammenführt** und **Fragen zu Projekten beantwortet** – mit Quellenangaben, sodass jede Antwort
nachprüfbar ist.

Leitbild: *Alles zu einem Projekt an einem Ort, und man kann es fragen.*

## 3. Nutzer

Brandschutzingenieure und Projektleiter im Büro (eine Handvoll bis wenige Dutzend Personen).
Fachlich stark, wenig Zeit. Bedienung im Browser, auf Deutsch, ohne Schulung verständlich.

## 4. Kernfunktionen (Phase A – Aufbau)

1. **Projektregister** – Projekte werden aus der Ordnerstruktur erkannt (Ordnername beginnt mit der
   Projektnummer). Liste, Suche, Projektseite.
2. **Dokumente einlesen** – Text aus TXT, Markdown, PDF, DOCX extrahieren, in Abschnitte zerlegen,
   mit Embeddings indexieren; inkrementell (nur Geändertes neu verarbeiten).
3. **Mails einlesen** – aus einer Mailquelle; jede Mail mit Anhängen; Anhänge wie Dokumente
   indexieren.
4. **Mails Projekten zuordnen** – sicher über die Projektnummer (Betreff, Text, Anhangname);
   sonst als **Vorschlag** über inhaltliche Ähnlichkeit, den ein Mensch bestätigt oder verwirft.
   Unklare Mails landen in einem **Zuordnungs-Eingang**.
5. **Fragen beantworten** – pro Projekt (und projektübergreifend) per Retrieval (Volltext + Vektor),
   Reranking und Sprachmodell. Jede Antwort nennt ihre **Quellen** (Datei/Seite bzw. Mail/Datum).
   Weiß das System etwas nicht, sagt es das.
6. **Zeitachse je Projekt** – Dokumente und Mails chronologisch.
7. **Weboberfläche** – Projektliste, Projektseite (Dokumente, Mails, Zeitachse, Fragefeld),
   Zuordnungs-Eingang.
8. **Zugangsschutz** – Anmeldung; ohne Anmeldung keine Daten.
9. **plMail-Anbindung** – Mails produktiv über die **JMAP-API von plMail** lesen (siehe
   [docs/plmail-jmap.md](docs/plmail-jmap.md)).

## 5. Spätere Funktionen (Phase B – nur mit benanntem Nutzen)

- **Auflagen und Fristen** aus Baugenehmigungen extrahieren (strukturierte Liste je Projekt).
- **Abgleich** Brandschutzkonzept ↔ Baugenehmigung: Welche Auflagen sind im Konzept adressiert?
- **Projektzusammenfassung** („Stand des Projekts“) auf Knopfdruck.
- **Antwortentwurf** zu einer Mail auf Basis der Projektakte (nur Entwurf, nie automatisch senden).
- **Gescannte PDFs und Pläne** über das multimodale Modell lesen (siehe Abschnitt 7).
- Benachrichtigung bei neuen, unzugeordneten Mails.

## 6. Daten – wichtig

**Es gibt keine echten Firmendaten in der Entwicklung.** Der Maintainer erzeugt **alle Testdaten
selbst**: fiktive Projekte, fiktive Dokumente und fiktive Mails. Anforderungen an den Demo-Datensatz:

- **Projektnummern** im Format `BS-JJJJ-NNNN` (z. B. `BS-2024-0137`). Das Format ist in der
  Konfiguration als regulärer Ausdruck einstellbar (`PROJECT_NUMBER_PATTERN`), denn das echte Büro
  kann ein anderes verwenden.
- Mindestens **8 Projekte** unterschiedlicher Art (Schule, Pflegeheim, Bürogebäude, Tiefgarage,
  Hotel, Industriehalle, Kita, Wohnhochhaus) mit realistischen Ordnern, z. B.
  `BS-2024-0137 Grundschule Am Lindenhof/01 Genehmigung/Baugenehmigung.pdf`.
- Pro Projekt Dokumente in mehreren Formaten (TXT/MD/PDF/DOCX): Baugenehmigung mit nummerierten
  Auflagen (Rettungswege, Rauchableitung, Brandmeldeanlage, Feuerwehrzufahrt, Abweichungen …),
  Brandschutzkonzept, Prüfbericht, Besprechungsprotokoll.
- **Mindestens 60 Mails** als `.eml`, teils mit Anhängen, mit typischen Absendern (Bauamt,
  Prüfingenieur, Architekt, Bauherr, Feuerwehr). Davon:
  - die Mehrheit mit Projektnummer im Betreff,
  - einige mit Projektnummer nur im Text oder im Anhangnamen,
  - einige **ohne** Projektnummer, aber inhaltlich eindeutig (Adresse, Bauvorhaben),
  - einige ohne Projektbezug (Newsletter, Rechnung Bürobedarf) – dürfen keinem Projekt zugeordnet
    werden.
- **Fakten mit bekannter Antwort** in den Daten (z. B. „Auflage 7 der Baugenehmigung für
  BS-2024-0137: zweiter baulicher Rettungsweg über Außentreppe“), damit Tests die Frage-Antwort-
  Qualität automatisch prüfen können (kleiner **Evaluationssatz** mit Fragen und erwarteten
  Quellen).
- Erzeugung **deterministisch** (fester Seed) per Kommando, z. B. `python -m projektakte.demo`.
- Alle Namen, Adressen, Firmen frei erfunden. Keine echten Personen.

Im Betrieb wird mit `DEMO_MODE=1` der Demo-Datensatz beim Start in `DATA_DIR` erzeugt, falls er
fehlt – so laufen Staging und Produktion sofort mit Inhalt. Echte Daten bindet später ein Mensch
über die Konfiguration an (Projektordner read-only gemountet, plMail-App-Passwort).

## 7. KI-Dienste (im lokalen Netz, nichts geht nach außen)

| Dienst | Adresse (Standard) | Zweck |
|---|---|---|
| Sprachmodell (OpenAI-kompatibel, Chat Completions) | `http://192.168.178.62:8000/v1` | Antworten, Zuordnungs-Begründungen, Extraktion |
| Embeddings (Qwen3 4B Embedding) | `http://192.168.178.62:8001` | Vektoren für Abschnitte und Mails |
| Reranker (Qwen3 4B Reranker) | `http://192.168.178.62:4343/rerank` | Treffer neu sortieren |

Details, erwartete Formate und wie man sie prüft: [docs/ki-dienste.md](docs/ki-dienste.md).
Alle Adressen und Modellnamen sind per Umgebungsvariable einstellbar. **Tests dürfen diese Dienste
nicht brauchen** – sie laufen gegen kleine Fakes; die echten Dienste werden nur in Staging/Produktion
und in einem ausdrücklich markierten Integrationstest genutzt.

Das Sprachmodell wird bald gegen eine **multimodale Variante** getauscht, die auch Bilder/Dokumente
lesen kann. Der Code soll deshalb Text zuerst klassisch extrahieren und das Modell nur als Rückfall
für Scans und Pläne nutzen – und ohne Bildfähigkeit sauber weiterlaufen.

## 8. Leitplanken (nicht verhandelbar)

- **Nur lesen**: Projektordner und Postfächer werden nie verändert, nichts wird gelöscht oder
  verschoben, keine Mail wird gesendet.
- **Datenschutz**: Alle Daten bleiben im lokalen Netz. Keine Cloud-Dienste, keine Telemetrie.
- **Nachvollziehbarkeit**: Antworten nur mit Quellen; keine erfundenen Fakten. Lieber „nicht
  gefunden“.
- **Secrets** (Passwörter, App-Passwörter) nur aus Umgebungsvariablen, nie im Code oder in Logs.
- **Technik**: Python 3.12, FastAPI, SQLite (FTS5 für Volltext, Vektoren in SQLite oder NumPy-
  Dateien), serverseitig gerenderte Oberfläche (Jinja2, gern mit HTMX). Keine schwergewichtige
  Infrastruktur (kein Elasticsearch, kein separater Vektor-Server), solange SQLite reicht.
- Läuft als **ein Docker-Container**; Daten unter `DATA_DIR` (`/data`).
- `GET /health` antwortet immer schnell mit `{"status": "ok", ...}`, auch wenn KI-Dienste fehlen.
  Den Zustand der KI-Dienste meldet `GET /health/deps`.
- Tests mit `pytest`, schnell (< 1 min), ohne Netz. Ausführen mit `sh scripts/test.sh`.

## 9. Qualitätskriterien

- Zuordnung im Demo-Datensatz: alle Mails mit Projektnummer richtig; Mails ohne Projektbezug nie
  einem Projekt zugeordnet; inhaltliche Vorschläge mehrheitlich richtig.
- Evaluationssatz: Für jede Frage steht die erwartete Quelle unter den ersten 5 Treffern.
- Startzeit des Containers < 10 s (Indexierung läuft im Hintergrund).
- Oberfläche auf einem Laptop-Bildschirm ohne horizontales Scrollen benutzbar.
