# Roadmap

Kleine, prüfbare Schritte in dieser Reihenfolge. Jeder Schritt ist ein eigener PR mit Tests.
Die Begründung steht jeweils in [VISION.md](VISION.md). (Der Maintainer übernimmt diese Liste beim
ersten Start in sein Gedächtnis und pflegt diese Datei danach selbst.)

## Phase A – Aufbau

Stand 2026-11-08: alle 14 Schritte umgesetzt, dazu Zuordnung in plMail (15) und die Nachbesserung
nach der unabhängigen Prüfung (16; Version 0.4.0). Vor dem produktiven Einsatz: Formate
der echten KI-Dienste prüfen und in docs/ki-dienste.md eintragen, Schwellwerte mit echten Daten
überprüfen (siehe DECISIONS.md).

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
- [x] **11. Hintergrund-Indexierung** – beim Start und periodisch neue/geänderte Dateien und Mails
  verarbeiten, ohne den Webserver zu blockieren; `DEMO_MODE=1` erzeugt beim Start fehlende
  Demo-Daten.
- [x] **12. Weboberfläche** – Projektliste mit Suche, Projektseite (Dokumente, Mails, Zeitachse,
  Fragefeld mit Antwort und Quellen), Zuordnungs-Eingang mit Bestätigen/Verwerfen. Deutsch.
- [x] **13. Zugangsschutz** – Anmeldung mit Benutzern aus der Konfiguration (Passwort-Hash), Sitzungs-
  Cookie; alle Seiten und APIs außer `/health` geschützt.
- [x] **14. plMail über JMAP** – zweite Mailquelle über die JMAP-API von plMail (App-Passwort, siehe
  docs/plmail-jmap.md), inkrementell über `Email/changes`; Tests gegen einen kleinen Fake-JMAP-Server.

## Nach Phase A

- [x] **15. Zuordnung in plMail** – keine automatische Zuordnung; der Mensch wählt das Projekt in plMail
  beim Lesen, Projektakte übernimmt es per JMAP (docs/plmail-integration.md). Projektakte-Seite fertig
  und gegen einen Fake-plMail getestet; die plMail-Seite beschreibt docs/plmail-prompt.md.

- [x] **16. Nachbesserung nach unabhängiger Prüfung** – 67 bestätigte Mängel (Antwortqualität und
  Fake-KI, Indexierung, Mails/Zuordnung, Betrieb/Sicherheit, Oberfläche) behoben und gegengeprüft;
  Messwerkzeug `python -m projektakte.evaluate --real` für die echten Dienste.
- [x] **16a. Schlussprüfung und Gegenprüfungen (0.5.0)** – Review und Live-Vorführung, danach vier
  Runden Gegenprüfung; u. a. eigener Leseprozess mit Speicher- und Rechenzeitgrenze für fremde Dateien,
  Zeitbudget je Frage in jeder Einstellung, keine falschen grünen Antworten der Fake-KI auf
  Prüffragen.
- [ ] **17. Mit den echten Diensten kalibrieren** – im Büronetz `evaluate --real` laufen lassen,
  Formate in docs/ki-dienste.md eintragen, Schwellwerte einstellen; danach Version 1.0.

## Phase B – Ideen (nur mit benanntem Nutzen)

- Auflagen und Fristen aus Baugenehmigungen extrahieren
- Abgleich Brandschutzkonzept ↔ Baugenehmigung
- Projektzusammenfassung
- Antwortentwurf zu einer Mail (nur Entwurf)
- Gescannte PDFs/Pläne über das multimodale Modell
