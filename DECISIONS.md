# Entscheidungen

Kurz begründete Entscheidungen des Maintainers, die nicht in VISION.md festgelegt sind.
Neueste Entscheidungen werden je Roadmap-Schritt ergänzt.

## Allgemein

- **HTTP-Clients nur mit der Standardbibliothek** (`urllib`): keine zusätzliche Abhängigkeit für
  drei einfache JSON-Schnittstellen. Proxy-Umgebungsvariablen werden für KI-Dienste und plMail
  bewusst ignoriert – die Dienste liegen im lokalen Netz und nichts soll versehentlich über einen
  Proxy nach außen gehen.
- **Fakes als echte HTTP-Server** (`projektakte/fake_ai`): Tests starten denselben Server auf
  `127.0.0.1` mit Port 0 in einem Thread. So laufen die echten Clients samt Formaterkennung in den
  Tests mit, ohne Netz und ohne die echten Dienste. Die Logik der Fakes steckt in reinen Funktionen
  (`fake_ai/core.py`).
- **Formaterkennung**: Embeddings zuerst OpenAI, dann TEI; Reranker zuerst TEI (`texts`), dann
  Cohere/Jina (`documents`). Das erkannte Format wird gemerkt und bei 404/405/415/422 neu ermittelt
  (Server getauscht). `EMBED_FORMAT`/`RERANK_FORMAT` erzwingen ein Format. Die Antwort des Rerankers
  wird unabhängig von der Anfrageform erkannt (Liste vs. `results`).
- **`/health/deps` macht echte Mini-Aufrufe** (Modellliste + Chat mit 8 Tokens, ein Embedding, ein
  Reranking mit zwei Texten), parallel und mit `HEALTH_TIMEOUT`. Nur so lässt sich das Format
  sicher erkennen. Antwortcode ist immer 200, der Zustand steht im Feld `status` (`ok`/`degraded`).
  `/health` selbst ruft nie einen KI-Dienst auf.
- **`LLM_EXTRA_BODY`** statt fest eingebauter Schalter wie `enable_thinking`: unbekannte Felder
  lehnen manche Server ab; so lässt sich das je Server per Konfiguration einstellen.
- **Docker in der Entwicklungs-Sandbox**: Das Basis-Image wurde dort lokal um das Proxy-Zertifikat
  ergänzt; das Dockerfile im Repo bleibt unverändert. `scripts/docker-smoke.sh` baut das Image und
  prüft `/health` und `/health/deps`.

## Schritt 2 – Demo-Daten

- **Eigene PDF-/DOCX-/PNG-Writer** (`projektakte/demo/writers.py`) statt `fpdf2`/`python-docx`
  beim Schreiben: keine zusätzliche Laufzeitabhängigkeit, und die Dateien sind Byte für Byte
  reproduzierbar (python-docx schreibt Zeitstempel in die Metadaten). PDFs nutzen Helvetica mit
  WinAnsi-Kodierung, damit Umlaute, „m²“ und „m³/h“ extrahierbar bleiben.
- **Fiktive Orte** Musterstadt, Beispielstadt, Fiktivhausen; Domains auf `.example` (RFC 2606).
- **Dateidatum = Dokumentdatum** (`os.utime`), damit die Zeitachse auch ohne Datum im Text stimmt.
- **Fakten an festen Auflagen-Nummern**, die übrigen Auflagen kommen aus einem allgemeinen Pool in
  per Seed gemischter Reihenfolge – so sind die Baugenehmigungen unterschiedlich, die Prüffakten
  aber stabil.
- Der Generator räumt nur in seinem eigenen Ordner (`$DATA_DIR/demo`) auf und schreibt Dateien nur,
  wenn sich der Inhalt geändert hat.

## Schritt 3 – Demo-Mails und Evaluationssatz

- **Mail-Kategorien im Manifest**: Der Generator schreibt die erwartete Zuordnung je Mail nach
  `demo/manifest.json`; Tests prüfen damit die Qualitätskriterien aus VISION.md Abschnitt 9.
- **Schwere Negativbeispiele**: Mehrere Mails ohne Projektbezug enthalten Brandschutz-Vokabular
  (Newsletter des Fachverbands, Seminarangebot, Feuerlöscher-Werbung), damit die inhaltliche
  Zuordnung nicht einfach „irgendwas mit Brandschutz“ einem Projekt zuschlägt.
