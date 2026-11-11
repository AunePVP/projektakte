# Changelog

Alle nennenswerten Änderungen. Format angelehnt an „Keep a Changelog“; ein Abschnitt je
Roadmap-Schritt.

## [Unveröffentlicht] – Projektordner vom Dateiserver

- **PDF-Seiten mit Text als Kurven werden gelesen:** Seiten ohne Textebene und ohne Bild (gedruckte Vorschriften
  aus PDF-Druckertreibern, CAD-Pläne mit Text als Umrissen, Schriften ohne Zeichenzuordnung) galten als leer
  („kein Text gefunden“). Jetzt zeichnet PDFium (neue Abhängigkeit `pypdfium2`) die Seite in Graustufen (300 dpi,
  große Pläne kleiner, höchstens 16 Megapixel, höchstens 20 s je Seite) und sie geht wie ein Scan an Tesseract und
  in die Bildauswertung. Wirklich leere Seiten (nichts dunkler als hellgrau) bleiben leer. Textebenen aus fast nur
  Ersatz-, Steuer- oder Privatzeichen gelten als fehlend. DB-Migration 15: PDF-Dateien mit „kein Text gefunden“ und
  vollständige PDF-Dateien mit einer Seite ohne Abschnitte vor der letzten Textseite werden einmal neu gelesen
  (auch Mail-Anhänge). Ohne `pypdfium2` bleibt alles wie bisher.
- **Dokumentarten:** Jedes Dokument (auch Mail-Anhänge) bekommt eine Dokumentart – Brandschutzkonzept,
  Brandschutznachweis, Baugenehmigung, Prüfbericht, Stellungnahme, Gutachten, Flucht- und Rettungsplan, Protokoll,
  Angebot, Rechnung, Leistungsverzeichnis, Schreiben, Foto … –, bestimmt aus dem Titelbereich der ersten Seite, der
  Dateiname nur als schwacher Hinweis. Nicht starr: unbekannte Arten wie „Löschwasserberechnung“ werden als freier
  Text gespeichert. Unklare Dokumente bestimmt im Hintergrund das Sprachmodell (neue Einstellung `DOKUMENTART_KI`,
  `auto` = nur mit den echten Diensten; ein Aufruf nach dem anderen, nie neben einer Frage; „unklar“ ist erlaubt).
  Geänderter Text (z. B. nach der Bildauswertung) → neu bestimmt. Schon eingelesene Dokumente werden im Hintergrund
  nachgetragen, Fortschritt auf der Statusseite. Etikett an Suchtreffern, in der Ordneransicht, im Betrachter und auf
  der Dokumentseite. DB-Migration 14 (Spalten und Indizes in `documents`, nur hinzugefügt).
- **Suche nach Dokumentart:** Nennt die Suche eine Dokumentart – in jeder Form („Brandschutzkonzepte“, „BS-Konzept“,
  „Brandschutzkozept“, „Protokolle“) –, stehen die Dokumente dieser Art oben, die neuesten zuerst, dann verwandte
  Arten (Brandschutznachweis), dann alles andere; nichts wird weggelassen. „Brandschutzkonzept Kita“ verbindet Art und
  weitere Wörter. Bei Fragen gehen die passenden Abschnitte dieser Dokumente zusätzlich an den Reranker.
  `GET /api/search` liefert `doc_types`, `corrections` und je Treffer `doc_type`/`type_match`.
- **Tippfehler:** Ein Suchwort, das nichts findet („Branschutzkonzept“, „Rettungswek“), wird auch als nächstliegendes
  häufiges Wort des Index gesucht; die Suchseite sagt „meinten Sie …?“. Wortschatz bis 400.000 Wörter im Speicher,
  im Hintergrund gelesen; wenige Millisekunden je Suche.
- Synonyme: Fluchtplan = Fluchtwegplan = Flucht- und Rettungsplan, Prüfbericht = Prüfungsbericht,
  Übereinstimmungserklärung = Übereinstimmungsbestätigung, Feuerwehrplan = Feuerwehreinsatzplan, Aktenvermerk =
  Aktennotiz.
