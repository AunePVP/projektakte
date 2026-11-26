# Seitenmodell (Nemotron Parse + Typ-Kopf) – Umsetzungsplan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Die Bildauswertung liest Seiten über den eigenen Dienst (`VISION_URL`), speichert je Seite einen Typ und
übernimmt Modelltext nur für Scan-Seiten; Fragen bleiben beim großen Modell.

**Architecture:** Ein eigener Client (`ai/seitenmodell.py`) neben dem `LLMClient`. Stufe 1 (`extract.py`) stellt mit
Seitenmodell alle PDF-Seiten zurück und merkt, welche eine echte Textebene haben; `VisionPass` ruft den Client, hält
Typ und Rohtext dauerhaft in `page_types` fest und setzt das Dokument aus dem Rohtext zusammen (reine Funktionen in
`seitentext.py`, dort auch die Kopf-/Fußzeilenregel). Ohne `VISION_URL` läuft kein neuer Code.

**Tech Stack:** Python 3.12, SQLite (Migration 20), `urllib` über `ai/http.py`, Jinja2/HTMX, pytest.

**Spec:** `docs/superpowers/specs/2026-11-24-seitenmodell-design.md` (maßgeblich; dieser Plan nennt nur die
Entscheidungen zur Umsetzung).

## Global Constraints

- Ohne `VISION_URL` ändert sich kein Verhalten; mit `AI_MODE=fake` wird `VISION_URL` ignoriert (Startwarnung).
- Kein Fake-Dienst: nichts in `projektakte/fake_ai`, kein HTTP-Server in Tests für das Seitenmodell.
- Der große `LLMClient` bekommt mit `VISION_URL` nie ein Bild (`vision=False`), auch nicht mit `LLM_VISION=1`.
- Für das Seitenmodell: kein Prompt, kein `find_loop`, kein `_loop_check`, kein zweiter Versuch, kein `stream`.
- Seitenbilder: vorhandene Aufbereitung (`extract.page_images`), `VISION_DPI` 175 mit `VISION_URL`.
- Code-Kommentare und Docstrings Englisch; alles Sichtbare Deutsch. Typnamen genau `Text`, `Plan`, `Foto`, `Leer`,
  `Unsicher`.
- Keine echten Firmendaten: Testtexte erfunden (Musterstadt, `.example`).
- Nach jedem Task `sh scripts/test.sh` grün; ein Commit je Task, deutscher Titel. `tests/eval_holdout.json` nie öffnen.
- Keine neuen Abhängigkeiten.

## Review Focus

1. Rohtext mit Text außerhalb von Blöcken oder mit abgeschnittenem letzten Block (`finish = length`) → nichts geht
   verloren, keine Marken im Indextext (Task 1).
2. Antwort mit HTTP 200, aber ohne brauchbare TYP-Zeile und ohne `kmw.typ` → Fehler mit neuem Versuch, kein Typ
   geraten (Task 2).
3. Seite mit echter Textebene, Modell sagt `Leer` oder scheitert dauerhaft → Text bleibt, Dokument bleibt vollständig
   (Task 5).
4. Datei wird während der Bildauswertung geändert (anderes sha256) → alte `page_types`-Zeilen weg, alte Antwort
   verworfen (Tasks 4 und 5).
5. Dokument mit weniger als 3 Seiten oder mit Kopfzeile, die sich nur in Ziffern unterscheidet → erstes: nichts
   entfernt; zweites: nur das erste Vorkommen bleibt (Task 1).

---

### Task 1: Rohtext umwandeln, Kopf- und Fußzeilen (`seitentext.py`)

**Files:**
- Create: `projektakte/seitentext.py`
- Test: `tests/test_seitentext.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) Block: klasse: str; text: str; box: tuple[float, float, float, float] | None`
  - `parse_blocks(raw: str) -> list[Block]`
  - `block_key(block: Block) -> str` (kleingeschrieben, jede Ziffernfolge → `#`, Leerraum zusammengefasst)
  - `repeated_keys(pages: list[list[Block]]) -> set[str]`
  - `page_text(blocks: list[Block], drop: frozenset[str] | set[str] = frozenset()) -> str`
  - `document_texts(raws: dict[int, str]) -> dict[int, str]` (Seite → Indextext, Regel angewandt)
  - `LOOP_NOTE: re.Pattern` für `[Schleife des Modells: N× wiederholt, Rest entfernt]`

