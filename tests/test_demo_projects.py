import hashlib
import re
import zipfile

from projektakte.config import DEFAULT_PROJECT_NUMBER_PATTERN
from projektakte.demo import ensure, generate, is_current
from projektakte.demo.data import PROJECTS


def _hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_generation_is_deterministic(tmp_path):
    generate(tmp_path / "a")
    generate(tmp_path / "b")
    a, b = _hashes(tmp_path / "a"), _hashes(tmp_path / "b")
    assert a == b and len(a) > 30
    # regenerating in place changes nothing
    generate(tmp_path / "a")
    assert _hashes(tmp_path / "a") == a


def test_projects_structure(tmp_path):
    generate(tmp_path)
    root = tmp_path / "projekte"
    folders = sorted(p.name for p in root.iterdir())
    assert len(folders) >= 8
    pattern = re.compile(DEFAULT_PROJECT_NUMBER_PATTERN)
    assert all(pattern.match(f) for f in folders)
    kinds = {p["kind"] for p in PROJECTS}
    assert {"Schule", "Pflegeheim", "Bürogebäude", "Tiefgarage", "Hotel", "Industriehalle", "Kita",
            "Wohnhochhaus"} <= kinds
    assert (root / "BS-2024-0137 Grundschule Am Lindenhof/01 Genehmigung/Baugenehmigung.pdf").exists()
    suffixes = {p.suffix for p in root.rglob("*") if p.is_file()}
    assert {".pdf", ".docx", ".md", ".txt"} <= suffixes
    for folder in root.iterdir():  # every project has all four formats
        assert {".pdf", ".docx", ".md", ".txt"} <= {p.suffix for p in folder.rglob("*") if p.is_file()}


def test_known_facts_are_in_the_files(tmp_path):
    generate(tmp_path)
    gs = tmp_path / "projekte/BS-2024-0137 Grundschule Am Lindenhof"
    pdf = (gs / "01 Genehmigung/Baugenehmigung.pdf").read_bytes()
    assert b"Auflage 7 \\(Rettungswege\\):" in pdf
    assert "Außentreppe".encode("cp1252") in pdf
    docx = zipfile.ZipFile(tmp_path / "projekte/BS-2023-0777 Industriehalle Nordring/02 Konzept/"
                           "Brandschutzkonzept.docx").read("word/document.xml").decode()
    assert "192 m³/h über 2 Stunden" in docx
    md = (tmp_path / "projekte/BS-2023-0412 Pflegeheim Sonnenhang/03 Prüfung/Prüfbericht Nr. 3.md")
    assert "Feststellanlage der Rauchschutztür T30-RS" in md.read_text()


def test_ensure_only_generates_when_missing(tmp_path):
    assert not is_current(tmp_path)
    assert ensure(tmp_path) is True
    assert is_current(tmp_path)
    assert ensure(tmp_path) is False


def test_file_dates_follow_document_dates(tmp_path):
    generate(tmp_path)
    f = tmp_path / "projekte/BS-2024-0137 Grundschule Am Lindenhof/01 Genehmigung/Baugenehmigung.pdf"
    import datetime as dt
    assert dt.datetime.fromtimestamp(f.stat().st_mtime, dt.timezone.utc).date() == dt.date(2024, 3, 12)
