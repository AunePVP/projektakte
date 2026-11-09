"""scripts/ablage_pruefen.py: the office's filing rule (Ziffer/2 Ziffern/1234_Bauvorhaben/123456_Auftrag/
123456<Trenner><Unterordner>) and what Projektakte reads of it. Folders without a number in front are
not part of the numbered filing and are ignored – except inside an order, where every subfolder must
start with the order's number."""
from __future__ import annotations

import csv
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("ablage_pruefen", ROOT / "scripts" / "ablage_pruefen.py")
ablage = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ablage)

CATS = ablage.CATEGORIES


def order(root: Path, rel: str, cats=CATS, files=True, extra=()) -> Path:
    folder = root / rel
    number = folder.name[:6]
    for c in [f"{number}-{c}" for c in cats] + list(extra):
        (folder / c).mkdir(parents=True, exist_ok=True)
        if files:
            (folder / c / "Bericht.pdf").write_bytes(b"x")
    return folder


def clean_share(root: Path) -> Path:
    order(root, "2/28/2842_Beratung Filiale/284200_Beratung Filiale")
    order(root, "2/28/2842_Beratung Filiale/284201_Zwischendecke Filiale", extra=["284201_Abgabe_FW"])
    order(root, "1/15/1507_Kita Sonnenhof/150700_Brandschutzkonzept Kita")
    # folders without a number in front are not part of the numbered filing – never reported
    for rel in ("@-Normen/ASR", "AHO Heft 17", "2/Damp", "2/28/2842_Beratung Filiale/Pläne"):
        (root / rel).mkdir(parents=True, exist_ok=True)
        (root / rel / "Dokument.pdf").write_bytes(b"x")
    (root / "Liste.xlsx").write_bytes(b"x")
    return root


def test_a_share_that_follows_the_rule_has_no_deviations(tmp_path, capsys):
    root = clean_share(tmp_path / "projekt")
    assert ablage.main([str(root)]) == 0
    out = capsys.readouterr().out
    assert "Projekte (Aufträge) gefunden:       3" in out and "Keine Abweichungen" in out
    assert "Dateien außerhalb von Aufträgen:    0" in out