- [ ] **Step 1: Tests schreiben** (`tests/test_seitentext.py`), Rohtexte im Format aus Spec Abschnitt 3:
  - `test_blocks_in_order_with_class_and_box`: zwei Blöcke → Klassen `Page-header`, `Text`; `box == (0.0918, 0.2516, 0.8906, 0.2844)`.
  - `test_text_outside_blocks_is_kept`: `"lose Zeile\n\n<x_0.1><y_0.1>A<x_0.2><y_0.2><class_Text>"` → erster Block
    `Block("Text", "lose Zeile", None)`.
  - `test_cut_off_last_block_is_kept`: Rohtext endet mit `<x_0.1><y_0.5>abgeschnittener Sat` → letzter Block Text
    `"abgeschnittener Sat"`, `box is None`.
  - `test_page_text_strips_marks_keeps_markdown`: Ergebnis enthält `# Prüfbericht Brandschutz` und `**fett**`, kein
    `<x_`, kein `<class_`; Blöcke durch genau eine Leerzeile getrennt.
  - `test_unescapes_angle_brackets`: `An Max \<m@buero.example>` → `An Max <m@buero.example>`.
  - `test_empty_picture_dropped`, `test_loop_note_removed_from_text`.
  - `test_table_block_unchanged`: Inhalt eines `Table`-Blocks kommt zeichengleich zurück.
  - `test_repeated_header_kept_once`: 4 Seiten, Kopf `Musterbüro GmbH`, Fuß `Seite N von 4` → in `document_texts`
    stehen beide nur auf Seite 1.
  - `test_two_pages_nothing_removed`; `test_header_on_two_of_six_pages_stays`;
    `test_only_header_and_footer_classes_count`: gleicher `Text`-Block auf allen Seiten bleibt überall.
- [ ] **Step 2:** `sh scripts/test.sh tests/test_seitentext.py` → FAIL (Modul fehlt).
- [ ] **Step 3: Implementieren.** Blockmuster:
  `<x_([\d.]+)><y_([\d.]+)>(.*?)<x_([\d.]+)><y_([\d.]+)><class_([^>]+)>` mit `re.DOTALL`; Text zwischen den Treffern
  und nach dem letzten wird zu `Block("Text", …, None)`, führende Koordinaten-Marken eines abgeschnittenen Restes
  entfernt. `repeated_keys`: nur Klassen `Page-header`/`Page-footer`; Schlüssel auf mindestens 3 Seiten **und**
  mindestens `len(pages) / 2` Seiten. `document_texts`: je Schlüssel bleibt der Block auf der ersten Seite, auf der
  er vorkommt.
- [ ] **Step 4:** Tests grün, ganze Suite grün.
- [ ] **Step 5:** Commit „Seitenmodell: Rohtext in Seitentext umwandeln, wiederkehrende Kopf- und Fußzeilen“.

### Task 2: Einstellungen, Client, `/health/deps`

**Files:**
- Create: `projektakte/ai/seitenmodell.py`, `tests/test_seitenmodell_client.py`, `tests/test_seitenmodell_real.py`
- Modify: `projektakte/config.py`, `projektakte/ai/services.py`, `projektakte/ai/llm.py`,
  `projektakte/extract_worker.py` (`_Reporting`), `deploy/projektakte.env.example`, Statusseiten-Vorlage der KI-Dienste
- Test: `tests/test_config.py`, `tests/test_health_deps.py` (ergänzen)

