# Entscheidungen

Kurz begründete Entscheidungen des Maintainers, die nicht in VISION.md festgelegt sind – erst eine
Zusammenfassung, darunter die Details je Roadmap-Schritt.

## Zusammenfassung

1. **Fakes sind echte HTTP-Server** (`python -m projektakte.fake_ai`), Tests starten sie auf
   `127.0.0.1`. Dieselben Clients mit Formaterkennung laufen in Tests, mit `AI_MODE=fake` und gegen
   die echten Dienste. Umschalten nur über `LLM_URL`/`EMBED_URL`/`RERANK_URL` & Co.
2. **Formaterkennung zur Laufzeit** für Embeddings (OpenAI/TEI) und Reranker (TEI/Cohere, Anfrage
   und Antwort getrennt), robust gegen `<think>`, Timeouts und Ausfälle; `/health/deps` zeigt das
   erkannte Format. Ein Fingerabdruck des Embedding-Dienstes löst nach dem Umschalten automatisch
   die Neuberechnung aller Vektoren aus.
3. **Nur Standardbibliothek, wo es reicht**: HTTP (`urllib`), PDF/DOCX/PNG-Erzeugung für Demo-Daten,
   Passwort-Hashes (PBKDF2), Sitzungs-Cookie (HMAC). Neue Abhängigkeiten (gepinnt): pypdf,
   python-docx, numpy, jinja2, python-multipart.
4. **SQLite für alles**: FTS5 für Volltext (mit eigener Faltung ä→a, ß→ss), Vektoren als BLOB und
   für die Suche als NumPy-Matrix im Speicher, additive Migrationen über `PRAGMA user_version`.
5. **Hybride Suche mit Reciprocal Rank Fusion** (keine Score-Kalibrierung nötig), Reranker auf die
   30 besten Kandidaten, Rückfall ohne Reranker bzw. ohne Vektoren.
6. **Mail-Vorschläge als „Abstimmung“** der ähnlichsten Abschnitte (BM25 + Vektor), mit Pflicht zu
   gemeinsamen Begriffen und Absender-Historie; reine Vektor-Ähnlichkeit war mit den Fakes zu
   unzuverlässig (3/8 statt 8/8). Menschliche Entscheidungen werden getrennt gespeichert.
7. **Antworten nur mit Quellen**: nummerierte Quellen im Prompt, nur zitierte Quellen zurück,
   „Nicht gefunden“ ohne LLM-Aufruf, wenn nichts passt; Antworten ohne Zitat werden markiert.
8. **Ein Hintergrund-Thread** für die Indexierung (Start in < 2 s, auch während der Indexierung
   antwortet `/health` sofort), keine Task-Queue.
9. **Oberfläche serverseitig** mit lokal ausgeliefertem HTMX, alles auch ohne JavaScript; keine
   externen Skripte (auch die Swagger-UI ist abgeschaltet).
10. **Zugangsschutz ohne Standardpasswort**: Benutzer nur aus der Konfiguration; `/health` bleibt
    öffentlich, `/health/deps` ist geschützt; HTTP Basic nur für API-Skripte.
11. **plMail strikt lesend** über dokumentierte JMAP-Methoden; fehlende Funktionen sind in
    docs/plmail-jmap.md als Vorschläge notiert.
12. **Demo-Daten byte-genau reproduzierbar**, mit schweren Negativbeispielen und einem
    Evaluationssatz aus 20 Fragen (darunter 2 ohne Antwort), der in den Tests vollständig besteht.

Bekannte Grenzen: Die Schwellwerte (`ASSIGN_MIN_SHARE`, `ASSIGN_MIN_LEAD`, `ASK_MIN_RERANK`,
`SEARCH_MIN_RERANK`, `ASSIGN_MIN_SIMILARITY`) sind mit den Fakes eingestellt und müssen mit echten
Modellen und Daten überprüft werden (`python -m projektakte.evaluate --real`). Die Antwortqualität
mit dem Fake-LLM sagt nur etwas über den Ablauf, nicht über das echte Modell. Die Versionsnummer
bleibt daher unter 1.0, bis die echten Dienste geprüft sind. Nach der unabhängigen Prüfung wurden
67 Mängel behoben (Abschnitt „Nachbesserung“ unten).

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
  ≤ 250 Zeichen hat. Überschriften (Markdown `#`, DOCX- und ODF-Formatvorlagen „Heading“/„Überschrift“)
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

## Schritt 9 – Hybride Suche

- **Reciprocal Rank Fusion** (k = 60) statt gewichteter Score-Summen: BM25- und Kosinuswerte sind
  nicht vergleichbar skaliert, Ränge schon. Keine Kalibrierung nötig, auch nicht beim Wechsel auf
  die echten Embeddings.
- **Anfrage für FTS5**: gefaltete, leicht gestemmte Inhaltswörter (ohne Stopp- und Fragewörter),
  ODER-verknüpft mit Präfixsuche (`"rettungsweg"*`); kurze Zahlen bleiben erhalten („Auflage 7“).
  BM25 gewichtet die Titelspalte (Datei, Projekt, Überschrift) doppelt.
- **Reranker-Eingabe** = Titelzeile (Datei bzw. Betreff, Projekt, Überschrift) + Abschnittstext,
  auf 2000 Zeichen gekürzt; nur die besten 30 Kandidaten (docs/ki-dienste.md).
- **Vielfalt**: höchstens 3 Abschnitte je Dokument bzw. Mail in der Ergebnisliste.

## Schritt 10 – Fragen beantworten

- **Prompt**: Systemanweisung (nur Quellen, `[n]` zitieren, sonst „Nicht gefunden“, Zahlen wörtlich)
  und eine Nutzernachricht `Quellen: [1] <Quelle> – Projekt …\n<Text> … Frage: …`. Der Fake-LLM
  parst genau dieses Format; ein echtes Modell braucht es nicht, profitiert aber von der klaren
  Struktur. Kontextfenster sparsam: höchstens 6 Abschnitte bzw. 8000 Zeichen.
- **Nur zitierte Quellen** werden als Belege zurückgegeben; alle übergebenen Quellen stehen zusätzlich
  in `context`. Ungültige Nummern werden ignoriert. Ohne jeden Verweis wird die Antwort markiert
  und alle Fundstellen werden gezeigt – Nachprüfbarkeit geht vor.
- **„Nicht gefunden“ zweistufig**: ohne Suchtreffer bzw. mit Reranker-Score < `ASK_MIN_RERANK`
  wird das Modell nicht gefragt; sonst entscheidet das Modell anhand der Anweisung. Der Schwellwert
  ist absichtlich niedrig (0,01), weil echte Reranker-Scores anders verteilt sind als die der Fakes.
- **Ausfall des Sprachmodells** ist kein HTTP-Fehler: Status `llm_unavailable` mit den Fundstellen.
- Fragen werden nicht protokolliert (Datenschutz); nur Fehler der Dienste landen im Log.

## Schritt 11 – Hintergrund-Indexierung

- **Ein Thread statt Task-Queue**: Ein Daemon-Thread mit `Event`-gesteuertem Warten reicht für ein
  Büro dieser Größe; keine zusätzliche Infrastruktur (Celery, Redis). Der Thread startet im
  FastAPI-`lifespan`, sodass Importe und Tests ohne `with TestClient(...)` keine Threads starten.
- **SQLite im WAL-Modus** mit kurzen Transaktionen (Commit je Projekt, je 50 Mails, je
  Embedding-Stapel): Leser sehen den Fortschritt, Schreibzugriffe der Oberfläche (Zuordnungen) warten
  höchstens kurz (`busy_timeout`).
- **Globale Sperre um `run_index`**, damit CLI, Tests und Hintergrund-Thread nie gleichzeitig
  indexieren.
- **Demo-Daten im Hintergrund**: auch das Erzeugen des Demo-Datensatzes verzögert den Start nicht.
  Veraltete Demo-Daten (andere `DEMO_VERSION`) werden neu erzeugt.

## Schritt 12 – Weboberfläche

- **HTMX lokal ausgeliefert** (`projektakte/static/htmx.min.js`, Version 2.0.4, Lizenz 0BSD) statt
  per CDN: Die Oberfläche lädt nichts aus dem Internet (Datenschutz, Betrieb ohne Internetzugang).
- **Progressive Verbesserung**: Jede Aktion ist ein normales Formular (POST + Redirect bzw. volle
  Seite); HTMX tauscht nur Teile aus (Antwort, Eingangseintrag, gefilterte Projektliste).
- **Eine Projektseite mit Sprungmarken** statt Tabs mit eigenem Zustand: Fragefeld oben, darunter
  Zeitachse, Dokumente und Mails – auf dem Laptop schnell überblickbar und ohne JavaScript nutzbar.
- **Fehlerseiten**: Browser-Anfragen bekommen eine deutsche HTML-Seite, API-Anfragen weiter JSON.
- **Originaldateien** nur über die Dokument-ID, nie über einen Pfad aus der Anfrage; zusätzlich wird
  geprüft, dass die Datei unter `PROJECTS_DIR` oder dem Anhang-Cache liegt.
- **Zeitzone der Anzeige**: Europe/Berlin (falls die Zeitzonendatenbank fehlt: UTC).

## Schritt 13 – Zugangsschutz

- **Benutzer in der Konfiguration** (wie in der Roadmap gefordert), keine Benutzerverwaltung in der
  Oberfläche. Passwörter nur als PBKDF2-SHA256-Hash mit Salz (Standardbibliothek, keine Abhängigkeit
  wie bcrypt/argon2). Für Docker gibt es zusätzlich `AUTH_USERS_FILE`, weil `$` im Hash in
  Compose-Dateien sonst maskiert werden muss.
- **Eigenes signiertes Cookie** (HMAC-SHA256 über Benutzer + Ablaufzeit) statt Server-Sitzungen:
  zustandslos, übersteht Neustarts, wenn `SECRET_KEY` gesetzt ist. Ein aus der Konfiguration
  entfernter Benutzer wird bei der nächsten Anfrage abgemeldet.
- **CSRF**: `SameSite=Lax` verhindert, dass fremde Seiten POST-Anfragen mit dem Cookie auslösen;
  alle ändernden Aktionen sind POST.
- **HTTP Basic nur für `/api/*` und `/health/*`** – für `curl`-Prüfungen beim Umschalten auf die
  echten Dienste; Browserseiten nutzen ausschließlich die Anmeldeseite.
- **`/health/deps` ist geschützt** („alles außer `/health`“), weil es interne Adressen und Fehler
  nennt. Monitoring nutzt `/health`.
- **Kein Standardpasswort**: Ohne konfigurierte Benutzer ist keine Anmeldung möglich; die Anmeldeseite
  erklärt, wie man Benutzer anlegt. `AUTH_DISABLED=1` gibt es nur für die Entwicklung und wird in
  `APP_ENV=production` ignoriert.
- **Gegen Durchprobieren**: 5 Fehlversuche je Benutzer und IP-Adresse führen zu 5 Minuten Sperre;
  unbekannte Benutzernamen kosten dieselbe Rechenzeit wie bekannte.

## Schritt 14 – plMail über JMAP

- **Gleicher Speicherweg wie eml**: JMAP-Mails werden zu `MailRecord`s und laufen durch
  `store_mail`, Zuordnung und Suche ohne Sonderfälle. Schlüssel `jmap:<accountId>:<emailId>`.
- **State der ersten Seite** wird beim Erstabgleich gespeichert (nicht der letzten), damit keine
  Mail verloren geht, die während des Blätterns eintrifft; doppelt gelieferte Mails sind durch den
  Upsert harmlos.
- **Fingerabdruck vor dem Download**: `updated` meldet auch reine Flag-Änderungen; ohne inhaltliche
  Änderung werden Anhänge nicht erneut geladen.
- **Nur dokumentierte Eigenschaften** werden bei `Email/get` angefragt (plus `fetchHTMLBodyValues`
  nach RFC 8621 für reine HTML-Mails). Fehlende Funktionen sind in docs/plmail-jmap.md als
  Vorschläge notiert, nicht umgangen.
- **Fehler isoliert**: Ein nicht erreichbares plMail oder ein abgelehntes App-Passwort steht im
  Indexierungsergebnis und in `/health/deps`; die eml-Quelle und alles andere laufen weiter.

## Nach Phase A – Zuordnung in plMail (Wunsch des Betreibers)

- **plMail ist die Wahrheit über die Zuordnung**, Projektakte liest sie per JMAP. Alternative wäre
  gewesen, dass plMail die Zuordnung per POST an Projektakte schickt. Dagegen spricht: Projektakte
  müsste eine schreibende Schnittstelle für Fremdsysteme anbieten, die Zuordnung ginge bei einem
  Neuaufbau des Index verloren, und plMail könnte sie nicht selbst anzeigen. So bleibt Projektakte
  gegenüber plMail rein lesend (VISION.md Abschnitt 8) und die Zuordnung ist in plMail sichtbar.
- **Eigene JMAP-Capability + Email-Eigenschaft** (`urn:plmail:params:jmap:projektakte`,
  `plmail:projektakte`, Namensraum-Präfix nach RFC 8620 für Herstellererweiterungen) statt eines
  IMAP-Keywords: Keywords würden zum IMAP-Server synchronisiert und wären für andere Mailprogramme
  sichtbar; außerdem fehlten Benutzer und Zeitpunkt. Fehlt die Capability, läuft alles wie bisher.
- **Kleine Integrations-API mit eigenem Token** für das, was plMail von Projektakte braucht
  (Projektliste, Vorschlag, Anstoß zum Abgleich). Das Token öffnet nur `/api/integration/*`, kein
  Zugriff auf Mails oder Dokumente. Aufrufe nur serverseitig aus plMail.
- **Vorschläge zustandslos** aus Betreff/Text/Anhangnamen, damit plMail sie schon anzeigen kann,
  bevor Projektakte die Mail abgeglichen hat; dieselbe Abstimmungslogik wie im Eingang.
- **`manual` als Standard, sobald plMail angebunden ist** (überschreibbar mit `ASSIGN_MODE`); ohne
  plMail (Demo mit eml-Ordner) bleibt `auto`. Im Modus `manual` ist auch eine Projektnummer nur ein
  Vorschlag. Ein Moduswechsel bewertet alle Mails neu.
- **plMail-Fork gelesen** (Stand `224a1c9`), damit der Auftrag passt: Email-State ist dort ein
  explizites Change-Log (`MailChangeRecorder::emailChanged`), Vendor-URNs müssen in
  `Capability::SUPPORTED` stehen, `Email/get` ignoriert unbekannte Eigenschaften und hat mit
  `replyFrom` schon ein Opt-in-Muster, Fork-Regeln verlangen `fork_`-Tabellen und markierte
  Änderungen. Der Fork bevorzugt sonst eigene JMAP-Methoden statt neuer Email-Eigenschaften
  (FORK.md 22); hier ist die Eigenschaft trotzdem richtig, weil die Zuordnung über `Email/changes`
  bemerkt werden muss – das kostet drei kleine markierte Änderungen am Original.