- **Evaluationssatz als Python-Daten** (`projektakte/demo/eval.py`), `tests/eval.json` wird daraus
  erzeugt (`python -m projektakte.demo --eval tests/eval.json`); ein Test prüft, dass beide gleich
  sind. Quellen werden über den Dateipfad im Projektordner bzw. den Mail-Betreff angegeben;
  Antworten werden über `answer_contains` geprüft. Zwei Fragen haben bewusst keine Antwort in den
  Daten („nicht gefunden“ muss kommen).
- Deterministische MIME-Grenzen (`set_boundary`) und Message-IDs, damit auch die Mails Byte für Byte
  reproduzierbar sind.

## Schritt 4 – Projektregister

- **Suche bis Tiefe 2**: Projekte dürfen direkt unter `PROJECTS_DIR` oder eine Ebene tiefer liegen
  (z. B. Jahresordner `2024/BS-2024-0137 …`). Unterordner eines Projekts werden nicht nach weiteren
  Projekten durchsucht; versteckte Ordner werden übersprungen.
- **Nummern werden großgeschrieben gespeichert**, die Suche ist unabhängig von Groß-/Kleinschreibung.
  Eine Nummer darf nicht an Buchstaben/Ziffern kleben (`BS-2024-01370` ist keine Nummer).
- **Verschwundene Projekte werden inaktiv**, nicht gelöscht – ihre Mails und Zuordnungen bleiben.
- **Additive Migrationen** statt ORM: eine Liste von SQL-Skripten, Stand in `PRAGMA user_version`.

## Schritt 5 – Textextraktion und Abschnitte

- **Abschnitte entlang von Absätzen**: PDF-Zeilenumbrüche werden zu Absätzen zusammengefügt,
  Listenpunkte („Auflage 7 …“, „TOP 2 …“, „1.“) bleiben eigene Einheiten. Einheiten werden bis
  900 Zeichen gepackt; der nächste Abschnitt beginnt mit dem letzten Absatz des vorigen, wenn dieser
  ≤ 250 Zeichen hat. Überschriften (Markdown `#`, DOCX-Formatvorlagen „Heading“/„Überschrift“)
  beginnen einen neuen Abschnitt und werden als Kontext gespeichert. Abschnitte überschreiten nie
  eine Seitengrenze, damit die Seitenangabe stimmt.
- **Eigene Faltung für den Volltext** (`textutil.fold`: Kleinschreibung, Umlaute ohne Punkte, ß→ss),
  weil FTS5 `remove_diacritics` „ß“ nicht auflöst. Index und Anfragen nutzen dieselbe Faltung.
- **FTS-Titelspalte** mit Dateiname, Projektnummer, Projektname und Überschrift – so finden Fragen
  wie „… der Grundschule“ auch Abschnitte, deren Text das Wort nicht enthält.
- **Bildauswertung nur als Rückfall**: Klassische Extraktion zuerst; Seiten mit < 20 Zeichen gelten
  als Scan. Ohne `LLM_VISION=1` werden solche Dateien mit Begründung als „übersprungen“ geführt und
  in der Oberfläche angezeigt, die Indexierung läuft sauber weiter.
- **Fehlertoleranz**: Jede Datei wird für sich verarbeitet; Fehler landen als Status `error` mit
  Meldung am Dokument, nicht als Abbruch.
- Dateien > 80 MB, versteckte Dateien und Office-Sperrdateien (`~$…`) werden übersprungen.

## Schritt 6 – Embeddings

- **Vektoren in SQLite** (BLOB float32 je Abschnitt) und für die Suche als eine NumPy-Matrix im
  Speicher. Bei einigen zehntausend Abschnitten ist ein Matrix-Vektor-Produkt schneller als jeder
  Index-Aufbau; ein separater Vektor-Server ist nicht nötig. Die Matrix wird nur neu geladen, wenn
  sich der Zähler `embed_generation` geändert hat.
- **Fingerabdruck statt Konfigurationsvergleich**: Vor jedem Lauf wird ein fester Referenztext
  eingebettet und mit dem gespeicherten Vektor verglichen (Modellname, Dimension, Kosinus > 0,999).
  Damit wird ein Wechsel von den Fakes zu den echten Diensten auch dann erkannt, wenn der
  Modellname gleich bleibt – alte Vektoren werden verworfen und neu berechnet, ohne Codeänderung.
