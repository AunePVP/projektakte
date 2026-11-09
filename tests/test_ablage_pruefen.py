"""scripts/ablage_pruefen.py: the office's filing rule (Ziffer/2 Ziffern/1234_Bauvorhaben/123456_Auftrag/
123456-Kategorie) and what Projektakte reads of it – one share with every kind of deviation."""
from __future__ import annotations

import csv
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("ablage_pruefen", ROOT / "scripts" / "ablage_pruefen.py")
ablage = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ablage)

CATS = ablage.CATEGORIES


def order(root: Path, rel: str, cats=CATS, files=True) -> Path:
    folder = root / rel
    number = folder.name[:6]
    for c in cats:
        (folder / f"{number}-{c}").mkdir(parents=True, exist_ok=True)
        if files:
            (folder / f"{number}-{c}" / "Bericht.pdf").write_bytes(b"x")
    return folder


def clean_share(root: Path) -> Path:
    order(root, "2/28/2842_Beratung Filiale/284200_Beratung Filiale")
    order(root, "2/28/2842_Beratung Filiale/284201_Zwischendecke Filiale")
    order(root, "1/15/1507_Kita Sonnenhof/150700_Brandschutzkonzept Kita")
    return root


def test_a_share_that_follows_the_rule_has_no_deviations(tmp_path, capsys):
    root = clean_share(tmp_path / "projekt")
    assert ablage.main([str(root)]) == 0
    out = capsys.readouterr().out
    assert "Projekte (Aufträge) gefunden:       3" in out and "Keine Abweichungen" in out


def test_every_deviation_is_found(tmp_path, capsys):
    root = clean_share(tmp_path / "projekt")
    site = root / "2/28/2842_Beratung Filiale"
    order(root, "2/28/2842_Beratung Filiale/Archiv/284205_Alter Auftrag")  # too deep: not found
    order(root, "2/28/2843_Halle/284301_Halle A")
    order(root, "2/29/2943_Halle Zwei/284301_Halle B")  # number twice
    (root / "2/28/2844_Lager/284401-Mail").mkdir(parents=True)  # a category without its order folder
    (root / "2/28/2844_Lager/284401-Mail/Mail.msg").write_bytes(b"x")
    (site / "Notiz.pdf").write_bytes(b"x")  # a file in the site folder: in no order
    (root / "Archiv").mkdir()
    (root / "2/30").mkdir(parents=True)  # level 2 not starting with the level-1 digit
    (root / "2/28/2845-Halle").mkdir(parents=True)  # dash instead of underscore
    order(root, "2/28/2950_Fremd/295000_Fremd")  # site number does not fit the 28 folder
    order(root, "2/28/284601_Direkt")  # order without a site folder (still read: depth 3)
    order(root, "2/28/2842_Beratung Filiale/284701_Anderes Bauvorhaben")  # order number does not fit the site
    o = root / "2/28/2842_Beratung Filiale/284201_Zwischendecke Filiale"
    (o / "284201-Fotos").mkdir()  # unknown category
    (o / "284299-Text").mkdir()  # category with another number
    (o / "Liesmich.txt").write_text("x")  # a file directly in the order
    order(root, "1/15/1508_Schule/150800_Teilweise", cats=("Mail", "Text"))  # categories missing
    order(root, "1/15/1509_Leer/150900_Leer", files=False)  # an order without files
    (root / "1/15/1510_Ohne Auftrag").mkdir(parents=True)  # a site without orders

    target = tmp_path / "abweichungen.csv"
    assert ablage.main([str(root), "--csv", str(target), "--beispiele", "50"]) == 1
    out = capsys.readouterr().out
    report = ablage.check(root)
    paths = {kind: [p for p, _ in items] for kind, items in report.issues.items()}

    assert paths["nicht_gefunden"] == ["2/28/2842_Beratung Filiale/Archiv/284205_Alter Auftrag"]
    assert paths["doppelt"] == ["2/29/2943_Halle Zwei/284301_Halle B"]
    assert paths["kategorie_als_projekt"] == ["2/28/2844_Lager/284401-Mail"]
    assert "2/28/2842_Beratung Filiale" in paths["dateien_ausserhalb"]
    # the files of an order that is not found are one entry (the order), not one per category folder
    assert ("2/28/2842_Beratung Filiale/Archiv/284205_Alter Auftrag", "8 Datei(en)") in \
        report.issues["dateien_ausserhalb"]
    assert paths["ebene1"] == ["Archiv"] and paths["ebene2"] == ["2/30"]
    assert report.issues["bauvorhaben_name"] == [("2/28/2845-Halle", "Trennzeichen nach der Nummer: „-“ statt „_“")]
    assert paths["bauvorhaben_nummer"] == ["2/28/2950_Fremd"]
    assert paths["auftrag_ohne_bauvorhaben"] == ["2/28/284601_Direkt"]
    assert paths["auftrag_nummer"] == ["2/28/2842_Beratung Filiale/284701_Anderes Bauvorhaben",
                                       "2/29/2943_Halle Zwei/284301_Halle B"]  # the duplicate is misfiled, too
    assert paths["kategorie_unbekannt"] == ["2/28/2842_Beratung Filiale/284201_Zwischendecke Filiale/284201-Fotos"]
    assert paths["kategorie_nummer"] == ["2/28/2842_Beratung Filiale/284201_Zwischendecke Filiale/284299-Text"]
    assert ("1/15/1508_Schule/150800_Teilweise", "Ausgang, Bilder, CAD, LV, Sonstiges, Tab") in \
        report.issues["kategorie_fehlt"]
    assert paths["dateien_im_auftrag"] == ["2/28/2842_Beratung Filiale/284201_Zwischendecke Filiale"]
    assert paths["auftrag_leer"] == ["1/15/1509_Leer/150900_Leer"]
    assert paths["bauvorhaben_leer"] == ["1/15/1510_Ohne Auftrag", "2/28/2844_Lager"]  # only a stray category
    # what Projektakte reads: the 6-digit orders up to level 4, the category folder as a project of its own
    assert report.counts["projekte"] == 10 and report.ext_in[".msg"] == 1
    assert "FEHLER  Auftrag wird von Projektakte nicht gefunden: 1" in out
    assert "HINWEIS Unbekannte Kategorie: 1" in out
    rows = list(csv.reader(target.open(encoding="utf-8-sig"), delimiter=";"))
    assert rows[0] == ["Schwere", "Art", "Pfad", "Detail"] and len(rows) > 15


def test_wrong_calls_and_unreadable_folders(tmp_path, capsys):
    assert ablage.main([str(tmp_path / "fehlt")]) == 2
    try:
        ablage.main([str(tmp_path), "--muster", "("])
    except SystemExit as exc:
        assert exc.code == 3
    else:
        raise AssertionError("an unusable pattern must be a wrong call")
    assert "Fehler: --muster ist kein gültiger regulärer Ausdruck" in capsys.readouterr().err