- **Bildauswertung und Embeddings laufen neben dem Einlesen:** In der Anwendung liest das Bildmodell die
  Scan-Seiten ab dem Start in einem eigenen Thread, während der Lauf noch Dateien liest und Tesseract arbeitet –
  nicht mehr erst am Ende jedes Laufs. Höchstens `VISION_CONCURRENCY` (3) Aufrufe gleichzeitig, ein Platz des
  KI-Servers bleibt für Fragen frei; Reihenfolge wie bisher (vorgezogene, Ordner, neueste zuerst). Die Seitenbilder
  bereitet ein zweiter Leseprozess mit denselben Grenzen vor (leer etwa 50 MB; Container-Speicher mindestens App +
  2 × `EXTRACT_MEMORY_MB`). `VISION_PASS_SECONDS` ist jetzt die Länge einer Runde, danach beginnt sofort die
  nächste. Ebenso machen die Embeddings neue Abschnitte laufend zu Vektoren (eigener Thread, neue Einstellung
  `EMBED_CONCURRENCY`, Standard 2 Stapel zu 32 Abschnitten gleichzeitig) – neue Dokumente und von der
  Bildauswertung neu geschriebene sind nach kurzer Zeit auch über die Vektorsuche zu finden. Die Phasen
  „Embeddings“ und „Bildauswertung“ am Ende des Laufs entfallen in der Anwendung (`python -m projektakte.index`
  behält sie). Ergebnisse für eine inzwischen geänderte Datei bzw. einen neu geschriebenen Abschnitt werden
  verworfen, nichts wird doppelt eingebettet. `LLM_VISION=0`: keine Bildauswertung. Beim Beenden bricht die
  Bildauswertung sofort ab (Seiten unterwegs werden nach dem nächsten Start gelesen). DB-Migration 13 (Teilindex
  für Abschnitte ohne Vektor, nur hinzugefügt).
- **Ordner „aus“ wird nicht gelesen:** Ein ausgeschalteter oberster Ordner wird gar nicht mehr gelesen (vorher kam
  er nur ans Ende): keine neuen oder geänderten Dateien, kein Tesseract, keine Bildauswertung; ein Projekt daraus,
  das gerade gelesen wird, hört nach der aktuellen Datei auf. Schon Gelesenes bleibt im Index und durchsuchbar,
  die Projekte bleiben im Register, Mails sind nicht betroffen. Anzeige „aus – wird nicht gelesen“, Projekte
  „Ordner ausgeschaltet“ (nicht in der Warteschlange); vorziehen geht erst nach dem Einschalten. API:
  `switched_off`, Zustand `off`.
- **Statusseite „Wird gerade gelesen“:** „x von y Dateien“ zählt jede Datei (vorher nur in 25er-Schritten). Neu:
  „Lauf seit“, „Projekt seit … · noch ≈ …“, „Ø … je gelesene Datei“ (nur wirklich gelesene, die unveränderten
  daneben), „Warteschlange: noch N Projekte · grob ≈ …“; Bildauswertung mit Seiten beim Modell, Datei und Seite,
  ausstehenden Seiten, Ø je Seite, Seiten je Stunde und Restzeit; Embeddings mit Stapeln beim Dienst, fertigen und
  offenen Abschnitten, letzter Datei, Rate und Restzeit. Gleitende Durchschnitte in der Datenbank (gelten nach einem
  Neustart weiter), Schätzungen erst mit genug Werten und grob gerundet („≈ 3 Tage“). Jede laufende Tätigkeit steht
  da, „Alles gelesen“ nur, wenn nichts läuft. Zustand der Indexierung nennt alle („läuft (Dokumente) ·
  Bildauswertung läuft, 3 gleichzeitig · Embeddings laufen“), „Abschnitte … ohne Vektor“ mit Rate und Restzeit.
  `GET /api/reihenfolge` liefert die Zeiten (`current.seconds_*`, `eta_*`) sowie `vision` und `embeddings`.
- **Ladesymbol:** dreht gleichmäßig weiter – die Abfrage alle 4 s ersetzt nur noch den Inhalt der Karte, nicht das
  Symbol (vorher sprang es alle paar Sekunden auf den Anfang).
- Beim allerersten Start konnten Indexierung und Hintergrund-Threads gleichzeitig die Datenbank anlegen
  („table documents already exists“) – jetzt nacheinander.

