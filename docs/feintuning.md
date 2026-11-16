# Feintuning eines Seitenlesers für die Bildauswertung

Diese Datei fasst ein einzelnes Thema zusammen: **Warum die Bildauswertung ein eigenes, feingetuntes Modell braucht,
was es liefern soll und wie wir dahin kommen.** Sie ist der Einstieg für einen Chat, der nur dieses Thema behandelt.
Allgemeines zu Projektakte: `UEBERGABE.md`, `README.md`, `DECISIONS.md`.

Stand: 16. November 2026, Branch `claude/lucid-cerf-h2058c`.

---

## 1. Das Problem

Projektakte liest gescannte Seiten in zwei Stufen. Stufe 1 übernimmt die Textebene der PDF (Tesseract ist beim Nutzer
ausgeschaltet, `TESSERACT=0`). Stufe 2, die **Bildauswertung**, schickt jede Scan-Seite als Bild an ein Sprachmodell
(heute `qwen35-4b`, vLLM auf einem DGX Spark) und ersetzt den Text der Seite durch dessen Antwort. Rund 160.000 Seiten
warten noch. Die Indexierung ruht, bis ein geeignetes Modell gefunden ist.

Was im Betrieb und in Tests schiefging, unabhängig vom Modell:

- **Erfundenes:** Auf einem Brandschutzplan „las“ ein Modell Räume, die es nicht gibt. Auf einer Berichtsseite erfand
  Qwen rund 20 „Stempel“, die den Inhalt zusammenfassten, und wiederholte sie bis zur Token-Grenze.
- **Schleifen:** Auf Seiten ohne Text (Produktfoto eines Datenblatts) schrieb ein OCR-Modell eine erfundene,
  hochzählende Liste von Fragen bis zur Grenze. Hochzählende Listen fängt keine Schleifenerkennung zuverlässig.
- **Irrelevantes:** Pläne ergaben Hunderte Fetzen (Maße, Flurstücke, Raumnamen), Fotos eine Bildbeschreibung
  („Es gibt keinen Text auf diesem Bild …“). Nichts davon gehört in den Suchindex.
- **Tempo:** Qwen schreibt im Betrieb rund 1.150 Antwort-Tokens je Seite, der Spark schafft so rund 800 Seiten pro Stunde.
- **Eine Textebene beweist nichts:** Bei Plänen lag manchmal Text darunter, der auf der Seite gar nicht zu sehen ist.

**Kern des Problems:** Eine einzige Anweisung „lies die Seite“ für alle Seitenarten führt bei kleinen Modellen zu
Erfundenem, Schleifen und Rauschen. Ein spezialisiertes OCR-Modell liest zwar sauber, befolgt aber keine Regeln wie „bei
Plänen nur das Schriftfeld“.

## 2. Was auf eine Seite gehört (festgelegt mit dem Nutzer)

Maßstab ist: **Was würde ein Mensch hinschreiben, der alle Dokumente von Hand indexiert?** Der Index soll Dokumente
finden und Fragen mit Beleg beantworten.

Grundregeln: nur was **sichtbar** auf der Seite steht, nichts erfinden, nichts beschreiben, nichts zusammenfassen;
Unleserliches als `[unleserlich]`; steht nichts Lesbares da, bleibt die Seite leer.

| Seitentyp | Hinschreiben | Nicht hinschreiben |
|---|---|---|
| **Text** (Brief, Bericht, Bescheid, Prüfbericht, Gutachten, Formular, Datenblatt) | den vollständigen Text wörtlich in Lesereihenfolge; Tabellen als Tabellen; Ankreuzfelder ☒/☐ mit Beschriftung | – |
| **Kopf- und Fußzeilen** | einmal je Dokument (Absender, Empfänger, Datum, Aktenzeichen, Betreff) – das Modell schreibt sie immer, die App entfernt Zeilen, die auf fast jeder Seite gleich wiederkehren | Wiederholungen, Seitenzahlen, Bank, Handelsregister |
| **Stempel** | nur inhaltlich bedeutsame, wörtlich: Genehmigung, Prüfvermerk, Eingang mit Datum | dekorative Firmenstempel; nie zusammenfassen oder erfinden |
| **Handschrift** | Vermerke („erledigt“, „i. O.“, Korrekturen, Daten) an ihrer Stelle als `[handschriftlich: …]`; `[Unterschrift]` | – |
| **Logos** | nichts | – |
| **Plan** | **Schriftfeld** (Planart, Objekt/Adresse, Geschoss/Bereich, Plan-Nr., Index, Datum, Maßstab, Verfasser), **Legende**, Genehmigungsstempel | Raumnamen, Maße, Achsen, Flurstücke |
| **Foto** | nur sichtbarer Text (Typenschilder, Zulassungsnummern, Beschriftungen) | Beschreibung des Fotos |
| **Leer** (Grafik, Hintergrund, Produktbild ohne Text) | nichts | – |

