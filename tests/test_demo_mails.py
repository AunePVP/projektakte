import email
import json
import re
from email import policy
from pathlib import Path

import pytest

from projektakte.config import DEFAULT_PROJECT_NUMBER_PATTERN
from projektakte.demo import generate
from projektakte.demo.generate import eval_set

NUM = re.compile(DEFAULT_PROJECT_NUMBER_PATTERN)


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    root = tmp_path_factory.mktemp("demo")
    return root, generate(root)


def _parse(path: Path):
    msg = email.message_from_bytes(path.read_bytes(), policy=policy.default)
    body = msg.get_body(("plain", "html"))
    text = body.get_content() if body else ""
    names = [a.get_filename() or "" for a in msg.iter_attachments()]
    return msg, text, names


def test_mail_counts_and_categories(demo):
    root, manifest = demo
    mails = manifest["mails"]
    assert len(mails) >= 60
    assert len(list((root / "mails").glob("*.eml"))) == len(mails)
    cats = [m["category"] for m in mails]
    assert cats.count("subject") > len(mails) / 2
    for c in ("body", "attachment", "content", "unrelated"):
        assert cats.count(c) >= 4, c
    assert sum(1 for m in mails if m["attachments"]) >= 10


def test_categories_match_where_the_number_is(demo):
    root, manifest = demo
    for meta in manifest["mails"]:
        msg, text, names = _parse(root / meta["file"])
        subject = str(msg["Subject"])
        in_subject = bool(NUM.search(subject))
        in_body = bool(NUM.search(text))
        in_names = any(NUM.search(n) for n in names)
        cat = meta["category"]
        if cat == "subject":
            assert in_subject and meta["project"] in subject
        elif cat == "body":
            assert not in_subject and in_body and not in_names
        elif cat == "attachment":
            assert not in_subject and not in_body and in_names
        else:  # content / unrelated: no number anywhere
            assert not (in_subject or in_body or in_names), meta
        if cat == "unrelated":
            assert meta["project"] is None
        else:
            assert NUM.fullmatch(meta["project"])


def test_typical_senders(demo):
    root, manifest = demo
    senders = " ".join(str(_parse(root / m["file"])[0]["From"]) for m in manifest["mails"])
    for who in ("bauaufsicht@", "pruefung@wendehals-pi", "feuerwehr", "architekten", "sonnenhang"):
        assert who in senders


def test_category_counts_are_stable(demo):
    """Other tests (assignment quality) rely on these numbers."""
    _, manifest = demo
    cats = [m["category"] for m in manifest["mails"]]
    numbered = sum(cats.count(c) for c in ("subject", "body", "attachment"))
    assert (numbered, cats.count("content"), cats.count("unrelated")) == (61, 8, 7)


def _date(root, meta):
    from email.utils import parsedate_to_datetime
    return parsedate_to_datetime(str(_parse(root / meta["file"])[0]["Date"]))


def test_dates_fit_the_texts(demo):
    """DEMO-12: no 'Newsletter November' in March, no summer party in January, local summer time."""
    root, manifest = demo
    by_subject = {m["subject"]: m for m in manifest["mails"]}
    assert _date(root, by_subject["Newsletter November: neue Muster-Richtlinien und Termine"]).month == 11
    assert _date(root, by_subject["Einladung Sommerfest"]).month in (5, 6, 7, 8)
    try:
        from zoneinfo import ZoneInfo
        berlin = ZoneInfo("Europe/Berlin")
    except Exception:  # noqa: BLE001 – no tz database on this system
        berlin = None
    for meta in manifest["mails"]:
        d = _date(root, meta)
        if berlin is not None:  # offsets follow German summer/winter time
            assert d.utcoffset() == d.replace(tzinfo=None).replace(tzinfo=berlin).utcoffset(), meta["subject"]
        _, text, _ = _parse(root / meta["file"])
        if "eröffnen" in text or "Eröffnung" in text:  # opening of the car park lies ahead
            assert "September" in text and d.month <= 8, meta["subject"]
        if "31. des Monats" in text:
            assert d.month in (1, 3, 5, 7, 8, 10, 12)


def test_eval_json_is_in_sync_with_generator():
    data = json.loads((Path(__file__).parent / "eval.json").read_text(encoding="utf-8"))
    assert data == eval_set()
    assert len(data) >= 45
    cats = {q["category"] for q in data}
    assert {"fact", "paraphrase", "abbreviation", "stand", "two_source", "enumeration", "cross", "negative",
            "trap"} <= cats
    assert len({q["id"] for q in data}) == len(data)
    for q in data:
        assert q["question"] and isinstance(q["sources"], list)
        assert q.get("not_found") or (q["sources"] and (q["answer_contains"] or q.get("answer_regex")))


def test_eval_answers_are_not_in_the_question():
    import re
    for q in eval_set():
        question = q["question"].lower()
        assert not any(a.lower() in question for a in q["answer_contains"]), q["id"]
        assert not any(re.search(p, q["question"], re.I) for p in q.get("answer_regex", [])), q["id"]
        assert not all(re.search(p, q["question"], re.I) for p in q.get("answer_all", []) or ["(?!)"]), q["id"]


def test_eval_has_the_vision_question_for_every_project():
    projects = {q["project"] for q in eval_set() if q["question"] == "Was ist der letzte Stand mit dem Bauherrn?"}
    assert len(projects) == 8


def _attachment_text(root, meta, filename) -> str:
    msg = email.message_from_bytes((root / meta["file"]).read_bytes(), policy=policy.default)
    for att in msg.iter_attachments():
        if att.get_filename() == filename:
            from projektakte.extract import extract
            return extract(Path(filename), att.get_payload(decode=True)).text
    return ""


def test_eval_mail_facts_exist(demo):
    """The first source of every question contains one of the expected answer strings."""
    root, manifest = demo
    by_subject = {m["subject"]: m for m in manifest["mails"]}
    for q in eval_set():
        if not q["sources"] or not q["answer_contains"]:
            continue
        src = q["sources"][0]
        project = src.get("project") or q["project"]
        if "mail_subject" in src:
            _, text, _ = _parse(root / by_subject[src["mail_subject"]]["file"])
        elif "attachment" in src:
            meta = next(m for m in manifest["mails"]
                        if src["attachment"] in m["attachments"] and m["project"] == project)
            text = _attachment_text(root, meta, src["attachment"])
        else:
            continue  # files: tests/test_extract_chunks.py
        assert any(a.lower() in text.lower() for a in q["answer_contains"]), q["id"]
