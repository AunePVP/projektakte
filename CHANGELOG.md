# Changelog

Alle nennenswerten Änderungen. Format angelehnt an „Keep a Changelog“; ein Abschnitt je
Roadmap-Schritt.

## [Unveröffentlicht] – Projektordner vom Dateiserver

- **Kompakte Vektorsuche (`VECTOR_SEARCH=kompakt`, Standard):** Im Arbeitsspeicher liegt nur noch 1 Bit je
  Dimension (320 statt 10.240 Byte je Abschnitt bei 2560 Dimensionen); die Suche wählt damit 2000
  Kandidaten vor und bewertet sie mit den vollen Vektoren aus SQLite genau. Ein Projekt mit höchstens 2000
  Abschnitten wird genau durchsucht. Bisheriges Verhalten mit `VECTOR_SEARCH=matrix`. Daten und Index
  bleiben unverändert.
- **Dokumente als Ordneransicht:** Die Projektseite zeigt die Dokumente nicht mehr als eine lange Liste,
  sondern Ordner für Ordner – oben die Ordner mit der Zahl ihrer Dokumente (und wie viele davon nicht
  vollständig gelesen sind), ein Klick zeigt Unterordner und Dateien darin. Mit HTMX wird nur die Ansicht
  ersetzt und die Adresse (`/projekte/<Nr>?ordner=…`) mitgeführt, ohne JavaScript lädt die Seite neu.

- **Bauvorhaben mit Unterprojekten** (`PROJECT_PARENT_DIGITS`, im Büro `4`, Standard aus): Die Aufträge eines
  Bauvorhaben-Ordners (`1015_Altstadt/101500_…`, auch in einem Zwischenordner `1015_PDF_…/101504_…`) sind dessen
  Unterprojekte. Das Bauvorhaben ist ein eigener Eintrag mit den Dateien seines Ordners außerhalb der Aufträge
  (`1015-Plaene-Aufzug/…`, Dateien direkt im Bauvorhaben); Fragen und Suche zum Bauvorhaben umfassen alle
  Unterprojekte samt Mails, zu einem Auftrag nur diesen. Der Auftrag `101500` bleibt ein Auftrag (kein
  Zusammenstoß mit dem Bauvorhaben `1015`). Projektliste mit eingerückten Unterprojekten, Bauvorhaben-Seite
  mit Unterprojekten, Link „Bauvorhaben: …“ auf der Auftragsseite, Kennzeichnung in den Projektauswahlen;
  `/api/projects` und `/api/projects/{nr}` mit `kind`, `parent`, `children`, `/api/status` und
  `/api/integration/info` zählen Bauvorhaben getrennt. Mails werden automatisch nur Aufträgen zugeordnet, von
  Hand auch einem Bauvorhaben. DB-Migration 9 (Spalten `parent`, `kind`, nur hinzugefügt); Einschalten liest
  nur die Dateien der Bauvorhaben neu. `scripts/ablage_pruefen.py --bauvorhaben 4` rechnet wie die App,
  `python -m projektakte.structure` schlägt die Einstellung vor, wenn die Aufträge in Bauvorhaben liegen
  (nicht bei Jahresordnern wie `2024_Auftraege`). Ein nicht lesbarer Zwischenordner lässt sein Bauvorhaben
  stehen; Dateien, die zwischen Bauvorhaben und Auftrag oder zwei Bauvorhaben wechseln, ziehen um statt neu
  gelesen zu werden; Ausschalten meldet eine Sammelzeile statt „Ordner fehlt“ je Bauvorhaben. „Zum
  Bauvorhaben“ nicht für Normnummern (DIN 1015), „Auftrag“ als Projektfrage nur bei einem Bauvorhaben, die
  Zahlen eines Bauvorhabens bleiben in der gefilterten Liste vollständig. Die Ablageprüfung meldet einen
  Ordner mit fremder Nummer im Bauvorhaben mit `--bauvorhaben` als Hinweis (`fremde_nummer_bauvorhaben`:
  gelesen als Datei des Bauvorhabens), nicht als Fehler.
- **„Letzte Aktivität“ in der Projektliste** zeigt kein Datum in der Zukunft mehr (ein Datum aus dem
  Dokumenttext wie „29.06.2030“): es zählt das neueste Datum bis heute.

- **Projektordner per SMB:** Anleitung in deploy/README.md (Lesekonto, `/etc/fstab` mit `ro` und
  `iocharset=utf8`, automount); `compose.yaml` reicht die Freigabe mit `propagation: rslave` durch,
  damit der Container sie auch sieht, wenn sie erst nach ihm eingebunden wird.