- **Mail-Link in plMail**: `https://<plmail>/mail/message/{emailId}` (Route `MailController::message`).

## Nachbesserung nach der unabhängigen Prüfung (0.4.0)

Die Prüfung (12 Prüfer) fand 67 bestätigte Mängel. Behoben in fünf Paketen mit getrennten
Dateien, die parallel bearbeitet und danach zusammengeführt wurden; anschließend hat ein
unabhängiger Prüfer jeden Befund mit den ursprünglichen Nachstell-Skripten erneut geprüft. Einige
frühere Entscheidungen sind dadurch **ersetzt** – das steht jeweils dabei.

**Antworten und Suche**
- *Fake-Chat als extraktiver Leser* (ersetzt die Wortabdeckungsregel aus Schritt 10): Sätze werden
  nach den Themenwörtern der Frage bewertet (Synonyme, Wortformen, IDF über die gegebenen Quellen,
  Wortart-Gewichte), geantwortet wird ab 70 % Abdeckung der nicht projektbezogenen Themenwörter.
  Grund: Der alte Fake und der Evaluationssatz waren aufeinander abgestimmt; freie Fragen endeten
  meist mit „nicht gefunden“, Fragen mit Projektnamen bekamen unpassende Zitate. Geprüft an freien
  Fragen des Prüfers, die nicht im Evaluationssatz stehen.
- *Evaluationssatz 79 Fragen*, Prüfstrings stehen nicht in der Frage, Negativfragen nennen ein
  Projekt. Er bleibt ein Ablaufnachweis; die Qualität echter Modelle misst `projektakte.evaluate --real`.
- *Synonyme nur auf der Fragenseite des Volltexts*, nicht in Embeddings und Zuordnung – stabile
  Vektor-Fingerabdrücke und Zuordnungsqualität, echte Embeddings kennen Synonyme ohnehin.
- *Unbelegte Antworten* (`no_citation`) werden gezeigt, aber als unbelegt und ohne Quellenliste
  (ersetzt Schritt 10, wo alle Fundstellen als Quellen kamen). *`truncated`* ist ein eigener Status:
  Ein technischer Abbruch darf nicht als fachliches „nicht gefunden“ erscheinen.
- *„Mögliche Fundstellen“ bei jedem Status außer `answered`*: Ein falsches „nicht gefunden“ ist im
  Brandschutz gefährlicher als eine unsichere Antwort; Nachprüfbarkeit geht vor.
- *Quellennummern bleiben die des Antworttextes*, nicht zitierte Stellen eingeklappt – das Zitat [3]
  muss zur Liste passen.
- *Suchschwelle nur mit Reranker*: Nur dessen Score ist vergleichbar; ohne Reranker wird nichts
  ausgeblendet.

**Indexierung**
- *Extraktionsprofil je Dokument* (Bildauswertung an/aus, Modell): Unverändert heißt jetzt Größe und
  mtime gleich **und** Ergebnis vollständig oder Profil gleich und Wiederholzeit nicht erreicht. So
  wirkt der angekündigte Wechsel auf das multimodale Modell ohne Datenbanklöschung; vollständige
  Ergebnisse werden bei einem Modellwechsel bewusst nicht neu gelesen (zu teuer). Fehler mit
  Backoff 15 min × 2^(n−1), höchstens wöchentlich.
- *Kurze Schreibtransaktionen*: Extraktion (`prepare_document`) und Schreiben (`write_document`)
  getrennt, Commit je Dokument bzw. Mail, Zuordnung in Häppchen von höchstens 50 Mails bzw. 0,5 s.
  SQLite hat nur einen Schreiber; die Sperre darf nie während langsamer Arbeit gehalten werden.
- *Projektfilter der Vektorsuche per SQL* (ersetzt das Mitspeichern des Projekts in der Matrix):
  Zuordnungen ändern sich ohne neue Vektoren; der Filter kostet je Suche 1–2 ms.
- *Dateien verschwundener Projekte verlassen den Index* (ergänzt Schritt 4): abgeleitete Daten, die
  zurückkommen, sobald der Ordner wieder da ist; Mails und Entscheidungen bleiben. Gleiche Nummer
  mit neuem Pfad wird verschoben statt neu extrahiert; ein neuer Name erneuert nur die
  Volltext-Titel, nicht die Vektoren.
- *Plausibilitätsprüfung der Quellen*: Ein fehlender, unlesbarer oder plötzlich leerer
  `PROJECTS_DIR`/`MAIL_DIR` ändert nichts (Warnung statt Deaktivieren/Löschen) – ein nicht
  eingebundenes Netzlaufwerk darf den Index nicht leeren.
- *Symlinks*: Grenze ist `PROJECTS_DIR` (aufgelöst); Datei-Links nur mit Ziel innerhalb, Ordner-Links
  werden nie betreten, gelesen wird mit `O_NOFOLLOW`.

**Mails und Zuordnung**
- *Mindest-Evidenz über gemeinsame Fachbegriffe* (≥ 3, ohne Projekthistorie ≥ 4) statt fester BM25-
  oder Vektorwerte: modellunabhängig und in der Begründung nachvollziehbar; die Vektorschwelle ist
  optional (`ASSIGN_MIN_SIMILARITY`). Sehr häufige Wörter (> 50 % der Abschnitte) zählen nicht.
- *Newsletter/Werbung über Kopfzeilen, Absender und Formulierungen*, nicht über Ähnlichkeit –
  themenspezifische Werbung ist inhaltlich genauso ähnlich wie echte Projektpost.
- *Unbekannte Projektnummer = kein Vorschlag für ein anderes Projekt*: Die erste Mail zu einem neuen
  Projekt kommt oft vor dem Ordner; ein falscher Vorschlag würde aus Gewohnheit bestätigt.
- *Entscheidungen zusätzlich unter der Message-ID*, verschobene Dateien am Inhalts-Hash erkannt:
  Pfade sind kein stabiler Schlüssel, Entscheidungen sind die einzigen nicht wiederherstellbaren
  Daten. Merker (Newsletter, plMail-Wiederholliste) liegen in `meta` – keine Schemaänderung.
- *plMail-Vollabgleich mit Überlappung und ID-Abgleich*, gelöscht wird erst nach Bestätigung durch
  `Email/get` (kein Anker-Paging in plMail).

**Betrieb und Sicherheit**
- *Sitzungsepoche statt Sitzungstabelle* (ersetzt das rein zustandslose Cookie aus Schritt 13):
  Abmelden erhöht die Epoche des Benutzers und beendet alle seine Sitzungen, auch kopierte Cookies
  und über Neustarts hinweg; Abmelden auf einem Gerät meldet auch die anderen ab – für ein
  Büro-Tool akzeptabel, an Gemeinschaftsrechnern erwünscht.
- *Bremse statt Sperre* (ersetzt die feste Sperre nach 5 Fehlversuchen aus Schritt 13): Die feste
  Sperre je IP und Name ließ sich hinter einem gemeinsamen Proxy oder Terminalserver zum Aussperren
  von Kollegen missbrauchen. Exponentielles Backoff mit Obergrenze plus Geräte-Cookie (OWASP-Muster
  „device cookies“) verhindert das und hält das Raten trotzdem langsam.
- *CSRF über `Sec-Fetch-Site`/`Origin`* zusätzlich zu SameSite=Lax: SameSite schützt nicht vor
  Nachbar-Subdomains und nicht vor Basic-Zugangsdaten. Kein Token, weil alle unterstützten Browser
  `Sec-Fetch-Site` senden und ein Token jedes Formular und jeden HTMX-Aufruf berührt hätte.
- *CSP ohne `unsafe-inline`*, über einen Test abgesichert.
- *`AUTH_DISABLED` per Allowlist* (`dev`, `test`) statt Blocklist (`production`): Ein vertipptes oder
  vergessenes `APP_ENV` darf den Zugangsschutz nicht abschalten. *Image standardmäßig `production`*:
  sichere Voreinstellung für das Auslieferungsartefakt; die Demo setzt `APP_ENV=dev`.
- *TLS beim Reverse-Proxy*, die App bleibt bei HTTP; `COOKIE_SECURE` in `production` an.
  *Proxy-Vertrauen explizit* (`FORWARDED_ALLOW_IPS`), sonst sähe die Anmeldebremse nur die Proxy-IP
  oder Clients könnten ihre Adresse fälschen.
- *Zugriffsprotokoll ohne Query-String*: Suchbegriffe sind Projekt- und Personendaten.
- *`/health/ready` öffentlich, aber datenarm* – Monitoring und HEALTHCHECK ohne Zugangsdaten;
  `/health/deps` bleibt geschützt. *Sicherung über die SQLite-Backup-API* plus JSON der
  Entscheidungen als schemaunabhängiges zweites Netz.

**Tests**
- *Parallel mit pytest-xdist* (neue Entwicklungsabhängigkeit, gepinnt): Mit 559 Tests lag der
  serielle Lauf bei 73 s und damit über der Leitplanke „< 1 min“; parallel ≈ 25 s.

**Runde 2 (nach der ersten Gegenprüfung)**
- *Fake-KI „fail closed“*: Eine falsche Antwort mit Quellenangabe ist vor dem Team schlimmer als
  „nicht gefunden“. Wert-Fragen werden nur mit einem Wert der gefragten Art beim gefragten Thema
  beantwortet. Lockerere Regeln brachten auf dem Prüfsatz höchstens 0,64 statt 0,57, dafür
  erfundene Antworten – deshalb nicht übernommen.
- *Zurückgehaltener Prüfsatz*: von einem unabhängigen Autor aus den Demo-Dokumenten geschrieben, beim
  Tuning nur über Summen sichtbar. Das 80-%-Ziel bleibt bestehen; der Test ist „xfail“ mit der echten
  Zahl im Grund, eine Untergrenze fängt Rückschritte ab. Bewusst *keine* gezielten Ergänzungen der
  Abkürzungsliste für Kürzel, die nur aus dem Prüfsatz bekannt wurden.
- *Belegte Negativ-Fakten sind Antworten*: „Es liegen keine Mängel mehr vor [1]“ ist eine Aussage über
  die Quelle, keine Absage. Absagen werden nur ohne Zitat bzw. mit starken Absageformen erkannt.
- *Compare-and-Set in der Zuordnung*: Ein Ergebnis wird nur geschrieben, wenn die Mail noch so ist wie
  beim Lesen; menschliche Entscheidungen gewinnen immer. `decide()` liest und schreibt in einer
  Transaktion, Schreibtransaktionen nehmen die Sperre sofort (`BEGIN IMMEDIATE`).
- *Werbeerkennung zweiseitig präzise*: ein starkes Signal oder zwei schwache, nie bei Absendern mit
  Projekthistorie; Kopfzeilen gelten nicht bei Projektverteilern derselben Firma. Kopien erben keine
  Entscheidung über die Message-ID (konstante Message-IDs würden sonst zu Fehlzuordnungen führen).
- *Karenzzeit vor dem Löschen* fehlender Projektordner (24 h und 3 Läufe): Ein kurz nicht eingebundenes
  Laufwerk soll keine Neuextraktion und Neueinbettung auslösen; aus Suche und Antworten verschwinden
  die Dateien trotzdem sofort.
- *Demo-Bereinigung beim Umstieg*: Demo-Inhalte erkennt Projektakte am Pfad unter `DATA_DIR/demo` (und
  bei Mails zusätzlich an der Message-ID); menschliche Entscheidungen bleiben.
- *Bremse je Adresse nur „weich“*: höchstens ein Versuch alle 2 s (Warteschlange bis 20 s, darüber
  503), aber kein Abweisen richtiger Passwörter – hinter Terminalserver, NAT oder Docker-Gateway traf
  die harte Bremse alle Kollegen. Hart gesperrt wird nur der Name mit Fehlversuchen; HTTP Basic nur je
  Benutzer+Adresse. Preis: Namens-Spraying von einer Adresse ist auf etwa 1800 Versuche je Stunde
  begrenzt; bekannte Namen schützen die Bremsen je Name.
- *`/health/ready` ohne Tabellenscan*: ausstehende Vektoren aus der Statistik des letzten Laufs, weil ein
  COUNT über 300 000 Abschnitte kalt 6 s dauerte – länger als das HEALTHCHECK-Timeout.
- *Eingang mit Schlüssel-Blättern* (Cursor auf Sortierschlüssel und ID) statt OFFSET: Nach Entscheidungen
  rutschen sonst Mails nach vorne und „Weiter“ überspringt sie. `/api/inbox` bleibt offsetbasiert.
- *Kennungstreffer immer sichtbar*: Ein echter Cross-Encoder bewertet Projektnummern oder Aktenzeichen
  nicht zuverlässig; ein wörtlicher Treffer ist aber eindeutig.

**Runde 3 (nach der zweiten Gegenprüfung)**
- *Gesamtbudget je Frage (`ASK_DEADLINE`, 180 s)* statt einer Summe von Einzel-Zeitlimits: Mit bis zu
  4 Suchen konnte eine Frage im schlimmsten Fall 600 s dauern – länger als jeder Proxy und die
  5-Minuten-Grenze der Browser. Jeder KI-Aufruf bekommt höchstens die Restzeit, Zusatzsuchen laufen
  nur, solange für das Sprachmodell genug übrig ist (Reserve: die Hälfte des Budgets bzw.
  `LLM_TIMEOUT`); ohne Restzeit kommen die Fundstellen wie bei einem Ausfall.
- *Reranker-Timeout ohne zweite Formaterkennung*: Nur eine unerwartete Antwortform deutet auf einen
  getauschten Server; eine Zeitüberschreitung wurde sonst zweimal abgewartet.
- *Teil-Absagen*: Trägt ein anderer Teilsatz (getrennt durch „;“ oder „–“) ein gültiges Zitat, ist es
  eine Antwort – echte Modelle schreiben so („… vorgesehen [2]; ein genauer Tag ist nicht genannt“).
- *Werbeerkennung*: Starke Signale nur aus Massenmail-Kopfzeilen und Marketingadressen; schwache
  Signale (Werbewörter, Vertriebs-/Redaktions-/Automatikadressen, Shop-Domains) zählen nur bei
  Absendern ohne Projektverlauf **und** wenn die Mail kein Projekt konkret nennt (Unterscheidungswort
  des Namens, Anschrift oder Ansprechperson). Bekannte Grenze: Enthält Werbung zufällig ein solches
  Wort (etwa „Hotels mit Seeblick“), gilt das als Projektbezug.