Das Modell schreibt in die **erste Zeile den Typ**: `TYP: Text`, `TYP: Plan`, `TYP: Foto` oder `TYP: Leer`. Damit kann
die Suche später nach Typ filtern, die App erkennt Fehleinordnungen, und `TYP: Leer` heißt sicher „bewusst nichts“.

Die Anweisung an das Modell steht wörtlich in `scripts/ocr_vorgabe.py` (`PROMPT`). Entwürfe, Korrektur, Training und
später der Betrieb nutzen genau diese eine Fassung.

## 3. Was die Tests ergeben haben

Vergleich auf 49 wartenden Scan-Seiten (ohne CAD- und Bilder-Ordner), schlichte Anweisung „Seite abschreiben“,
8 Anfragen gleichzeitig:

| Modell | Hardware | Ø Antwort-Tokens | Schleifen | abgeschnitten | Bemerkung |
|---|---|---|---|---|---|
| Qwen 3.5 4B (ohne Denken) | DGX Spark | 583 | 2 | 1 | hält sich an Anweisungen; beschreibt Bilder ohne Text |
| GLM-OCR (0,9B) | RTX 4080 | 558 | 0 | 3 | sauberer Leser; erfindet bei Bildern ohne Text und bei Plänen |
| PaddleOCR-VL 1.6 | RTX 4080 | 1.224 | 10 | 11 | ohne seine Layout-Erkennung unbrauchbar |
| TeleOCR | RTX 4080 | – | – | – | startet mit vLLM nicht, mit SGLang nur Zeichensalat |

Wichtig: Mit **eingeschaltetem Denken** brauchte Qwen 2.628 Tokens für 646 Zeichen – im Vergleich und im Betrieb immer
`{"chat_template_kwargs":{"enable_thinking":false}}` mitschicken.

Folgerung: Keines der fertigen Modelle hält die Tabelle aus Abschnitt 2 ein. Die Antwortlänge war bei Qwen und GLM-OCR
gleich; der Unterschied liegt im Verhalten, nicht im Lesen.

## 4. Die Lösung: Feintuning

Ein feingetuntes Modell lernt an Beispielen von euren Seiten **das Verhalten aus Abschnitt 2**: Seitentyp erkennen,
bei Text vollständig abschreiben, bei Plänen nur Schriftfeld und Legende, bei Fotos nur sichtbaren Text, bei Leerem
nichts. Ein Aufruf je Seite, keine eigene Klassifizierung davor.

- **Grundmodell:** offen. Sicherer Start ist Qwen (läuft schon, versteht Anweisungen, von den Trainingswerkzeugen am
  besten unterstützt). Vor dem Training 2–3 Kandidaten mit `PROMPT` auf den 49 Prüfseiten vergleichen (ohne Training)
  und dabei nach dem aktuellen Stand kleiner Modelle (2–4B) suchen. Die Trainingsdaten passen zu jedem Grundmodell.
- **Verfahren:** LoRA (nur ein kleiner Zusatz wird trainiert).
- **Hardware:** Training auf dem **DGX Spark** (128 GB, volle Genauigkeit BF16, Daten liegen dort; ARM-Prozessor –
  NVIDIA-PyTorch-Container, Werkzeug noch zu prüfen: LLaMA-Factory, ms-swift oder Unsloth). Die RTX 4080 (16 GB) reicht
  für Entwürfe, Korrektur und schnelle Versuche, für ein 4B-Modell nur mit 4-Bit (QLoRA).
- **CAD-Pläne** gehören in die Trainingsdaten, obwohl sie im Betrieb gerade ausgeschlossen sind (`VISION_SKIP_CAD`,
  `SKIP_READ_FOLDERS=CAD`). Liest das feingetunte Modell Pläne gut, können sie wieder dazu.

## 5. Ablauf und Werkzeuge

Alle Werkzeuge sind gebaut und getestet. Die Seiten sind echte Projektdaten: Sie bleiben im Büronetz, nichts kommt ins
Repository.

1. **Prüfsatz** (vorhanden): 49 Seiten in `ocr-test-2` auf dem Spark/PC, erzeugt mit
   `python -m projektakte.ocr_testseiten --anzahl 50 --ziel /data/ocr-test-2`. Diese Seiten dürfen **nie** ins Training.