- **Dokument-Betrachter:** Dokumente öffnen im Fenster der Anwendung statt auf einer neuen Seite – aus der
  Ordneransicht, den Anhängen, der Karte der Zeitleiste, den Quellen einer Antwort, den Suchtreffern und den
  Anhängen einer Mail. Rechts über der Seite (Handy: bildschirmfüllend) mit Dateiname, Ordner, Schließen (✕/Esc),
  ‹ › durch die Liste, „In neuem Fenster öffnen“/„Originaldatei herunterladen“ und „Textansicht“. PDF im
  PDF-Betrachter des Browsers (Quellen mit Seite öffnen auf der Seite), JPEG/PNG/GIF/WebP direkt mit Zoom per
  Klick, TIFF/BMP als vom Server umgewandelte Vorschau mit Seitenwahl (begrenzt: Größe vor dem Entpacken geprüft,
  eine Umwandlung zur Zeit), Mails, Word, OpenDocument und Text als gelesener Text mit Sprung zum zitierten
  Abschnitt. HTML/SVG nie aktiv, ausstehende oder fehlende Dateien mit Hinweis. Zurück im Browser schließt den
  Betrachter. Die Dokumentseite zeigt PDF/Bild/Vorschau über den Abschnitten (`?seite=N`). Ohne JavaScript wie
  bisher. Neu: `GET /dokumente/{id}/ansicht` (Fenster-Inhalt), `GET /dokumente/{id}/vorschau?seite=N`,
  `static/betrachter.js`, `projektakte/betrachter.py`.
- **Sicherheits-Header:** Originaldateien (`/dokumente/{id}/datei…`) dürfen von der Anwendung selbst eingebettet
  werden (`frame-ancestors 'self'`, `X-Frame-Options: SAMEORIGIN`; weiter ohne Skripte, außer PDF in der
  Sandbox); die Seiten erlauben Rahmen nur vom eigenen Ursprung (`frame-src 'self'`) und keine Plugins
  (`object-src 'none'`) und lassen sich weiter nirgends einbetten. HTML, SVG, XML und Skriptdateien werden als
  `application/octet-stream` zum Herunterladen ausgeliefert; GIF und WebP öffnen im Browser.

- **Reihenfolge beim Einlesen (Statusseite):** Neuer Bereich unter den bisherigen Karten. *Reihenfolge der
  Ordner*: die obersten Ordner unter `PROJECTS_DIR` mit „N von M Projekten fertig“, verschiebbar (▲/▼) und
  abschaltbar („aus“: ihre Projekte kommen zuletzt, werden aber gelesen); gespeichert in der Datenbank, gilt
  sofort und nach Neustarts; Voreinstellung mit der neuen Einstellung `INDEX_FOLDER_ORDER` (z. B. `3,2`).
  *Wird gerade gelesen*: Projekt, „x von y Dateien“, Phase, Balken und die nächsten drei Nummern, alle 4 s
  aktualisiert. *Projekte*: Reiter Warteschlange / Fertig / Alle mit Platz, Nummer, Ordner, Dateien und Stand
  („wartet“, „vorgezogen“, „wird gelesen“, „✓ fertig“ / „fertig, N übersprungen“ mit Zeitpunkt), Suche nach
  einem Teil der Nummer, höchstens 50 Zeilen; „Vorziehen“/„zurück“. Alles auch ohne JavaScript.
- **Der Dokumentendurchgang folgt dieser Reihenfolge:** vorgezogene Projekte, dann die noch nicht fertigen
  Ordner für Ordner (neueste Nummer zuerst; Bauvorhaben bei ihren Aufträgen), dann die fertigen zur Prüfung auf
  neue und geänderte Dateien. Je Projekt wird gespeichert, wann es vollständig gelesen wurde (mit Leseprofil)
  und was es nicht lesen konnte; eine neue oder geänderte Datei stellt es zurück in die Warteschlange, ein
  Neustart macht dort weiter. Änderungen auf der Statusseite gelten für alle noch nicht angefangenen Projekte des
  laufenden Laufs. Die Bildauswertung nimmt Dokumente vorgezogener Projekte zuerst, dann nach Ordnern (darin wie
  bisher neueste zuerst). DB-Migration 11 (nur hinzugefügt).
- **API:** `GET /api/reihenfolge`, `POST /api/reihenfolge/ordner`, `/vorziehen`, `/zurueck`.

