# Entwurf: Bildauswertung über das eigene Seitenmodell (Nemotron Parse + Typ-Kopf)

Stand: 24. November 2026. Status: Entwurf, wartet auf Freigabe des Nutzers.

## 1. Absicht

Die Bildauswertung wechselt vom allgemeinen Modell (Qwen über `LLM_URL`) auf den eigenen Dienst auf der DGX Spark
(Repository `nemotron-tune`, `eigenbau/server.py`). Der Dienst ordnet jede Seite einem Typ zu
(Text / Plan / Foto / Leer / Unsicher) und schreibt nur Text-Seiten ab. Gründe: rund 0,3–0,6 s je Seite statt rund
50 s, keine erfundenen Inhalte auf Plänen und Fotos, ein Seitentyp für Filter und Anzeige. Fragen bleiben beim
großen Modell (`LLM_URL`).

## 2. Ziele, Nicht-Ziele, Fehlschläge

**Ziele**
- Mit `VISION_URL` liest das Seitenmodell die Seiten; ohne `VISION_URL` verhält sich alles wie heute.
- Jede Seite jeder PDF- und Bilddatei bekommt einen Seitentyp, dauerhaft gespeichert.
- Der Seitentyp ist in der Suche filterbar und wird angezeigt.
- Wiederkehrende Kopf- und Fußzeilen stehen je Dokument nur einmal im Index.

**Nicht-Ziele (bewusst nicht in diesem Schritt)**
- Seiten mit Schleife an ein anderes Modell schicken (nur Roadmap).
- Tabellen und Formeln des Rohtexts umformen (bleiben, wie das Modell sie liefert; Entscheidung später mit dem Nutzer).
- Word-, ODT-, Text-Dateien und Mailtexte als Seitenbilder lesen (sie haben keine Seitenbilder, bleiben wie heute).
- Ein Fake-Dienst für das Seitenmodell (`fake_ai` bleibt unverändert).

**Fehlschläge (auch wenn es technisch läuft)**
- Exakter Text einer echten Textebene wird durch gelesenen (fast exakten) Modelltext ersetzt.
- Ein Ausfall des Seitenmodells macht Dokumente unsuchbar oder hält das Einlesen an.
- Ohne `VISION_URL` ändert sich irgendein Verhalten.

## 3. Der Dienst (Vorgabe des Nutzers)

- `POST {VISION_URL}/chat/completions`, OpenAI-Format, Modell `nemotron-kmw`. Genau ein Bild je Anfrage als
  `image_url` mit `data:`-URL (base64). Text im Prompt wird ignoriert.
- Anfrage ohne Bild → Antwort `OK`. `GET /models`, `GET /health` (unter der Basis ohne `/v1`: Plätze, Warteschlange,
  Zähler). Optional Bearer-Schlüssel.
- Fehler: 400 (kaputtes Bild, http-URL, mehrere Bilder), 504 (Zeitlimit), 500 (GPU-Fehler, dann `/health` 503).
- Antwort: `content`, erste Zeile `TYP: Text|Plan|Foto|Leer|Unsicher`. Nur bei Text folgt der Rohtext. Zusätzlich
  `"kmw": {"typ", "kopf", "sicherheit", "tokens", "finish", "ms", "bild", "schleife", "schleifen"}`.
- Rohtext: Blöcke `<x_L><y_O>Inhalt<x_R><y_U><class_Art>`, durch Leerzeilen getrennt, Koordinaten 0–1. Der Inhalt
  enthält bereits Markdown (`# Überschrift`, `**fett**`) und maskierte Zeichen (`\<`). Klassen u. a. Text, Title,
  Section-header, Table, Page-header, Page-footer, Picture, Caption, List-item.
- `kmw.schleife = true`: Text ist schon gekürzt und enthält `[Schleife des Modells: N× wiederholt, Rest entfernt]`.
  Kein Fehler, kein zweiter Versuch.
- Bis 8 Text-Seiten gleichzeitig; Anfragen warten in der Warteschlange des Dienstes.

Format-Beispiel (erfunden):

```
TYP: Text
<x_0.0><y_0.0>Musterbüro GmbH · Beispielstadt<x_0.999><y_0.0164><class_Page-header>

<x_0.0918><y_0.0969># Prüfbericht Brandschutz<x_0.8428><y_0.1492><class_Section-header>

<x_0.0918><y_0.2516>Die Löschwassermenge beträgt 96 m³/h über 2 Stunden.<x_0.8906><y_0.2844><class_Text>

<x_0.001><y_0.9852>Seite 1 von 4<x_0.999><y_0.9992><class_Page-footer>
```

