# Changelog

Alle nennenswerten Änderungen. Format angelehnt an „Keep a Changelog“; ein Abschnitt je
Roadmap-Schritt.

## [Unveröffentlicht]

### Schritt 11 – Hintergrund-Indexierung
- Indexierung in einem Hintergrund-Thread: beim Start, danach alle `INDEX_INTERVAL` Sekunden und auf
  Anforderung (`POST /api/reindex`); nie zwei Läufe gleichzeitig.
- `DEMO_MODE=1` erzeugt fehlende oder veraltete Demo-Daten beim Start (im Hintergrund).
- `GET /api/status`: Phase, letzter Lauf, Statistik, Fehler, Zählerstände.
- Startzeit unabhängig von der Datenmenge; `/health` antwortet auch während der Indexierung sofort.
- Logging-Konfiguration beim Start über `python -m projektakte`.

### Schritt 10 – Fragen beantworten
- `POST /api/ask` (Projekt optional): hybride Suche → Prompt mit nummerierten Quellen (Zeichenbudget)
  → Antwort mit `[n]`-Verweisen; zurück kommen nur die tatsächlich zitierten Quellen.
- „Nicht gefunden“ ohne LLM-Aufruf, wenn nichts passt; erkennt auch „Nicht gefunden“ des Modells.
- Ausfall des Sprachmodells: Antwort „KI-Dienst nicht erreichbar“ mit den Fundstellen statt Fehler.
- Antworten ohne Quellenverweis werden markiert (`status: no_citation`).
- Fake-LLM: bessere Satzgrenzen (Ordnungszahlen, Abkürzungen), Überschriften und Mailkopf werden
  nicht zitiert; Stemmer kennt „zu“-Infinitive (abzunehmen → abnehmen).
- Evaluationssatz als Test: alle 18 Fragen mit Antwort enthalten den erwarteten Fakt und zitieren
  eine erwartete Quelle; beide Fragen ohne Antwort ergeben „nicht gefunden“.
- Demo-Daten Version 3: Signatur der Prüfingenieurin mit Funktionsbezeichnung.

### Schritt 9 – Hybride Suche mit Reranker
- Volltext (FTS5/BM25, Titelspalte doppelt gewichtet) + Vektorsuche, Reciprocal Rank Fusion,
  höchstens 3 Abschnitte je Quelle, Reranker auf die besten 30 Kandidaten (beide Antwortformate).
- Rückfall: ohne Reranker Reihenfolge der hybriden Suche, ohne Embeddings nur Volltext – mit Hinweis
  im Ergebnis; `/health/deps` meldet den Reranker-Ausfall samt Rückfallverhalten.
- `GET /api/search?q=…&project=…&k=…` mit Quellenangabe je Treffer (Datei/Seite bzw. Mail/Datum).
- Evaluationssatz als Test: Für alle 18 Fragen mit Quelle steht diese unter den ersten 5 Treffern
  (mit den Fakes aktuell immer auf Rang 1 oder 2).

### Schritt 8 – Mails zuordnen
- Sichere Zuordnung über bekannte Projektnummern (Betreff → Text → Anhangname); mehrere oder
  unbekannte Nummern landen mit Begründung im Zuordnungs-Eingang.
- Vorschlag über inhaltliche Ähnlichkeit: Mail als Suchanfrage (BM25 + Vektor, Reciprocal Rank
  Fusion) gegen Projektdateien und sicher zugeordnete Mails, Stimmanteil je Projekt mit Schwellwerten
  `ASSIGN_MIN_SHARE`/`ASSIGN_MIN_LEAD`, Absender-Historie als Zusatzstimme.
- `GET /api/inbox`, `POST /api/mails/{id}/assign` (bestätigen, zuordnen, verwerfen, kein Projektbezug);
  Entscheidungen in `assign_decisions`, sie überstehen eine Neuindexierung.
- Tests mit den Qualitätskriterien aus VISION.md Abschnitt 9 (Demo: alle 61 Mails mit Nummer richtig,
  0 von 7 Mails ohne Projektbezug zugeordnet oder vorgeschlagen, 8 von 8 inhaltlichen Vorschlägen richtig).
- Grundbausteine der Suche (`search.py`: FTS-Anfrage, BM25, RRF).

### Schritt 7 – Mails einlesen
- Mailquelle „Ordner mit .eml“ (`MAIL_DIR`, rekursiv, nur lesend, inkrementell über Größe/Änderungszeit
  und Inhalts-Hash; verschwundene Dateien werden aus dem Index entfernt).
- Kopf (Absender, Empfänger, Datum in UTC, Message-ID), Text (HTML wird in Text umgewandelt,
  Zeichensätze wie ISO-8859-1) und Anhänge.
- Mailtext und Anhänge werden in Abschnitte zerlegt, volltext- und vektorindexiert; Anhänge als
  Dokumente mit Kopie im eigenen Cache (`$DATA_DIR/cache/attachments`).
- `GET /api/mails` (Filter `project`, `status`, `q`, Blättern) und `GET /api/mails/{id}`.
- Gemeinsames Mail-Modell (`MailRecord`) für alle Quellen (Vorbereitung für plMail/JMAP).

### Schritt 6 – Embeddings
- Vektoren je Abschnitt als float32-BLOB in SQLite, berechnet in Stapeln (`EMBED_BATCH`), Commit je Stapel.
- Abschnitte werden mit Kontext (Datei, Projekt, Überschrift) eingebettet, Suchanfragen mit der
  Qwen3-Instruktion.
- Fingerabdruck des Embedding-Dienstes (Modell, Dimension, Referenzvektor): wechselt der Dienst
  (z. B. Fake → echtes Qwen3), werden alle Vektoren automatisch neu berechnet.
- Fällt der Dienst aus, bleibt der Volltext nutzbar; fehlende Vektoren werden beim nächsten Lauf
  nachgeholt.
- Vektorsuche über eine zwischengespeicherte NumPy-Matrix (neu geladen nur nach Änderungen).
- Neue Abhängigkeit: `numpy==2.5.4`.

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