- **Kontext im Einbettungstext** (Datei – Projekt – Überschrift, dann Abschnitt), aber keine
  Instruktion für Dokumente; Suchanfragen bekommen die in docs/ki-dienste.md empfohlene Instruktion.
- Ausfall des Dienstes ist kein Fehler des Laufs: Volltextsuche funktioniert weiter, die Lücke wird
  beim nächsten Lauf geschlossen.

## Schritt 7 – Mails einlesen

- **Ein Mail-Modell für alle Quellen** (`mails/model.py`): Die eml-Quelle und später JMAP liefern
  `MailRecord`s, gespeichert wird zentral in `mails/store.py`. Schlüssel ist `source_key`
  (`eml:<relativer Pfad>` bzw. `jmap:<Account>:<Email-Id>`).
- **Mailtext wird wie ein Dokument behandelt**: Betreff, Absender und Datum stehen im ersten
  Abschnitt, damit Fragen wie „Was hat die Feuerwehr geschrieben?“ den Absender finden.
- **Anhänge als Kopie im eigenen Cache** (nach SHA-256 abgelegt), weil die eml-Datei bzw. das
  Postfach nicht verändert werden darf und die Oberfläche Anhänge zum Öffnen anbieten soll.
- **Verschwundene eml-Dateien** werden aus dem Index entfernt (die Quelle ist maßgeblich);
  menschliche Zuordnungsentscheidungen werden ab Schritt 8 getrennt gespeichert und bleiben.

## Schritt 8 – Mails zuordnen

- **Vorschlag als „Abstimmung der ähnlichsten Abschnitte“** statt reiner Vektor-Ähnlichkeit: Ein
  erster Ansatz (Mittel der 3 besten Kosinus-Werte je Projekt) traf mit den Hash-Fakes nur 3 von 8
  inhaltlichen Mails und schlug eine Rechnung einem Projekt vor – Briefköpfe, Grußformeln und
  allgemeines Brandschutz-Vokabular dominieren. Jetzt wird die Mail wie eine Suchanfrage behandelt:
  BM25 (gewichtet seltene Begriffe wie Straßen- und Projektnamen hoch) und Vektorsuche liefern je 15
  Treffer, per RRF verschmolzen; jeder Treffer stimmt für sein Projekt. Der Stimmanteil ist die
  Sicherheit. Ergebnis im Demo-Datensatz: 8/8 richtig, Mails ohne Projektbezug höchstens 26 %
  Stimmanteil (Schwelle 50 %). Mit echten Embeddings trägt der Vektoranteil mehr Semantik bei; die
  Schwellen sind per Umgebungsvariable einstellbar.
- **Gemeinsame Begriffe Pflicht**: Ein Vorschlag braucht mindestens einen Volltexttreffer im
  vorgeschlagenen Projekt – reine Vektornähe reicht nicht (verhindert Zufallstreffer bei Mails wie
  „Wartung der Kaffeemaschine“).
- **Absender-Historie**: Hat ein Absender bisher (sicher) nur zu einem Projekt geschrieben, zählt das
  wie zwei Spitzentreffer. Architekten mit mehreren Projekten bekommen so keinen Bonus.
- **Belege**: Nur Projektdateien und *sicher* (oder von Menschen) zugeordnete Mails dienen als
  Vergleich – Vorschläge stützen sich nie auf andere Vorschläge.
- **Status**: `assigned` (sicher), `suggested`, `unassigned` (Eingang), `confirmed` (von Menschen
  zugeordnet), `none` (kein Projektbezug). „Verwerfen“ entfernt nur den Vorschlag; die Mail bleibt
  im Eingang, das verworfene Projekt wird für diese Mail nicht mehr vorgeschlagen.
- **Entscheidungen getrennt gespeichert** (`assign_decisions`, Schlüssel = `source_key`), damit
  sie eine Neuindexierung oder einen Neuaufbau der Mail-Tabelle überstehen.
- Ohne Vektoren (Embedding-Dienst aus) bleiben Mails im Eingang mit dem Hinweis, dass der Vorschlag
  nachgeholt wird; sichere Zuordnungen funktionieren immer.