**Interfaces:**
- Produces:
  - `Settings.vision_url: str`, `.vision_model: str` (Voreinstellung `nemotron-kmw`), `.vision_api_key: str`
    (`repr=False`); `Settings.vision_url == ""` mit `AI_MODE=fake`.
  - `TYPES = ("Text", "Plan", "Foto", "Leer", "Unsicher")`
  - `@dataclass PageReading: typ: str; kopf: str | None; sicherheit: float | None; raw: str; schleife: bool; finish: str | None; ms: int`
  - `reading_from(data: dict) -> PageReading` (rein; `AIServiceError` ohne gültigen Typ)
  - `class PageRejected(AIServiceError)` (HTTP 400: dauerhaft)
  - `class SeitenmodellClient(url, model, *, timeout, api_key="")` mit `read_page(image: bytes, mime: str) -> PageReading`,
    `probe(timeout: float) -> dict`, `last_ok_at`, `last_error`, `last_error_at`
  - `AIServices.vision: SeitenmodellClient | None`; `check()` liefert dann `services["bildauswertung"]`
  - `LLMClient(..., page_model: str = "")`, in `config()`/`from_config`; `_Reporting.page_model`
  - `config.apply_vision_env(settings) -> None`: setzt `os.environ["VISION_DPI"] = "175"`, wenn `vision_url` gesetzt
    und `VISION_DPI` nicht in der Umgebung steht; aufgerufen in `AIServices.from_settings`.

- [ ] **Step 1: Tests.**
  - `test_config.py`: ohne `VISION_URL` → `vision_url == ""`; mit → Schrägstrich am Ende entfernt, Modell
    `nemotron-kmw`; `AI_MODE=fake` + `VISION_URL` → `vision_url == ""` und eine Warnung mit „VISION_URL“;
    `VISION_URL` + `VISION_DPI=150` → Warnung mit „175“; Schlüssel nicht in `repr(settings)`.
  - `test_seitenmodell_client.py` (nur `reading_from`, kein HTTP): Antwort wie im Spec → `typ == "Text"`, `raw` ohne
    TYP-Zeile, `schleife is False`; `content == "TYP: Plan"` → `raw == ""`; TYP-Zeile fehlt, `kmw.typ == "Foto"` →
    `"Foto"`; TYP-Zeile `Unsicher`, `kmw.kopf == "Plan"` → `kopf == "Plan"`; `content == "OK"` ohne `kmw` →
    `AIServiceError`; `TYP: Tabelle` → `AIServiceError`; `kmw.schleife` wahr → `schleife is True`.
  - `test_health_deps.py`: ohne `VISION_URL` hat `check()` genau die drei bisherigen Dienste.
  - `test_seitenmodell_real.py`: `pytestmark` = `integration` + `skipif` ohne `PROJEKTAKTE_INTEGRATION=1` oder ohne
    `VISION_URL`. Fälle laut Spec 6.10; Seitenbilder aus den Demo-PDFs über `extract.page_images` bei 175 dpi, weiße
    Seite als erzeugtes PNG 1240×1754, kaputtes Bild = 20 Zufallsbytes → `PageRejected`.
- [ ] **Step 2:** neue Tests → FAIL.
- [ ] **Step 3: Implementieren.** `read_page`: `POST {url}/chat/completions`, Körper nur `model` und `messages` mit
  einem `image_url`-Teil (`data:{mime};base64,…`). `HTTPStatusError` mit Status 400 → `PageRejected`. `probe`:
  `GET {url}/models`, `POST` ohne Bild (`content` muss mit `OK` beginnen → `ok`), `GET` `{url ohne /v1}/health` als
  `info["dienst"]` (Fehler dabei nur als Hinweis). `AIServices.from_settings`: mit `vision_url` wird der `LLMClient`
  mit `vision=False, page_model=s.vision_model` gebaut. `busy_note` auch für den vierten Dienst.
- [ ] **Step 4:** Suite grün (Integrationstest wird übersprungen).
- [ ] **Step 5:** Commit „Seitenmodell: Einstellungen VISION_URL/VISION_MODEL, eigener Client, Prüfung in /health/deps“.

### Task 3: Tabelle `page_types` (Migration 20)

**Files:**
- Create: `projektakte/seitentypen.py`, `tests/test_seitentypen.py`
- Modify: `projektakte/db.py`

**Interfaces:**
- Produces:
  - Migration 20: `page_types` wie Spec 6.3, dazu `ALTER TABLE page_texts ADD COLUMN layer INTEGER NOT NULL DEFAULT 0`.
  - `seitentypen.store(conn, doc_id: int, page: int, *, typ: str, kopf=None, sicherheit=None, schleife=False, finish=None, source="modell", layer=False, raw=None) -> None` (INSERT OR REPLACE, kein Commit)
  - `seitentypen.clear(conn, doc_id: int) -> None`
  - `seitentypen.known(conn, doc_id: int) -> dict[int, sqlite3.Row]`
  - `seitentypen.counts(conn, doc_id: int) -> dict[str, int]` (Schlüssel: Typnamen und `"Schleife"`)
  - `seitentypen.summary(counts: dict[str, int]) -> str` → `"12 Text, 3 Plan, 1 Foto, 2 leer, 1 unsicher, 1 Schleife"` (leere Posten fehlen; `""` ohne Zeilen)
  - `seitentypen.label(row) -> str | None` → `None` für Text ohne Schleife, sonst `"Plan"`, `"Foto"`,
    `"Typ unsicher (vermutlich Plan)"`, `"Schleife"`

