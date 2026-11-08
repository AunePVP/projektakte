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