- **Projektstruktur:** `PROJECT_FOLDER_DEPTH` (1–6) für Projektordner unter Jahres- oder
  Kundenordnern. `python -m projektakte.structure` liest nur Ordner- und Dateinamen und nennt
  Nummernschema, Ebene, gefundene Projekte, Ausnahmen, weitere Schemata, nicht gelesene Dateitypen,
  die Textmenge je Projekt (Token, passt in 32k/128k/1M?) und die Zeilen für `projektakte.env`.
- **`PROJECT_NUMBER_PATTERN=auto` als Vorschlag mit Bestätigung:** gelesen wird erst, wenn eine Person
  den Vorschlag auf der Statusseite oder per Befehl bestätigt; eine bestätigte Struktur ändert sich nie
  von selbst. Jahre, Daten und kurze Ordnernummern sind nie Projektnummern, nummerierte Unterordner
  gehören zum Projekt, Angebote/Anfragen/Rechnungen werden nie automatisch Projekte.
- Ordner- und Dateinamen mit falscher Zeichenkodierung brechen das Einlesen nicht mehr ab: Ordner
  werden unter ihrem echten Namen gelesen (auch „Originaldatei öffnen“), Dateien übersprungen und
  gemeldet, bereits eingelesene Dokumente mit Umlauten bleiben bei einer falschen Einbindung im
  Index. Ein unbrauchbares `PROJECT_NUMBER_PATTERN` hält die Anwendung nicht an.
- Mit `auto` gilt die Ebene des bestätigten Vorschlags (eine eingestellte Ebene wird gemeldet, nicht
  verwendet); „Bitte prüfen“-Hinweise bei Angebotsordnern, Schemata mit mehr Ablagestruktur und
  Kundenordnern mit nummerierten Unterprojekten; ein leerer Einhängepunkt löscht keinen Vorschlag;
  `/api/status` ohne absoluten Pfad der Freigabe.
- Unterordner mit derselben Nummer wie ihr Projekt (`284201-Mail` in `284201_…`) gelten als dessen
  Gliederung – kein „Bitte prüfen“-Hinweis mehr, der sie als Projekte vorschlägt.
- `scripts/ablage_pruefen.py`: prüft eine Ablage der Form Ziffer/2 Ziffern/1234_Bauvorhaben/
  123456_Auftrag/123456-Kategorie und meldet Fehler (betreffen Projektakte: Auftrag nicht gefunden,
  Nummer passt nicht zum Bauvorhaben, Dateien außerhalb eines Auftrags) und Hinweise (Abweichungen von
  der Regel), wahlweise als CSV.
- **Mehrere Ordner mit derselben Projektnummer sind ein Projekt** (vorher wurde nur der zuerst gefundene
  gelesen): Auftragsordner, Teilordner außerhalb davon (`119300-Bilder` direkt im Bauvorhaben), Kopien und
  verschachtelte Kopien werden zusammen gelesen. Hauptordner und Name nach einer Formregel
  (`<Nr>_<Name>` vor nur Nummer vor `<Nr>-<ein Wort>`); ein Projekt nur aus Teilordnern heißt nach seinem
  Bauvorhaben. DB-Migration 8 (Tabelle `project_folders`, nur hinzugefügt) – bestehende Projekte und
  Dokumente bleiben, nur die Dateien der weiteren Ordner werden neu gelesen. Projektseite und
  `/api/projects/{nr}` (`folders`) zeigen alle Ordner; umbenannte weitere Ordner werden verschoben, nicht
  neu gelesen; statt einer Warnung „Projektnummer doppelt“ je Ordner eine Info-Zeile je Lauf.
- Ein Ordner mit der Nummer eines Auftrags, der in einem Bauvorhaben mit anderer Nummer liegt (meist ein
  Datum: `1827_…/Fotos/230512_Begehung`), wird nicht mit dem echten Auftrag `2305_…/230512_…`
  zusammengelegt, sondern im Protokoll genannt; ein Jahresordner im Bauvorhaben (`1827_…/2013_Archiv/…`)
  zählt nicht als anderes Bauvorhaben.
- Behoben: Ein nicht lesbarer Ordner oberhalb eines Projektordners (fehlende Rechte) brach den
  Dokumentenlauf mit `PermissionError` ab – jetzt bleiben die Dokumente dieses Ordners unverändert und
  der Lauf geht weiter.