def test_every_deviation_is_found(tmp_path, capsys):
    root = clean_share(tmp_path / "projekt")
    site = root / "2/28/2842_Beratung Filiale"
    lost = order(root, "2/28/2842_Beratung Filiale/1015_PDF_100303/284205_Alter Auftrag")  # in a numbered
    (lost / "284205-CAD" / "284205_Archiv").mkdir()  # inside the lost order: reported with it, not again
    order(root, "2/28/2843_Halle/284301_Halle A")
    dup = order(root, "2/29/2943_Halle Zwei/284301_Halle B")  # number twice (and misfiled)
    (dup / "284301-Bilder" / "284301_140306").mkdir()  # a dated photo folder in the skipped duplicate
    (root / "2/28/2844_Lager/284401-Mail").mkdir(parents=True)  # a category without its order folder
    (root / "2/28/2844_Lager/284401-Mail/Mail.eml").write_bytes(b"x")
    order(root, "1/18/1827_Siemens/182700_Konzept")
    (root / "1/18/1827_Siemens/231020_Bilder für Konformität").mkdir()  # a date read as an order
    (root / "1/18/1827_Siemens/231020_Bilder für Konformität/Foto.jpg").write_bytes(b"x")
    (site / "Notiz.pdf").write_bytes(b"x")  # a file in the site folder: in no order
    (root / "2/30").mkdir(parents=True)  # level 2 not starting with the level-1 digit
    (root / "2/28/2845-Halle").mkdir(parents=True)  # dash instead of underscore
    order(root, "2/28/2950_Fremd/295000_Fremd")  # site number does not fit the 28 folder
    order(root, "2/28/284601_Direkt")  # order without a site folder (still read: depth 3)
    o = root / "2/28/2842_Beratung Filiale/284201_Zwischendecke Filiale"
    (o / "Abgabe").mkdir()  # a subfolder without the order's number
    (o / "284299-Text").mkdir()  # a subfolder with another number
    (o / "Liesmich.txt").write_text("x")  # a file directly in the order
    order(root, "1/15/1509_Leer/150900_Leer", files=False)  # an order without files
    (root / "1/15/1510_Ohne Auftrag").mkdir(parents=True)  # a site without orders

    target = tmp_path / "abweichungen.csv"
    assert ablage.main([str(root), "--csv", str(target), "--beispiele", "50"]) == 1
    out = capsys.readouterr().out
    report = ablage.check(root)
    paths = {kind: [p for p, _ in items] for kind, items in report.issues.items()}

    assert paths["nicht_gefunden"] == ["2/28/2842_Beratung Filiale/1015_PDF_100303/284205_Alter Auftrag"]
    assert paths["doppelt"] == ["2/29/2943_Halle Zwei/284301_Halle B"]
    assert paths["fremde_nummer"] == ["1/18/1827_Siemens/231020_Bilder für Konformität",
                                      "2/29/2943_Halle Zwei/284301_Halle B"] or \
        paths["fremde_nummer"] == ["1/18/1827_Siemens/231020_Bilder für Konformität"]
    assert paths["kategorie_als_projekt"] == ["2/28/2844_Lager/284401-Mail"]
    assert paths["unterordner_ohne_nummer"] == ["2/28/2842_Beratung Filiale/284201_Zwischendecke Filiale/Abgabe"]
    assert paths["unterordner_nummer"] == ["2/28/2842_Beratung Filiale/284201_Zwischendecke Filiale/284299-Text"]
    outside = dict(report.issues["dateien_ausserhalb"])
    assert outside == {"2/28/2842_Beratung Filiale": "1 Datei(en)",
                       "2/28/2842_Beratung Filiale/1015_PDF_100303/284205_Alter Auftrag": "8 Datei(en)",
                       "2/29/2943_Halle Zwei/284301_Halle B": "8 Datei(en)"}  # the skipped duplicate
    assert paths["ebene2"] == ["2/30"]
    assert report.issues["bauvorhaben_name"] == [("2/28/2845-Halle", "Trennzeichen nach der Nummer: „-“ statt „_“")]
    assert paths["bauvorhaben_nummer"] == ["2/28/2950_Fremd"]
    assert paths["auftrag_ohne_bauvorhaben"] == ["2/28/284601_Direkt"]
    assert "2/28/2842_Beratung Filiale/1015_PDF_100303" in paths["auftrag_name"]
    assert paths["dateien_im_auftrag"] == ["2/28/2842_Beratung Filiale/284201_Zwischendecke Filiale"]
    assert paths["auftrag_leer"] == ["1/15/1509_Leer/150900_Leer"]
    assert paths["bauvorhaben_leer"] == ["1/15/1510_Ohne Auftrag", "2/28/2844_Lager", "2/28/2845-Halle"]
    # folders without a number in front are never reported
    reported = [p for items in paths.values() for p in items]
    assert not any(p.startswith(("@-Normen", "AHO", "2/Damp")) or p.endswith("/Pläne") for p in reported)
    assert "kategorie_unbekannt" not in paths and "kategorie_fehlt" not in paths  # only with --kategorien
    assert "FEHLER  Auftrag wird von Projektakte nicht gefunden: 1" in out
    assert "FEHLER  Ordner im Auftrag ohne Nummer vorne: 1" in out
    rows = list(csv.reader(target.open(encoding="utf-8-sig"), delimiter=";"))
    assert rows[0] == ["Schwere", "Art", "Pfad", "Detail"] and len(rows) > 15


def test_category_names_only_on_request(tmp_path):
    """With --kategorien: unknown subfolder names, and the categories most orders have but one lacks
    (an office whose newer template added "Ausgang": the old orders lack it – that is no deviation)."""
    root = tmp_path / "projekt"
    for i in range(4):
        order(root, f"2/28/284{i}_Alt {i}/284{i}00_Alt {i}", cats=[c for c in CATS if c != "Ausgang"])
    order(root, "2/28/2849_Neu/284900_Neu", extra=["284900_Abgabe_FW"])
    order(root, "2/28/2848_Knapp/284800_Knapp", cats=[c for c in CATS if c not in ("Ausgang", "LV")])
    assert not ablage.check(root).issues.get("kategorie_fehlt")
    report = ablage.check(root, categories=CATS)
    assert "Ausgang" not in report.standard and "LV" in report.standard
    assert report.issues["kategorie_fehlt"] == [("2/28/2848_Knapp/284800_Knapp", "LV")]
    assert report.issues["kategorie_unbekannt"] == [("2/28/2849_Neu/284900_Neu/284900_Abgabe_FW", "Abgabe_FW")]


def test_wrong_calls_and_unreadable_folders(tmp_path, capsys):
    assert ablage.main([str(tmp_path / "fehlt")]) == 2
    try:
        ablage.main([str(tmp_path), "--muster", "("])
    except SystemExit as exc:
        assert exc.code == 3
    else:
        raise AssertionError("an unusable pattern must be a wrong call")
    assert "Fehler: --muster ist kein gültiger regulärer Ausdruck" in capsys.readouterr().err