- *Vorrang bei der Anmeldung*: Ein vorhandener Name ohne eigenen Fehlversuch von dieser Adresse wird
  an der Warteschlange vorbei geprüft; ein falsches Passwort dort wird wie in der Warteschlange
  beantwortet, damit sich keine Namen erraten lassen.
- *Pixel-Hash statt Datei-Hash* für den Demo-Fluchtwegplan im Test: zlib-Varianten komprimieren
  verschieden, die Rasterung ist ganzzahlig und damit plattformunabhängig.

**Runde 4 (Schlussprüfung: Review und Live-Vorführung)**
- *Zeitnot ändert nicht die Aussage*: Bleibt keine Zeit für die Überblickssuche einer Zusammenfassung,
  bekommt das Sprachmodell die Fundstellen der Frage selbst (auch unter `ASK_MIN_RERANK`). Vorher
  endete die Frage mit „nichts gefunden“ – eine falsche Aussage über die Akte, obwohl die Reserve des
  Modells unberührt war. (In Runde 5 ersetzt: die Zusatzsuchen laufen immer, siehe dort.)
- *Eigene Meldung für das aufgebrauchte Budget* („Das Zeitbudget der Frage ist aufgebraucht …“) statt
  „KI-Dienst nicht erreichbar“; der Status bleibt `llm_unavailable`, damit der Vertrag von `/api/ask`
  gleich bleibt. Gleiche Hinweise mehrerer Suchen erscheinen einmal; Sekunden in Meldungen gerundet
  und mit Komma.
- *Formaterkennung innerhalb eines Timeouts*: Alle Versuche einer Erkennung (Reranker, Embeddings)
  teilen sich die Zeit des einen Aufrufs. Sonst konnte ein Dienst mit unerwarteter Antwort das
  Dreifache seines Timeouts – und damit mehr als `ASK_DEADLINE` – verbrauchen.
- *`ASK_DEADLINE` mindestens 10 s*: kleinere Werte werden angehoben statt abgelehnt (die Anwendung
  startet weiter), mit Warnung beim Start; ebenso eine Warnung, wenn `LLM_TIMEOUT` nicht unter
  `ASK_DEADLINE` liegt. Bei 1 s scheiterte vorher jede Frage still nach 0 s.
- *Verneinte Zitate gehören zur Absage*: Ein zitierter Teilsatz zählt nur als Teilantwort, wenn er
  etwas aussagt – „weder [1] noch [2] nennen ihn“ oder „auch [3] erwähnt ihn nicht“ beschreibt nur,
  was fehlt (außer es folgt ein Gegensatz: „…, laut [3] aber im August“). Gilt für Teilsätze im ersten
  Satz und für die folgenden Sätze. Belegte negative Tatsachen („Es liegen keine Mängel mehr vor
  [1].“) bleiben Antworten.
- *Bilder einer Scan-Seite nach Schlüssel entdoppeln, Grenzen vor dem Dekodieren*: pypdf dekodiert
  ein Bild bei jedem Zugriff neu; eine 16-KB-Datei, die ein großes Bild 200-mal zeichnet, belegte
  1,8 GB. Jetzt wird jedes verschiedene Bild einmal dekodiert und gesendet; vor dem Dekodieren gelten
  je Seite höchstens 200 Bilder und 150 Megapixel nach der Größenangabe im PDF (ein A0-Plan mit
  300 dpi passt). Was darüber liegt, wird nicht dekodiert, sondern mit Hinweis ausgelassen, ohne
  Wiederholung – die Datei ändert sich dadurch nicht.
- *Demo liest Bilder*: Mit `DEMO_MODE=1` und `AI_MODE=fake` ist `LLM_VISION` standardmäßig an (die
  Fake-Transkription ist festgelegt), damit der Demo-Fluchtwegplan nicht als „übersprungen“
  erscheint. Mit echten Diensten bleibt `LLM_VISION` eine bewusste Entscheidung.
- *Evaluation mit Budget*: `python -m projektakte.evaluate` misst mit `ASK_DEADLINE` und den
  Synonymen wie die Anwendung und zählt die Fragen, bei denen das Budget griff.
- *Gebäudetyp ist schwächer als der Name*: Ein Wort aus dem Projektnamen zählt als Hinweis auf das
  Projekt halb, wenn es der Gebäudetyp vor dem eigentlichen Namen ist („Tiefgarage“ Marktplatz,
  „Wohnhochhaus“ Am Stadtpark), voll beim Namen selbst („Hafenkante“). Benennt eine Frage mehrere
  Projekte gleich stark („die Tiefgarage im Wohnhochhaus“), bleiben die Wörter Themenwörter, die eine
  Antwort enthalten muss. Vorher fiel „Tiefgarage“ als Projektname aus dem Thema, und die Fake-KI
  antwortete grün aus dem falschen Projekt.

**Runde 5 (Gegenprüfung der Schlussprüfung)**
- *Zusatzsuchen laufen immer, innerhalb des Budgets*: Statt Überblicks- und Beteiligtensuche bei
  Zeitnot auszulassen (bei `ASK_DEADLINE=10` selbst bei schnellen Diensten, und ohne eigene Fundstellen
  wieder mit „nichts gefunden“), laufen sie mit der Restzeit der Suchen; jeder KI-Aufruf darin entfällt
  von selbst, wenn weniger als eine Sekunde bleibt, und die Volltextsuche liefert trotzdem die
  Projektbeschreibung. Die Reserve des Sprachmodells bleibt unberührt.
- *Ein Hinweis statt vier*: Alles, was das Budget kappte, steht in einer Zeile („Zeitbudget der Frage
  (ASK_DEADLINE = 10 s) erreicht – nicht abgewartet: Vektorsuche, Reranker, Sprachmodell“); ein vom
  Budget gekürzter Aufruf heißt nicht mehr „nicht verfügbar“.
- *Gesamtdauer statt Leerlaufzeit*: Das Socket-Timeout gilt je Lesevorgang; ein Dienst, der seine
  Antwort Byte für Byte schickt, hielt einen Aufruf beliebig lange. Der Antwortkörper wird jetzt bis zu
  einem festen Endzeitpunkt gelesen (das Socket-Timeout jeweils auf die Restzeit gesetzt). Meldungen
  nennen das konfigurierte Zeitlimit, nicht die Restzeit eines Teilversuchs.
- *Ungültige Zahlen fallen auf den Standard zurück*: Zeitlimits müssen größer 0 sein – `LLM_TIMEOUT=0`
  hätte jeden Aufruf sofort scheitern lassen (und als „Zeitbudget aufgebraucht“ gemeldet); `inf`/`nan`
  ließen den Start abstürzen bzw. das Budget unbestimmt. Jeder Fall wird beim Start als Warnung
  genannt.
- *Meta-Sätze gehören zur Absage, Fakten nicht*: Nach einer Absage zählt ein zitierter Satz nur als
  Teilantwort, wenn er etwas über die Sache sagt. Meta-Sätze beschreiben die Quellen selbst – was ihnen
  fehlt („weder [1] noch [2] nennen ihn“, „fehlt eine entsprechende Angabe“), worauf sie sich
  beschränken („[1] und [2] beschreiben nur die Anlage“, „beziehen sich auf …“) – oder verweisen
  („Möglicherweise …“, „Bitte prüfen Sie …“). „Laut [2] gibt es keine offenen Mängel mehr“ ist ein
  Fakt, auch wenn er verneint ist; „[2] nennt nur den Monat März“ auch (ein Wert). Gemessen an einer
  Matrix aus 54 realistischen Antworten (31 Absagen, 23 Teilantworten) plus 5 aus dem Review: alle
  richtig, vorher 20 bzw. 6 falsch.

**Runde 6 (Lesen fremder Dateien)**
- *Eigener Leseprozess statt weiterer Einzelgrenzen*: Jede Gegenprüfung fand eine neue Art, mit einer
  kleinen Datei Gigabytes oder Minuten zu verbrauchen (weiche Masken, JPEG 2000, Textextraktion von
  pypdf). Mail-Anhänge kommen von außen und werden automatisch gelesen. Deshalb liest ein eigener,
  dauerhafter Prozess (`spawn`, erbt nicht den Speicher der Anwendung) PDF, Word und Mails mit
  Betriebssystem-Grenzen je Datei: Adressraum und **Rechenzeit** – nicht Wanduhrzeit, weil das Warten
  auf ein langsames Sichtmodell bei einem langen Scan legitim ist; dafür eine großzügige Grenze „ohne
  Fortschritt“, die jede Modellantwort zurücksetzt. Kosten: etwa 0,5 ms je Datei und ein Prozess mit
  rund 50 MB.
- *Grenzen sind endgültig, Ausfälle nicht*: Rechenzeit- und Speichergrenze, Absturz des Leseprozesses
  hängen an der Datei – kein erneuter Versuch (sonst liest jeder Lauf dieselbe Bombe). Die Grenze
  „ohne Fortschritt“ kann am Modell liegen – erneuter Versuch mit wachsendem Abstand.
- *Nur JSON aus dem Leseprozess*: Der Prozess, der fremde Dateien parst, gilt als möglicherweise
  übernommen; die Anwendung entpickelt nichts von ihm.
- *Abbruch beim Beenden ist kein Fehler der Datei*: Wird die Anwendung während des Lesens beendet,
  bleibt die Datei ungelesen und wird beim nächsten Start gelesen, statt als fehlgeschlagen vermerkt
  zu werden.
- *Einzelgrenzen bleiben trotzdem*: Bilder je PDF-Objekt, echte Größe aus dem Bildkopf, verkleinertes
  Dekodieren, Grenzen je Seite und Dokument – sie halten den normalen Speicherbedarf klein (A0-Plan
  0,33 statt 1,1 GB), der Leseprozess ist die Absicherung für alles, was sie nicht vorhersehen.

**Projektordner vom Dateiserver**
- *Vorschlag mit Bestätigung statt stiller Automatik*: Drei Runden Gegenprüfung mit über 150
  nachgebauten Büro-Ablagen haben gezeigt, dass sich aus Ordnernamen allein nicht in jedem Fall sicher
  erkennen lässt, welche Ordner Projekte sind – Prüfberichtsordner in Projekten, Kundennummern mit
  Unterprojekten, Angebote im gleichen Nummernformat. Eine Automatik, die still entscheidet, verliert
  dann unbemerkt Projekte oder liest Angebote als Projekte. Deshalb ist `auto` ein Vorschlag mit
  Belegen (Beispiele, Fundorte, Ausnahmen, weitere Schemata, „Bitte prüfen“), den eine Person einmal
  bestätigt; ein falscher Vorschlag fällt an den Beispielen auf. Eine bestätigte Struktur ändert sich nie
  von selbst – eine halb eingebundene Freigabe darf nie ändern, welche Ordner als Projekte gelten.
- *Feste Regeln in der Erkennung*: Kandidaten nur innerhalb einer Ebene; Jahre, Jahresbereiche, Monate,
  Daten und Nummern mit höchstens zwei Ziffern sind nie Projektnummern; eine Nummer, die mit der des
  übergeordneten Projekts beginnt, ist interne Gliederung; Ausreißer verbreitern kein Schema;
  verschiedene Schemata werden nie automatisch verknüpft. Rein numerische Schemata bekommen eine
  Jahressperre, die nie still Projekte ausschließt (sonst Hinweis oder kein Vorschlag).
- *SMB nur lesend und außerhalb der Anwendung*: Der Server bindet die Freigabe mit einem Lesekonto und
  `ro` ein; Projektakte sieht nur einen Ordner. `propagation: rslave`, weil automount und Neustarts des
  Dateiservers die Freigabe nach dem Container einbinden.

**Mehrere Ordner mit derselben Nummer**
- *Zusammenführen statt „der erste gewinnt“*: Auf der echten Freigabe lagen 98 Auftragsnummern in mehreren
  Ordnern – meist Teilordner außerhalb des Auftragsordners und Kopien desselben Auftrags. Deren Dateien
  wurden bisher nie gelesen. Ein Projekt mit Hauptordner (`projects.path`, unverändert) und weiteren
  Ordnern (`project_folders`) hält die Migration rein additiv.
- *Formregel statt Wortliste*: Ob ein Ordner der Auftrag ist (`<Nr>_<Name>`, `<Nr> <Name>`), nur die Nummer
  trägt oder ein Teilordner ist (`<Nr>-<ein Wort>`), entscheidet die Form, nicht eine Liste von
  Büro-Wörtern – sie gilt für die Unterordner jedes Büros. Preis: ein Auftrag, der fälschlich
  `284201-Zwischendecke` heißt, gilt als Teilordner und wird nach seinem Bauvorhaben benannt.
- *Name aus dem Bauvorhaben nur, wo das passt*: Ein Projekt nur aus Teilordnern heißt nach dem Ordner
  darüber, dessen Nummer die Projektnummer fortsetzt (`1193_…` über `119300-Bilder`) – aber nur, wenn die
  Ablage ihre Projekte überwiegend `<Nr>_<Name>` nennt oder die Teilordner verschiedene Wörter tragen.
  Ablagen, die Projekte `1234-Kita` nennen, behalten so ihre Namen (sonst hieße jedes Projekt nach seinem
  Gruppenordner `1_Hochbau`). „Fortsetzen“ ist ein Strukturmerkmal: ein Jahresordner `2024` ist kein
  Bauvorhaben von `2024-017`.
- *Ordner in einem fremden Bauvorhaben nicht zusammenlegen*: `\d{6}` passt auf Daten (`230512_Begehung`).
  Liegt ein Ordner in einem Bauvorhaben, dessen Nummer die Projektnummer nicht fortsetzt, während es
  einen Ordner im passenden Bauvorhaben gibt, würde Zusammenlegen fremde Fotos und Schreiben in die
  Antworten eines anderen Auftrags bringen – er wird ausgelassen und genannt. Ohne passenden Ordner
  bleibt alles wie bisher (andere Ablagen, Kundenordner mit eigener Nummer); ein Jahresordner zwischen
  Bauvorhaben und Auftrag stört nicht, weil irgendein Bauvorhaben darüber passen muss.
- *`rel_path` des Hauptordners bleibt*: Dateien des Hauptordners behalten ihren Pfad, Dateien weiterer
  Ordner bekommen den Ordnernamen davor – bestehende Projekte mit einem Ordner sehen aus wie vorher, und
  ein Wechsel des Hauptordners schreibt nur Pfade um, liest nichts neu. Preis: Besteht ein Projekt nur
  aus Teilordnern, steht vor den Dateien des Hauptordners kein Teilname (`Bilder.txt` neben
  `119300-CAD/…`).