- Ablage-Prüfung: mehrere Ordner mit einer Nummer und Teilordner ohne ihren Auftragsordner sind jetzt
  Hinweise (sie werden gelesen); „Nummer passt nicht zum Bauvorhaben“ auch tiefer und nach dem
  Bauvorhaben, nicht nach einem Jahresordner darin; neue Zeile „davon aus mehreren Ordnern“.
- **OpenDocument lesen:** `.odt`/`.ods`/`.odp` mit Vorlagen und die alten OpenOffice-1.x-Formate
  (`.sxw`/`.sxc`/`.sxi`) – auf der Freigabe lagen rund 14.150 `.odt`-Dateien, die bisher übersprungen
  wurden. Ohne neue Abhängigkeit (zipfile, expat), im eigenen Leseprozess, mit Grenzen (64 MB entpackter
  Inhalt, bei größeren Tabellen der Anfang; 10.000 Bestandteile; 100 Ebenen; 500.000 Elemente je Absatz,
  Zeile oder Folie; 10.000 verschiedene Namen; 32 Mio. Zeichen; Wiederholungen in Tabellen höchstens
  100-mal und zusammen höchstens 1 Mio. Zeichen). Zip-Bomben, andere Kompressionen als ODF sie kennt,
  DTDs mit Entities und passwortgeschützte Dateien werden ohne erneuten Versuch abgelehnt.
- Mail-Anhänge, die eine frühere Version als „Format … nicht unterstützt“ gespeichert hat, werden aus
  dem Anhang-Cache gelesen, sobald ihr Format unterstützt wird (betrifft `.odt`-Anhänge).
- Läuft das Lesen einer Datei in die Speichergrenze, wird sie als „zu groß“ vermerkt, statt den
  Leseprozess beim Protokollieren ein zweites Mal an der Grenze scheitern zu lassen (vorher: Absturz
  des Leseprozesses und endlose Wiederholung).
- Verschlüsselte PDFs (AES), die nur gegen Bearbeiten geschützt sind, werden gelesen (neue Abhängigkeit
  `cryptography`; vorher Fehler „cryptography>=3.1 is required“); PDFs, die zum Öffnen ein Passwort
  brauchen, werden ohne erneuten Versuch übersprungen.
- Große CAD-Pläne: pypdf liest bis zu 8 Mio. Bausteine je Seiteninhalt statt 2 Mio. (vorher „Reached maximum
  number of tokens per stream“, die Beschriftung fehlte); `fonttools` als Abhängigkeit, damit Zeichen
  eingebetteter CFF-Schriften richtig gelesen werden (vorher Warnung „fontTools is required“).
- `VISION_SKIP_FOLDERS` (z. B. `Bilder`): Fotos in Ordnern wie `263900-Bilder` gehen nicht an die Bildauswertung;
  ohne die Einstellung werden sie später nachgelesen.
- Protokoll: je gelesener Datei eine Zeile „Gelesen: <Datei> – ok (Bildauswertung), 3 Seite(n), 7 Abschnitt(e), 12,3 s“.
- 1917 Tests (4 Integrationstests übersprungen, 1 xfail).

## [0.5.0] – 2026-11-09 – Schlussprüfung und Gegenprüfungen

Nach 0.4.0 folgten eine Schlussprüfung (Code-Review und Live-Vorführung) und vier Runden
Gegenprüfung, jeweils mit unabhängigen Prüfern, die jede Korrektur erneut angriffen.

### Schlussprüfung
- Fragen: Zusammenfassungen ohne Zeit für die Überblickssuche fragen das Sprachmodell mit den
  eigenen Fundstellen (vorher „nichts gefunden“); aufgebrauchtes Budget mit eigener Meldung statt
  „KI-Dienst nicht erreichbar“; Hinweise entdoppelt, Sekunden gerundet mit Komma; die
  Formaterkennung von Reranker und Embeddings bleibt zusammen in einem Timeout; `ASK_DEADLINE`
  mindestens 10 s, Warnungen beim Start für zu knappe Werte und `LLM_TIMEOUT` ≥ `ASK_DEADLINE`;
  verneinte zitierte Teilsätze („weder [1] noch [2] nennen ihn“) bleiben Absagen; `evaluate` misst
  mit `ASK_DEADLINE` und Synonymen.
- Fake-KI: Verfasser-Fragen („geschrieben“), Bindestrich-Wörter („Funk-Rauchwarnmelder“),
  Verben mit Vokalwechsel („vertritt“), Zuständigkeit aus der Signatur, genannter Ort mit Beleg im
  selben Projekt; ein Gebäudetyp eines anderen Projekts („die Tiefgarage im Wohnhochhaus“) ist kein
  Projektname mehr – vorher zwei falsche grüne Antworten. Prüfsatz 0,68, Absagen 12/12.
