# Feintuning eines Seitenlesers – verschoben nach `aunepvp/tune`

Das Thema „feingetunter Seitenleser für die Bildauswertung“ hat ein eigenes Repository: **`aunepvp/tune`**. Dort stehen
in `CLAUDE.md` Problem, Vorgabe je Seitentyp (Text/Plan/Foto/Leer), Messungen, Entscheidungen, Ablauf, Befehle und
nächste Schritte, dazu die Werkzeuge (Vorgabe, Vergleich, Entwürfe, Korrektur mit JSONL-Export).

In Projektakte bleibt:
- **der Export der Seiten** (`projektakte/ocr_testseiten.py`), weil er die Datenbank, den abgesicherten Leseprozess und
  die Bildaufbereitung der App braucht:
  ```bash
  docker compose exec app python -m projektakte.ocr_testseiten --anzahl 50 --ziel /data/ocr-test-2
  docker compose exec app python -m projektakte.ocr_testseiten --training --anzahl 600 --ziel /data/training --ohne-seiten /data/ocr-test-2/seiten.json
  ```
  Nach einer Änderung der Bildaufbereitung die Seitenbilder eines Exports unter denselben Nummern neu schreiben
  (Texte und Korrekturen bleiben):
  ```bash
  docker compose exec app python -m projektakte.ocr_testseiten --erneuern --ziel /data/training
  docker compose exec app python -m projektakte.ocr_testseiten --erneuern --ziel /data/ocr-test-2
  ```
- **später der Einbau des fertigen Modells**: eigene Einstellungen `VISION_URL`/`VISION_MODEL`, die Anweisung aus
  `tune/scripts/ocr_vorgabe.py` (genau diese Fassung), die `TYP:`-Zeile auswerten und speichern, wiederkehrende Kopf- und
  Fußzeilen je Dokument entfernen.

Begründung der Vorgabe: `DECISIONS.md`, „Was auf eine Seite gehört“.