## 4. Wege und Wahl

1. **Eigener Client, bestehender Ablauf (gewählt):** neuer Client nur für das Seitenmodell; `VisionPass` behält
   Warteschlange, Reihenfolge, sha-Prüfung, Statusseite und Wiederholungen und bekommt eine Abzweigung für das neue
   Ergebnis.
2. **`LLMClient` erweitern:** weniger Code, vermischt aber Prompt, Schleifenerkennung und zweiten Versuch mit einem
   Dienst, der nichts davon braucht. Der Nutzer will einen eigenen Client.
3. **Neuer Ablauf neben dem alten:** sauber getrennt, verdoppelt aber rund 1.000 Zeilen Ablauf- und Statuslogik.

## 5. Vom Nutzer entschieden

- Eigene Einstellungen `VISION_URL` / `VISION_MODEL` / `VISION_TIMEOUT`, eigener Client neben dem LLM-Client.
- Seitenbilder wie beim Trainingsexport: 175 dpi, nicht drehen. Der Dienst verkleinert selbst auf höchstens 1664×2048.
  Gemeint ist dieselbe Aufbereitung wie in `ocr_testseiten` (`extract.page_images`): die Seite, wie ein Betrachter
  sie zeigt (also mit `/Rotate` der PDF), höchstens `MAX_IMAGE_PIXELS`, ohne zusätzliche Ausrichtungskorrektur.
  Bilddateien gehen unverändert an den Dienst, wie im Export ohne `--max-aufloesung`.
- **Alle** Seiten von PDF- und Bilddateien gehen ans Modell, auch Seiten mit Textebene. Ausnahme: Ordner, die über
  die Einstellungen ausgelassen sind.
- **Variante B:** Hat eine Seite eine echte Textebene, bleibt deren Text im Index; das Modell liefert dort nur den Typ
  (und den Rohtext zum Speichern). Der Modelltext ersetzt den Seitentext nur bei Scans (ohne Textebene oder mit
  OCR-Ebene unter einem Scanbild).
- Je Typ (bei Scan-Seiten): **Text** → umgewandelter Rohtext ersetzt den Text. **Plan / Foto** → kein Modelltext,
  Text aus Stufe 1 (Textebene/Tesseract) bleibt. **Leer** → Seite ohne Text. **Unsicher** → Text aus Stufe 1 bleibt,
  Seite als „Typ unsicher“ markiert, `kmw.kopf` ist der vermutete Typ.
- Es wird nur noch Text abgeschrieben; Pläne und Fotos nicht mehr (DECISIONS „Was auf eine Seite gehört“ anpassen).
- Keine OCR-Anweisung mitschicken (`vision_prompt` entfällt für dieses Modell).
- Schleife: Seite als „Schleife“ markieren, Text übernehmen, kein zweiter Versuch, `find_loop` nicht anwenden.
- **Rohtext wird vorerst dauerhaft gespeichert**, für jede gelesene Seite. Das kann später entfallen (in DECISIONS und
  Roadmap notieren).
- Kopf- und Fußzeilen, die im Dokument wiederkehren, bleiben nur einmal stehen.
- Alles wird neu gelesen. `docs/feintuning.md` wird gelöscht.
- Kein Test mit einem Fake-Dienst. Den Test gegen die Spark startet der Nutzer selbst.

## 6. Entwurf

### 6.1 Einstellungen (`config.py`)

| Variable | Voreinstellung | Bedeutung |
|---|---|---|
| `VISION_URL` | leer | Basis des Seitenmodells, z. B. `http://<spark>:8767/v1`. Leer: alles wie heute. |
| `VISION_MODEL` | `nemotron-kmw` | Modellname in der Anfrage. |
| `VISION_API_KEY` | leer | Bearer-Schlüssel, nur aus der Umgebung, nicht in `repr`. |
| `VISION_TIMEOUT` | 600 (wie heute) | Sekunden je Seite, einschließlich Wartezeit in der Warteschlange des Dienstes. |
| `VISION_DPI` | 175 mit `VISION_URL`, sonst 150 | Auflösung der Seitenbilder. |
| `VISION_CONCURRENCY` | 3 (wie heute) | Empfehlung für das Seitenmodell: 8 (env-Beispiel, README). |