- Daten: Ein mehrfach gezeichnetes Bild einer Scan-Seite wird einmal dekodiert und gesendet (eine
  16-KB-Datei belegte vorher 1,8 GB); je Seite höchstens 200 Bilder und 150 Megapixel laut PDF vor
  dem Dekodieren (`EXTRACT_VERSION` 4). Demo mit `AI_MODE=fake` liest Bilder (`LLM_VISION` an).
- Oberfläche: „BS 2024 0233“ mit Leerzeichen zeigt „Zum Projekt“ und markiert die ganze Nummer;
  Eingang zählt eine verworfene Karte nach „Weiter“ nicht doppelt; Quellen-Ausschnitte beginnen an
  der Überschrift ihres Abschnitts.

### Gegenprüfung (Runden 5–8)
- **Eigener Leseprozess:** PDF-, Word- und Mail-Dateien (auch Anhänge) werden in einem eigenen,
  dauerhaften Prozess mit Speicher- und Rechenzeitgrenze je Datei gelesen (`EXTRACT_ISOLATION`,
  `EXTRACT_MEMORY_MB`, `EXTRACT_CPU_SECONDS`, `EXTRACT_WALL_SECONDS`). Eine beschädigte oder
  absichtlich aufgeblähte Datei kann weder den Speicher der Anwendung füllen noch die Indexierung lange
  aufhalten; sie wird mit Grund vermerkt und nur neu gelesen, wenn genau die Grenze erhöht wird, an der
  sie scheiterte. Der Prozess endet mit der Anwendung; startet er nicht, läuft die Indexierung weiter.
- **Scans:** Bilder je PDF-Objekt einmal je Dokument dekodiert, echte Größe aus dem Bildkopf,
  verkleinertes Dekodieren großer JPEG/JPEG-2000-Pläne (A0: 0,33 statt 1,1 GB), weiche Masken gezählt,
  begrenzter Inhaltslauf und Grenzen je Dokument; reine Vektorseiten gelten als Seiten ohne Text
  (`EXTRACT_VERSION` 6).
- **Fragen:** Überblicks- und Beteiligtensuche laufen immer innerhalb des Zeitbudgets; ein Hinweis
  nennt, was nicht abgewartet wurde; jeder KI-Aufruf ist auch bei tröpfelnden Antworten nach seiner
  Gesamtdauer begrenzt; ungültige Zeitlimits (0, `inf`, keine Zahl) fallen mit Warnung auf den
  Standard zurück. Absagen echter Modelle: Meta-Sätze über die Quellen („[1] und [2] beschreiben nur
  die Anlage“, „fehlt eine entsprechende Angabe“, „Möglicherweise …“) gehören zur Absage, belegte
  Fakten („Laut [2] gibt es keine offenen Mängel mehr“) bleiben Antworten – 59 Musterantworten als Test.
- **Fake-KI:** keine falschen grünen Antworten mehr bei abweichenden Bezeichnern (Wohnbereich C,
  2. OG, erster Rettungsweg, vor dem Umbau), Datum ohne Jahr, anderem Bauteil, anderem Dokument oder
  der Adresse einer Person; Verfasserfragen über die Fake-Vektoren (die echte Volltextsuche bleibt ohne
  diese Synonyme); Fluchtwegplan-Fragen beantwortbar; Zusammenfassungen ohne Schlussformel. Eigener
  Satz 103/103, Prüfsatz 0,68 (19/28), Absagen 12/12, keine erfundene Antwort.
- **Oberfläche:** Projektnummer mit Leerzeichen nur im konfigurierten Format verbunden; Eingang
  zeigt schon gezeigte Karten markiert statt versteckt (signiert, kein Link kann Mails verbergen);
  Teilwort-Markierung nur bei echten Komposita; `PROJECT_NUMBER_PATTERN` auf allen Seiten.

### Tests
- 1567 Tests (1562 bestanden, 4 Integrationstests übersprungen, 1 xfail), parallel mit pytest-xdist
  (≈ 60 s). Docker-Smoke grün.

## [0.4.0] – 2026-11-08 – Nachbesserung nach unabhängiger Prüfung