- [ ] **Step 1: Tests:** Migration auf frischer DB (`PRAGMA user_version == 20`), `store` zweimal für dieselbe Seite
  ersetzt, Löschen des Dokuments löscht die Zeilen (CASCADE), `summary`- und `label`-Texte genau wie oben.
- [ ] **Step 2–4:** FAIL → implementieren → Suite grün (Tests, die `user_version` prüfen, anpassen).
- [ ] **Step 5:** Commit „Seitentyp je Seite speichern (Tabelle page_types, Migration 20)“.

### Task 4: Stufe 1 – alle Seiten zurückstellen

**Files:**
- Modify: `projektakte/extract.py`, `projektakte/documents.py`, `projektakte/indexer.py` (falls dort `skip_photos`
  betroffen), `projektakte/mails/` nur soweit `reprocess_attachments` es braucht
- Test: `tests/test_seitenmodell_stufe1.py`

**Interfaces:**
- Consumes: `LLMClient.page_model` (Task 2), `seitentypen.store/clear/known` (Task 3).
- Produces:
  - `extract.page_model(llm) -> str` (`getattr(llm, "page_model", "") or ""`)
  - `EXTRACT_VERSION = 9`; `extraction_profile(llm)` mit Seitenmodell → `f"v9:seitenmodell:{name}{'+ocr' if ocr else ''}"`;
    `profile_key` lässt auch diesen Modellnamen weg.
  - `Extracted.layer_pages: list[int]` (Seiten in `vision_pages` mit echter Textebene) und
    `Extracted.cad_pages: list[int]` (ohne Modell als Plan eingeordnet).
  - `page_texts.layer` gefüllt; `store_page_texts(..., layer: set[int] = frozenset())`.
  - `documents.pending_message(note, pages, *, types_only: bool = False)` → mit `types_only`
    `"Seitentyp ausstehend: N Seite(n)"`.