Mit `VISION_URL` ist die Bildauswertung an, unabhängig von `LLM_VISION`. `LLM_VISION` entscheidet dann nichts mehr
über Seiten: Das große Modell bekommt nie ein Bild, auch nicht mit `LLM_VISION=1`, und jeder Weg, der eine Datei liest
(auch `store_document`, `reprocess_attachments`), stellt die Seiten zurück (zweistufig) statt das Modell direkt zu
rufen. `VISION_RETRY_BODY` wird mit `VISION_URL` nicht benutzt. Startwarnung, wenn `VISION_URL` gesetzt ist und
`VISION_DPI` ausdrücklich auf einem anderen Wert als 175 steht. Mit `AI_MODE=fake` wird `VISION_URL` ignoriert
(Warnung), damit Demo und Tests unverändert bleiben. *(Aufruf des Entwurfs.)*

### 6.2 Client (`ai/seitenmodell.py`)

- `SeitenmodellClient(url, model, timeout, api_key)` mit `read_page(image: bytes, mime: str) -> PageReading`.
  Eine Anfrage, ohne `stream`, ohne `temperature`/`max_tokens`/`extra_body`, ein leerer Text-Teil ist nicht nötig.
  HTTP über `ai/http.py` (Standardbibliothek, Proxy-Variablen ignoriert).
- `PageReading`: `typ`, `kopf`, `sicherheit`, `raw` (Rohtext ohne TYP-Zeile), `schleife`, `finish`, `ms`.
  `typ` kommt aus der TYP-Zeile; fehlt sie, aus `kmw.typ`. Fehlt beides oder ist der Wert unbekannt, ist die Antwort
  ein Fehler (`AIServiceError`), die Seite wird später erneut versucht.
- Fehler: 400 → dauerhaft „nicht auswertbar“ (eigene Ausnahme, kein neuer Versuch, Text aus Stufe 1 bleibt).
  504, 500, Netz, Zeitlimit → Fehler mit Wartezeit wie heute (`retry_delay`).
- `probe(timeout)` für `/health/deps`: `GET /models`, Anfrage ohne Bild (erwartet `OK`), `GET /health` des Dienstes
  (Plätze, Warteschlange als Info). `last_ok_at`, `last_error` wie bei den anderen Clients („ausgelastet“).
- `AIServices` bekommt `vision: SeitenmodellClient | None`; `check()` zeigt dann einen vierten Dienst
  „bildauswertung“. Die Statusseite zeigt ihn mit.
- Der Client wird nur im App-Prozess benutzt (aus `VisionPass`), nie im Leseprozess.

### 6.3 Speichern (Migration 20)

Neue dauerhafte Tabelle, `page_texts` bleibt Zwischenspeicher wie heute:

```sql
CREATE TABLE page_types (
    doc_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page       INTEGER NOT NULL,
    typ        TEXT NOT NULL,              -- Text | Plan | Foto | Leer | Unsicher
    kopf       TEXT,                       -- kmw.kopf: vermuteter Typ (wichtig bei Unsicher)
    sicherheit REAL,
    schleife   INTEGER NOT NULL DEFAULT 0,
    finish     TEXT,                       -- kmw.finish (stop | length | …)
    source     TEXT NOT NULL DEFAULT 'modell',  -- modell | cad (ohne Modell: Plan) | render (leer gezeichnet: Leer)
    layer      INTEGER NOT NULL DEFAULT 0, -- 1: Seite hat eine echte Textebene, deren Text gilt (Variante B)
    raw        TEXT,                       -- Rohtext des Modells (nur Typ Text), vorerst dauerhaft
    PRIMARY KEY (doc_id, page)
);
CREATE INDEX page_types_typ ON page_types(typ, doc_id, page);
```

Eine Zeile je Seite, die das Modell gesehen hat, dazu CAD-Seiten (6.4) und Seiten, die PDFium leer zeichnet
(`typ = Leer`, `source = render`). Ohne Zeile bleiben Scan-Seiten jenseits von `MAX_DOC_VISION_CALLS` und Seiten, die
der Dienst mit 400 ablehnt. Die Zeilen eines Dokuments werden gelöscht, wenn die Datei mit
anderem Inhalt (sha256) neu gelesen wird; bei gleichem Inhalt bleiben sie und die Seiten gelten als gelesen
(ersetzt für das Seitenmodell das „known“ aus `page_texts`). Bilddateien haben Seite 1.