Eine Prüfung mit 12 Prüfern (6 Blickwinkel, 6 Gegenprüfer) fand 67 bestätigte Mängel. Alle sind
behoben (5 Pakete, eigene Commits) und erneut gegengeprüft; nur die Prüfung gegen die echten
KI-Dienste ist hier nicht möglich (dafür gibt es jetzt `python -m projektakte.evaluate --real`).

### Antworten und Suche
- Fake-Sprachmodell als extraktiver Leser: beantwortet frei formulierte Fragen (Synonyme,
  Abkürzungen, Wortformen), zählt Projektnummer/-name nicht als Treffer, beantwortet „letzter
  Stand“ mit der neuesten Quelle, zitiert den passenden Satz einmal („Laut Baugenehmigung (S. 2),
  Auflage 7 …: … [1]“), nie Überschriften, Tabellen, Grußformeln; sonst „Nicht gefunden.“.
  Fake-Reranker ignoriert Füllwörter und Projektnummern.
- Status-Vertrag von `/api/ask`: `answered`, `no_citation` (unbelegt, keine Quellen), `not_found`,
  `truncated` (neu: abgeschnittene Ausgabe/nur Denkschritte), `llm_unavailable`; `context` enthält
  immer die Fundstellen. Absagen echter Modelle und Quellenformen (`[Quelle 2]`, `[1–3]`, „Quelle 2“)
  werden erkannt und vereinheitlicht; Prompt mit nummerierten Regeln und Formatbeispiel;
  Dokumentquellen mit Datum; überlappende Abschnitte zusammengeführt; Aufzählungen mit dem besten
  Treffer je Projekt.
- Volltext mit Abkürzungen/Synonymen (eingebaute Liste + `SYNONYMS_FILE`) und Wortformen; Daten in
  Quellenangaben in Ortszeit. `/health/deps`: fehlendes `GET /models` ist nur ein Hinweis.
- `python -m projektakte.evaluate [--real]`: Antwortqualität und Schwellwerte gegen die
  konfigurierten Dienste messen. Evaluationssatz 79 statt 20 Fragen.
- Demo-Daten (Version 4): Datumsangaben passen zu den Texten, Sommer-/Winterzeit, erkennbarer
  Fluchtwegplan; Mail-Kategorien unverändert.

### Indexierung
- Extraktionsprofil je Dokument: Nach `LLM_VISION=1` oder Modellwechsel werden übersprungene/
  unvollständige Dokumente (auch Anhänge) nachgelesen; Fehler mit Backoff (15 min … 7 Tage)
  wiederholt; fehlgeschlagene Bildseiten werden gemeldet statt still verworfen.
- Kurze Schreibtransaktionen (Lesen/Extrahieren außerhalb der Sperre, Commit je Dokument).
- Vektorsuche mit Projektfilter berücksichtigt Zuordnungen sofort; Matrix nur einmal je
  Embedding-Lauf neu geladen, halber Spitzenspeicher.
- Umbenannte/verschobene Projektordner behalten Dokumente und Vektoren, neue Namen erneuern die
  Volltext-Titel; Dateien verschwundener Projekte verlassen den Index.
- Schutz bei fehlendem/leerem Laufwerk (Warnung statt Deaktivieren/Löschen), Symlinks aus
  `PROJECTS_DIR` heraus werden nie gelesen, UTF-16/32-Textdateien.
- Migration 6 (`documents.extract_profile`, `incomplete`, `attempts`, `retry_after`).

### Mails und Zuordnung
- Mail-Import: Anhänge vor dem Schreiben vorbereitet, jede Mail atomar (SAVEPOINT) und einzeln
  committet; Cache-Dateinamen byte-sicher. plMail: Fehler je Mail isoliert und mit Backoff
  wiederholt, Erstabgleich mit Überlappung und ID-Abgleich.
- Verschobene .eml-Dateien behalten Mail und Entscheidung; Entscheidungen zusätzlich unter der
  Message-ID. Fehlendes/leeres `MAIL_DIR` löscht nichts.
- Zuordnung in Häppchen (4000 Mails: 277 s → 10 s, Eingang bleibt bedienbar); unbekannte
  Projektnummern ohne Fremdvorschlag und automatische Zuordnung, sobald der Ordner existiert (auch
  für plMail-Zuordnungen); Vorschläge brauchen gemeinsame Fachbegriffe (optional
  `ASSIGN_MIN_SIMILARITY`), Newsletter/Werbung bekommen keine; Begründungen in Alltagssprache.
- Mailkopf im Abschnittstext mit deutscher Ortszeit; `/api/integration/suggest` nimmt optional
  `headers`.