- [ ] **Step 1: Tests** (PDFs mit den Schreibern aus `projektakte/demo/writers.py`, Stellvertreter-`llm` =
  `types.SimpleNamespace(vision=False, page_model="nemotron-kmw", model="x")`):
  - `test_all_pages_pending_with_page_model`: 3-seitige Text-PDF → `vision_pages == [1, 2, 3]`,
    `layer_pages == [1, 2, 3]`, `status == "ok"`, Text der Seiten vorhanden.
  - `test_scan_page_is_not_a_layer_page`: Scan-PDF aus den Demo-Daten → Seite in `vision_pages`, nicht in `layer_pages`.
  - `test_without_page_model_unchanged`: dieselbe Text-PDF mit `llm=None` → `vision_pages == []`.
  - `test_cad_rule_only_without_text_layer` (mit `VISION_SKIP_CAD=1`): Seite ohne Bild und ohne Textebene → in
    `cad_pages`, nicht in `vision_pages`; Seite ohne Bild mit echter Textebene (auch A1) → in `vision_pages`.
  - `test_profile_marks_page_model`: `extraction_profile(...) .startswith("v9:seitenmodell:nemotron-kmw")`.
  - `test_document_with_only_layer_pages_is_complete`: nach `write_document` → `incomplete == 0`,
    `vision_pending == 3`, `message == "Seitentyp ausstehend: 3 Seite(n)"`, `page_texts.layer == 1` für alle Seiten.
  - `test_cad_pages_get_type_plan`: `page_types`-Zeile `typ == "Plan"`, `source == "cad"`.
  - `test_same_content_keeps_types`: zweites `write_document` mit gleichem sha256 und Profil → `page_types` bleibt,
    diese Seiten nicht mehr `pending`; anderes sha256 → `page_types` des Dokuments leer.
  - `test_complete_pdf_without_mark_is_due`: `reprocess_due` für Zeile mit `incomplete = 0`, Profil `v8:text+ocr`,
    `ext = ".pdf"`, neues Profil mit Seitenmodell → `True`; für `.docx` → `False`; ohne Seitenmodell → `False`.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3: Implementieren.**
  - `_pdf_pages`: mit `page_model(llm)` immer der zurückgestellte Weg (auch ohne `defer`); `vision_pages` = alle
    Seiten außer CAD-Seiten; `layer_pages` = Seiten in `pages`, die nicht in `layer_scans` sind.
    `MAX_DOC_VISION_CALLS` zählt nur Nicht-Ebenen-Seiten. CAD (Variante a): die vorhandene Regel (`vision_skip_cad()`,
    Seite ohne Textebene zeichnet kein Bild) bleibt und füllt `cad_pages`; in `_images_of` darf sie mit Seitenmodell
    Ebenen-Seiten nicht treffen (`page_images` bekommt dafür die Ebenen-Seiten genannt).
  - `_image` (Bilddateien): mit Seitenmodell immer zurückgestellt, Seite 1, nie Ebenen-Seite.
  - `write_document`: `known` mit Seitenmodell aus `seitentypen.known` statt aus `page_texts`; `incomplete` und
    Meldung nur wegen Seiten ohne Ebene; `seitentypen.clear` bei anderem sha256 oder Profil; CAD-Zeilen schreiben.
  - `reprocess_due(row, profile, now, limits)`: zusätzlich fällig, wenn das neue Profil `:seitenmodell:` trägt, das
    gespeicherte nicht, und `row["ext"]` `.pdf` oder ein Bildformat (`extract.IMAGE_EXT`) ist. Aufrufer müssen `ext`
    mitlesen; vollständige PDF-/Bild-Anhänge in `reprocess_attachments` einschließen.
- [ ] **Step 4:** Suite grün.
- [ ] **Step 5:** Commit „Seitenmodell: alle PDF- und Bildseiten für die Bildauswertung vormerken (Stufe 1)“.

### Task 5: Bildauswertung mit dem Seitenmodell (`vision.py`)

**Files:**
- Modify: `projektakte/vision.py`, `projektakte/background.py`, `projektakte/indexer.py`
- Test: `tests/test_seitenmodell_ablauf.py`

**Interfaces:**
- Consumes: `SeitenmodellClient.read_page`, `PageReading`, `PageRejected` (Task 2); `seitentypen.*` (Task 3);
  `seitentext.document_texts` (Task 1); `page_texts.layer` (Task 4).
- Produces:
  - `VisionPass(conn, llm, *, page_model=None, …)`; `run_vision(conn, llm, settings, *, progress=None, page_model=None)`;
    `VisionWorker(settings, llm, *, page_model=None, live=None)`; `VisionWorker.wanted(llm, page_model=None)`.
  - `VisionLive.snapshot()["types"]: dict[str, int]` (in dieser Runde gelesene Seiten je Typ).
  - `stats["types"]` im Ergebnis von `VisionPass.run()`.