Seiten ohne Zeile sind „nicht eingeordnet“ (Word, ODT, Mails, ausgelassene Ordner, noch nicht gelesen).

### 6.4 Welche Seiten ans Modell gehen (`extract.py`, Stufe 1)

Mit Seitenmodell listet Stufe 1 **alle** Seiten einer PDF als ausstehend (`Extracted.vision_pages`) und merkt, welche
eine echte Textebene haben (neue Liste `layer_pages`, in `page_texts` als neue Spalte `layer`). Stufe 1 bleibt sonst
gleich: Textebene bzw. Tesseract sofort durchsuchbar.

- „Echte Textebene“ = Seite mit mindestens 20 lesbaren Zeichen, die **kein** Scan mit OCR-Ebene ist (`_is_scan`
  falsch, nicht `_garbled`) – die vorhandene Unterscheidung.
- Ausgelassen wie heute: `SKIP_READ_FOLDERS` (wird gar nicht gelesen), `VISION_SKIP_FOLDERS` (Fotos nicht ans
  Modell), in der Statusseite abgeschaltete Ordner.
- `VISION_SKIP_CAD=1` gilt weiter, aber enger gefasst – **offene Frage an den Nutzer, siehe Abschnitt 8**. Bis
  dahin gilt: CAD-Seite = zeichnet kein Bild **und** hat keine echte Textebene (das ist die heutige Bedeutung, denn
  heute kommen nur Seiten ohne Textebene dorthin). Sie geht nicht ans Modell und bekommt `typ = Plan`,
  `source = cad`. Seiten mit echter Textebene gehen immer ans Modell – sonst würde jede am Rechner erzeugte
  Textseite (zeichnet auch kein Bild) als Plan eingeordnet.
- `MAX_DOC_VISION_CALLS` (500 Seiten je Dokument) gilt weiter für Scan-Seiten. Seiten mit echter Textebene zählen
  nicht dagegen und machen ein Dokument nie unvollständig; die Grenze `MAX_DOC_PIXELS` je Aufruf des Leseprozesses
  bleibt. *(Aufruf des Entwurfs.)*
- Dokumentstatus: Warten nur noch Seiten mit echter Textebene auf ihren Typ, ist das Dokument **nicht** unvollständig
  und zeigt nicht „Bildauswertung ausstehend“ als Mangel (Meldung „Seitentyp ausstehend: N Seite(n)“, `incomplete`
  nur wegen Scan-Seiten). Die Warteschlange (`vision_pending`) wird dafür mitbenutzt. Lehnt der Dienst eine solche
  Seite mit 400 ab oder scheitert sie dauerhaft, bleibt sie ohne Typ; das Dokument bleibt vollständig.
  *(Aufruf des Entwurfs.)*
- Der direkte Weg (Modellaufruf im Leseprozess, `defer_vision=False`) wird mit dem Seitenmodell nicht benutzt; mit
  Seitenmodell läuft alles zweistufig über `VisionPass`.

### 6.5 Ergebnis je Seite (`vision.py`)

`VisionPass._call` ruft mit Seitenmodell `read_page` für das eine Seitenbild (kein Prompt, kein bekannter Text).
Ergebnis wird sofort in `page_types` und `page_texts` festgehalten (eine Transaktion, sha-Prüfung wie heute):

| Seite | Typ | Text der Seite danach | Vermerk |
|---|---|---|---|
| echte Textebene | jeder | Textebene bleibt | Typ gespeichert |
| Scan | Text | umgewandelter Rohtext | – |
| Scan | Text, `schleife` | umgewandelter Rohtext (gekürzt) | „Schleife“ |
| Scan | Text, `finish = length` | umgewandelter Rohtext | „abgeschnitten“ (unvollständig, kein neuer Versuch) |
| Scan | Text, Rohtext leer | Text aus Stufe 1 bleibt | „kein Text“ |
| Scan | Plan / Foto | Text aus Stufe 1 bleibt | – |
| Scan | Unsicher | Text aus Stufe 1 bleibt | „Typ unsicher (vermutlich …)“ |
| Scan | Leer | leer | – |

Eine Seite mit echter Textebene, die das Modell „Leer“ nennt, behält ihren Text (eine Fehleinordnung darf keinen
exakten Text löschen). *(Aufruf des Entwurfs.)*