- *Kein Aufschub für einen verschwundenen weiteren Ordner*: Seine Dateien verlassen den Index wie
  verschwundene Dateien; ein nicht lesbarer Ordner darüber oder ein nicht eingebundenes Laufwerk ändern
  dagegen nichts, und ganze Projekte haben weiter 24 h / 3 Läufe Aufschub.
- *Gleiche Dateien in zwei Ordnern bleiben zwei Dokumente*: Erkennen und Zusammenlegen spart wenig und
  macht Quellenangaben mehrdeutig.
- *Trennzeichen aus der Position im Treffer*: keine neue benannte Gruppe im regulären Ausdruck, weil ein
  eigenes `PROJECT_NUMBER_PATTERN` nur `num`/`name` reserviert.

**Bauvorhaben und Unterprojekte**
- *Beides statt entweder – oder*: Die offene Frage „Projekt = Auftrag oder Bauvorhaben?“ ist mit „beides“
  entschieden. Aufträge bleiben die Projekte (Mails nennen die 6-stellige Nummer und werden weiter darüber
  zugeordnet), Bauvorhaben fassen sie als Unterprojekte zusammen: Fragen über alle Aufträge eines
  Bauvorhabens und die Dateien direkt im Bauvorhaben-Ordner sind möglich, ohne dass ein Auftrag seine
  Nummer, seine Mails oder seine Dokumente verliert.
- *Bauvorhaben als eigene Zeile in `projects`*: `kind = 'bauvorhaben'`, die Aufträge nennen es in `parent`
  (Migration 9, nur hinzugefügt, ohne Fremdschlüssel: Bauvorhaben und Aufträge werden in einem Lauf in
  beliebiger Reihenfolge geschrieben, ein fehlendes Bauvorhaben darf nie auf seine Aufträge durchschlagen).
  So nutzen Bauvorhaben alles, was Projekte schon können – weitere Ordner, Frist beim Verschwinden, nicht
  lesbare Ordner, Ausblenden und Wiederherstellen, Volltexttitel mit dem Namen, Projektseite und
  Originaldatei –, ohne eigene Lebenszyklus-Logik.
- *Eine Einstellung statt Automatik*: `PROJECT_PARENT_DIGITS` nennt die Stellenzahl der Bauvorhaben-Nummer.
  Automatisch („ein Ordner über dem Projekt, dessen Nummer die Projektnummer fortsetzt“) würden in
  anderen Ablagen falsche Bauvorhaben entstehen: Jahresordner `2024_Auftraege` über `202401_…`,
  Kundennummern, Gruppenordner `1_Hochbau` über `1234-Kita`. Wie beim Nummernschema entscheidet eine Person;
  `python -m projektakte.structure` schlägt die Zeile nur vor, wenn die Projekte überwiegend in solchen
  Ordnern liegen. Standard ist aus – bestehende Installationen ändern sich nicht.
- *Der äußerste passende Ordner, Nummern immer ganz verglichen*: Ein Zwischenordner `1015_PDF_100303` im
  Bauvorhaben ist kein eigenes Bauvorhaben (der äußerste Ordner mit genau so vielen Ziffern gewinnt). Der
  Auftrag `101500` ist ein Auftrag und nie das Bauvorhaben `1015`: Bauvorhaben und Auftrag werden nur über
  `parent` verbunden und Nummern nur ganz verglichen – nie über einen Präfix (der Auftrag `101599` in einem
  fremden Bauvorhaben gehört nicht zu `1015`). Eine Bauvorhaben-Nummer, die selbst ein Projekt ist (ein
  Schema mit mehreren Längen), wird ausgelassen.
- *Erkennung außerhalb von `scan_projects`*: Die Strukturerkennung und die Ablage-Prüfung zählen das Ergebnis
  des Scans als Projektordner und vergleichen es mit bestätigten Vorschlägen; Bauvorhaben im Scan hätten
  bestätigte Schemata ungültig gemacht. `registry.bauvorhaben` arbeitet nur auf den Pfaden der gefundenen
  Aufträge und liest nichts zusätzlich. Ein Bauvorhaben gibt es nur, solange ein Auftrag darin liegt; ein
  kopiertes Bauvorhaben ohne Auftrag bleibt ungelesen.
- *Dateien des Bauvorhabens = was in keinem Projektordner liegt*: Der Lauf über den Bauvorhaben-Ordner betritt
  keinen registrierten Ordner (Aufträge und ihre weiteren Ordner, andere Projekte, Ordner anderer Bauvorhaben,
  auch Projekte in ihrer Frist) – verglichen in der gespeicherten Form, damit Namen mit falscher Kodierung
  passen –, sodass keine Datei doppelt gelesen und kein Dokument zwischen Auftrag und Bauvorhaben hin und her
  geschrieben wird. Zusätzlich übernimmt ein Bauvorhaben nie die Datei eines aktiven Projekts. Bewusst
  gehören auch Ordner zum Bauvorhaben, die kein Projekt sind: ein Datumsordner mit fremder Nummer, ein
  Auftrag tiefer als `PROJECT_FOLDER_DEPTH` – sie liegen im Bauvorhaben und wären sonst gar nicht lesbar.
  Ein Projekt in seiner Frist hält seinen Ordner nur, solange es noch Dokumente hat: Danach gehört der
  Ordner dem Bauvorhaben, wie bei einer neu eingerichteten Ablage – sonst hinge das Ergebnis von der
  Vorgeschichte ab und die Dateien wären für immer ungelesen.
- *Dokumente umziehen statt neu lesen*: Wechselt eine Datei vom Bauvorhaben zu einem Auftrag (ein neuer
  Auftragsordner) oder von einem Bauvorhaben zum anderen (eine Kopie `1073_…/1071_…` bekommt einen Auftrag
  und wird weiterer Ordner von `1071`), zieht das Dokument samt Abschnitten und Vektoren um; nur die
  Volltexttitel werden neu geschrieben. Ein Bauvorhaben löscht eine Datei, die jetzt ein anderes
  Bauvorhaben liest, nicht selbst – das andere übernimmt sie im selben Lauf, gleich in welcher Reihenfolge
  die beiden gelesen werden.
- *Ein Bauvorhaben ist nur über seine Aufträge sichtbar*: Kann ein Ordner im Bauvorhaben nicht gelesen
  werden (ein Zwischenordner mit dem einzigen Auftrag), bleiben Bauvorhaben und dieser Ordner wie sie
  sind – wie ein Projekt unter einem nicht lesbaren Ordner. Sonst würden seine eigenen Dateien
  ausgeblendet und nach der Frist gelöscht, obwohl der Ordner die ganze Zeit lesbar war. Ein Bauvorhaben,
  dessen Ordner noch da ist (kein Auftrag mehr darin, Einstellung aus), wird mit der Frist ausgeblendet, aber
  in einer Sammelzeile im Protokoll gemeldet, nicht als „Ordner fehlt“ je Bauvorhaben.
- *Umfang über eine Funktion*: `db.project_scope` liefert für ein Bauvorhaben sich selbst und alle
  Unterprojekte, für einen Auftrag nur ihn. Suche (Volltext, Kennungen, Vektoren), Fragen (Überblick,
  Beteiligte, neueste Quellen) und deren Mails nutzen sie; ein Auftrag sieht nie Geschwister oder sein
  Bauvorhaben. Listen der API (`/documents`, `/api/mails?project=`) bleiben genau diese Nummer – ein
  stiller Wechsel hätte den Vertrag mit plMail und Skripten geändert.
- *Mails nie automatisch an ein Bauvorhaben*: Nummer im Text, Inhaltsvergleich, Namensbezüge, Absender-Bonus
  und Wortstatistik arbeiten nur mit Aufträgen; auch ein Schema, das 4 Ziffern mitfinden würde, ordnet nie
  einem Bauvorhaben zu und nennt dessen Nummer nicht „unbekannt“. Von Hand (Eingang, plMail) darf ein
  Bauvorhaben gewählt werden. Ob ein Absender „bisher nur zu einem Projekt“ schrieb, zählt aber die ganze
  Vorgeschichte: Wer zu einem Auftrag und (von Hand zugeordnet) zu dessen Bauvorhaben schrieb, schrieb zu
  zweien – sonst bekäme ein Geschwister-Auftrag den Bonus.
- *„Auftrag“ heißt nur beim Bauvorhaben „Projekt“*: „In welchem Auftrag …?“ und „Welche Aufträge …?“ fragen
  nur bei einer Frage zu einem Bauvorhaben über dessen Unterprojekte. Ohne Auswahl bleibt „Welchen Auftrag
  hat das Bauamt erteilt?“ eine Frage nach einem Vertrag, nicht nach einem Projekt („Unterprojekt“ gilt
  überall, das Wort ist eindeutig).
- *„Zum Bauvorhaben“ nicht für Normnummern*: Eine Zahl nach DIN, EN, ISO, VdS, DGUV … ist die Nummer einer
  Norm (DIN 4102), auch wenn es ein Bauvorhaben mit dieser Nummer gibt.
- *Kein Hinweis für Jahresordner*: Der Strukturbericht schlägt `PROJECT_PARENT_DIGITS=4` nicht vor, wenn alle
  vermeintlichen Bauvorhaben-Nummern Jahre sind (`2023_Auftraege/202301_…`) – genau der Fall, für den es
  eine Einstellung statt einer Automatik gibt.
- *Bauvorhaben-Name nicht in die Volltexttitel der Aufträge*: Beim Einschalten müssten die Titel aller
  Abschnitte aller Aufträge neu geschrieben werden (im Büro über 3.000 Aufträge, eine lange Schreibsperre).
  Stattdessen begrenzt die Auswahl des Bauvorhabens die Suche, der Reranker und das Sprachmodell sehen den
  Bauvorhaben-Namen in der Quellenzeile („Projekt 101501 Umbau Station (Bauvorhaben 1015 Altstadt)“), und
  eine Suche nach der Bauvorhaben-Nummer bietet „Zum Bauvorhaben“ an. Vektoren bleiben unverändert.
- *Letzte Aktivität ohne Zukunft*: Das Dokumentdatum kommt oft aus dem Text (eine Frist, ein geplanter
  Termin). Die Projektliste nimmt das neueste Datum, das nicht nach heute liegt; gespeicherte Datumswerte
  bleiben, weil Suche und Antworten sie brauchen.
- *Zahlen des Bauvorhabens auch gefiltert ganz*: Filtert die Projektliste auf einen Auftrag, zeigt sie das
  Bauvorhaben darüber mit den Dokumenten, Mails, der letzten Aktivität und der Zahl aller Unterprojekte –
  dieselben Werte wie ungefiltert, wie es der Hinweis unter der Liste sagt.

**OpenDocument mit der Standardbibliothek**
- *zipfile und expat reichen*: keine neue Abhängigkeit; gelesen werden nur `content.xml` und für die
  Passwortprüfung `META-INF/manifest.xml`. Wie Word-Dateien im eigenen Leseprozess.
- *Beim Parsen lesen, je Absatz/Zeile/Folie*: Jede Einheit wird als kleiner Baum gesammelt, in Zeilen
  umgesetzt und verworfen – eine Tabelle mit einer Million Zeilen braucht den Speicher einer Zeile. Weil
  ein leeres Element nur vier Byte XML braucht, aber als Knoten etwa 200 Byte, hat auch eine Einheit eine
  Grenze (500.000 Elemente), ebenso die Zahl verschiedener Namen (expat merkt sich jeden für die ganze
  Datei).
- *Nur gespeichert oder deflate*: Andere Kompressionen (bzip2, LZMA) entpackt zipfile unter Python 3.12
  in einem Block beliebiger Größe – sie kommen in ODF nicht vor und werden abgelehnt; so begrenzen die
  beim Lesen gezählten Bytes den Speicher wirklich.
- *Große Tabellen teilweise statt gar nicht*: Über 64 MB entpacktem Inhalt wird der Anfang gelesen und
  das Dokument als unvollständig markiert – Raumbücher, LVs und Exporte verlieren sonst ihren ganzen Text.
- *Externe DTD erlaubt, eigene nicht*: OpenOffice 1.x schreibt immer `<!DOCTYPE … "office.dtd">`; sie wird
  nie geladen. Interne DTD-Teile und Entity-Deklarationen werden abgelehnt (Billion Laughs, XXE).
- *Fußnoten nach dem Absatz*: „35 m2“ aus Wert und Fußnotenzeichen würde die Volltextsuche stören.
- *Keine automatischen Nummern aus Formatvorlagen*, kein `meta.xml` (oft Reste der Vorlage) – wie bei Word.
- *Wiederholungen begrenzt*: Leere Zellen und Zeilen werden nie vervielfacht (LibreOffice schreibt den
  Rest eines Blatts als eine leere Zeile mal eine Million), nicht leere höchstens 100-mal und je Datei
  höchstens 1 Mio. Zeichen an Kopien – gleiche Kopien bringen der Suche nichts.
- *Grenzen sind endgültig*: unvollständig, ohne erneuten Versuch (eine neue Extraktionsversion liest
  sie wieder); DTD und Passwort ändern sich nie. `EXTRACT_VERSION` bleibt: Dateien eines bisher nicht
  gelesenen Formats wurden nie gespeichert und werden als neue Dateien gelesen – nur Mail-Anhänge waren
  gespeichert („Format … nicht unterstützt“) und werden einmal aus dem Cache nachgelesen.

**Dokumente als Ordneransicht**
- *Aus dem Index, nicht von der Freigabe*: Die Ansicht baut die Ordner aus den Pfaden der gelesenen
  Dokumente. Kein Klick greift auf den Dateiserver zu (schnell, auch wenn die Freigabe gerade fehlt, kein
  zweiter Weg an den Grenzen von `PROJECTS_DIR` vorbei); dafür fehlen nicht gelesene Dateitypen – die
  Seite sagt das.
- *Ordner als Adressparameter, ohne `..`*: Der Ordner ist nur ein Filter auf die Dokumente des Projekts;
  leere, `.`- und `..`-Teile werden entfernt, ein unbekannter Ordner zeigt einen Hinweis.
- *Zahl und Probleme am Ordner*: Damit ein übersprungener Scan tief in der Ablage nicht unsichtbar wird,
  zeigt jeder Ordner, wie viele Dokumente darunter nicht vollständig gelesen sind.

**Kompakte Vektorsuche**
- *Warum*: Die ganze float32-Matrix im RAM war für etwa 100.000 Abschnitte gedacht (1 GB). Die echte Ablage
  (rund 214.000 lesbare Dateien in 3.353 Aufträgen) ergibt grob 1–5 Mio. Abschnitte, also 10–50 GB RAM.