- [ ] **Step 1: Tests** mit Stellvertreter `class Stub: def read_page(self, image, mime): …` (gibt `PageReading`s
  nach Seitenreihenfolge zurück oder wirft), ohne HTTP; Dokumente über `write_document` aus Task 4:
  - `test_scan_text_replaces_stage1_text`: Abschnitt enthält den umgewandelten Text, kein `<x_`; `page_types.raw`
    gleich dem Rohtext; `documents.method == "vision"`.
  - `test_layer_page_keeps_layer_text`: Typ `Text` mit anderem Rohtext → Abschnittstext = Textebene, `raw` gespeichert,
    `layer == 1`.
  - `test_layer_page_typed_leer_keeps_text`.
  - `test_scan_leer_has_no_text`: Stufe-1-Text vorhanden, Typ `Leer` → kein Abschnitt für die Seite.
  - `test_plan_and_foto_keep_stage1_text`; `test_unsicher_keeps_text_and_kopf` (`kopf == "Plan"`).
  - `test_loop_page_marked_no_second_call`: `schleife=True` → `schleife == 1`, Text ohne den Vermerk, Stub genau
    einmal je Seite gerufen.
  - `test_finish_length_marks_partial`: Dokument `incomplete == 1`, Meldung enthält „abgeschnitten“, kein neuer Versuch
    (`retry_after IS NULL`).
  - `test_rejected_page_is_final`: `PageRejected` → Seite `skipped`, kein `page_types`-Eintrag; bei Ebenen-Seite
    bleibt das Dokument vollständig.
  - `test_service_error_retries_later`: `AIServiceError` → `state == "failed"`, `retry_after` gesetzt.
  - `test_blank_render_is_leer`: von PDFium leer gezeichnete Seite → `typ == "Leer"`, `source == "render"`.
  - `test_repeated_header_once_in_document`: 4 Scan-Seiten mit gleichem `Page-header` → genau ein Abschnitt des
    Dokuments enthält ihn.
  - `test_changed_file_drops_answer`: sha256 zwischen Vorbereiten und Speichern geändert → keine `page_types`-Zeile.
  - `test_big_model_gets_no_image`: `llm` mit `describe_image`, das `AssertionError` wirft → Lauf endet ohne Fehler.
  - `test_page_texts_dropped_page_types_stay`: nach Abschluss `page_texts` leer, `page_types` vollständig.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3: Implementieren.**
  - `_call`: mit `page_model` genau ein `read_page` für das eine Seitenbild; Rückgabe trägt das `PageReading`.
  - `_store`: Typ und Rohtext in derselben Transaktion wie `page_texts` (nach der sha-Prüfung). Seitentext nach
    Tabelle Spec 6.5; für Scan-Seiten vom Typ Text wird in `page_texts.text` vorläufig `seitentext.page_text` der
    einzelnen Seite geschrieben (das Dokument ist schon vor seiner letzten Seite lesbar).
  - `_write_document`: mit `page_model` die Texte aller Scan-Seiten vom Typ Text aus `page_types.raw` über
    `seitentext.document_texts` neu bilden (Kopf-/Fußzeilenregel über das ganze Dokument); Ebenen-Seiten, Plan, Foto,
    Unsicher aus `page_texts.text`; Leer (Scan) ohne Text. `incomplete`/Meldung: Ebenen-Seiten zählen nie.
  - `_prepare`: Problem „Seite leer“ → `seitentypen.store(typ="Leer", source="render")`.
  - `run()`: mit `page_model` kein `_loop_check`, kein `_recheck_one`.
  - `background.py`, `indexer.py`, `web.py` (Stelle bei Zeile ~1608): `ai.vision` durchreichen; Worker läuft, wenn
    `page_model` gesetzt ist.
- [ ] **Step 4:** Suite grün (bestehende Bildauswertungs-Tests unverändert grün).
- [ ] **Step 5:** Commit „Bildauswertung über das Seitenmodell: Typ je Seite, Modelltext nur für Scans“.

### Task 6: Suche und Anzeige

**Files:**
- Modify: `projektakte/search.py`, `projektakte/api.py`, `projektakte/web.py`, `projektakte/webtext.py`,
  Vorlagen unter `projektakte/templates/` (Suchseite, Treffer/Quellen, Dokumentseite, Statuskarte)
- Test: `tests/test_seitentyp_suche.py`

**Interfaces:**
- Consumes: `seitentypen.label/counts/summary` (Task 3), `VisionLive.snapshot()["types"]` (Task 5).
- Produces:
  - `hybrid_search(..., page_types: Sequence[str] = ())`; erlaubte Werte `TYPES` und `"ohne"`.
  - `Hit.page_type: str | None`, `Hit.page_label: str | None`; in `Hit.to_dict()` als `page_type`, `page_label`.
  - `GET /api/search?…&page_type=Plan` (mehrfach erlaubt; unbekannter Wert → 422); Suchseite: Formularfeld `seitentyp`.

- [ ] **Step 1: Tests** (DB von Hand gefüllt: Abschnitte auf Seiten mit Typ Text, Plan, Unsicher, ohne Zeile):
  `test_filter_plan_only`, `test_filter_ohne_returns_untyped_and_mail_sections`, `test_no_filter_returns_all`,
  `test_filter_applies_to_vector_candidates` (Treffer nur über den Vektor kommt mit falschem Typ nicht zurück),
  `test_hit_label` (`"Typ unsicher (vermutlich Plan)"`), `test_api_rejects_unknown_page_type` (422),
  `test_search_page_shows_select_and_label` (HTML enthält `name="seitentyp"` und `S. 3 · Plan`),
  `test_document_page_shows_summary` (enthält `Seiten: 1 Text, 1 Plan`).