### Betrieb und Sicherheit
- Abmelden beendet alle Sitzungen des Benutzers serverseitig (Sitzungsepoche); Laufzeit
  serverseitig begrenzt; kaputte Cookies führen nicht mehr zu HTTP 500.
- Anmeldebremse mit exponentiellem Backoff statt fester Sperre, Geräte-Cookie gegen Aussperren,
  begrenzte gleichzeitige Passwortprüfungen; Proxy-Vertrauen über `FORWARDED_ALLOW_IPS`.
- Startprüfungen: `AUTH_DISABLED` nur mit `APP_ENV=dev`, in `production` starker `SECRET_KEY`
  Pflicht, `DATA_DIR` muss beschreibbar sein (deutsche Meldung). `COOKIE_SECURE` in `production`
  standardmäßig an. Docker-Image: `APP_ENV=production`, HEALTHCHECK, schlanker (`.dockerignore`),
  `constraints.txt`.
- Sicherheits-Header (CSP ohne `unsafe-inline`, Frame-Schutz, nosniff, Referrer-Policy,
  `Cache-Control: no-store`) und Ablehnung zustandsändernder Anfragen fremder Seiten.
- Zugriffsprotokoll ohne Query-Strings, einheitliches Logformat, `LOG_LEVEL`.
- Neu: `GET /health/ready`, `python -m projektakte.backup`, Betriebsbeispiel mit TLS-Proxy unter
  `deploy/`. Login und Logout in `auth_web.py`.

### Oberfläche
- Antwortanzeige nach Status-Vertrag: zitierte Quellen mit denselben Nummern wie im Text, weitere
  Fundstellen eingeklappt; sonst „Mögliche Fundstellen“ und „In der Suche öffnen“. Bereichszitate
  `[1–3]` werden vollständig verlinkt.
- Suche blendet schwache Treffer aus (`SEARCH_MIN_RERANK`, einblendbar), hebt Suchbegriffe hervor.
- Zuordnungs-Eingang: 50 Mails je Seite mit Filtern, eine gemeinsame Projektliste, Zähler und
  Badge aktualisieren sich sofort, verworfene Karten bleiben bedienbar, belegte Datenbank gibt
  einen freundlichen Hinweis (API: 503 + `Retry-After`). `/api/inbox` paginiert.
- Projektseite mit „Anhänge aus Mails“, echte Plurale, Daten in Ortszeit; Statusseite zeigt
  Warnungen und im Fake-Modus „simuliert“; roter Balken bei ausgeschaltetem Zugangsschutz; keine
  Inline-Skripte/-Styles.

### Runde 2 (nach der ersten Gegenprüfung)
- Antworten: Fake-KI lehnt im Zweifel ab (Wert-Fragen nur mit passendem Wert), belegte Negativ-Fakten
  („Es liegen keine Mängel mehr vor [1]“) sind Antworten, Kompositum-Abgleich über das Grundwort,
  Tabellen als „Spalte: Wert“, Zusammenfassungen; jede Quelle trägt als `snippet` die relevante Stelle;
  Nutzertexte ohne Dienst-URLs und Einstellungsnamen; Kennungssuche wörtlich.
- Zurückgehaltener Prüfsatz eines unabhängigen Autors (`tests/eval_holdout.json`, 40 Fragen),
  `evaluate --set holdout --summary-only`, Vorprüfung der Dienste, Exit-Codes 0/1/2; eigener Satz 94 Fragen.
- Indexierung: Scan-PDFs werden mit Pillow (neue Abhängigkeit `pillow==12.3.0`) wirklich gelesen,
  nicht lesbare Bilder werden wiederholt statt als „kein Text“ abgelegt; Karenzzeit (24 h, 3 Läufe)
  vor dem Löschen der Dateien fehlender Projektordner; Demo-Daten werden beim Umstieg auf echte Daten
  entfernt (`index --purge-demo`); `MAIL_ALLOW_EMPTY`.
- Mails/Zuordnung: Compare-and-Set gegen überschriebene Entscheidungen, präzise Werbeerkennung in beide
  Richtungen, Message-ID-Entscheidungen nur noch für verschobene Mails, ausstehende plMail-Anhänge
  sichtbar und nachgeladen, `BEGIN IMMEDIATE` gegen „database is locked“, Symlinks in `MAIL_DIR`,
  Anhänge ohne Zeichensatz als UTF-8, saubere „gemeinsame Begriffe“.