- *Vorauswahl mit Vorzeichen-Bits, genaue Bewertung aus SQLite*: 1 Bit je Dimension (Vorzeichen nach
  Abzug des Mittelwertvektors – Satzvektoren teilen eine gemeinsame Richtung, ohne Zentrieren wären die
  meisten Bits überall gleich), Vergleich über die Zahl unterschiedlicher Bits mit NumPy, keine neue
  Abhängigkeit. Die 2000 besten (mindestens 10 × k) werden mit den gespeicherten Vektoren genau bewertet,
  die Rangfolge und die Werte sind also die genauen.
- *Gemessen mit künstlichen Vektoren* (200.000 × 1024, viele dichte Themengruppen): RAM 26 statt 819 MB,
  je Suche etwa 40 ms; die besten 10 stimmen zu 98 % mit der exakten Suche überein, die besten 50 zu 81 % –
  die Abweichungen liegen bei Treffern mit fast gleichem Wert (Unterschied unter 0,003), danach entscheiden
  ohnehin Volltext und Reranker. Mit 500 Kandidaten waren es nur 89 % / 55 %, mit 5000 99 % / 93 % bei
  60 ms. Echte Satzvektoren lassen sich meist besser in Bits fassen; gemessen wird mit der echten KI
  (Schritt 17, Vergleich mit `VECTOR_SEARCH=matrix`).
- *In einem Projekt exakt*: Hat der Suchbereich höchstens so viele Abschnitte wie Kandidaten, wird jeder
  genau bewertet – Fragen in einem Auftrag verlieren nichts.
- *Ausgeblendete Abschnitte*: Ohne Projektfilter werden die Kandidaten beim Lesen auf sichtbare
  beschränkt; sind zu wenige sichtbar (viele Projekte fehlen, Abschnitte seit dem Laden gelöscht), wird
  die Vorauswahl vervierfacht, höchstens bis zum 64-fachen.
- *Matrix bleibt wählbar*: zum Vergleich in der Messung und falls die Bits mit einem Modell schlecht
  funktionieren.

**Projektseite: Zeitleiste links, Rest rechts**
- *Warum*: Ein Projekt mit über 300 Dokumenten seit 2014 machte die Zeitachse zu einer langen Liste. Neues
  soll genau, Altes nur noch grob erscheinen – und Dokumente, Anhänge und Mails sollen nicht mehr
  untereinander durch die Seite führen. Gewählt nach Entwürfen im Gespräch (Kalender, Fischauge, Ausschnitt,
  Minikarte …); Meilensteine und Schieberegler wollte der Betreiber nicht.
- *Wurzelkurve statt Logarithmus*: Position = (Alter / ältestes Alter)^0,45. Die letzten Wochen sind fein
  aufgelöst, ein altes Jahr braucht rund 100 px, und die Skala bleibt lesbar (ein echter Logarithmus staucht
  die ältesten Jahre auf Striche). Höhe 460 bis 2400 px je nach Zahl der Einträge, die Leiste scrollt für sich.
- *Bezugspunkt ist der neueste Eintrag*, höchstens heute: Ein ruhendes Projekt hat keine leere Fläche oben,
  ein Eintrag in der Zukunft verschiebt nichts. Einträge ohne Datum haben einen eigenen Link.
- *Zeilen, wo Platz ist, sonst Etiketten*: Zeilen dürfen höchstens 26 px unter ihren Punkt rutschen (eine
  feine Linie zeigt, wohin sie gehören); dichtere Strecken werden zu einem Etikett mit Zahl und Zeitraum, das
  die Einträge dieser Tage rechts auflistet. Bis 30 Einträge gibt es nie Etiketten – die Leiste wird länger.
  Punkte am selben Ort und Linien über sechs je Etikett werden weggelassen, damit eine Seite mit Tausenden
  Einträgen leicht bleibt (491 Einträge: 84 KB).
- *SVG mit `foreignObject`*: Die Seite erlaubt keine Inline-Styles (CSP), Positionen müssen Attribute sein –
  das kann SVG. Die klickbaren Zeilen sind echte HTML-Links in `foreignObject`, weil htmx den Klick auf einen
  SVG-`<a>` nicht abbricht (die Seite würde zusätzlich neu geladen). Alle Maße rechnet `zeitleiste.py`, die
  Vorlage druckt sie nur; die Farben stehen in `app.css`.
- *Auswahl in der Adresse*: `?auswahl=` trägt den Eintrag oder die Strecke, damit Zurück-Taste, Lesezeichen
  und Links gehen; Strecken sind Datumsbereiche (stabil, auch wenn sich die Etiketten ändern). Unbrauchbare
  Werte zeigen keine Karte statt eines Fehlers. Ein Klick tauscht nur die Zeichnung und (außerhalb der
  Reihenfolge) die Karte, damit die Leiste nicht an den Anfang springt.
- *Reiter statt Abschnitte*: Dokumente, Anhänge, Mails sind Reiter mit Zahl; Mails und Anhänge zeigen höchstens
  200 Zeilen (`alle zeigen`), weil ein großes Projekt sonst tausende Zeilen in die Seite schreibt.

**Scans in zwei Stufen (Phase „Bildauswertung“)**
- *Warum*: Gemessen auf der Ablage: rund 892.000 PDF-Seiten, davon 110.000 Scans mit OCR-Ebene und 180.000 ohne
  Text. Das Bildmodell braucht 40–60 s je Seite, der KI-Server nimmt 4 Anfragen gleichzeitig und beantwortet auch
  die Fragen. Beim Einlesen auf das Modell zu warten hieße: wochenlang keine neuen Dateien und Mails. Und
  handschriftliche Vermerke auf Scans mit OCR-Ebene gingen ganz verloren, weil Seiten mit Textebene nie an das
  Modell gingen.
- *Stufe 1 sofort*: OCR-Ebene oder Tesseract (`tesseract`-Programm mit `-l deu` über `subprocess`, keine neue
  Python-Abhängigkeit; fehlt es oder die deutschen Sprachdaten, still ohne). Das Dokument ist gleich
  durchsuchbar; die Scan-Seiten stehen mit ihrem Text in `page_texts`, damit Stufe 2 das Dokument Seite für
  Seite wieder zusammensetzen kann, ohne die Datei neu zu lesen.
- *Variante A (Bild + Text)*: Das Modell bekommt das Seitenbild und den erkannten Text und gibt die ganze Seite
  zurück. Die Texterkennung liefert die Buchstaben, das Bild die Korrektur, die Handschrift und die Stempel an
  der richtigen Stelle. Nur Handschrift abfragen wäre billiger, verliert aber die Zuordnung zur Zeile
  („erledigt“ – welche Klappe?). Gibt das Modell nichts zurück, bleibt der Text der ersten Stufe.
- *Wann eine Seite ein Scan ist*: ohne Textebene jedes Bild (wie bisher); mit Textebene nur Bilder mit zusammen
  ≥ 1 Megapixel, die ≥ 50 % der Seite bedecken (Streifen zählen zusammen; ohne bekannte Lage nur die Pixel).
  Die Megapixel-Grenze stammt aus der Zählung auf der Ablage; die Flächenregel hält Fotos in Berichten und Logos
  heraus. Ein ganzseitiges Briefbogen-Bild hinter digitalem Text gilt als Scan (kostet Zeit, schadet nicht).
  Die erste Prüfung liest nur die angegebenen Bildgrößen der Ressourcen, ohne Seiteninhalt – digitale Seiten
  kosten fast nichts.
- *Parallel, aber nur das Modell*: Die Seitenbilder dekodiert und setzt weiter der Leseprozess unter seinen
  Grenzen zusammen (je Anfrage wenige Seiten, als JSON-Kopf plus rohe Bildbytes, nie gepickelt); die App schickt
  nur die fertigen JPEG/PNG aus bis zu `VISION_CONCURRENCY` Daemon-Threads an das Modell. So bleibt ein
  Leseprozess (keine Pufferung von Hunderten Bildern, keine weiteren Prozesse mit eigenem Speicherbudget),
  nicht vertrauenswürdige PDF-Daten werden nie im App-Prozess geparst, und das Vorbereiten (unter einer Sekunde) hält
  mit 3 Modellaufrufen à 50 s leicht Schritt. Daemon-Threads statt ThreadPoolExecutor: eine Antwort unterwegs
  hält das Beenden der App nicht auf. Standard 3, damit einer der 4 Plätze für Fragen frei bleibt.
- *Zeitbudget je Lauf* (`VISION_PASS_SECONDS`, 1800 s): danach werden keine neuen Seiten mehr vergeben, laufende
  noch abgewartet; dann folgen neue Dateien und Mails im nächsten Lauf. Jede gelesene Seite wird sofort
  gespeichert, das Dokument am Ende seiner Seiten (oder des Laufs) neu in Abschnitte zerlegt – nur dieses, seine
  Vektoren macht ein Embedding-Lauf direkt danach. Neueste zuerst (Dokumentdatum, dann Änderungszeit): Neues
  ist meist gefragter.
- *Fehler*: Modell aus → Seite mit wachsendem Abstand erneut (15 min … 7 Tage), ihr Text der ersten Stufe bleibt
  durchsuchbar. Kein Bild lesbar → Seite übersprungen und vermerkt; Teile, die fehlen, Grenzen des Leseprozesses
  und fehlende Komponenten wirken wie bisher (Wiederholung, `|limit:` im Profil).
- *Extraktionsversion 7*: nötig, weil vollständige ältere PDF-Dateien nicht wissen, ob sie Scans mit OCR-Ebene
  enthalten. Die Version markiert, was schon geprüft ist; die Prüfung läuft nebenbei in der Bildauswertung,
  während das Modell arbeitet, und nur für PDF-Dateien, die ohne Modell gelesen wurden (eine zweite schnelle
  Vorprüfung ohne Textauszug lässt reine Digital-Dateien gleich durch). Was das Modell schon gelesen hat, bleibt.
  Der Bump liest sonst nur unvollständige Dokumente neu (sie bekommen jetzt Tesseract bzw. Stufe 2). Das Profil
  trägt „+ocr“, wenn Tesseract da ist: Nachinstallieren liest die unvollständigen nach.
- *Direkte Aufrufe* (`extract()`, Skripte, Tests) lesen weiter in einem Zug mit dem Modell (ebenfalls mit Bild +
  Text); nur ein Indexierungslauf arbeitet in zwei Stufen (`documents.vision_later`).
- *Fotos in `VISION_SKIP_FOLDERS`*: weder Modell noch Tesseract (Baustellenfotos ergeben keinen Text, nur
  Zeichenmüll; Tesseract-Text mit weniger als drei Wörtern wird ohnehin verworfen).

**Seiten mit Text als Kurven (PDFium)**
- *Warum*: Ein Auszug einer gedruckten Bauordnung aus einem PDF-Druckertreiber hat keine Schrift und kein Bild,
  nur rund 45.000 Pfadbefehle je Seite (jeder Buchstabe ein Umriss) – gespeichert als „kein Text gefunden“, obwohl
  der Browser ihn lesbar zeigt. Ohne Textebene und ohne Bild ging eine Seite weder an Tesseract noch an das Modell.
- *PDFium über `pypdfium2`*: echter Renderer, Lizenz Apache-2.0/BSD-3, fertige Wheels für x86_64 und aarch64 ohne
  weitere Abhängigkeiten; Poppler/Ghostscript als Programm wären größer und brächten GPL bzw. AGPL. Gezeichnet wird
  nur, was sonst als leer gälte (keine Bilder, auch nach abgebrochener Inhaltsprüfung), im Leseprozess unter seinen
  Grenzen: Graustufen, 300 dpi, höchstens `MAX_IMAGE_PIXELS`, angerechnet auf `MAX_DOC_PIXELS`; fortschreitend
  gezeichnet und nach `RENDER_SECONDS` (20 s) abgebrochen („Seiteninhalt zu umfangreich“). Gemessen: 0,04 s Zeichnen,
  1,1 s mit PNG-Kodierung je
  A4-Seite mit 1,3 MB Pfaden.
- *Leer*: Erst das gezeichnete Bild entscheidet (kein Pixel dunkler als 200 von 255) – billig und ohne eigene
  Inhaltsanalyse. Ohne Lesen (kein Modell, kein Tesseract) wird nur klein (1 Megapixel) geprüft.
- *Unbrauchbare Textebene*: nur der eindeutige Fall (mehr als die Hälfte Ersatz-, Steuer- oder Privatzeichen).
  Falsch zugeordnete, aber „normale“ Buchstaben sind billig nicht erkennbar und bleiben, wie sie sind.
- *Seiten mit kleinem Bild* (Logo) und Text als Kurven werden weiter nur mit ihren Bildern gelesen – bewusst
  nicht geändert, weil das die geprüfte Scan-Erkennung berührt.
- *Bestand*: Migration 15 statt Versionssprung – ein Versionssprung liest nur unvollständige Dokumente. Markiert
  werden PDF-Dateien „leer“ und vollständige, bei denen vor der letzten Textseite eine Seite ohne Abschnitte liegt
  (`documents.pages` ist die letzte Seite mit Text; Lücken am Ende sieht die Abfrage nicht).

**Reihenfolge beim Einlesen (Statusseite „Reihenfolge“)**
- *Warum*: Der erste Lauf über die ganze Freigabe dauert Tage. Das Büro will bestimmen, was zuerst lesbar ist
  („erst alle Projekte aus Ordner 3, dann aus 2“), einzelne Projekte vorziehen und sehen, was fertig ist.
- *Reihenfolge*: vorgezogene Projekte (in der Reihenfolge des Vorziehens) → noch nicht fertige Projekte Ordner
  für Ordner → fertige Projekte in derselben Reihenfolge. Die fertigen werden weiter in jedem Lauf durchgegangen
  (unveränderte Dateien kosten nur ein `stat`), damit neue und geänderte Dateien wie bisher in jedem Lauf
  gefunden werden – nur eben nach der Warteschlange. Mails laufen unverändert nach den Dokumenten. Im Ordner
  höchste Nummer zuerst (wie die neuesten Dokumente in der Bildauswertung: Neues ist meist gefragter); vorher
  war die Reihenfolge die der Einfügung in die Datenbank, Bauvorhaben am Schluss.
- *Ordner*: die oberste Ebene unter `PROJECTS_DIR` (bei eurer Ablage `1` … `9`), nur Ordner mit Projekten.
  Nicht platzierte Ordner: nummerierte absteigend (`3, 2, 1`, passt zu „neueste zuerst“), dann die übrigen nach
  Namen, Projekte direkt in `PROJECTS_DIR` zuletzt. „Aus“ stellte zuerst nur ans Ende; seit dem Wunsch des Büros
  wird ein ausgeschalteter Ordner gar nicht gelesen (Abschnitt „Ordner „aus“ wird nicht gelesen“ unten). Ein
  ausgeschalteter Ordner lässt sich nicht über eingeschaltete schieben (die Anzeige entspricht immer der
  tatsächlichen Reihenfolge).