2. **Trainingsseiten exportieren** (auf dem Spark, im Ordner `deploy`):
   ```bash
   docker compose exec app python -m projektakte.ocr_testseiten --training --anzahl 600 --ziel /data/training --ohne-seiten /data/ocr-test-2/seiten.json
   docker compose cp app:/data/training ./training
   ```
   Mischung: 55 % wartende Scans, 15 % CAD-Pläne, 15 % digitale PDF-Seiten, 15 % Fotos (`TRAINING_MIX`), je Dokument
   höchstens zwei Seiten; `seiten.json` nennt je Seite Herkunft und Art (`quelle`).
3. **Entwürfe** mit Qwen nach der Vorgabe (auf dem Spark; fortsetzbar):
   ```bash
   python3 ../scripts/ocr_entwurf.py --ordner training --modell "Qwen|http://localhost:8003/v1|qwen35-4b" --extra-body '{"chat_template_kwargs":{"enable_thinking":false}}' --parallel 8
   ```
   Schreibt `<Seite>.entwurf.txt`; Schleifen und abgeschnittene Antworten werden als leerer Entwurf abgelegt.
4. **Korrigieren** im Browser (auf dem PC; Ordner `training` und die Skripte `ocr_vorgabe.py`, `ocr_vergleich.py`,
   `ocr_korrektur.py` dorthin kopieren):
   ```powershell
   python ocr_korrektur.py --ordner training
   ```
   Dann `http://127.0.0.1:8765` öffnen: links das Bild, rechts der Entwurf, Typ wählen (Alt+1–4), „Speichern & weiter“
   (Strg+Enter), „nicht verwenden“ für unbrauchbare Seiten. Schreibt `<Seite>.soll.txt`. Aufwand grob 1–3 Minuten je
   Textseite, weniger bei Plänen und Fotos; ab rund 200 Seiten lohnt ein erster Trainingsversuch.
5. **Trainingsdaten ausgeben:**
   ```powershell
   python ocr_korrektur.py --ordner training --export training.jsonl
   ```
   Eine Zeile je Seite: `{"messages":[{"role":"user","content":"<image>…PROMPT…"},{"role":"assistant","content":"TYP: …"}],"images":["…"]}`
   (das Format von LLaMA-Factory und ms-swift; die Bildpfade sind absolut und müssen auf dem Trainingsrechner stimmen).
6. **Training** (noch zu bauen): Rezept für das gewählte Werkzeug auf dem Spark, LoRA auf das Grundmodell.
7. **Prüfen** (noch zu bauen bzw. zu erweitern): das feingetunte Modell mit `PROMPT` auf den 49 Prüfseiten, verglichen mit
   handkorrigierten Soll-Texten dieser Seiten (die 49 Seiten dafür ebenfalls korrigieren – aber nie trainieren). Maße:
   Typ richtig, Text vollständig, nichts erfunden, Schleifen, Antwort-Tokens.
8. **Einbau in Projektakte** (noch zu bauen): eigene Einstellungen für das Bildmodell (`VISION_URL`, `VISION_MODEL`),
   `PROMPT` statt der heutigen Anweisung, `TYP:` auswerten (Typ speichern, `Leer` als bewusst leere Seite), wiederkehrende
   Kopf- und Fußzeilen je Dokument entfernen. Fragen beantwortet weiter das große Modell.

## 6. Offene Entscheidungen

- Grundmodell (Abschnitt 4).
- Trainingswerkzeug auf dem Spark (ARM): LLaMA-Factory, ms-swift oder Unsloth – was dort zuverlässig läuft.
- Wie viele Seiten korrigiert werden (Start ab ~200, Ziel 300–800).
- Ob CAD-Pläne im Betrieb wieder dazukommen (nach dem Prüfen).
- Ob Tesseract wieder eingeschaltet wird – mit einem guten Seitenleser für alle Scans eher nicht nötig.

## 7. Dateien

| Datei | Zweck |
|---|---|
| `scripts/ocr_vorgabe.py` | die Anweisung (`PROMPT`), die Typen, `split`/`join` der Antwortform |
| `projektakte/ocr_testseiten.py` | Seiten exportieren (Prüfsatz; `--training` für Trainingsseiten) |
| `scripts/ocr_vergleich.py` | Modelle auf einem Seitenordner vergleichen (Tempo, Tokens, Schleifen, HTML nebeneinander) |
| `scripts/ocr_entwurf.py` | Entwürfe nach der Vorgabe erzeugen |
| `scripts/ocr_korrektur.py` | Korrigieren im Browser, Export als JSONL |
| `tests/test_ocr_vergleich.py`, `tests/test_ocr_training.py` | Tests dazu |