- Sicherheit/Betrieb: Bremse je Adresse verzögert nur noch (kein Aussperren hinter gemeinsamer IP),
  Basic nur je Benutzer+Adresse; `/health/ready` mit konstantem Aufwand; deutsche Fehlerseite für
  unbekannte Adressen; eigene CSP für Originaldateien; Version im Startlog; Proxy-Timeout 240 s;
  Start in `production` ohne Benutzer wird abgebrochen.
- Oberfläche: Eingang blättert per Schlüssel, Kennungstreffer immer sichtbar, bereinigte Ausschnitte und
  Dokumentansicht, Originaldatei mit Dateinamen in der URL, mobile Ansicht ohne Wortbrüche.

### Runde 3 (nach der zweiten Gegenprüfung)
- Fragen: Gesamtbudget je Frage `ASK_DEADLINE` (Standard 180 s) – kein KI-Aufruf läuft darüber hinaus,
  vorher bis zu 600 s im schlimmsten Fall; ein Reranker-Timeout löst keine zweite Formaterkennung mehr
  aus. Teil-Absagen echter Modelle („Fakt [2]; ein genauer Tag ist nicht genannt“) bleiben Antworten.
  Fake-KI: Maße passend zur Dimension, „Wann wurde …?“ nur mit dem Datum des Ereignisses (keine
  Pflichten), nur das genannte Projekt, keine Floskeln/Signaturen/Rollenzeilen, viele freie
  Überblicksfragen, Unterzeichner-Fragen; `SYNONYMS_FILE` wirkt auch in Fake-Reranker und -Chat;
  Ausschnitte zeigen die Aussage statt der Signatur; `evaluate` mit Exit 2 bei 0 Fragen. Prüfsatz 0,64.
- Daten: Mailtexte ohne Zeichensatz-Angabe behalten ihre Umlaute; Streifen-/Kachelscans und teilweise
  unlesbare Seiten werden erkannt und wiederholt; `jbig2dec` im Image; Verschiebe-Erkennung über die
  Message-ID nur mit gleichem Datum und gleicher Größe; Werbeerkennung prinzipieller (schwache Signale
  nur ohne Projektbezug); Demo-Bereinigung entfernt auch Anhang-Cachedateien und meldet sich auf der
  Statusseite.
- Sicherheit: Vorrang für Benutzernamen ohne eigenen Fehlversuch – eine Flut von derselben Adresse
  hält Kollegen und Monitoring nicht auf; keine Dateinamen im Zugriffsprotokoll und Caddy-Fehlerlog;
  Favicon auch auf den Datei-Betrachterseiten; Proxy-Zeitlimit 240 s passend zu `ASK_DEADLINE`.
- Oberfläche: Seitenzeile im Eingang nach „Verwerfen“ richtig, Ausschnitte an Wortgrenzen und ohne
  Mailkopf, keine doppelten Absätze auf der Dokumentseite, „Datei öffnen“ nur bei vorhandener Datei
  („Anhang ausstehend“).
- Demo: Fluchtwegplan nach DIN ISO 23601/ISO 7010 neu gezeichnet (Version 5).

### Tests
- 918 Tests (913 bestanden, 4 Integrationstests übersprungen, 1 xfail), parallel mit pytest-xdist
  (≈ 35 s auf 4 Kernen; `PYTEST_WORKERS=0` für einen Prozess).
  Ein Test ist bewusst „xfail“: das 80-%-Ziel der Fake-KI auf dem zurückgehaltenen Prüfsatz.

## [0.3.0] – 2026-11-08 – Zuordnung in plMail

- `ASSIGN_MODE=manual` (Standard, sobald plMail angebunden ist): keine automatische Zuordnung mehr.
  Mails aus plMail werden in plMail beim Lesen zugeordnet; andere Mails bekommen nur Vorschläge
  (auch die Projektnummer schlägt nur vor) und werden im Projektakte-Eingang bestätigt.
- JMAP: Projektakte fordert die plMail-Erweiterung `urn:plmail:params:jmap:projektakte` mit der
  Email-Eigenschaft `plmail:projektakte` an, sofern plMail sie anbietet, und übernimmt die Zuordnung
  (Rangfolge: plMail > Projektakte-Entscheidung > Automatik). Neuer Status „in plMail offen“.
- Integrations-API für plMail mit `INTEGRATION_TOKEN`: `GET /api/integration/info`,
  `GET /api/integration/projects`, `POST /api/integration/suggest` (Vorschläge für eine gelesene Mail,
  speichert nichts), `POST /api/integration/notify` (sofort abgleichen).