Nicht für das Seitenmodell: `find_loop`, `_loop_check`, zweiter Versuch, `vision_prompt`, `page_tokens`.

### 6.6 Umwandlung des Rohtexts (`seitentext.py`, reine Funktionen)

- `parse_blocks(raw) -> list[Block(x0, y0, x1, y1, klasse, text)]`: Blöcke per Muster; Text außerhalb von Blöcken
  bleibt als Block der Klasse `Text` ohne Koordinaten erhalten (nichts geht verloren).
- `page_text(blocks) -> str`: Reihenfolge wie geliefert (Lesereihenfolge des Modells), Marken entfernt, Markdown
  bleibt, `\<`-Maskierungen aufgelöst, Blöcke durch Leerzeile getrennt. `Picture`-Blöcke ohne Text entfallen.
  Der Vermerk `[Schleife des Modells: …]` wird aus dem Indextext entfernt (die Markierung steht in `page_types`).
  Tabellen bleiben unverändert.
- `# …`-Überschriften beginnen wie bisher einen neuen Abschnitt (`chunking.py`).

### 6.7 Kopf- und Fußzeilen (beim Zusammensetzen des Dokuments)

In `_write_document`, über alle Seiten des Dokuments, deren Text vom Modell stammt:
- Schlüssel eines Blocks der Klasse `Page-header` / `Page-footer`: Text kleingeschrieben, Ziffern durch `#` ersetzt,
  Leerraum zusammengefasst.
- Kommt ein Schlüssel auf **mindestens 3 Seiten und mindestens der Hälfte** dieser Seiten vor, bleibt der Block nur
  beim ersten Vorkommen stehen, auf allen anderen Seiten entfällt er.
- Der Rohtext in `page_types.raw` bleibt vollständig; die Regel lässt sich ohne neues Lesen ändern.
- Seiten mit echter Textebene behalten ihre Kopf- und Fußzeilen wie heute (Folge von Variante B).

Weil Umwandlung und Kopfzeilenregel beim Zusammensetzen aus `raw` laufen, kann ein späterer Befehl alle Dokumente
ohne das Modell neu zusammensetzen. Der Befehl selbst gehört nicht zu diesem Schritt.

### 6.8 Neu lesen

Das Extraktionsprofil bekommt mit Seitenmodell eine Marke (`…:seitenmodell:<Modell>`), `EXTRACT_VERSION` steigt.
PDF- und Bilddateien, deren Profil die Marke nicht trägt, sind beim nächsten Lauf fällig – auch vollständige
(anders als beim bisherigen Modellwechsel, weil sie keinen Seitentyp haben). Reihenfolge wie beim Einlesen.
Das wiederholt Stufe 1 für diese Dateien, einschließlich Tesseract für Scans; Seiten, die Qwen gelesen hatte, haben
bis zum Seitenmodell wieder den Text aus Stufe 1. Vom Nutzer so gewollt („wir lesen alles eh neu“). Vollständige
PDF- und Bild-Anhänge von Mails werden genauso fällig (aus dem Anhang-Cache).
Ein späterer Wechsel des Modellnamens liest vollständige Dokumente nicht von selbst neu (wie heute).

### 6.9 Suche und Anzeige

- `hybrid_search(..., page_types=[...])`: Treffer nur aus Abschnitten, deren Seite einen der Typen hat
  (`chunks.doc_id, chunks.page` gegen `page_types`); Wert `ohne` = nicht eingeordnet. Gilt für Volltext und Vektor.
- Suchseite: Auswahl „Seitentyp“ (Alle · Text · Plan · Foto · Unsicher · nicht eingeordnet), als normales
  Formularfeld (funktioniert ohne JavaScript). API: `GET /api/search?...&page_type=Plan`.
- Treffer und Quellen: kleines Etikett am Seitenhinweis, nur wenn nicht „Text“: „S. 3 · Plan“, „S. 7 · Typ unsicher
  (vermutlich Plan)“, „S. 9 · Schleife“.
- Dokumentseite: eine Zeile „Seiten: 12 Text, 3 Plan, 1 Foto, 2 leer, 1 unsicher, 1 Schleife“.
- Statusseite, Karte „Wird gerade gelesen“: Zähler je Typ der in dieser Runde gelesenen Seiten; Dienst
  „Bildauswertung“ bei den KI-Diensten.