- **Scans in zwei Stufen (Phase „Bildauswertung“):** Beim Einlesen ruft Projektakte das Bildmodell nicht mehr
  auf. Gescannte Seiten mit OCR-Textebene behalten zunächst ihren Text, Seiten ohne Textebene liest Tesseract
  (`tesseract-ocr` + `tesseract-ocr-deu`, jetzt im Docker-Image; fehlt es, wie bisher ohne). Das Dokument ist
  sofort durchsuchbar und als „Bildauswertung ausstehend: N Seite(n)“ markiert. Am Ende jedes Laufs liest die
  neue Phase „Bildauswertung“ die Seiten mit dem Modell – neueste Dokumente zuerst, höchstens
  `VISION_CONCURRENCY` Aufrufe gleichzeitig (Standard 3) und höchstens `VISION_PASS_SECONDS` je Lauf (Standard
  1800 s, danach weiter im nächsten Lauf). Das Modell bekommt Seitenbild **und** bisherigen Text und gibt die
  Seite vollständig zurück: OCR-Fehler berichtigt, Handschrift als „[handschriftlich: …]“, Stempel als
  „[Stempel: …]“ an der passenden Stelle. Neu: auch Scans **mit** Textebene gehen an das Modell (eine Seite mit
  Textebene gilt als Scan, wenn ihre Bilder mindestens 1 Megapixel haben und die halbe Seite bedecken) –
  handschriftliche Vermerke darauf gingen bisher verloren; digitale Seiten nie. Ältere, ohne Modell gelesene
  PDF-Dateien prüft die Phase nebenbei auf solche Seiten. Mail-Anhänge gehen denselben Weg. Statusseite und
  `/api/status` zeigen ausstehende Seiten und Dokumente, das Protokoll jede gelesene Seite. Die Seitenbilder
  bereitet der Leseprozess vor, Tesseract läuft dort mit dessen Grenzen. Extraktionsversion 7 (liest nur
  unvollständige Dokumente neu), DB-Migration 10 (Spalten und Tabelle `page_texts`, nur hinzugefügt).
  `LLM_VISION=0`: nur Tesseract, das Dokument bleibt unvollständig und wird mit `LLM_VISION=1` nachgelesen.
  Antwortlänge der Bildauswertung bis 4000 Token (ganze Seiten).

- **Neue Projektseite: Zeitleiste links, Rest rechts** (aus dem abgestimmten Entwurf): Die Zeitachse ist keine
  lange Liste mehr, sondern eine senkrechte Zeitleiste – oben neu, die letzten Wochen mit viel Platz, alte
  Jahre knapp (Wurzelkurve). Punkte an den Daten, Zeilen mit Linien dazu, Etiketten für dichte Stellen,
  Randmarken für Wochen, Monate und Jahre, Filter Alle/Dokumente/Mails. Ein Klick zeigt die Karte des Eintrags
  oder die Einträge einer Strecke rechts oben (`?auswahl=d12`, `m5`, `z2024-01-01_2024-03-31`, `zohne`).
  Rechts darunter das Fragefeld und Reiter für Dokumente (Ordneransicht), Anhänge und Mails (je 200 Zeilen,
  dann „alle zeigen“). Breitere Seite (1400 statt 1100 px) nur für die Projektseite. Ohne JavaScript über
  Links, mit JavaScript nur der geänderte Teil (`/projekte/<Nr>/zeitleiste`, `/inhalt`).
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
- **Große Planscans:** Farbscans über 25 Megapixel (z. B. A1 mit 300 dpi) wurden mit „Bild zu groß zum Lesen
  (LimitReachedError …)“ abgelehnt – pypdf entpackt standardmäßig höchstens 75 MB je Bild. Jetzt bis 450 MB
  (A0 mit 300 dpi in Farbe; gemessen etwa 1 GB Speicher, 7 s, unter der Grenze des Leseprozesses). Dokumente,
  die an der alten Grenze scheiterten, werden einmal neu gelesen (Migration 12).
- Protokoll: „Bild nicht lesbar“ steht je Dokument und Ursache nur noch einmal (Streifenscans mit JBIG2 ohne
  `jbig2dec` meldeten es je Streifen); der Fortschritt nennt die echte Seitennummer („Seite 22 (2 von 5)“).
- `PROJECTS_TOP_FOLDERS` (z. B. `1,2,3,4,5,6,7,8,9`): nur diese obersten Ordner unter `PROJECTS_DIR` werden nach
  Projekten durchsucht; Vorlagen-, Archiv- und andere Ordner daneben nicht. Bekannte Projekte von dort werden inaktiv.
- 2131 Tests (2126 bestanden, 4 Integrationstests übersprungen, 1 xfail), neu `tests/test_bildauswertung_stufen.py`,
  `tests/test_index_reihenfolge.py`, `tests/test_dokument_betrachter.py`, `tests/test_grosse_plaene.py`,
  `tests/test_bildauswertung_parallel.py`, `tests/test_dokumentarten.py`, `tests/test_tippfehler.py` und `tests/test_oberste_ordner.py`.

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