- *Gespeichert*: Tabelle `index_folders` (Migration 11, nur hinzugefügt). Leer → `INDEX_FOLDER_ORDER` gilt; die
  erste Änderung auf der Seite speichert die ganze Reihenfolge, ab dann gilt sie (sonst wäre unklar, was eine
  geänderte Umgebungsvariable gegen eine Einstellung auf der Seite bedeutet). „Auf Standard zurücksetzen“ leert
  die Tabelle. Ordner, die gerade keine Projekte haben, behalten ihren Platz hinten.
- *Bauvorhaben*: Aufträge zählen im Ordner ihres Bauvorhabens und bleiben zusammen, sortiert nach dem neuesten
  Auftrag; das Bauvorhaben selbst (seine Dateien außerhalb der Aufträge) direkt danach. Zusammengeführte Projekte
  (mehrere Ordner einer Nummer) zählen im Ordner ihres Hauptordners. Welches Bauvorhaben eine Datei liest, die
  zwei lesen könnten, regelt wie bisher die Übergabe (unabhängig davon, wer zuerst dran ist).
- *Fertig*: `projects.done_at` wird gesetzt, wenn der Dokumentendurchgang eines Projekts über alle seine Ordner
  endet, zusammen mit dem Leseprofil (`done_profile`). Fertig heißt `done_at` gesetzt und Profil = aktuelles
  Profil – ein anderes Modell oder `LLM_VISION=1` stellt alles wieder an, ohne etwas umzuschreiben. Muss eine
  Datei gelesen werden (neu, geändert, Wiederholung fällig), wird `done_at` vorher gelöscht: Bricht der Lauf ab,
  steht das Projekt in der Warteschlange und wird nach dem Neustart zuerst gelesen. Kein erreichbarer Ordner →
  nicht fertig. Billiger als jede Datei gegen das Dateisystem zu prüfen, und richtig, weil jeder Lauf ohnehin
  jedes Projekt durchgeht. Die Bildauswertung zählt nicht dazu (eigene Anzeige „Bildauswertung ausstehend“).
- *„N übersprungen“*: Dateien und Ordner, die das Projekt nicht lesen konnte – Lesefehler, zu groß, Name in
  falscher Zeichenkodierung, nicht auflistbare Ordner, Dokumente mit Status „Fehler“. Nicht gelesene Dateitypen
  (`.dwg` …) zählen nicht, sonst stünde fast jedes Projekt als Warnung da.
- *Vorziehen*: `projects.pinned` (fortlaufende Zahl); wird beim Fertigwerden gelöscht.
- *Änderungen im laufenden Lauf*: Jede Änderung erhöht einen Zähler in `meta`; der Lauf vergleicht ihn vor
  jedem Projekt (eine kleine Abfrage) und sortiert die noch nicht angefangenen neu. Auch ein Lauf in einem
  anderen Prozess (`python -m projektakte.index`) sieht das. Die Bildauswertung prüft denselben Zähler vor
  jedem Dokument.
- *Bildauswertung*: Stufen vorgezogen → Ordner (ausgeschaltete zuletzt, Anhänge ohne Projekt nach den
  eingeschalteten Ordnern); innerhalb einer Stufe bleibt „neueste Dokumente zuerst“. Fertig/nicht fertig spielt
  hier keine Rolle.
- *„Wird gerade gelesen“*: Der Lauf meldet Projekt, Dateizahl (die Dateien eines Ordners werden dafür vorher
  aufgelistet – einige Tausend Pfade je Projekt, unkritisch) und Phase an ein Objekt im Prozess
  (`reihenfolge.LIVE`); die Karte fragt es alle 4 s ab, ohne Lauf alle 30 s. Ein Abruf liest nur diesen Zustand
  und die Versionszahl; nur nach einer Änderung wird die Reihenfolge für „Danach“ neu berechnet.
- *Viele Projekte*: Die Seite liest alle Projekte mit einer Abfrage und sortiert in Python (15.000 Projekte:
  deutlich unter einer Sekunde), Dateizahlen nur für die höchstens 50 gezeigten Zeilen mit einer gruppierten
  Abfrage. Ein Test baut 15.000 Projekte und zählt die SQL-Anweisungen.
- *API*: eigener Endpunkt `/api/reihenfolge` statt `/api/status`, weil `/api/status` billig bleiben soll
  (Monitoring); Änderungen wie die anderen schreibenden Endpunkte als POST mit JSON, gleicher Schutz
  (Anmeldung, keine fremden Seiten) durch die Middleware.

**Dokument-Betrachter (Dateien im Fenster ansehen)**
- *Warum*: Originaldateien öffneten bisher nur auf einer neuen Seite im Betrachter des Browsers; wer mehrere
  Quellen einer Antwort oder die Pläne eines Ordners durchsehen will, verlor jedes Mal die Projektseite.
- *Fenster statt Seite*: Ein Bereich rechts über der Seite (Handy: ganzer Bildschirm), kein Nebeneinander im
  Raster: die Projektseite hat schon zwei Spalten, eine dritte wäre auf dem Laptop zu schmal für einen Plan. Der
  Inhalt kommt als HTML-Fragment vom Server (`/dokumente/{id}/ansicht`), ein kleines Skript
  (`static/betrachter.js`, unter `script-src 'self'`) hängt sich an Links mit `data-betrachter`. Der `href` bleibt
  die Dokumentseite: ohne JavaScript ändert sich nichts, mittlere Maustaste/Strg-Klick öffnen wie bisher die Seite.
  Die Adresse `…/ansicht` ohne das Skript (neuer Tab) leitet auf die Dokumentseite um. ‹ › blättert durch die
  Dokument-Links derselben Liste (Tabelle oder Liste um den Link, gleiche Dokumente hintereinander zählen einmal) –
  ohne Abfrage am Server, die Liste steht ja schon auf der Seite.
- *Zurück-Knopf: ein eigener History-Eintrag mit derselben Adresse* (`history.pushState({bt: …})`), kein
  `hx-push-url` und kein `#hash`. Eine eigene Adresse für den Betrachter hätte beim Neuladen eine Projektseite mit
  offenem Fenster gebraucht (doppelte Darstellung), ein Hash stört die Sprungmarken `#c…`/`#inhalt`. Zurück schließt
  das Fenster, Vorwärts öffnet es wieder; Blättern im Fenster ersetzt den Eintrag (sonst bräuchte man zehnmal
  Zurück). htmx stellt bei „popstate“ seine Einträge wieder her, indem es die Seite neu aufbaut (die Zeitleiste
  verlöre ihre Scrollposition); zwischen dem Eintrag des Fensters und dem der Seite mit derselben Adresse wird das
  übersprungen (`window.onpopstate` von htmx wird dafür einmal umhüllt). Navigiert htmx bei offenem Fenster
  (Ordner, Reiter), schließt das Fenster vorher, damit es nicht in htmx' Seitenspeicher landet.
- *PDF im `<iframe>` der bestehenden Dateiroute* (Betrachter des Browsers, `#page=N` für die Seite der Quelle)
  statt pdf.js: keine neue Abhängigkeit, kein eigener Code für PDF-Inhalte. Dafür dürfen Originaldateien vom eigenen
  Ursprung eingebettet werden (`frame-ancestors 'self'`, `X-Frame-Options: SAMEORIGIN` für ältere Browser); ihre
  skriptfreie CSP und die Sandbox für alles außer PDF bleiben. Die Fehlerseite unter derselben Adresse und alle
  Seiten der Anwendung bleiben `DENY`. Die Seiten erlauben Rahmen nur vom eigenen Ursprung (`frame-src 'self'`,
  vorher implizit über `default-src`) und keine Plugins (`object-src 'none'`).
- *HTML, SVG, XML, Skripte*: nie im Rahmen, nie als `<img>` – sie liefen sonst im Ursprung der Anwendung. Die
  Dateiroute liefert sie als `application/octet-stream` mit `attachment` (dazu die Sandbox-CSP); der Betrachter
  zeigt den gelesenen Text.
- *TIFF/BMP: Umwandlung im Webprozess mit Pillow, nicht im Leseprozess*: Der Leseprozess liest während eines Laufs
  eine Datei nach der anderen (bis zu Minuten je Datei); wer einen Plan ansehen will, darf nicht dahinter warten.
  Stattdessen eigene Grenzen: Dateigröße (300 MB), Größe **vor dem Entpacken** aus dem Kopf der Datei geschätzt
  (200 Megapixel, etwa 320 MB im Speicher samt Umwandlung – ein A0-Plan mit 300 dpi in Schwarzweiß braucht etwa
  140 MB; eine „Dekompressionsbombe“ wird abgelehnt, bevor sie Speicher kostet), Pillows eigene Bombenprüfung,
  **eine Umwandlung zur Zeit** (weitere warten bis 20 s, dann 503), Ausgabe höchstens 3.200 Pixel Kantenlänge
  (schnelles ganzzahliges Verkleinern, dann fein). Schwarzweiß/Grau als PNG (Pläne bleiben scharf und klein),
  Farbe als JPEG. Mehrseitige TIFF Seite für Seite (die Seitenzahl kommt aus den Köpfen, höchstens 999).
- *Kein Zwischenspeicher*: weder auf der Platte noch im Browser (`no-store` wie überall, damit Dokumente nicht auf
  gemeinsam genutzten Rechnern liegen bleiben). Eine Seite erneut umzuwandeln kostet bei großen Plänen ein bis zwei
  Sekunden; ein Cache bräuchte Aufräumen, Größengrenzen und Schutz vor veralteten Bildern – erst, wenn es im
  Betrieb stört.
- *Mails, Word, OpenDocument, Text*: der gelesene, bereinigte Text aus dem Index (dieselbe Darstellung wie die
  Dokumentseite), kein LibreOffice im Image – zu groß, zu langsam und eine weitere Angriffsfläche für fremde
  Dateien. Die Originaldatei gibt es zum Herunterladen.
- *Fehlende Datei*: Ein noch nicht gelieferter Anhang oder eine verschwundene Datei zeigt dieselben Hinweise wie
  Dokument- und Mailseite und den gelesenen Text, nie einen leeren Rahmen.
- *Grenzen*: Mails selbst (nicht als Datei) öffnen weiter ihre eigene Seite. Ob der PDF-Betrachter im Rahmen
  erscheint, hängt vom Browser ab (Chromium, Firefox, Edge ja; manche Handy-Browser zeigen PDF in Rahmen nicht –
  auf schmalen Bildschirmen steht darum unter dem Rahmen ein Link „in neuem Fenster öffnen“).

## Große Planscans: 450 MB je Bild statt pypdfs 75 MB

Im ersten Lauf auf echten Daten scheiterten Planmappen (A1, Farbe, 300 dpi: 70 Megapixel, 209 MB Rohdaten) an
pypdfs eigener Grenze von 75 MB je entpacktem Bild bzw. Bildpuffer („LimitReachedError: Requested image buffer
size … exceeds limit 75000000“). Diese Grenze ist ein Schutz für Programme ohne eigene Grenzen; Projektakte liest
PDF-Dateien im Leseprozess mit Speicher- und Zeitgrenze und prüft vorher Pixel je Seite (`MAX_PAGE_PIXELS`) und
je Dokument.
- *`PDF_IMAGE_BYTES` = 450 MB* für alle Entpack- und Puffergrenzen von pypdf (Flate, LZW, RunLength, JBIG2,
  Bildpuffer): ein A0-Plan mit 300 dpi in Farbe (140 MP, 418 MB) passt. Gemessen unter 2 GB Adressraum (Grenze
  des Leseprozesses): A1 Farbe 0,5 GB Spitze und 4 s, A0 Farbe 1 GB und 7 s, A0 Grau 0,3 GB. Wer
  `EXTRACT_MEMORY_MB` kleiner setzt, bekommt für solche Pläne wie bisher „zu groß“ mit dem Hinweis auf die Grenze.
- *Einmal neu lesen (Migration 12)* statt `EXTRACT_VERSION` zu erhöhen: Eine Erhöhung würde alle unvollständigen
  Dokumente neu lesen und alle vollständigen PDF-Dateien erneut auf Scan-Seiten prüfen lassen. Die Migration
  markiert nur Dokumente, deren Bilder an der Daten- oder Speichergrenze scheiterten; sie läuft genau einmal, eine
  Datei jenseits der neuen Grenze wird also nicht in jedem Lauf wieder gelesen.
- *Protokoll*: „Bild nicht lesbar“ je Dokument und Ursache nur einmal (Streifenscans mit JBIG2 ohne `jbig2dec`
  schrieben eine Zeile je Streifen); der Fortschritt nennt die Seitennummer im PDF und die laufende Nummer unter
  den Scan-Seiten („Seite 22 (2 von 5)“) – vorher passten die beiden Zählungen nicht zusammen.

## Einlesen, Bildauswertung und Embeddings nebeneinander

Auf der echten Freigabe dauert der erste Dokumentendurchgang Tage (Tesseract für rund 180.000 Scan-Seiten). Die
Bildauswertung und die Embeddings kamen erst danach – das Modell und der Embedding-Dienst warteten tagelang, obwohl
der KI-Server vier Anfragen gleichzeitig nimmt und der Embedding-Dienst ein eigener Dienst ist.

- *Eigene Threads in der Anwendung* (`vision.VisionWorker`, `embed_worker.EmbedWorker`), gestartet und beendet mit
  dem Indexierungs-Thread (`background.py`), keine Task-Queue. Der Lauf lässt seine Phasen „Embeddings“ und
  „Bildauswertung“ dann aus (`run_index(vision=False, embeddings=False)`; Statistik „läuft im Hintergrund“).
  `python -m projektakte.index`, `evaluate` und die Tests ohne Anwendung behalten die Phasen am Ende des Laufs –
  dort gibt es keinen zweiten Thread. Mit `LLM_VISION=0` startet keine Bildauswertung.
- *Gleichzeitigkeit*: Bildauswertung höchstens `VISION_CONCURRENCY` (3) Modellaufrufe – einer der vier Plätze des
  KI-Servers bleibt für Fragen frei, wie bisher; nur ein Durchgang der Bildauswertung je Prozess (`vision._PASS_LOCK`),
  die Grenze gilt also insgesamt. Embeddings höchstens `EMBED_CONCURRENCY` (2) Stapel zu 32 Abschnitten: ein
  zweiter Stapel hält den Dienst beschäftigt, während der erste geschrieben wird, und eine Frage braucht nur einen
  kurzen Aufruf dazwischen. Mehr bringt bei einem GPU-Dienst kaum etwas (er bündelt selbst).