- Fragen (`ask.py`) filtern nicht nach Seitentyp.

Skizze Suchseite:

```
Suche: [ rauchabzug treppenraum        ] Projekt: [ alle ▾ ] Seitentyp: [ Alle ▾ ] [Suchen]

1. Pruefbericht_2019.pdf · S. 3 · Plan            Projekt 284201
   … Schriftfeld: Treppenraum T2, Rauchabzug …
2. Gutachten.pdf · S. 12                           Projekt 284201
   … Der Rauchabzug im Treppenraum wird …
```

### 6.10 Tests

- Reine Funktionen ohne Dienst: Umwandlung (`seitentext.py`) an erfundenen Rohtexten im echten Format,
  Kopf-/Fußzeilenregel, TYP-Zeile/`kmw` auswerten, Einstellungen, Migration, Suchfilter.
- Ablaufregeln aus 6.5 in `VisionPass` mit einem kleinen Python-Objekt als Stellvertreter des Clients, das feste
  `PageReading`s zurückgibt – kein HTTP-Server, nichts in `fake_ai`. *(Aufruf des Entwurfs: Ohne das bliebe die
  Regel „Leer löscht Text nur bei Scans“ ungetestet.)*
- `tests/test_seitenmodell_real.py`, Marke `integration`, läuft nur mit `PROJEKTAKTE_INTEGRATION=1` und `VISION_URL`:
  Modellliste, Anfrage ohne Bild → `OK`, `/health`, eine Demo-Textseite (aus den erfundenen Demo-PDFs, 175 dpi) →
  `TYP: Text` mit bekannten Wörtern und gültigen Blöcken, eine weiße Seite → `Leer`, `kmw`-Felder vorhanden, kaputtes
  Bild → 400. Startet der Nutzer im Büronetz.
- Bestehende Suite bleibt grün (`sh scripts/test.sh`); die bisherigen Tests der Bildauswertung laufen weiter über
  `LLM_VISION` und die Fake-KI.

### 6.11 Dokumente

- `DECISIONS.md`: neuer Abschnitt „Eigenes Seitenmodell“ (Variante B und warum, Rohtext vorerst gespeichert,
  Kopf-/Fußzeilenregel, 175 dpi, kein Fake-Dienst als Ausnahme zu Punkt 1, kein zweiter Versuch); „Was auf eine Seite
  gehört“ anpassen (nur Text wird abgeschrieben, Typ `Unsicher`).
- `ROADMAP.md`: Seiten mit Schleife an ein anderes Modell; Tabellen/Formeln umformen; Rohtext später evtl. verwerfen;
  Modelltext auch für Seiten mit Textebene (Variante A) als Option; Befehl „neu zusammensetzen ohne Modell“.
- `README.md`, `deploy/projektakte.env.example`, `docs/ki-dienste.md`, `CHANGELOG.md`, `UEBERGABE.md` (Branch,
  Abschnitte 7b und 8). `docs/feintuning.md` löschen, Verweise darauf entfernen.
- Keine echten Firmendaten: Beispiele und Testtexte sind erfunden.

## 7. Dem Erbauer überlassen

- Namen der Hilfsfunktionen, Aufteilung der Commits (ein Commit je Thema, deutsche Titel).
- Ob `page_texts.layer` oder eine eigene Liste den Unterschied Textebene/Scan trägt.
- Wortlaut der deutschen Meldungen und Startwarnungen.
- Bildformat an den Dienst: die vorhandene Aufbereitung (`for_model`, JPEG/PNG); TIFF/BMP-Bilddateien vor dem Senden
  nach PNG wandeln, falls der Dienst sie mit 400 ablehnt.

## 8. Offene Frage an den Nutzer

**CAD-Pläne mit echter Textebene.** Viele CAD-Exporte haben echten Text (Beschriftungen) und zeichnen kein Bild.
Unter „alle Seiten ans Modell“ würden sie gezeichnet (12–30 s je Seite, gemessen im November) und dann vom Modell als
Plan eingeordnet. Möglichkeiten: (a) so lassen – jede Seite mit Textebene geht ans Modell, große Pläne kosten Zeit;
(b) mit `VISION_SKIP_CAD=1` Seiten ohne Bild, die größer als A3 sind, ohne Modell als Plan einordnen; (c) wie (b),
aber schon ab größer als A4.