- Mail-Seite verweist bei plMail-Mails auf plMail (`PLMAIL_WEB_URL`), Umhängen in Projektakte ist
  dort gesperrt; Statusseite zeigt Zuordnungsmodus und offene plMail-Mails.
- docs/plmail-integration.md (Schnittstellenvertrag) und docs/plmail-prompt.md (Auftrag für plMail).
- Fake-JMAP-Server bildet die Erweiterung nach; Tests für den gesamten Ablauf.

## [0.2.0] – 2026-11-08 – Phase A umgesetzt

### Abschluss
- Markierter Integrationstest gegen die echten KI-Dienste (`tests/test_integration_real.py`, nur mit
  `PROJEKTAKTE_INTEGRATION=1`).
- docs/ki-dienste.md: Stand der Formatprüfung, Hinweise zum Umschalten und zu den Fakes.
- README neu gegliedert (Schnellstart, Betrieb, Funktionsweise, API, Konfiguration, Codeaufbau);
  DECISIONS.md mit Zusammenfassung.
- Swagger-UI/ReDoc abgeschaltet (laden Skripte aus dem Internet); `/openapi.json` bleibt.
- Version 0.2.0 (1.0 nach Prüfung mit den echten Diensten).
- `docs/letzter-testlauf.txt`: Ausgabe des letzten Testlaufs (169 bestanden, 3 Integrationstests
  übersprungen; Gegenprobe unter Python 3.12) und der Container-Prüfung.

### Schritt 14 – plMail über JMAP
- Zweite Mailquelle über die JMAP-API von plMail (`PLMAIL_URL`, `PLMAIL_TOKEN` als Bearer):
  Session-Erkennung mit Rückfall, alle Konten, Erstabgleich seitenweise (`Email/query` + `Email/get`,
  Neusortierung nach den Query-IDs), danach inkrementell über `Email/changes` mit gespeichertem
  `state`, Anhänge über die `downloadUrl`.
- Strikt lesend (nur `Email/query`, `Email/get`, `Email/changes`); Fehler je Konto, keine Abbrüche.
- `GET /health/deps` und die Statusseite melden plMail (erreichbar, Konten, Antwortzeit).
- Fake-JMAP-Server für die Tests (`tests/fake_jmap.py`).
- docs/plmail-jmap.md: Umsetzung und Vorschläge an plMail.

### Schritt 13 – Zugangsschutz
- Anmeldung mit Benutzern aus `AUTH_USERS`/`AUTH_USERS_FILE` (PBKDF2-SHA256, 600 000 Iterationen),
  `python -m projektakte.passwd NAME` erzeugt Einträge.
- HMAC-signiertes Sitzungs-Cookie (HttpOnly, SameSite=Lax, optional Secure), Ablauf nach
  `SESSION_HOURS`; entfernte Benutzer verlieren ihre Sitzung sofort.
- Alle Seiten und APIs außer `/health` (sowie Anmeldeseite und statische Dateien) geschützt; Seiten
  leiten zur Anmeldung, APIs antworten 401; HTTP Basic für Skripte (nur API und `/health/deps`).
- Sperre nach 5 Fehlversuchen (5 Minuten je Benutzer und Adresse), Schutz vor offenen Weiterleitungen.
- Zuordnungsentscheidungen speichern den Benutzernamen.
- `scripts/docker-smoke.sh` prüft zusätzlich Zugangsschutz und eine Frage mit Quelle im Container.

### Schritt 12 – Weboberfläche
- Deutsche, serverseitig gerenderte Oberfläche (Jinja2) mit HTMX (lokal unter `/static`, 0BSD):
  Projektliste mit Live-Filter, Projektseite (Fragefeld, Zeitachse, Dokumente, Mails), Zuordnungs-
  Eingang (Bestätigen/Verwerfen/Zuordnen/Kein Projektbezug), Fragen, Suche, Mail- und Dokumentseiten
  mit Abschnitten und Originaldatei, Statusseite mit Dienstezustand.
- Antworten mit verlinkten Quellenverweisen `[n]`; Quellen springen zum Abschnitt im Dokument.
- Alle Formulare funktionieren auch ohne JavaScript; Fehlerseiten auf Deutsch (API bleibt JSON).
- Responsives Layout ohne horizontales Scrollen (geprüft bei 1366 px und 390 px Breite).
- Originaldateien werden nur ausgeliefert, wenn sie im Index stehen und unter `PROJECTS_DIR` bzw.
  dem Anhang-Cache liegen.
- Neue Abhängigkeiten: `jinja2==3.1.6`, `python-multipart==0.0.32`.

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