- *Seitenbilder: ein zweiter Leseprozess statt eines geteilten.* Der Leseprozess des Laufs ist für eine ganze Datei
  belegt – Tesseract auf einer 100-seitigen Scan-Mappe dauert Minuten. Teilten sich beide den Prozess (mit Sperre),
  wartete die Bildauswertung dahinter und das Modell stünde still. Der zweite Prozess (`extract_worker.own_worker`)
  hat dieselben Grenzen (`EXTRACT_MEMORY_MB`, CPU, Wandzeit), damit auch die Grenzvermerke im Profil (`|limit:`)
  dieselbe Bedeutung haben. Gemessen: leer etwa 50 MB RSS (die App etwa 65 MB); im schlimmsten Fall braucht er wie
  der erste bis zu `EXTRACT_MEMORY_MB`, ein A0-Farbplan etwa 1 GB. Daher Container-Speicher mindestens App + 2 ×
  `EXTRACT_MEMORY_MB` (`deploy/compose.yaml`: 6 GB statt 4 GB).
- *SQLite*: jeder Thread eine eigene Verbindung (WAL, `busy_timeout` 30 s wie bisher), Schreiben in kurzen
  Transaktionen. Prüfen und Schreiben gehören zusammen: Die Bildauswertung nimmt dafür `BEGIN IMMEDIATE` und
  speichert eine Seite bzw. setzt ein Dokument nur zusammen, wenn die Datei noch dieselbe ist (`sha256`) – hat der
  Dokumentendurchgang eine geänderte Datei inzwischen neu gelesen, warten deren neue Seiten und die alte Antwort
  wird verworfen (Statistik `discarded`). Ein Vektor wird nur gespeichert, wenn der Abschnitt noch ohne Vektor ist
  und denselben Text hat (`vectors.STORE_SQL`): Ein neu geschriebenes Dokument löscht seine Abschnitte, und SQLite
  kann einem neuen Abschnitt dieselbe Nummer geben. Ein so übergangener Abschnitt kommt in der nächsten Runde dran
  (der Embedding-Thread beginnt nach dem Ende wieder vorn). Kein Vektor doppelt, keiner für den falschen Text.
- *Migration 13*: Teilindex `chunks_without_vector` (nur Abschnitte ohne Vektor). Der Embedding-Thread sucht und
  zählt sie vor jeder Runde, die Statusseite bei jedem Aufruf – ohne Index wäre das jedes Mal ein Durchlauf über
  alle Abschnitte. Nur hinzugefügt (`IF NOT EXISTS`).
- *`VISION_PASS_SECONDS` bleibt, heißt jetzt „Runde“*: Nach der Zeit endet ein Durchgang der Bildauswertung und der
  nächste beginnt sofort. Wozu: fehlgeschlagene Seiten, deren Wartezeit inzwischen um ist, und übersprungene
  Dokumente kommen wieder dran, und die Seiten bleiben nie länger als eine Runde ungeschrieben. Neue Dokumente
  übernimmt die Runde schon vorher (Liste alle 60 s neu gelesen, `REFRESH_SECONDS`), ebenso eine geänderte
  Reihenfolge. Ohne Arbeit schläft der Thread, bis der Lauf ein Dokument mit Scan-Seiten schreibt
  (`documents.VISION_WAITING`) oder 60 s um sind; der Embedding-Thread ebenso bis zu neuen Abschnitten
  (`vectors.CHUNKS_WAITING`) oder 30 s.
- *Beenden*: Die Bildauswertung wartet nicht auf Antworten unterwegs (Daemon-Threads wie bisher); ihr Leseprozess
  wird beendet. Was noch nicht gespeichert ist, liest sie nach dem nächsten Start. Ein Dokument, dessen Seiten alle
  gelesen, aber noch nicht zusammengesetzt waren, setzt sie dann ohne Modell zusammen (vorher blieb so ein Dokument
  als „ausstehend“ stehen). Der Embedding-Thread lässt die Stapel beim Dienst noch zu Ende laufen (deren eigene
  Zeitgrenze).
- *Datenbank anlegen*: Beim Start eines frischen `DATA_DIR` legten Indexierung und Hintergrund-Threads gleichzeitig
  die Tabellen an („table documents already exists“); `db.init_db` ist jetzt im Prozess gesperrt.
- *Vektoren nach der Bildauswertung*: macht der Embedding-Thread, sobald die Abschnitte geschrieben sind (vorher
  ein Embedding-Lauf am Ende der Phase).

## Ordner „aus“ wird nicht gelesen

Wunsch des Büros: „Wenn ich einen Ordner ausschalte, soll er aus sein und nicht gelesen werden.“ Vorher kam er nur
ans Ende.
- *Was „aus“ heißt*: kein Lauf liest seine Projekte (auch keine neuen oder geänderten Dateien, kein Tesseract), die
  Bildauswertung nimmt seine Dateien nicht (auch nicht die Nachprüfung älterer PDF-Dateien). Ein Projekt daraus,
  das gerade gelesen wird, hört nach der aktuellen Datei auf (`Plan.switched_off`, höchstens einmal je Sekunde eine
  kleine Abfrage), wird nicht als fertig markiert und entfernt nichts – die nicht durchgesehenen Dateien gelten
  nicht als verschwunden.
- *Nichts wird gelöscht*: Was gelesen ist, bleibt im Index und durchsuchbar, die Projekte bleiben im Register und
  aktiv (der Registerabgleich weiß nichts von „aus“). Wer einen Ordner aus der Suche nehmen will, braucht etwas
  anderes – „aus“ ist eine Einstellung für das Lesen, kein Löschen, und einschalten soll ohne Neulesen weitergehen.
- *Mails* und ihre Anhänge sind nicht betroffen, auch wenn sie einem Projekt eines ausgeschalteten Ordners
  zugeordnet sind (sie kommen nicht aus dem Ordner).
- *Zuordnung zum Ordner* wie bei der Reihenfolge: Aufträge eines Bauvorhabens gehören zum Ordner des Bauvorhabens,
  zusammengeführte Projekte zum Ordner ihres Hauptordners.
- *Vorziehen geht nicht* (Meldung „erst den Ordner einschalten“): Ein vorgezogenes Projekt, das nicht gelesen wird,
  wäre ein Widerspruch auf der Seite. Wird der Ordner eines schon vorgezogenen Projekts ausgeschaltet, gilt „aus“.
- *Anzeige*: Ordnerzeile „aus – wird nicht gelesen“, Projekte „Ordner ausgeschaltet“ (Reiter „Alle“, nicht in der
  Warteschlange), Hinweis unter der Warteschlange, Bildauswertung „N Seiten in ausgeschalteten Ordnern“.
- *Keine Umgebungsvariable `INDEX_FOLDER_OFF`*: Sie hätte durch alle Aufrufer der Reihenfolge gereicht werden müssen,
  und die Einstellung auf der Statusseite bleibt ohnehin in der Datenbank erhalten. `INDEX_FOLDER_ORDER` bleibt,
  wie sie war.

## Statusseite: echte Dateizahl, Zeiten und Schätzungen, ruhiges Ladesymbol

- *25er-Schritte*: Die Karte erfuhr die Dateizahl nur jede 25. Datei (`ProjectRun.step`, um den Zustand selten zu
  sperren). Das kostet nichts: Der Zustand liegt im Speicher, ein Zähler unter einer Sperre. Jetzt jede Datei.
- *Ladesymbol*: HTMX ersetzte alle 4 s die ganze Karte samt Ladesymbol, die CSS-Animation begann jedes Mal von vorn.
  Jetzt ersetzt HTMX nur den Inhalt (`#rf-jetzt-inhalt`); das Ladesymbol steht daneben, im Seitenquelltext danach
  (per `order: -1` vorn angezeigt), und wird über den Geschwister-Selektor `.idle ~ .rf-spin` ausgeblendet – ohne
  Skript und ohne Inline-Stil (CSP). Im Browser (Chromium) geprüft: dasselbe Element über alle Abfragen, die
  Animationszeit läuft durch.
- *Jede Tätigkeit für sich*: Seit Bildauswertung und Embeddings nebenher laufen, zeigt die Karte Lauf, Bildauswertung
  und Embeddings je in einer eigenen Zeile; „Alles gelesen“ und das langsame Abfragen (30 s) nur, wenn nichts läuft.
- *Zeiten*: „Lauf seit“ und „Projekt seit“ aus dem Zustand im Speicher. Ø je *gelesene* Datei zählt nur Dateien,
  die gelesen und geschrieben wurden; unveränderte (ein `stat`) stehen als Zahl daneben – ein Durchschnitt über
  beide sagte bei der Prüfung fertiger Projekte nichts. Restzeit des Projekts aus seiner eigenen bisherigen Rate
  (alle durchgesehenen Dateien, auch nicht lesbare Typen), erst nach 20 Dateien.
- *Warteschlange über Projekte, nicht Dateien*: Wie viele Dateien die noch nicht gelesenen Projekte haben, weiß
  niemand, bevor sie aufgelistet sind (das kostet über das Netz selbst Stunden). Darum Projekte übrig × gleitender
  Durchschnitt je Projekt der Warteschlange, „grob“, erst nach 5 Projekten und 20 gelesenen Dateien. Projekte sind
  verschieden groß; über viele gemittelt reicht das für „≈ 3 Tage“ oder „≈ 2 Wochen“.
- *Gleitende Durchschnitte in `meta`* (`zeiten.py`, je Wert eine Zeile JSON, Gewicht der älteren 0,98 je neuem –
  etwa die letzten 50 zählen): überstehen Neustarts und folgen einem schneller oder langsamer gewordenen Dienst. Je
  Projekt geschrieben, wenn es fertig ist; je Datei gesammelt alle 50 Dateien und am Projektende; je Seite in der
  Transaktion, die die Seite speichert; je Abschnitt nach jeder Embedding-Runde. Nie eine eigene Schreibtransaktion
  je Datei.
- *Bildauswertung*: Seiten je Stunde = Gleichzeitigkeit × 3600 ÷ Ø Sekunden je Seite, Restzeit = ausstehende Seiten
  (ohne ausgeschaltete Ordner) × Ø ÷ Gleichzeitigkeit, nach 10 Seiten. *Embeddings*: Ø je Abschnitt ist die Zeit
  einer Runde (mit ihren gleichzeitigen Stapeln) je Abschnitt, Restzeit = Abschnitte ohne Vektor × Ø, nach 100.
- *Deutsch gerundet*: „2 Std. 14 Min.“ für Verstrichenes, „≈ 2 Std. 15 Min.“, „≈ 3 Tage“, „≈ 4 Wochen“ für
  Schätzungen (auf 5 Minuten, Stunden, halbe Tage gerundet) – eine Schätzung soll nicht genauer aussehen, als sie ist.

## Dokumentarten und Tippfehler in der Suche

Wunsch des Büros: „Brandschutzkonzept“ soll die Brandschutzkonzepte ganz oben finden, auch wenn die Datei anders
heißt; andere Dokumente ebenso; vertippte Suchwörter sollen trotzdem finden; die Arten dürfen nicht starr sein.

- *Art aus dem Inhalt, der Dateiname nur als Hinweis* (`dokumentarten.detect`): Im Büro heißen Dateien `Scan_0815.pdf`,
  `Dok_17_final.pdf` oder sogar falsch (`Brandschutzkonzept_alt.pdf` mit einer Stellungnahme darin). Was ein Dokument
  ist, steht auf der ersten Seite – so liest es auch ein Mensch. Gelesen werden die ersten 25 Zeilen (höchstens
  2.000 Zeichen); eine Zeile *beginnt* mit der Art (nach Adjektiven wie „Brandschutztechnische“ und Zusätzen wie
  „Entwurf des …“). Ein Wort mitten im Satz („gemäß Brandschutzkonzept“) zählt nicht, sonst wäre jede Notiz ein
  Konzept. Briefkopfzeilen (GmbH, Tel., www, @) werden übersprungen, ein „Betreff:“ eines Briefes macht ein
  „Schreiben“ (ein Anschreiben zum Konzept ist kein Konzept). Der Dateiname entscheidet nur, wenn der Inhalt nichts
  sagt – mit geringer Sicherheit (0,45), die nie als „klar“ gilt; stimmen Name und Inhalt überein, steigt sie leicht.
- *Nicht starr*: Die Startliste (`SEED`) bildet Namen, Abkürzungen und Zusammensetzungen auf eine Art ab
  (BS-Konzept, Abschlagsrechnung → Rechnung, Fluchtwegplan → Flucht- und Rettungsplan). Ein Titelwort, das auf einen
  bekannten Kopf endet (-berechnung, -nachweis, -protokoll, -plan, -gutachten …), aber in keiner Liste steht, wird
  als eigene Art gespeichert („Löschwasserberechnung“, Einzahl, Schlüssel gefaltet). Gespeichert wird freier Text,
  keine Aufzählung; die Suche kennt alle im Index vorkommenden Arten (gruppiert aus `documents`, 30 s gemerkt). Eigene
  Namen über die vorhandene `SYNONYMS_FILE` („A = B“-Gruppen), keine zweite Konfiguration.
- *Sprachmodell nur bei Unklarem, im Hintergrund*: Die Regeln kosten Mikrosekunden und laufen beim Schreiben der
  Abschnitte (`documents.write_chunks` – neue, neu gelesene Dateien, Anhänge, nach der Bildauswertung). Nur wenn
  danach keine klare Art feststeht und genug Text da ist, fragt der Thread `DokumentartWorker` das Modell: die ersten
  1.500 Zeichen und den Dateinamen, Antwort als JSON mit `"sicher"`; „unklar“, `null` oder `"sicher": false` lassen
  die schwache Vermutung stehen (lieber nichts als falsch). Ein Modell für jedes Dokument wäre bei ~200.000 Dateien
  tagelange Last auf dem KI-Server, der schon Bildauswertung (3 Plätze) und Fragen bedient; die Regeln treffen die
  üblichen Titelseiten. Darum: ein Aufruf nach dem anderen, `max_tokens` 80, und nie, solange eine Frage läuft
  (`QUESTIONS`-Zähler in `ask.ask`) – eine Frage wartet höchstens auf einen kurzen Klassifizierungsaufruf. Mit der
  Fake-KI wird nie gefragt (`DOKUMENTART_KI=auto`): sie müsste sich eine Art ausdenken.
- *Neu bestimmen nur bei geändertem Text*: Gespeichert wird ein Hash des Textanfangs samt Dateiname. Gleicher
  Anfang → die Art bleibt (auch eine vom Modell). Ersetzt die Bildauswertung den Tesseract-Text, ändert sich der
  Anfang, die Art wird neu bestimmt und das Modell ggf. erneut gefragt.