- [ ] **Step 2:** FAIL.
- [ ] **Step 3: Implementieren.** Filter als SQL-Bedingung auf `c.doc_id, c.page` gegen `page_types` in `fts_search`,
  `identifier_search` und beim Laden der Vektorkandidaten (dort nach `load_hits` filtern, bevor der Reranker läuft).
  Auswahl der Suchseite: Alle · Text · Plan · Foto · Unsicher · nicht eingeordnet. Etikett am Seitenhinweis nur,
  wenn `page_label` gesetzt ist. Statuskarte „Wird gerade gelesen“: Zeile mit `seitentypen.summary` der Runde;
  Dienst „Bildauswertung“ in der Liste der KI-Dienste. `ask.py` bleibt ohne Filter.
- [ ] **Step 4:** Suite grün.
- [ ] **Step 5:** Commit „Suche: Filter und Anzeige des Seitentyps“.

### Task 7: Dokumentation

**Files:**
- Modify: `DECISIONS.md`, `ROADMAP.md`, `README.md`, `CHANGELOG.md`, `UEBERGABE.md`, `docs/ki-dienste.md`,
  `deploy/projektakte.env.example`, `deploy/README.md` (falls dort `LLM_VISION` für Scans empfohlen wird)
- Delete: `docs/feintuning.md` (und Verweise darauf)

- [ ] **Step 1:** Inhalte laut Spec 6.11. Zusätzlich festhalten:
  - DECISIONS, neuer Abschnitt „Eigenes Seitenmodell für die Bildauswertung“: Variante B mit Begründung (exakter Text
    der Textebene; Lesefehler am Beispiel doppelter spitzer Klammern, ohne echte Namen), Rohtext **vorerst** dauerhaft
    gespeichert und später verzichtbar, CAD Variante a (alle Seiten mit Textebene ans Modell, `VISION_SKIP_CAD` nur für Seiten ohne Textebene; Risiko Zeichendauer), Kopf-/Fußzeilenregel und
    ihre Grenze (nur Scan-Seiten), kein Fake-Dienst als Ausnahme zu Zusammenfassung Punkt 1, Dokumentstatus bei
    Ebenen-Seiten. „Was auf eine Seite gehört“: nur Text wird abgeschrieben, Plan/Foto behalten Stufe 1, Typ `Unsicher`.
  - ROADMAP: neuer Abschnitt mit offenen Punkten: Seiten mit Schleife an ein anderes Modell; Tabellen/Formeln
    umformen (mit dem Nutzer entscheiden, Vorlage `postprocess_document()` aus `nvidia/NVIDIA-Nemotron-Parse`);
    Rohtext später evtl. verwerfen; Modelltext auch für Seiten mit Textebene (Variante A); Befehl „Dokumente ohne
    Modell neu zusammensetzen“.
  - README: Abschnitt „Scans in zwei Stufen“ um das Seitenmodell ergänzen, Tabelle der Einstellungen
    (`VISION_URL`, `VISION_MODEL`, `VISION_API_KEY`, `VISION_DPI` 175, Empfehlung `VISION_CONCURRENCY=8`), Suche
    (Seitentyp), Aufruf des echten Tests:
    `PROJEKTAKTE_INTEGRATION=1 VISION_URL=http://<spark>:8767/v1 sh scripts/test.sh -m integration tests/test_seitenmodell_real.py`.
  - UEBERGABE: Stand, Branch `claude/epic-tesla-5b8tm7`, Abschnitt 7b (neuer Betrieb), Abschnitt 8 Punkt 5, letzte
    Migration 20, Hinweis: Ablauf gegen den echten Dienst vom Nutzer noch nicht gemessen.
- [ ] **Step 2:** `grep -rn "feintuning" .` ohne Treffer außerhalb von CHANGELOG; Suite grün.
- [ ] **Step 3:** Commit „Dokumentation: Seitenmodell, Seitentyp, Roadmap; docs/feintuning.md entfernt“.
