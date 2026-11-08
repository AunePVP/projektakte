# Roadmap

Kleine, prüfbare Schritte in dieser Reihenfolge. Jeder Schritt ist ein eigener PR mit Tests.
Die Begründung steht jeweils in [VISION.md](VISION.md). (Der Maintainer übernimmt diese Liste beim
ersten Start in sein Gedächtnis und pflegt diese Datei danach selbst.)

## Phase A – Aufbau

- [x] **1. Konfiguration und Dienstestatus** – Alle Einstellungen aus Umgebungsvariablen
  (`projektakte/config.py` erweitern: `DATA_DIR`, `PROJECTS_DIR`, `MAIL_DIR`, `PROJECT_NUMBER_PATTERN`,
  `LLM_URL`, `LLM_MODEL`, `EMBED_URL`, `EMBED_MODEL`, `RERANK_URL`, `DEMO_MODE`). `GET /health/deps`
  prüft LLM, Embeddings und Reranker mit kurzem Timeout und meldet je Dienst ok/Fehler.
- [x] **2. Demo-Projekte und -Dokumente erzeugen** – `python -m projektakte.demo` legt deterministisch
  mindestens 8 fiktive Projekte mit Ordnerstruktur und Dokumenten (TXT, MD, PDF, DOCX) unter
  `DATA_DIR/demo/projekte` an (siehe VISION.md Abschnitt 6), inkl. bekannter Fakten für Tests.
- [x] **3. Demo-Mails erzeugen** – derselbe Generator legt mindestens 60 `.eml`-Dateien (teils mit
  Anhängen) unter `DATA_DIR/demo/mails` an: mit Projektnummer im Betreff, nur im Text, nur im
  Anhangnamen, ohne Nummer aber eindeutig, ohne Projektbezug. Dazu `tests/eval.json` mit Fragen,
  erwarteten Projekten und Quellen.
- [x] **4. Projektregister** – Ordner unter `PROJECTS_DIR` scannen, Projekte (Nummer, Name, Pfad) in
  SQLite speichern; `GET /api/projects`, `GET /api/projects/{nummer}`.
- [x] **5. Textextraktion und Abschnitte** – TXT/MD/PDF (pypdf)/DOCX (python-docx) lesen, in
  überlappende Abschnitte mit Seitenangabe zerlegen, in SQLite + FTS5 speichern; inkrementell über
  Datei-Hash.
- [x] **6. Embeddings** – Client für den Embedding-Dienst (Format siehe docs/ki-dienste.md), Vektoren
  je Abschnitt speichern; Stapelverarbeitung; Fake-Dienst für Tests.
- [x] **7. Mails einlesen** – Mailquelle „Ordner mit .eml“ (`MAIL_DIR`): Kopf, Text, Anhänge;
  Anhänge wie Dokumente verarbeiten; `GET /api/mails`.
- [x] **8. Mails zuordnen** – sichere Zuordnung über Projektnummer; sonst Vorschlag über
  Embedding-Ähnlichkeit mit Schwellwert; Zuordnungs-Eingang `GET /api/inbox`,
  `POST /api/mails/{id}/assign`. Qualitätskriterien aus VISION.md Abschnitt 9 als Tests.
- [x] **9. Hybride Suche mit Reranker** – Volltext (FTS5) + Vektor kombinieren, Reranker-Client
  (beide Antwortformate, siehe docs/ki-dienste.md), `GET /api/search?q=…&project=…`;
  Evaluationssatz als Test (erwartete Quelle unter den Top 5).
- [x] **10. Fragen beantworten** – `POST /api/ask` mit Projekt optional: Retrieval → Prompt mit
  nummerierten Quellen → Antwort mit Quellenverweisen; „nicht gefunden“, wenn nichts passt.
  Fake-LLM in Tests.
- [ ] **11. Hintergrund-Indexierung** – beim Start und periodisch neue/geänderte Dateien und Mails
  verarbeiten, ohne den Webserver zu blockieren; `DEMO_MODE=1` erzeugt beim Start fehlende
  Demo-Daten.
- [ ] **12. Weboberfläche** – Projektliste mit Suche, Projektseite (Dokumente, Mails, Zeitachse,
  Fragefeld mit Antwort und Quellen), Zuordnungs-Eingang mit Bestätigen/Verwerfen. Deutsch.
- [ ] **13. Zugangsschutz** – Anmeldung mit Benutzern aus der Konfiguration (Passwort-Hash), Sitzungs-
  Cookie; alle Seiten und APIs außer `/health` geschützt.
- [ ] **14. plMail über JMAP** – zweite Mailquelle über die JMAP-API von plMail (App-Passwort, siehe
  docs/plmail-jmap.md), inkrementell über `Email/changes`; Tests gegen einen kleinen Fake-JMAP-Server.

## Phase B – Ideen (nur mit benanntem Nutzen)

- Auflagen und Fristen aus Baugenehmigungen extrahieren
- Abgleich Brandschutzkonzept ↔ Baugenehmigung
- Projektzusammenfassung
- Antwortentwurf zu einer Mail (nur Entwurf)
- Gescannte PDFs/Pläne über das multimodale Modell
