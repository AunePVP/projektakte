# Projektakte

Internes KI-Werkzeug für ein Brandschutz-Ingenieurbüro: führt **E-Mails und Projektdateien je
Projektnummer** zusammen und beantwortet **Fragen zu Projekten mit Quellenangaben**. Läuft komplett
im lokalen Netz mit lokalen Modellen.

- Was gebaut werden soll und warum: [VISION.md](VISION.md)
- Reihenfolge: [ROADMAP.md](ROADMAP.md) · Entscheidungen: [DECISIONS.md](DECISIONS.md) ·
  Änderungen: [CHANGELOG.md](CHANGELOG.md)
- KI-Dienste (LLM, Embeddings, Reranker): [docs/ki-dienste.md](docs/ki-dienste.md)
- Anbindung des Mailprogramms plMail: [docs/plmail-jmap.md](docs/plmail-jmap.md)

## Stand

**Phase A ist umgesetzt** (alle 14 Roadmap-Schritte): Projektregister, Text- und Mail-Indexierung
(TXT/MD/PDF/DOCX/EML, Anhänge), Embeddings, Mail-Zuordnung mit Zuordnungs-Eingang, hybride Suche mit
Reranker, Fragen mit Quellen, Hintergrund-Indexierung, Weboberfläche, Zugangsschutz und plMail über
JMAP. Dazu kommt die Zuordnung der Mails in plMail (docs/plmail-integration.md). Eine unabhängige
Prüfung mit 12 Prüfern fand 67 Mängel; sie sind behoben und gegengeprüft (CHANGELOG 0.4.0). Danach
folgten eine Schlussprüfung mit Live-Vorführung und vier Runden Gegenprüfung (CHANGELOG 0.5.0), unter
anderem mit einem eigenen, begrenzten Leseprozess für fremde Dateien.
Entwickelt und getestet gegen Fake-KI-Dienste; die echten Dienste werden nur per Konfiguration
angebunden (siehe unten). Offen vor dem produktiven Einsatz: Formate der echten Dienste prüfen und
eintragen und die Schwellwerte mit `python -m projektakte.evaluate --real` einstellen
([docs/ki-dienste.md](docs/ki-dienste.md#stand-der-formatprüfung)).

## Schnellstart (Demo in einem Container)

```bash
docker build -t projektakte .
docker run -p 127.0.0.1:8000:8000 -e APP_ENV=dev -e DEMO_MODE=1 -e AI_MODE=fake -e AUTH_DISABLED=1 projektakte
```

Danach `http://localhost:8000/` öffnen. `DEMO_MODE=1` erzeugt den frei erfundenen Demo-Datensatz,
`AI_MODE=fake` startet die Fake-KI-Dienste im selben Prozess. Das Image startet sonst in
`APP_ENV=production` mit strengen Startprüfungen; `AUTH_DISABLED=1` ist nur mit `APP_ENV=dev`
erlaubt (sonst bricht der Start ab) und die Oberfläche zeigt dann einen roten Balken.
`scripts/docker-smoke.sh` baut das Image und prüft Start, `/health`, `/health/ready`, den
HEALTHCHECK, `/health/deps`, Zugangsschutz, Sicherheits-Header, eine Frage mit Quellenangabe, das
Log und die Sicherung.

## Starten

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
PORT=8000 .venv/bin/python -m projektakte
```

Für den Betrieb das Beispiel unter [deploy/](deploy/README.md) verwenden: Projektakte hinter einem
TLS-Reverse-Proxy (Caddy), Projektordner nur lesend eingebunden, Daten in einem benannten Volume.
Wichtig für `docker run` von Hand:

- `SECRET_KEY` zufällig mit mindestens 32 Zeichen
  (`python3 -c "import secrets; print(secrets.token_urlsafe(48))"`), in `production` Pflicht.
- Daten in einem benannten Volume (`-v projektakte-daten:/data`) oder bei einem Ordner auf dem Host
  vorher `sudo chown -R 10001:10001 ./data` – der Container läuft als Benutzer 10001. Ist `DATA_DIR`
  nicht beschreibbar, bricht der Start mit einer deutschen Meldung ab.
- In `production` gehen Sitzungs-Cookies nur über HTTPS (`COOKIE_SECURE`), also einen TLS-Proxy
  vorschalten; nur `localhost` geht auch ohne. `COOKIE_SECURE=0` nur zum Testen.
- Benutzerdatei einbinden und für Benutzer 10001 lesbar machen. In `production` bricht der Start
  ohne Benutzer bzw. mit nicht lesbarer `AUTH_USERS_FILE` mit einer deutschen Meldung ab.

```bash
sudo chown 10001:10001 benutzer.txt && sudo chmod 600 benutzer.txt   # nur für die App lesbar
docker run -p 127.0.0.1:8000:8000 -v projektakte-daten:/data \
  -v /srv/projekte:/projekte:ro -e PROJECTS_DIR=/projekte \
  -v "$PWD/benutzer.txt:/run/secrets/users:ro" -e AUTH_USERS_FILE=/run/secrets/users \
  -e SECRET_KEY=… projektakte
```

## Tests

```bash
sh scripts/test.sh
```

Das Skript legt eine virtuelle Umgebung (zwischengespeichert nach Inhalt der requirements-Dateien)
an und führt `pytest` parallel aus (pytest-xdist, `PYTEST_WORKERS=0` für einen Prozess; etwa 25 s
auf 4 Kernen). Tests brauchen kein Netz und keine KI-Dienste: Sie starten die Fakes auf
`127.0.0.1`. Dabei laufen auch der Evaluationssatz (`tests/eval.json`, erwartete Quelle unter den
ersten 5 Treffern, Antwort enthält den erwarteten Fakt) und die Zuordnungs-Qualitätskriterien aus
VISION.md Abschnitt 9. Ein ausdrücklich markierter Integrationstest gegen die echten Dienste läuft
nur mit `PROJEKTAKTE_INTEGRATION=1` (siehe docs/ki-dienste.md).

## Oberfläche

- **Projekte** – Liste mit Filter (Nummer/Name) und Suche über alle Projekte.
- **Projektseite** – Fragefeld (Antwort mit nummerierten, verlinkten Quellen), Zeitachse aus
  Dokumenten und Mails, Dokumente (mit Hinweis auf übersprungene Scans), Anhänge aus Mails, Mails
  mit Zuordnungsart.
- **Antworten** – bei einer belegten Antwort die zitierten Quellen (Nummern wie im Text), weitere
  Fundstellen eingeklappt. Ohne Antwort („nicht gefunden“, unbelegt, abgebrochen, KI nicht
  erreichbar) immer die „Möglichen Fundstellen“ und ein Knopf „In der Suche öffnen“.
- **Zuordnungs-Eingang** – Mails ohne sichere Projektnummer mit Vorschlag und Begründung;
  Bestätigen, Verwerfen, anderes Projekt wählen oder „Kein Projektbezug“. 50 Mails je Seite,
  Filter nach Vorschlag, Zähler aktualisieren sich sofort.
- **Fragen** und **Suche** – projektübergreifend oder auf ein Projekt beschränkt. Die Suche blendet
  schwach passende Treffer aus (`SEARCH_MIN_RERANK`), lässt sie aber auf Wunsch anzeigen.
- **Status** – Indexierung (mit Warnungen, z. B. fehlendes Laufwerk), Bestand und Zustand der
  KI-Dienste (im Fake-Modus „simuliert“) und von plMail.

Mail- und Dokumentseiten zeigen den Text bzw. die Abschnitte (mit Seitenangabe) und öffnen die
Originaldatei. Serverseitig gerendert (Jinja2), HTMX liegt lokal unter `/static` (kein CDN); alle
Formulare funktionieren auch ohne JavaScript. Keine Inline-Skripte oder -Styles (strenge CSP).
Ohne horizontales Scrollen auf Laptop und Handy. Ausschnitte in Antworten und Suche zeigen die
passende Stelle als Klartext (ohne Markdown-Zeichen), die Dokumentseite eine bereinigte Ansicht mit
Überschriften und Tabellen. Originaldateien öffnen im Betrachter des Browsers
(`/dokumente/{id}/datei/{Dateiname}`, eigene skriptfreie CSP). Die Suche zeigt Treffer, die eine
Projektnummer oder ein Aktenzeichen aus der Anfrage wörtlich enthalten, immer an. Der Eingang blättert
per Schlüssel – nach Entscheidungen werden keine Mails übersprungen. Unbekannte Adressen zeigen eine
deutsche Fehlerseite.

## Anmeldung

Ohne Anmeldung gibt es keine Daten – öffentlich sind nur `/health`, `/health/ready` (ohne Inhalte),
`/login` und `/static`.
Benutzer stehen in der Konfiguration, Passwörter nur als PBKDF2-Hash:

```bash
python -m projektakte.passwd anna        # fragt das Passwort ab, gibt "anna:pbkdf2_sha256$…" aus
```

Die Ausgabe kommt in `AUTH_USERS` (mehrere Einträge durch Komma getrennt) oder – praktischer für
Docker, weil `$` in Compose-Dateien maskiert werden muss – zeilenweise in eine Datei, deren Pfad
`AUTH_USERS_FILE` nennt. `SECRET_KEY` signiert das Sitzungs-Cookie (ohne ihn endet jede Sitzung
mit dem Neustart; in `production` ist er Pflicht). Skripte können die API mit HTTP Basic aufrufen,
z. B. `curl -u anna https://projektakte.lan/health/deps`.

- **Abmelden** beendet alle Sitzungen des Benutzers serverseitig (auf allen Geräten, auch kopierte
  Cookies). Eine Anmeldung gilt höchstens `SESSION_HOURS` (Standard 12, maximal 720). Deshalb ein
  Konto je Person, auch für Vorführungen – sonst fliegen beim Abmelden alle anderen mit heraus.
- **Anmeldebremse statt Sperre:** Fehlversuche für einen Benutzernamen verlängern die Wartezeit für
  diesen Namen exponentiell (je Rechner und Benutzer sowie je Benutzer, höchstens 5 Minuten).
  Fehlversuche mit anderen Namen von derselben Adresse (Terminalserver, NAT, Proxy) verzögern
  Anmeldungen nur, um höchstens 2 s je Versuch – wer das richtige Passwort hat und nicht selbst Ziel
  der Fehlversuche ist, kommt immer hinein. Ein Browser, mit dem man sich schon einmal angemeldet hat,
  wird durch Fehlversuche anderer nicht ausgesperrt. Skripte und Überwachung mit HTTP Basic (am besten
  mit eigenem Benutzer) sperren nur Fehlversuche für genau diesen Benutzer von genau dieser Adresse.
  Passwortprüfungen laufen nur wenige gleichzeitig; bei Überlast kommt 503 mit `Retry-After`. Ein
  vorhandener Benutzername ohne eigenen Fehlversuch von dieser Adresse wird auch während einer Flut
  sofort geprüft (Formular und Basic); 5 Fehlversuche für einen Namen von derselben Adresse sperren
  ihn dort – das Monitoring deshalb mit eigenem Konto betreiben.
- **Hinter einem Reverse-Proxy** `FORWARDED_ALLOW_IPS` auf dessen Adresse setzen, damit Anmeldebremse
  und Log die echte Client-Adresse sehen (siehe deploy/README.md).
- **Schutz vor fremden Formularen:** Zustandsändernde Anfragen anderer Seiten (`Sec-Fetch-Site`
  bzw. `Origin`) werden mit 403 abgelehnt; dazu kommen CSP und weitere Sicherheits-Header.
- `AUTH_DISABLED=1` schaltet den Schutz für die lokale Entwicklung aus – nur mit `APP_ENV=dev`,
  sonst startet die Anwendung nicht.

## Echte KI-Dienste anbinden

Keine Codeänderung nötig – nur Umgebungsvariablen setzen (`AI_MODE` weglassen oder `real`):

```bash
LLM_URL=http://192.168.178.62:8000/v1 LLM_MODEL=qwen3.8-flash-next \
EMBED_URL=http://192.168.178.62:8001 EMBED_MODEL=qwen3-embedding-4b \
RERANK_URL=http://192.168.178.62:4343/rerank python -m projektakte
```

Danach `GET /health/deps` aufrufen (oder die Statusseite öffnen): Dort steht je Dienst `ok`, das
**erkannte Format** (`openai-chat`; `openai`/`tei`; `tei`/`cohere`), die Antwortzeit und ggf. der
Fehler samt Rückfallverhalten. Die Formate vorher von Hand prüfen
(siehe [docs/ki-dienste.md](docs/ki-dienste.md)):

```bash
curl -s http://192.168.178.62:8000/v1/models
curl -s http://192.168.178.62:8000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.8-flash-next","messages":[{"role":"user","content":"Sag OK."}],"max_tokens":8}'
curl -s http://192.168.178.62:8001/v1/embeddings -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-embedding-4b","input":["Rauchableitung im Treppenraum"]}'
curl -s http://192.168.178.62:8001/embed -H 'Content-Type: application/json' \
  -d '{"inputs":["Rauchableitung im Treppenraum"]}'
curl -s http://192.168.178.62:4343/rerank -H 'Content-Type: application/json' \
  -d '{"query":"Rauchableitung","texts":["Text A","Text B"]}'
curl -s http://192.168.178.62:4343/rerank -H 'Content-Type: application/json' \
  -d '{"query":"Rauchableitung","documents":["Text A","Text B"],"top_n":2}'
```

Erkennt der Client das Format nicht oder rät falsch, lässt es sich mit `EMBED_FORMAT` bzw.
`RERANK_FORMAT` festlegen. Beim ersten Lauf mit den echten Embeddings werden alle Vektoren
automatisch neu berechnet (Fingerabdruck des Dienstes). Fällt der Reranker aus, sucht Projektakte
ohne ihn weiter; fällt der Embedding-Dienst aus, nur im Volltext; fällt das Sprachmodell aus,
antwortet die Fragefunktion mit „KI-Dienst nicht erreichbar“ und zeigt die Fundstellen. Bilder
(Scans, Pläne) werden erst mit `LLM_VISION=1` an das Modell geschickt (in der Demo mit
`AI_MODE=fake` ist das die Vorgabe: die Fake-Transkription des Fluchtwegplans ist festgelegt).

**Denkmodelle (Qwen3):** `LLM_EXTRA_BODY={"chat_template_kwargs":{"enable_thinking":false}}` setzen
oder `LLM_MAX_TOKENS` erhöhen. Sonst endet die Ausgabe manchmal nur mit Denkschritten; Projektakte
meldet das dann als „Antwort abgebrochen“ (Status `truncated`) mit den Fundstellen statt als
„nicht gefunden“. Fehlt dem Server `GET /v1/models`, ist das in `/health/deps` nur ein Hinweis.

### Antwortqualität messen

```bash
python -m projektakte.evaluate --real [--json ergebnis.json]
docker compose exec app python -m projektakte.evaluate --real      # im Container
```

Der Befehl prüft zuerst die Dienste, indexiert den Demo-Datensatz mit den konfigurierten Diensten in
einen eigenen Index (`$DATA_DIR/eval`, nie die Produktionsdaten; `--data-dir`, `--no-index`,
`--only ID`, `--category stand` zum Eingrenzen) und lässt den Evaluationssatz (103 Fragen) laufen;
`--set holdout` nimmt den zurückgehaltenen Prüfsatz eines unabhängigen Autors (40 Fragen), mit
`--summary-only` nur Summen ohne Fragetexte. Exit-Code 0 = Ziele erreicht (≥ 80 % beantwortbare
Fragen richtig, ≥ 90 % unbeantwortbare abgelehnt; `--min-correct`, `--min-refused`), 1 = verfehlt,
2 = Dienst nicht erreichbar oder keine Frage ausgewählt. Ausgabe je
Frage: erwartete Quelle in den Top 5, Fakt und Quelle in der Antwort, Status, Rerank-Scores,
Laufzeit; zusammengefasst die Score-Verteilung und eine Schwellwerttabelle. Damit
`ASK_MIN_RERANK`, `SEARCH_MIN_RERANK` und `ASSIGN_MIN_SIMILARITY` für die echten Modelle einstellen.
Mit den Fakes: eigener Satz 103/103; zurückgehaltener Prüfsatz 19 von 28 beantwortbaren Fragen
richtig (0,68 – das 80-%-Ziel ist für die Fake-KI nicht erreicht, der Test ist mit Begründung als
„xfail“ markiert, nicht abgesenkt), 12 von 12 unbeantwortbaren korrekt abgelehnt, keine erfundene
Antwort; die erwartete Quelle steht immer unter den ersten 5 Treffern.

## Fake-KI-Dienste

Für Entwicklung und Tests gibt es deterministische Fakes aller drei Dienste auf einem Port:

```bash
python -m projektakte.fake_ai --port 8999
LLM_URL=http://127.0.0.1:8999/v1 EMBED_URL=http://127.0.0.1:8999 \
  RERANK_URL=http://127.0.0.1:8999/rerank python -m projektakte
```

Kürzer: `AI_MODE=fake` startet dieselben Fakes im Prozess der Anwendung. Optionen des Fake-Servers:
`--embed-format openai|tei|both`, `--rerank-format tei|cohere|both`, `--no-think`, `--delay`,
`--chat-quirk length|reasoning_only|unterminated_think` (stellt abgeschnittene Antworten echter
Server nach) und `--no-models` (`GET /v1/models` antwortet 404).

Die Fakes: Embeddings = Hash der (gestemmten) Wörter auf 256 Dimensionen, normalisiert. Reranker =
gewichtete Übereinstimmung der Themenwörter der Frage (Synonyme und Wortformen, ohne Füllwörter
und Projektnummern), beide Antwortformate. Chat = ein extraktiver Leser: Er bewertet die Sätze der
nummerierten Quellen nach den Themenwörtern der Frage (Synonyme, Abkürzungen, Wortformen,
IDF-Gewichte; Projektnummer und -name zählen nicht), beantwortet Fragen nach dem „letzten Stand“
mit der neuesten Quelle und zitiert den passenden Satz einmal mit `[n]` („Laut Baugenehmigung
(S. 2), Auflage 7 …: … [1]“), sonst „Nicht gefunden.“; mit `usage` und `<think>`-Block. Er lehnt im
Zweifel ab: Fragen nach einem Wert (Kosten, Anzahl, Datum, Hersteller …) beantwortet er nur mit einem
Wert dieser Art beim gefragten Thema (Maße passend zur gefragten Dimension, „Wann wurde …?“ nur mit
dem Datum des Ereignisses, nie mit einer Pflicht), antwortet nur aus einem genannten Projekt und
zitiert nie Floskeln, Signaturen oder Rollenzeilen. Frei formulierte Fragen versteht er etwa in 7
von 10 Fällen (zurückgehaltener Prüfsatz 0,68) –
**für eine Vorführung mit eigenen Fragen aus dem Publikum die echten Dienste anbinden**. Die
Antwortqualität echter Modelle sagt der Fake nicht voraus – dafür gibt es `python -m projektakte.evaluate`.

## plMail anbinden (JMAP)

Zweite Mailquelle neben `MAIL_DIR`: In plMail unter **Einstellungen → App-Passwörter** ein
App-Passwort anlegen und setzen:

```bash
PLMAIL_URL=https://mail.example.local PLMAIL_TOKEN=plmail_… python -m projektakte
```

Projektakte liest die Session (`/.well-known/jmap`, sonst `/jmap/session`), durchläuft **alle**
Konten, holt beim ersten Mal alle Mails seitenweise (`Email/query` + `Email/get`, `PLMAIL_PAGE_SIZE`
je Seite) und danach nur noch Änderungen (`Email/changes` ab dem gespeicherten `state`). Anhänge
kommen über die `downloadUrl` der Session. Es werden ausschließlich lesende Methoden verwendet.
`GET /health/deps` meldet plMail mit Zahl der Konten. Details: [docs/plmail-jmap.md](docs/plmail-jmap.md).

**Zuordnung in plMail:** Sobald plMail angebunden ist, gilt `ASSIGN_MODE=manual` – Mails werden
nicht mehr automatisch Projekten zugeordnet. Man wählt das Projekt in plMail beim Lesen der Mail
(plMail holt Projektliste und Vorschlag über `/api/integration/*` mit `INTEGRATION_TOKEN`) und
plMail liefert die Entscheidung als JMAP-Eigenschaft `plmail:projektakte`, die Projektakte beim
Abgleich übernimmt. Schnittstellenvertrag: [docs/plmail-integration.md](docs/plmail-integration.md);
Auftrag für die plMail-Seite: [docs/plmail-prompt.md](docs/plmail-prompt.md).

## Wie es funktioniert

### Indexieren

Die Anwendung indexiert im Hintergrund: beim Start und danach alle `INDEX_INTERVAL` Sekunden
(Standard 300; `0` = nur beim Start und auf Anforderung). Der Webserver ist sofort erreichbar.
Mit `DEMO_MODE=1` wird der Demo-Datensatz vorher in `$DATA_DIR/demo` erzeugt, falls er fehlt.
Stand und Zahlen: `GET /api/status`; sofort neu indexieren: `POST /api/reindex` oder
`python -m projektakte.index` (ein Durchlauf im Vordergrund, ohne Webserver).

Projekte sind Ordner unter `PROJECTS_DIR`, deren Name mit einer Projektnummer beginnt
(`PROJECT_NUMBER_PATTERN`), direkt oder tiefer, z. B. in Jahres- oder Kundenordnern
(`PROJECT_FOLDER_DEPTH`). Das Nummernschema der eigenen Ablage nennt `python -m projektakte.structure`;
mit `PROJECT_NUMBER_PATTERN=auto` schlägt Projektakte es selbst vor, liest aber erst nach einer
Bestätigung auf der Statusseite (deploy/README.md, „Projektordner vom Dateiserver“).

**Mehrere Ordner mit derselben Nummer** sind ein Projekt: der Auftragsordner, seine Teilordner außerhalb
davon (`119300-Bilder` direkt im Bauvorhaben), Kopien (`111301_… -2013-11-04`) und verschachtelte Kopien
eines Bauvorhabens werden zusammen gelesen. Hauptordner ist ein Ordner der Form `<Nr>_<Name>` oder
`<Nr> <Name>`, sonst einer nur mit der Nummer, sonst ein Teilordner `<Nr>-<ein Wort>` – davon der am
wenigsten tief liegende. Der Projektname kommt aus dem Hauptordner. Ein Projekt nur aus Teilordnern heißt
nach seinem Bauvorhaben (dem Ordner darüber, dessen Nummer die Projektnummer fortsetzt: `1193_…` für
`119300`), wenn die Projekte der Ablage überwiegend `<Nr>_<Name>` heißen oder die Teilordner verschiedene
Wörter tragen; sonst bleibt das Wort der Name (Ablagen mit `1234-Kita`). Liegt ein Ordner mit der Nummer
in einem Bauvorhaben mit anderer Nummer, während es den Auftrag in seinem eigenen Bauvorhaben gibt (meist
ein Datum: `1827_…/Fotos/230512_Begehung` neben `2305_…/230512_Konzept`), wird er nicht mit dem Auftrag
gelesen; das Protokoll nennt ihn. Dateien weiterer Ordner erscheinen mit dem Ordnernamen davor, die
Projektseite zeigt alle Ordner. Wird ein weiterer Ordner nicht mehr gefunden, verlassen seine Dateien den
Index wie verschwundene Dateien; ein umbenannter Ordner wird verschoben, nicht neu gelesen.

Unterstützte Formate: TXT,
Markdown, CSV, PDF (pypdf, je Seite), DOCX (python-docx, mit Überschriften und Tabellen), EML.
Textdateien in UTF-8, UTF-16/32 (auch Windows-„Unicode“) oder cp1252. Bilder (PNG/JPG/TIFF) und
PDF-Seiten ohne Textebene werden nur mit `LLM_VISION=1` über das multimodale Modell gelesen, sonst
als „übersprungen“ vermerkt. Die Bilder gescannter PDF-Seiten liest Pillow aus (JPEG/PNG unverändert,
TIFF/CCITT, JPEG 2000 und CMYK umgewandelt, große Bilder verkleinert; Streifen- und Kachelscans
werden zu einem Seitenbild zusammengesetzt; JBIG2 über `jbig2dec`, das im Docker-Image enthalten
ist). Ein Bild (ein PDF-Objekt, auch unter mehreren Namen oder auf mehreren Seiten) wird je Dokument
nur einmal dekodiert und gesendet; große JPEG- und JPEG-2000-Bilder werden gleich verkleinert
dekodiert. Vor dem Dekodieren gelten Grenzen nach der echten Bildgröße aus dem Bildkopf (nicht nur der
Angabe im PDF): je Seite höchstens 200 verschiedene Bilder und 150 Megapixel, je Dokument höchstens 500
Bildauswertungen und 4000 Megapixel; der Seiteninhalt wird begrenzt durchlaufen (2000 Positionen,
1000 Formulare, 1 Mio. Operationen, 4 MB je Seite, 32 MB je Dokument). Was darüber liegt, wird mit
Hinweis ausgelassen und nicht wiederholt. Reine Vektorseiten (CAD-Pläne mit Text als Kurven) gelten als
Seiten ohne Text.

**Eigener Leseprozess:** PDF-, Word- und Mail-Dateien (auch Mail-Anhänge) werden nicht im
Anwendungsprozess gelesen, sondern in einem dauerhaften Leseprozess mit Grenzen je Datei:
Speicher `EXTRACT_MEMORY_MB` (2048), Rechenzeit `EXTRACT_CPU_SECONDS` (120 s; das Warten auf das
Sprachmodell zählt nicht) und `EXTRACT_WALL_SECONDS` (3600 s ohne Fortschritt). Eine beschädigte
oder absichtlich aufgeblähte Datei kann so weder den Speicher der Anwendung füllen noch die
Indexierung lange aufhalten: Sie wird als „Datei zu aufwendig zum Lesen“ bzw. „Datei zu groß zum
Lesen“ vermerkt, der Leseprozess startet neu. Im Docker-Betrieb das Speicherlimit des Containers
mindestens auf Anwendung + `EXTRACT_MEMORY_MB` setzen. Textdateien und Bilder liest die Anwendung
selbst; mit `APP_ENV=test` (Testsuite) ist der Leseprozess aus, `EXTRACT_ISOLATION=0/1` schaltet ihn
ausdrücklich.
Ist ein Teilbild nicht lesbar, gilt die Seite als „nur teilweise gelesen“ und wird später
erneut versucht. Jedes Dokument merkt sich sein Extraktionsprofil: Wird `LLM_VISION`
eingeschaltet oder das Modell gewechselt, liest der nächste Lauf alle bisher übersprungenen bzw.
unvollständigen Dokumente nach (auch Mail-Anhänge); Fehler werden mit wachsendem Abstand
wiederholt (15 min … höchstens 7 Tage). Mails werden aus `MAIL_DIR` (alle
`*.eml`, auch in Unterordnern) und optional aus plMail gelesen; Anhänge landen als Kopie in
`$DATA_DIR/cache` und werden wie Dokumente indexiert. Texte werden in überlappende Abschnitte mit
Seitenangabe zerlegt, in SQLite + FTS5 gespeichert und eingebettet. Unveränderte Dateien
(Größe/Änderungszeit, dann SHA-256) werden nicht erneut gelesen. Quellordner werden nie verändert;
Verknüpfungen (Symlinks), die aus `PROJECTS_DIR` herausführen, werden nie gelesen.

- **Kurze Schreibsperren:** Dateien und Anhänge werden außerhalb der Datenbank-Schreibsperre gelesen,
  extrahiert und heruntergeladen; gespeichert wird je Dokument bzw. Mail, die Zuordnung in Häppchen.
  Entscheidungen im Eingang gehen auch während einer großen Indexierung sofort.
- **Umbenannte oder verschobene Projektordner** (gleiche Nummer) behalten ihre Dokumente und Vektoren;
  ein neuer Projektname wird im Volltext nachgezogen. **Verschwundene Projekte** werden inaktiv, ihre
  Dateien verlassen sofort Suche und Antworten (Mails und Entscheidungen bleiben). Gelöscht werden die
  Dateien erst, wenn der Ordner mindestens 24 h und in 3 aufeinanderfolgenden Läufen fehlt; kommt er
  vorher zurück, ist alles ohne Neuverarbeitung wieder da.
- **Laufwerk fehlt:** Ist `PROJECTS_DIR` oder `MAIL_DIR` nicht da, nicht lesbar oder plötzlich leer,
  obwohl schon Inhalte bekannt sind, wird nichts deaktiviert oder gelöscht; die Statusseite zeigt
  eine Warnung.
- **Verschobene .eml-Dateien** behalten Mail, Vektoren und Entscheidung.
- **plMail:** Eine fehlerhafte Mail wird gezählt und später erneut versucht, der Lauf geht weiter.
  Ein Anhang, den plMail gerade nicht liefert, hält die Mail nicht auf: Er erscheint als „Anhang
  ausstehend“ und wird nachgeladen. Der Erstabgleich blättert mit Überlappung und gleicht danach alle
  IDs ab, damit gleichzeitiges Löschen keine Mail verschluckt.
- **Speicher:** Die Vektorsuche hält etwa Abschnitte × Dimension × 4 Byte im RAM (100 000 Abschnitte
  × 2560 Dimensionen ≈ 1 GB).

**Umstieg von der Demo auf echte Daten:** Der erste Lauf mit `DEMO_MODE=0` nach einem Demo-Lauf
entfernt die Demo-Projekte mit ihren Dateien und die Demo-Mails aus dem Index (menschliche
Entscheidungen bleiben); von Hand: `python -m projektakte.index --purge-demo`. Ein leerer `DATA_DIR`
ist trotzdem der sauberste Start. `MAIL_ALLOW_EMPTY=1` erlaubt, dass ein absichtlich geleerter
`MAIL_DIR` den Mail-Index leert (sonst nur eine Warnung).

### Suche

Volltext (SQLite FTS5, BM25) und Vektorsuche liefern je bis zu 50 Kandidaten, die per Reciprocal
Rank Fusion verschmolzen werden (höchstens 3 Abschnitte je Dokument/Mail). Die besten
`RERANK_TOP_K` (30) gehen an den Reranker. Jeder Treffer nennt seine Quelle
(„01 Genehmigung/Baugenehmigung.pdf, S. 2“ bzw. „Mail vom 18.06.2024 (Absender): Betreff“; Daten in
deutscher Ortszeit). Der Volltext kennt Abkürzungen und Fachsynonyme (BMA = Brandmeldeanlage,
Notlicht = Sicherheitsbeleuchtung, RWA …) und Wortformen; eigene Ergänzungen kommen in eine
UTF-8-Datei, deren Pfad `SYNONYMS_FILE` nennt (eine Gruppe je Zeile, `BMA = Brandmeldeanlage` gilt
in beide Richtungen, `EG -> Erdgeschoss` nur in eine, `#` für Kommentare; wird bei Änderung ohne
Neustart neu gelesen).

### Fragen beantworten

Die besten `ASK_TOP_K` (6) Treffer der Suche gehen – nummeriert und bis `ASK_CONTEXT_CHARS`
(8000 Zeichen) – mit der Frage an das Sprachmodell. Es darf nur diese Quellen verwenden, muss sie als
`[n]` zitieren und sonst „Nicht gefunden“ antworten (Prompt mit nummerierten Regeln und einem
Formatbeispiel). Dokumentquellen tragen ihr Datum („Stand 14.11.2024“); bei Fragen nach dem
„letzten Stand“ kommen die neuesten Treffer in den Kontext. Überlappende Nachbarabschnitte werden
zusammengeführt, Aufzählungen über mehrere Projekte bekommen den besten Treffer je Projekt. Ohne
passende Treffer (oder Reranker-Score unter `ASK_MIN_RERANK`) wird das Modell nicht gefragt.

Ergebnis (`status` in `/api/ask`):

| Status | Bedeutung | Anzeige |
|---|---|---|
| `answered` | Antwort mit gültigen Quellenverweisen | Antwort, zitierte Quellen, weitere Fundstellen eingeklappt |
| `no_citation` | Antwort ohne Quellenverweis | „Antwort ohne Quellenangabe – bitte prüfen“ + mögliche Fundstellen |
| `not_found` | nichts passt (auch Absagen wie „Dazu liegen keine Informationen vor“) | Meldung + mögliche Fundstellen + Link zur Suche |
| `truncated` | Ausgabe abgeschnitten oder nur Denkschritte | Meldung + mögliche Fundstellen |
| `llm_unavailable` | Sprachmodell nicht erreichbar oder Zeitbudget der Frage (`ASK_DEADLINE`) aufgebraucht | Meldung + mögliche Fundstellen |

Quellenverweise werden in vielen Formen erkannt (`[2]`, `[1–3]`, `[Quelle 2]`, „Quelle 2“) und zu
`[n]` vereinheitlicht. `context` enthält immer alle Fundstellen, die das Modell gesehen hat.

### Zuordnung der Mails

Mit `ASSIGN_MODE=manual` (Standard, sobald plMail angebunden ist) ordnet Projektakte nichts selbst
zu: Mails aus plMail werden in plMail zugeordnet, alle anderen bekommen im Zuordnungs-Eingang einen
Vorschlag (auch eine gefundene Projektnummer ist dann nur ein Vorschlag). Mit `ASSIGN_MODE=auto`:

1. **Sicher**: bekannte Projektnummer im Betreff, sonst im Text, sonst im Anhangnamen.
2. **Vorschlag**: Sonst dient die Mail als Suchanfrage (Volltext + Vektor) gegen alle Abschnitte der
   Projekte (Projektdateien und sicher zugeordnete Mails). Die besten Treffer stimmen für ihr Projekt;
   liegt eines klar vorn (`ASSIGN_MIN_SHARE`, `ASSIGN_MIN_LEAD`), teilt es mindestens 3 Fachbegriffe
   mit der Mail (4 bei Absendern ohne Projekthistorie) und erreicht es optional
   `ASSIGN_MIN_SIMILARITY`, wird es vorgeschlagen. Absender, die bisher nur zu einem Projekt
   geschrieben haben, zählen zusätzlich. Newsletter und Werbung bekommen keinen inhaltlichen
   Vorschlag: Ein starkes Signal genügt (Massenadresse wie `newsletter@`/`noreply@`, `List-Unsubscribe`
   bzw. `Precedence: bulk` – außer bei Projektverteilern), schwache Signale (Werbeformulierungen,
   `vertrieb@`, Shop-Domains) nur zu zweit; wer schon zu einem Projekt geschrieben hat, gilt nie als
   Werbung. Ein Zuordnungslauf überschreibt nie eine gleichzeitig getroffene menschliche Entscheidung.
3. **Unbekannte Projektnummer** (Ordner gibt es noch nicht): kein Vorschlag für ein anderes Projekt;
   sobald der Ordner existiert, wird die Mail zugeordnet. Das gilt auch für Zuordnungen aus plMail.
4. **Zuordnungs-Eingang**: Vorschläge bestätigt oder verwirft ein Mensch; die Begründung steht in
   Alltagssprache dabei. Entscheidungen bleiben auch nach einem Neuaufbau des Index erhalten und
   hängen zusätzlich an der Message-ID (überstehen verschobene .eml-Dateien).

Im Demo-Datensatz: alle 61 Mails mit Projektnummer richtig, keine der 7 Mails ohne Projektbezug
zugeordnet oder vorgeschlagen, 8 von 8 inhaltlichen Vorschlägen richtig.

## API

| Methode und Pfad | Zweck |
|---|---|
| `GET /health` | Lebenszeichen, ruft nie KI-Dienste auf (öffentlich) |
| `GET /health/ready` | Bereitschaft für Monitoring/HEALTHCHECK: Datenbank, Indexierungs-Thread, Alter des letzten erfolgreichen Laufs, ausstehende Vektoren (öffentlich, ohne Inhalte; 503 = nicht bereit) |
| `GET /health/deps` | Zustand der KI-Dienste und von plMail (erreichbar, Format, Antwortzeit) |
| `GET /api/status` | Indexierungsstand (Phase, letzter Lauf, Fehler) und Zahlen |
| `POST /api/reindex` | Indexierung sofort anstoßen |
| `GET /api/projects?q=` | Projektliste, optional gefiltert nach Nummer/Name |
| `GET /api/projects/{nummer}` | ein Projekt, mit `folders` (alle Ordner der Nummer, Hauptordner zuerst) |
| `GET /api/projects/{nummer}/documents` | Dokumente eines Projekts mit Status (ok/übersprungen/Fehler) |
| `GET /api/documents/{id}` | ein Dokument mit seinen Abschnitten (Seite, Überschrift, Text) |
| `GET /api/mails?project=&status=&q=` | Mails (neueste zuerst), filterbar |
| `GET /api/mails/{id}` | eine Mail mit Text und Anhängen |
| `GET /api/search?q=&project=&k=` | Hybride Suche (Volltext + Vektor, Reranker), Treffer mit Quelle |
| `POST /api/ask` | Frage beantworten: `{"question": "…", "project": "BS-…"}` (Projekt optional) → Antwort, `found`, zitierte Quellen |
| `GET /api/inbox?limit=&offset=&status=` | Zuordnungs-Eingang: Mails ohne sichere Zuordnung, mit Vorschlag und Begründung (seitenweise, `total`) |
| `GET /api/integration/info`, `GET /api/integration/projects`, `POST /api/integration/suggest`, `POST /api/integration/notify` | für plMail (Bearer `INTEGRATION_TOKEN`), siehe docs/plmail-integration.md |
| `POST /api/mails/{id}/assign` | Entscheidung: `{"action": "confirm"}`, `{"action": "assign", "project": "BS-…"}`, `{"action": "reject"}` (Vorschlag verwerfen), `{"action": "none"}` (kein Projektbezug); bei kurz belegter Datenbank 503 mit `Retry-After` |

Die OpenAPI-Beschreibung liegt nach der Anmeldung unter `/openapi.json` (Swagger-UI ist
abgeschaltet, weil sie Skripte aus dem Internet lädt).

## Demo-Datensatz

```bash
python -m projektakte.demo            # schreibt nach $DATA_DIR/demo (Standard ./data/demo)
python -m projektakte.demo --target /tmp/demo
python -m projektakte.demo --eval tests/eval.json   # Evaluationssatz neu schreiben
```

Erzeugt deterministisch (fester Seed, ohne Zeitstempel in den Dateien) 8 frei erfundene
Brandschutz-Projekte (Schule, Pflegeheim, Bürogebäude, Tiefgarage, Hotel, Industriehalle, Kita,
Wohnhochhaus) unter `demo/projekte/<Nummer> <Name>/` mit Baugenehmigung (PDF, nummerierte
Auflagen), Brandschutzkonzept (DOCX), Prüfbericht (Markdown), Besprechungsprotokoll (TXT) und
einem Bild-„Scan“ eines Fluchtwegplans, außerdem 76 Mails (`demo/mails/*.eml`, 15 mit Anhängen):
Projektnummer im Betreff (Mehrheit), nur im Text, nur im Anhangnamen, ohne Nummer aber inhaltlich
eindeutig und ohne Projektbezug (Newsletter, Rechnung …). `demo/manifest.json` hält fest, welche Mail
zu welchem Projekt gehört; `tests/eval.json` enthält 79 Fragen mit bekannter Antwort und Quelle
(Umformulierungen, Abkürzungen, „letzter Stand“ je Projekt, zwei Quellen, Aufzählungen über
Projekte, Negativfragen mit Projektnamen, Fallen).
Alle Namen, Firmen und Orte sind erfunden; Mailadressen nutzen die reservierte Domain `.example`.

## Konfiguration (Umgebungsvariablen)

| Variable | Standard | Bedeutung |
|---|---|---|
| `PORT` | `8000` | HTTP-Port |
| `DATA_DIR` | `./data` | Index, Datenbank, Anhang-Cache, Demo-Daten |
| `APP_ENV` | `dev` (im Docker-Image `production`) | `dev`, `staging`, `production` (`test` nur für die Testsuite). In `production` gelten strenge Startprüfungen |
| `LOG_LEVEL` | `INFO` | Log-Level für Anwendung und Webserver |
| `PROJECTS_DIR` | `$DATA_DIR/projekte` (mit `DEMO_MODE=1`: `$DATA_DIR/demo/projekte`) | Projektordner (read-only) |
| `MAIL_DIR` | `$DATA_DIR/mails` (mit `DEMO_MODE=1`: `$DATA_DIR/demo/mails`) | Ordner mit `.eml`-Dateien (read-only) |
| `PROJECT_NUMBER_PATTERN` | `BS-\d{4}-\d{4}` | Regulärer Ausdruck für Projektnummern; `auto` = Vorschlag, gilt erst nach Bestätigung auf der Statusseite |
| `PROJECT_FOLDER_DEPTH` | `2` | Ebene der Projektordner: 1 = direkt in `PROJECTS_DIR`, 2 = auch in Jahresordnern, 3 = Kunde/Jahr/Projekt (bis 6); bei `auto` immer aus dem bestätigten Vorschlag (eine eingestellte Ebene wird dann nicht verwendet, Warnung beim Start) |
| `DEMO_MODE` | `0` | `1` = Demo-Datensatz verwenden (und beim Start erzeugen, falls er fehlt) |
| `AI_MODE` | `real` | `fake` = Fake-KI-Dienste im selben Prozess starten, URLs werden ignoriert |
| `LLM_URL` | `http://192.168.178.62:8000/v1` | Sprachmodell (OpenAI-kompatibel) |
| `LLM_MODEL` | `qwen3.8-flash-next` | Modellname |
| `LLM_VISION` | `0` (Demo mit `AI_MODE=fake`: `1`) | `1` = Modell kann Bilder lesen (Rückfall für Scans/Pläne) |
| `LLM_TIMEOUT` / `LLM_MAX_TOKENS` | `120` / `800` | Sekunden / Antwortlänge |
| `LLM_EXTRA_BODY` | – | JSON, das in jede Chat-Anfrage gemischt wird, z. B. `{"chat_template_kwargs":{"enable_thinking":false}}` |
| `EMBED_URL` | `http://192.168.178.62:8001` | Embedding-Dienst (ohne `/v1`) |
| `EMBED_MODEL` | `qwen3-embedding-4b` | Modellname |
| `EMBED_FORMAT` | `auto` | `auto`, `openai` (`/v1/embeddings`) oder `tei` (`/embed`) |
| `EMBED_TIMEOUT` / `EMBED_BATCH` | `60` / `32` | Sekunden / Texte pro Anfrage |
| `EMBED_QUERY_INSTRUCTION` | Qwen3-Instruktion | Präfix für Suchanfragen |
| `RERANK_URL` | `http://192.168.178.62:4343/rerank` | Reranker (vollständige URL) |
| `RERANK_FORMAT` | `auto` | `auto`, `tei` (`texts`) oder `cohere` (`documents`) |
| `RERANK_MODEL` | – | nur falls der Server ein `model`-Feld verlangt |
| `RERANK_TIMEOUT` / `RERANK_TOP_K` | `30` / `30` | Sekunden / Kandidaten für das Reranking |
| `LLM_API_KEY`, `EMBED_API_KEY`, `RERANK_API_KEY` | – | optionale Bearer-Tokens (Secrets) |
| `HEALTH_TIMEOUT` | `3` | Timeout je Dienst für `/health/deps` |
| `INDEX_INTERVAL` | `300` | Sekunden zwischen Indexierungsläufen (`0` = nur beim Start/auf Anforderung) |
| `EXTRACT_ISOLATION` | `1` (`0` bei `APP_ENV=test`) | PDF/Word/Mails in einem eigenen Leseprozess mit Grenzen lesen |
| `EXTRACT_MEMORY_MB` | `2048` | Speichergrenze des Leseprozesses (mindestens 256) |
| `EXTRACT_CPU_SECONDS` | `120` | Rechenzeit je Datei; Warten auf das Sprachmodell zählt nicht |
| `EXTRACT_WALL_SECONDS` | `3600` | Abbruch einer Datei nach so langer Zeit ohne Fortschritt (neuer Versuch später) |
| `ASK_TOP_K` / `ASK_CONTEXT_CHARS` / `ASK_MIN_RERANK` | `6` / `8000` / `0.01` | Quellen je Frage / Zeichenbudget im Prompt / Mindest-Score des Rerankers |
| `ASK_DEADLINE` | `180` | Gesamtbudget einer Frage in Sekunden (alle Suchen + Sprachmodell); `0` = aus, mindestens 10 s. Über `LLM_TIMEOUT`, unter 300 s halten (Browser-Grenze), Proxy-Zeitlimit darüber |
| `SEARCH_MIN_RERANK` | `0.05` | Suchseite: Treffer mit niedrigerem Reranker-Score ausblenden (nur wenn der Reranker antwortet; auf Wunsch einblendbar) |
| `SYNONYMS_FILE` | – | eigene Abkürzungen/Synonyme für den Volltext (siehe „Suche“) |
| `ASSIGN_MIN_SHARE` / `ASSIGN_MIN_LEAD` | `0.5` / `0.2` | Mail-Vorschlag nur, wenn das beste Projekt mindestens diesen Anteil der ähnlichsten Abschnitte hat und so weit vor dem zweiten liegt |
| `MAIL_ALLOW_EMPTY` | `0` | `1` = ein leerer/fehlender `MAIL_DIR` leert den Mail-Index (sonst Schutz mit Warnung) |
| `ASSIGN_MIN_SIMILARITY` | `0` (aus) | zusätzlich nötige Vektorähnlichkeit für einen Mail-Vorschlag (modellabhängig, mit `projektakte.evaluate` einstellen) |
| `AUTH_USERS` | – | Benutzer `name:pbkdf2_sha256$…`, durch Komma getrennt (Secret) |
| `AUTH_USERS_FILE` | – | Datei mit einem Eintrag `name:hash` je Zeile |
| `SECRET_KEY` | zufällig | Schlüssel für das Sitzungs-Cookie (Secret, mindestens 32 Zeichen; in `production` Pflicht) |
| `SESSION_HOURS` | `12` | Gültigkeit einer Anmeldung (höchstens 720) |
| `COOKIE_SECURE` | in `production` `1`, sonst `0` | Cookie nur über HTTPS senden; explizit `0` schaltet ab (mit Warnung) |
| `AUTH_DISABLED` | `0` | `1` = kein Zugangsschutz, nur mit `APP_ENV=dev` erlaubt (sonst bricht der Start ab) |
| `FORWARDED_ALLOW_IPS` | nur `127.0.0.1` | IPs/Netze des Reverse-Proxys, deren `X-Forwarded-For`/`-Proto` gelten. Nie `*`, solange der App-Port auch ohne Proxy erreichbar ist |
| `PLMAIL_URL` | – | Basisadresse von plMail (z. B. `https://mail.example.local`) |
| `PLMAIL_TOKEN` | – | App-Passwort `plmail_…` (Secret, nur aus der Umgebung) |
| `PLMAIL_TIMEOUT` / `PLMAIL_PAGE_SIZE` | `30` / `100` | Sekunden / Mails je Abfrage |
| `PLMAIL_WEB_URL` | – | Link-Vorlage zum Öffnen einer Mail in plMail, z. B. `https://mail.lan/mail/message/{emailId}` |
| `ASSIGN_MODE` | `auto`, mit plMail `manual` | `manual` = keine automatische Zuordnung (plMail-Mails werden in plMail zugeordnet) |
| `INTEGRATION_TOKEN` | – | Token, mit dem plMail `/api/integration/*` aufruft (Secret) |
| `PUBLIC_URL` | Anfrage-URL | eigene Adresse für Links in API-Antworten; zugleich erlaubter Origin für Formulare hinter dem Proxy |

## Betrieb

- **TLS und Reverse-Proxy:** Beispiel mit Caddy unter [deploy/](deploy/README.md) (Compose-Datei,
  Caddyfile, Umgebungsvorlage). Die App selbst spricht HTTP; TLS übernimmt der Proxy.
- **Überwachung:** `GET /health/ready` (200 = bereit, `status` `ok`/`degraded`; 503 = Datenbank oder
  Indexierungs-Thread ausgefallen). Die Abfrage kostet unabhängig von der Datenmenge nur
  Millisekunden; `pending_vectors` ist der Stand nach dem letzten Indexierungslauf. Das Docker-Image
  hat einen HEALTHCHECK (`python -m projektakte.healthcheck`). `/health` bleibt die schnelle
  Lebendprüfung.
- **Zeitlimits:** Eine Frage wird synchron beantwortet (bis zu 4 Suchen mit Embedding und Reranker,
  dann das Sprachmodell). `ASK_DEADLINE` (Standard 180 s) begrenzt die ganze Frage: Kein
  KI-Aufruf läuft darüber hinaus (auch nicht bei tröpfchenweise antwortenden Diensten); reicht die
  Zeit für einen Aufruf nicht, sucht die Anwendung ohne ihn weiter (ein Hinweis nennt, was nicht
  abgewartet wurde), ohne Restzeit für das Sprachmodell kommen die gefundenen Stellen mit der Meldung
  „Zeitbudget der Frage aufgebraucht“. Zu knappe Werte (unter 10 s, oder nicht über `LLM_TIMEOUT`) und
  ungültige Zeitlimits meldet die Anwendung beim Start als Warnung („Konfiguration: …“). Das Lese-Timeout eines
  vorgeschalteten Proxys muss darüber liegen (nginx-Standard 60 s → `proxy_read_timeout 240s`; im
  Caddy-Beispiel `response_header_timeout 240s`), sonst bricht der Proxy mit 504 ab; die Startzeile
  nennt den Wert („längste Frage bis … s“). nginx vor einem Docker-Container: `FORWARDED_ALLOW_IPS`
  auf die Gateway-Adresse des Docker-Netzes setzen.
- **Logs:** einheitliches Format mit Zeitstempel; das Zugriffsprotokoll enthält keine
  Query-Strings und keine Dateinamen (also keine Suchbegriffe, auch nicht im Fehlerlog des
  Caddy-Beispiels); beim Start
  stehen Version und wirksame Konfiguration im Log (ohne Geheimnisse).
- **Reproduzierbares Image:** `constraints.txt` pinnt alle Laufzeitpakete einschließlich der
  transitiven Abhängigkeiten.

## Sicherung

Nicht wiederherstellbar sind nur die menschlichen Entscheidungen (Zuordnungs-Eingang); alles andere
entsteht beim Indexieren neu.

```bash
python -m projektakte.backup /pfad/sicherungen
docker compose exec app python -m projektakte.backup /data/sicherungen     # im Container
```

Die Sicherung läuft im Betrieb konsistent (SQLite-Backup-API) und schreibt
`projektakte-<Zeit>.sqlite3` sowie `zuordnungen-<Zeit>.json` (die Entscheidungen als
schemaunabhängiges zweites Netz). Wiederherstellen: App stoppen, die Datei als
`DATA_DIR/projektakte.sqlite3` kopieren, `-wal`/`-shm` daneben löschen, App starten. Die
Datenbankdatei im laufenden Betrieb nicht einfach kopieren – die letzten Transaktionen können dann
in der WAL-Datei fehlen.

## Aufbau des Codes

| Modul | Aufgabe |
|---|---|
| `projektakte/config.py` | alle Einstellungen aus der Umgebung |
| `projektakte/ai/` | Clients für LLM, Embeddings, Reranker; Formaterkennung; `AIServices.check()` |
| `projektakte/fake_ai/` | Fake-Dienste (`python -m projektakte.fake_ai`) |
| `projektakte/demo/` | Demo-Generator (Projekte, Dokumente, Mails, Evaluationssatz) |
| `projektakte/db.py` | SQLite, Migrationen |
| `projektakte/registry.py` | Projektordner erkennen |
| `projektakte/extract.py`, `chunking.py`, `documents.py` | Text extrahieren, Abschnitte, Dokument-Index |
| `projektakte/vectors.py` | Embeddings speichern, Vektorsuche |
| `projektakte/mails/` | Mail-Modell, eml-Quelle, plMail/JMAP-Quelle, Speicherung |
| `projektakte/assign.py` | Mail-Zuordnung und Entscheidungen |
| `projektakte/search.py`, `ask.py`, `textutil.py` | hybride Suche, Fragen beantworten, Synonyme/Wortformen |
| `projektakte/evaluate.py` | Antwortqualität messen (`python -m projektakte.evaluate`) |
| `projektakte/indexer.py`, `background.py` | Indexierungslauf, Hintergrund-Thread |
| `projektakte/integration.py` | API für plMail |
| `projektakte/auth.py`, `auth_web.py`, `passwords.py`, `passwd.py` | Benutzer, Sitzung, Anmeldebremse, Login/Logout, Passwort-Hashes |
| `projektakte/websecurity.py`, `startup.py`, `logsetup.py` | Sicherheits-Header und Schutz vor fremden Formularen, Startprüfungen, Logformat |
| `projektakte/backup.py`, `healthcheck.py` | Sicherung, Docker-HEALTHCHECK |
| `projektakte/api.py`, `web.py`, `inbox.py`, `templates/`, `static/` | JSON-API und Weboberfläche |
| `deploy/` | Betriebsbeispiel mit TLS-Reverse-Proxy |

## Datenschutz

Es werden keine echten Firmendaten für Entwicklung und Tests verwendet; der Demo-Datensatz ist frei
erfunden. Projektordner und Postfächer werden nur gelesen, nie verändert; es wird keine Mail
gesendet. Keine Cloud-Dienste, keine Telemetrie, keine externen Skripte in der Oberfläche. Secrets
kommen nur aus Umgebungsvariablen (bzw. einer Datei, deren Pfad dort steht) und erscheinen nicht in
Logs. Fragen und Suchbegriffe werden nicht protokolliert (das Zugriffsprotokoll enthält keine
Query-Strings). Alle angemeldeten Benutzer sehen alle Projekte und alle Mails der angebundenen
Postfächer – welche Postfächer gespiegelt werden, ist vor dem Rollout mit dem Datenschutz (und ggf.
dem Betriebsrat) festzulegen.