- *Nachtragen ohne Neu-Einlesen*: Migration 14 fügt nur Spalten und Indizes hinzu; `doc_type_hash IS NULL` heißt
  „noch nicht bestimmt“. Der Thread nimmt je Runde 200 solche Dokumente in einer kurzen Schreibtransaktion (die ersten
  8 Abschnitte genügen); ohne Anwendung macht es `python -m projektakte.index` am Ende des Laufs. Fortschritt auf der
  Statusseite (Teilindex, das Zählen kostet nichts).
- *Kein zusätzliches FTS-Feld*: Erwogen war, die Art bzw. den Titel als weitere, höher gewichtete FTS-Spalte zu
  führen. Die Titelspalte enthält Dateiname und Überschriften schon mit Gewicht 2. Die Art als Spalte von `documents`
  (indiziert mit dem Datum) braucht beim Nachtragen und bei jeder neuen Modellantwort nur ein UPDATE je Dokument statt
  das Neuschreiben aller FTS-Zeilen seiner Abschnitte (bei Millionen Abschnitten Stunden), und die Rangfolge „Art zuerst,
  neueste zuerst“ ist eine Sortierung, keine BM25-Gewichtung, die man gegen Erwähnungen austarieren müsste.
- *Rangfolge in der Suche* (`search.type_ranking`, `_types_first`): Nennt die Anfrage eine Art (Einzahl/Mehrzahl über den
  Wortstamm, Zusammensetzung „Kitabrandschutzkonzept“, Alias, Tippfehler bis Abstand 2, Familie „Protokolle“ →
  alle *-protokoll-Arten), kommen zuerst die Dokumente dieser Art (je ein Treffer, neueste zuerst, schwache Vermutungen
  hinter sicheren), dann verwandte Arten (Brandschutznachweis ↔ Brandschutzkonzept – andere Büros nennen dasselbe so),
  dann die übrigen Treffer in ihrer Reihenfolge. Mit weiteren Wörtern („Brandschutzkonzept Kita“) stehen die Dokumente
  der Art vorn, in denen diese Wörter vorkommen (Volltext auf diese Dokumente beschränkt); die anderen derselben Art
  kommen ans Ende statt wegzufallen. Nichts wird gefiltert. Treffer der Art gelten auf der Suchseite immer als
  „passend“, auch wenn der Reranker die Titelseite niedrig bewertet. „Mail“ und kleingeschriebenes „schreiben“/„bild“
  sind in Anfragen keine Art (Mails findet die Suche als Mails, „schreiben“ ist meist ein Verb). Nur die Familien der
  Startliste (Plan, Protokoll, Bericht, Bescheid … und „Konzept“) zählen – „Nachweis der Feuerwiderstandsdauer“ ist
  keine Suche nach Brandschutznachweisen.
- *Bei Fragen nur Kandidaten*: Für `ask` würde „Art zuerst“ die Titelseiten vor die Fundstelle schieben. Dort gehen nur die
  besten Abschnitte der Dokumente dieser Art zusätzlich (höchstens 6) an den Reranker; er entscheidet. Eigener
  Evaluationssatz und zurückgehaltener Prüfsatz blieben unverändert (103/103; 31/40 bestanden, 19/28 beantwortbar, 12/12
  Absagen, 0 erfundene).
- *Tippfehler gegen den echten Wortschatz* (`tippfehler.py`): Korrigiert wird nur ein Wort, das im Index nichts (oder
  höchstens 2 Abschnitte, dann nur zu einem mindestens 10-mal häufigeren Wort) findet – richtig geschriebene seltene
  Wörter bleiben unangetastet, Wörter mit Ziffern und unter 6 Buchstaben ebenso. Der Kandidat muss im Index stehen
  (mindestens 2 Abschnitte), Abstand 1 ab 6 und 2 ab 9 Buchstaben (Damerau/OSA: vertauschte Nachbarn = 1), bei
  Gleichstand das häufigere Wort. Ein unbekanntes Wort wird im Volltext *zusätzlich* gesucht, für Reranker und Vektoren
  ersetzt; ein seltenes nur zusätzlich. Die Suchseite nennt es („meinten Sie …? Danach wurde gesucht.“) mit Link.
- *Schnell bei Millionen Wörtern*: `fts5vocab` liest den ganzen Index – das geht nicht bei jeder Suche. Der Wortschatz
  wird einmal gelesen (temporäre fts5vocab-Tabelle, `ORDER BY doc DESC LIMIT 400000` – SQLite sortiert dabei nur die
  Spitze), nach Länge gruppiert, je Wort ein Buchstaben-Histogramm (uint8, NumPy, für 1 Mio. Wörter ~3 s). Eine Suche
  vergleicht nur Wörter passender Länge, deren Histogramm höchstens 2 × Abstand abweicht (eine Änderung ändert höchstens
  zwei Zähler), und misst die wenigen übrigen genau. Gemessen: 1,5 Mio. verschiedene Wörter im Index, Wortschatz
  lesen 3,1 s, Korrektur von zwei Wörtern 13 ms; 1 Mio. Wörter im Speicher: 15–17 ms je Wort, rund 85 MB (bei der
  Obergrenze 400.000 etwa ein Drittel). Kleine Indizes (unter 50.000 Abschnitten) werden beim ersten Suchen sofort
  gelesen, große im Hintergrund (bis dahin keine Korrektur); neu gelesen höchstens alle 10 Minuten, wenn neue
  Abschnitte dazukamen. Ein Trigramm- oder SymSpell-Index wäre schneller, kostet bei Millionen Wörtern aber ein
  Vielfaches an Speicher; die Messung zeigt, dass es nicht nötig ist.
- *Synonyme*: Die eingebaute Liste kennt jetzt Fluchtplan = Fluchtwegplan = Flucht- und Rettungsplan …, Prüfbericht =
  Prüfungsbericht, Übereinstimmungserklärung = Übereinstimmungsbestätigung, Feuerwehrplan = Feuerwehreinsatzplan,
  Aktenvermerk = Aktennotiz. Nicht aufgenommen: Brandschutzkonzept = Brandschutznachweis als Synonym – die Wörter
  stehen in Texten für verschiedene Dokumente; die Suche behandelt sie als *verwandte Arten* (gleich danach) und die
  Vektoren finden „BS-Nachweis“ zu „Brandschutzkonzept“ über die Bedeutung.

## Nur bestimmte oberste Ordner (`PROJECTS_TOP_FOLDERS`)

Die echte Freigabe hat neben den Nummernordnern 1 bis 9 weitere Ordner in der obersten Ebene (Vorlagen, Archiv
u. Ä.), in denen ebenfalls Ordner mit Projektnummern liegen. Sie sollen nicht gelesen werden.
- *Eine Einstellung statt „aus“ auf der Statusseite*: „aus“ ist für Ordner gedacht, die zur Ablage gehören und nur
  zeitweise nicht gelesen werden sollen (das Gelesene bleibt durchsuchbar). Ordner, die nie dazugehören, sollen gar
  nicht erst als Projekte auftauchen – auch nicht in der Projektliste.
- *Exakte Namen, keine Muster*: `1,2,…,9` ist eindeutig; ein Muster wäre bei Ordnern wie „1_alt“ überraschend.
- *Wirkung wie ein verschwundener Ordner*: Bekannte Projekte von dort werden inaktiv (der bestehende Weg der
  Registersynchronisation, Mails und Entscheidungen bleiben), ihre Dateien verlassen den Index.
- *Schutz*: Liegt keiner der genannten Ordner in `PROJECTS_DIR` (Tippfehler, Laufwerk nicht eingebunden), ändert der
  Lauf nichts und warnt – wie bei einem leeren Projektordner.
- Die Strukturerkennung (`PROJECT_NUMBER_PATTERN=auto`, `python -m projektakte.structure`) sieht weiter die ganze
  Freigabe; sie schlägt nur ein Schema vor.

## Projektübersicht: Ordneransichten und Unterprojekte als Teil des Projekts

Bei rund 3.000 Projekten war die Liste lang und langsam, und die Reihenfolge der Indexierung zählte Unterprojekte
(sechsstellig) als eigene Projekte neben dem vierstelligen Bauvorhaben.
- *Ansicht je Person, in `meta`* (`ansicht:<Nutzer>`), keine neue Tabelle: Liste (Standard, wie bisher), Kacheln,
  Spalten. Der Standard bleibt die Liste, damit nichts ungefragt anders aussieht.
- *Server rendert, Links statt JavaScript*: die Ordneransichten brauchen nur eine gruppierte Zählabfrage und beim
  Öffnen eines Ordners die Projekte dieses Präfixes (Seitengröße 100).
- *Ein Projekt = Zeile ohne Bauvorhaben-Elternteil*: Unterprojekte zählen nie mit.
- *Nur die Anzeige der Reihenfolge gruppiert* (`reihenfolge.group_entries`), nicht der Lauf: Indexer, `Plan`,
  `done_at` je Nummer bleiben, damit bereits Indexiertes gültig ist und nichts neu gelesen wird. Ein Bauvorhaben
  gilt als fertig, wenn alle Mitglieder fertig sind; „Vorziehen“ und „zurück“ wirken auf die ganze Gruppe.

## Indexierung pausieren

Das Einlesen eines großen Bestands dauert Stunden bis Wochen und belastet Dateiserver und KI-Dienste; man will es
zeitweise anhalten können (Backup, Messung, andere Last).
- *Ein gemeinsamer Schalter (`pause.PAUSE`)*, den jeder Hintergrund-Thread zwischen zwei Schritten prüft (Datei im
  Dokument-Lauf, Seite der Bildauswertung, Stapel der Embeddings, Runde der Dokumentarten, Beginn eines Laufs).
  Kein Abbrechen mitten im Schritt: Ein halb gelesenes Dokument oder ein verworfener Stapel wäre Arbeit für nichts.
- *Nur im Arbeitsspeicher*: Eine vergessene Pause soll nach einem Neustart nicht den Index still veralten lassen.
  Für „dauerhaft nicht lesen“ gibt es die Ordner-Schalter.
- *Beenden löst die Pause*, damit wartende Threads den Stopp sehen und der Container sauber herunterfährt.
- Suche, Fragen und Mail-Zuordnung von Hand bleiben unberührt.

## Wiederholungsschleifen der Bildauswertung

Ein kleines Bildmodell (4B) schrieb beim Abschreiben von Formularen und Tabellen manchmal dieselbe Zeile bis zur
Token-Grenze. Die Antwort wurde trotzdem übernommen und ersetzte den Tesseract-Text, und jede Schleife hielt einen der
gleichzeitigen Plätze des KI-Servers bis zu 4.000 Tokens lang fest.
- *Erkennen statt nur bestrafen*: Eine `presence_penalty` für alle Seiten verringert Schleifen, bestraft aber auch
  richtige Wiederholungen (Tabellenköpfe, gleiche Maße, Normen, „[handschriftlich: …]“) und kann beim wortgetreuen
  Abschreiben Werte verfälschen. Gemessen mit Qwen3.5-4B an echten Projekt-PDFs (38–76 Seiten je Stufe): mit 1,5
  fehlen rund 14 Prozentpunkte der Wörter, mit 0,6 noch 6, mit 0,3 noch 3 – und Schleifen verhindert sie trotzdem
  nicht zuverlässig. Darum bleibt der erste Versuch, wie er ist; nur eine Seite, die in eine Schleife lief, wird mit
  `VISION_RETRY_BODY` neu gefragt – anders gewürfelt (`temperature 0.7`, `top_p 0.8`, `top_k 20`), ohne Penalty. Der
  Retry steht in der Anfrage nach `LLM_EXTRA_BODY`; er setzt keine Penalty, damit eine dort gesetzte (z. B. 0 gegen
  eine Voreinstellung des Servers) bleibt.
- *Erkennung auf dem Server zuerst*: vLLM `repetition_detection` (`min_pattern_size 8`, `max_pattern_size 300`,
  `min_count 4`) brachte in derselben Messung 0 Schleifen und 32,7 statt 18,3 Seiten/min, ohne echten Text zu kürzen.
  Sie wird über `LLM_EXTRA_BODY` eingeschaltet (kein Code); die Prüfung hier bleibt das Sicherheitsnetz für Server
  ohne sie und repariert die vorher gespeicherten Schleifen. Sie schlägt erst bei sechs Wiederholungen an, nach vLLMs
  vier also praktisch nie zusätzlich.
- *Regel* (`ai/repetition.py`): eine Folge von 1–80 Wörtern mindestens sechsmal unmittelbar hintereinander, zusammen
  mindestens 400 Zeichen; Wörter werden genau verglichen (nur ohne Groß-/Kleinschreibung). Zeilen, die sich in einer
  Zahl unterscheiden, sind keine Schleife – hochzählende Schleifen enden an der Token-Grenze und werden als
  „abgeschnitten“ erkannt. Eine echte Seite mit sechs gleichen Zeilen über 400 Zeichen ist kaum denkbar; träfe es
  sie, behielte sie nur den Tesseract-Text (kein falscher Text, nur keine Handschrift).
- *Streaming*: Die Antwort wird mitgelesen (alle 300 Zeichen die letzten 600 Wörter, wenige Millisekunden); bei einer
  Schleife schließt der Client die Verbindung, vLLM und llama.cpp brechen die Anfrage dann ab. Ein Server, der
  `stream` nicht kennt, antwortet in einem Stück und wird danach geprüft. Nur die Bildauswertung streamt; Fragen
  bleiben unverändert.
- *Lieber ablehnen als falsch*: Schleift auch der zweite Versuch, gilt der Text der ersten Stufe (Textebene oder
  Tesseract); nur ohne ihn wird der Anfang vor der Schleife übernommen. Die Seite wird nicht von selbst neu gefragt
  (das Modell schliffe wieder), das Dokument gilt als unvollständig mit Hinweis.
- *Tokens je Seite*: erster Versuch etwa Prompt-Zeichen/2 + 500 Tokens (mindestens 1.500, höchstens 4.000) – deutscher
  Text hat 3–4 Zeichen je Token, die Antwort ist etwa so lang wie der bekannte Text plus Handschrift. Eine nur
  abgeschnittene Antwort wird einmal mit 4.000 Tokens und den üblichen Einstellungen neu gefragt.
- *Gespeicherte Schleifen*: Nach der Bildauswertung werden `page_texts` gelöscht; geprüft werden darum die Abschnitte
  der Dokumente mit `method = 'vision'` (Migration 16 markiert sie, `loop_check`). Die Prüfung braucht kein Modell und
  läuft in Paketen von 200 Dokumenten zu Beginn jeder Runde und nebenbei, während das Modell rechnet. Ein Dokument mit
  Schleife wird wie bei Migration 12/15 über ein unbekanntes Profil ganz neu gelesen (Tesseract und Bildauswertung
  nur für dieses Dokument).

