# Changelog

Alle nennenswerten Änderungen. Format angelehnt an „Keep a Changelog“; ein Abschnitt je
Roadmap-Schritt.

## [Unveröffentlicht]

### Schritt 5 – Textextraktion und Abschnitte
- Extraktion für TXT/MD/CSV, PDF (pypdf, seitenweise), DOCX (python-docx, Überschriften und Tabellen),
  EML; Bilder und PDF-Scans nur mit `LLM_VISION=1` über das multimodale Modell, sonst „übersprungen“.
- Überlappende Abschnitte (max. 900 Zeichen, Überlappung um einen Absatz) mit Seite und Überschrift.
- SQLite-Tabellen `documents`, `chunks` und FTS5-Index `chunks_fts` (Text gefaltet: ä→a, ß→ss).
- Inkrementell: Größe/Änderungszeit, dann SHA-256; gelöschte Dateien verschwinden aus dem Index.
- Dokumentdatum aus Dateiname, „Datum:“-Zeile, erstem Datum im Text oder Änderungszeit.
- `GET /api/projects/{nummer}/documents`, `GET /api/documents/{id}`.
- Neue Abhängigkeiten: `pypdf==6.21.4`, `python-docx==1.2.0`.

### Schritt 4 – Projektregister
- SQLite-Datenbank `$DATA_DIR/projektakte.sqlite3` mit additiven Migrationen (`PRAGMA user_version`).
- Projektordner unter `PROJECTS_DIR` werden bis Tiefe 2 erkannt (Ordnername beginnt mit der
  Projektnummer nach `PROJECT_NUMBER_PATTERN`); verschwundene Projekte werden inaktiv, nicht gelöscht.
- `GET /api/projects` (mit Suche `q`) und `GET /api/projects/{nummer}`; `python -m projektakte.index`.
- `projektakte.main` erzeugt die App erst bei Bedarf (keine Seiteneffekte beim Import).

### Schritt 3 – Demo-Mails erzeugen
- 76 deterministische `.eml`-Dateien unter `demo/mails` (50 mit Nummer im Betreff, 6 nur im Text,
  5 nur im Anhangnamen, 8 ohne Nummer aber eindeutig, 7 ohne Projektbezug), 15 mit Anhängen
  (PDF, DOCX, TXT), eine HTML-Mail, eine Mail in ISO-8859-1, Antworten mit `In-Reply-To`.
- `tests/eval.json`: 20 Fragen mit erwarteten Projekten, Quellen und Antwortbestandteilen,
  darunter projektübergreifende Fragen und zwei Fragen ohne Antwort in den Daten.
- `demo/manifest.json` mit der erwarteten Zuordnung jeder Mail.

### Schritt 2 – Demo-Projekte und -Dokumente erzeugen
- `python -m projektakte.demo` erzeugt 8 fiktive Projekte mit je Baugenehmigung (PDF), Brandschutz-
  konzept (DOCX), Prüfbericht (MD) und Besprechungsprotokoll (TXT), dazu ein Bild-Plan (PNG).
- Byte-genau reproduzierbar (Seed 4711, keine Zeitstempel); Dateidatum = Dokumentdatum.
- Eigene minimale PDF-, DOCX- und PNG-Writer ohne zusätzliche Abhängigkeiten.

### Schritt 1 – Konfiguration und Dienstestatus
- Alle Einstellungen aus Umgebungsvariablen (`projektakte/config.py`), Secrets nicht in `repr`.
- Clients für Sprachmodell (OpenAI Chat Completions, `<think>`-Filter, Bildeingabe nur mit
  `LLM_VISION=1`), Embeddings (OpenAI `/v1/embeddings` und TEI `/embed`, Formaterkennung zur
  Laufzeit) und Reranker (TEI- und Cohere/Jina-Format); Timeouts und saubere Fehler.
- Fake-Dienste `python -m projektakte.fake_ai --port 8999` (alle drei Schnittstellen, deterministisch);
  `AI_MODE=fake` startet sie im Prozess.
- `GET /health/deps`: je Dienst erreichbar, erkanntes Format, Antwortzeit, Fehler, Rückfallverhalten.
