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


def test_eval_json_is_in_sync_with_generator():
    data = json.loads((Path(__file__).parent / "eval.json").read_text(encoding="utf-8"))
    assert data == eval_set()
    assert len(data) >= 15
    for q in data:
        assert q["question"] and isinstance(q["sources"], list)
        assert q.get("not_found") or q["sources"]


def test_eval_mail_facts_exist(demo):
    root, manifest = demo
    by_subject = {m["subject"]: m for m in manifest["mails"]}
    for q in eval_set():
        for src in q["sources"]:
            if "mail_subject" in src:
                meta = by_subject[src["mail_subject"]]
                _, text, _ = _parse(root / meta["file"])
                assert any(a.lower() in text.lower() for a in q["answer_contains"]), q["id"]
