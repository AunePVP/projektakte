"""Quality criteria from VISION.md section 9 on the demo data set, plus the inbox workflow."""
import json
import sqlite3
import threading
import time
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from projektakte import assign
from projektakte.ai.services import AIServices
from projektakte.assign import Evidence, assign_mails, certain_project, judge, promotion, suggest_for_text
from projektakte.demo import generate
from projektakte.indexer import run_index
from projektakte.main import create_app
from projektakte.registry import number_regex

from .conftest import fake_settings

RX = number_regex(r"BS-\d{4}-\d{4}")
KNOWN = {"bs-2024-0137": "BS-2024-0137", "bs-2023-0412": "BS-2023-0412"}


def test_certain_rules_order_and_unknown_numbers():
    assert certain_project("BS-2024-0137 Frage", "BS-2023-0412", [], RX, KNOWN)[:2] == ("BS-2024-0137", "subject")
    assert certain_project("Frage", "zu bs-2023-0412", [], RX, KNOWN)[:2] == ("BS-2023-0412", "body")
    assert certain_project("Frage", "", ["BS-2024-0137_Plan.pdf"], RX, KNOWN)[:2] == ("BS-2024-0137", "attachment")
    assert certain_project("BS-2024-0137 und BS-2023-0412", "", [], RX, KNOWN)[:2] == (None, "ambiguous")
    assert certain_project("BS-2099-0001", "", [], RX, KNOWN)[:2] == (None, "unknown")
    assert certain_project("Hallo", "", [], RX, KNOWN)[:2] == (None, None)


@pytest.fixture()
def demo(tmp_path, fake_ai):
    generate(tmp_path / "demo")
    s = fake_settings(fake_ai, tmp_path, DEMO_MODE=1)
    run_index(s, AIServices.from_settings(s))
    manifest = json.loads((s.demo_dir / "manifest.json").read_text())
    expected = {"eml:" + m["file"].split("/", 1)[1]: m for m in manifest["mails"]}
    conn = sqlite3.connect(s.db_path)
    conn.row_factory = sqlite3.Row
    rows = {r["source_key"]: r for r in conn.execute("SELECT * FROM mails")}
    return s, expected, rows


def test_all_mails_with_number_assigned_correctly(demo):
    _, expected, rows = demo
    with_number = [k for k, m in expected.items() if m["category"] in ("subject", "body", "attachment")]
    assert len(with_number) >= 55
    for key in with_number:
        r = rows[key]
        assert (r["project"], r["assign_status"]) == (expected[key]["project"], "assigned"), r["subject"]
        assert r["assign_method"] == expected[key]["category"]


def test_unrelated_mails_never_assigned(demo):
    _, expected, rows = demo
    for key, m in expected.items():
        if m["category"] == "unrelated":
            r = rows[key]
            assert r["project"] is None and r["assign_status"] == "unassigned", r["subject"]
            assert r["suggested_project"] is None, r["subject"]


def test_content_suggestions_mostly_right_and_never_automatic(demo):
    _, expected, rows = demo
    content = [k for k, m in expected.items() if m["category"] == "content"]
    right = 0
    for key in content:
        r = rows[key]
        assert r["project"] is None and r["assign_status"] in ("suggested", "unassigned")
        right += r["suggested_project"] == expected[key]["project"]
    assert right / len(content) > 0.5
    assert right >= 6  # current quality on the demo data: 8 of 8


def test_inbox_and_decisions(demo):
    s, expected, rows = demo
    client = TestClient(create_app(s))
    inbox = client.get("/api/inbox").json()
    keys = {k for k, m in expected.items() if m["category"] in ("content", "unrelated")}
    assert inbox["count"] == len(keys)
    first = inbox["mails"][0]
    assert first["assign_status"] == "suggested" and first["suggested_project_name"]

    # confirm a suggestion
    r = client.post(f"/api/mails/{first['id']}/assign", json={"action": "confirm"}).json()
    assert r["assign_status"] == "confirmed" and r["project"] == first["suggested_project"]
    # its sections now belong to the project
    conn = sqlite3.connect(s.db_path)
    assert {p for (p,) in conn.execute("SELECT project FROM chunks WHERE mail_id = ?", (first["id"],))} == \
        {first["suggested_project"]}

    unrelated = next(m for m in inbox["mails"] if m["subject"].startswith("Rechnung"))
    assert client.post(f"/api/mails/{unrelated['id']}/assign", json={"action": "none"}).json()[
        "assign_status"] == "none"
    other = next(m for m in inbox["mails"] if m["assign_status"] == "suggested" and m["id"] != first["id"])
    r = client.post(f"/api/mails/{other['id']}/assign", json={"action": "reject"}).json()
    assert r["assign_status"] == "unassigned" and r["project"] is None
    manual = client.post(f"/api/mails/{other['id']}/assign",
                         json={"action": "assign", "project": "bs-2024-0137"}).json()
    assert manual["project"] == "BS-2024-0137"

    assert client.get("/api/inbox").json()["count"] == len(keys) - 3
    # errors
    assert client.post(f"/api/mails/{other['id']}/assign",
                       json={"action": "assign", "project": "BS-1999-0001"}).status_code == 400
    assert client.post("/api/mails/999999/assign", json={"action": "none"}).status_code == 404
    assert client.post(f"/api/mails/{other['id']}/assign", json={"action": "kaputt"}).status_code == 400

    # human decisions survive a complete re-import of the mails
    conn.execute("DELETE FROM chunks WHERE mail_id IS NOT NULL")
    conn.execute("DELETE FROM documents WHERE mail_id IS NOT NULL")
    conn.execute("DELETE FROM mails")
    conn.execute("DELETE FROM meta WHERE key LIKE 'eml_stamp:%'")
    conn.commit()
    run_index(s, AIServices.from_settings(s))
    after = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT subject, project, assign_status FROM mails")}
    assert after[first["subject"]] == (first["suggested_project"], "confirmed")
    assert after[unrelated["subject"]] == (None, "none")
    assert after[other["subject"]] == ("BS-2024-0137", "confirmed")


def test_without_embeddings_mails_wait_for_suggestions(tmp_path, fresh_fake_ai):
    generate(tmp_path / "demo")
    fresh_fake_ai.options["fail"] = ["embed"]
    s = fake_settings(fresh_fake_ai, tmp_path, DEMO_MODE=1)
    stats = run_index(s, AIServices.from_settings(s))["assignment"]
    assert stats["assigned"] >= 55 and stats["suggested"] == 0
    fresh_fake_ai.options["fail"] = []
    stats = run_index(s, AIServices.from_settings(s))["assignment"]
    assert stats["suggested"] >= 6


# --------------------------------------------------------------------------- review findings
def add_eml(s, name, subject, body, sender="Neu <neu@stadtwerke.example>", headers=()):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = sender, "buero@ib.example", subject
    m["Date"] = "Tue, 18 Jun 2024 10:30:00 +0200"
    for k, v in headers:
        m[k] = v
    m.set_content(body)
    (s.mail_dir / name).write_bytes(bytes(m))


def mail(s, name):
    conn = sqlite3.connect(s.db_path)
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM mails WHERE source_key = ?", ("eml:" + name,)).fetchone()


def test_unknown_number_gets_no_suggestion_and_is_assigned_once_the_folder_exists(tmp_path, fake_ai):
    """BUG-7: a mail naming a project number without folder gets no suggestion for another project
    (even if its content resembles one) and is assigned automatically once the folder exists."""
    generate(tmp_path / "demo")
    s = fake_settings(fake_ai, tmp_path, DEMO_MODE=1)
    add_eml(s, "neu.eml", "Neues Projekt BS-2026-0001: Tiefgarage",
            "Wir planen die Ventilatoren der Entrauchung der Tiefgarage auf der Ebene -1. Reicht die "
            "Zuluftnachströmung über die Rampe? Einfahrtshöhe 2,10 m, Feuerwehrpläne Tiefgarage")
    ai = AIServices.from_settings(s)
    run_index(s, ai)
    r = mail(s, "neu.eml")
    assert (r["assign_status"], r["assign_method"], r["suggested_project"]) == ("unassigned", "unknown", None)
    assert "BS-2026-0001 ist in Projektakte (noch) nicht bekannt" in r["assign_reason"]
    assert "kein Vorschlag für ein anderes Projekt" in r["assign_reason"]
    # checked again every run – unchanged, nothing written
    assert run_index(s, ai)["assignment"]["unchanged"] >= 1

    # state left by an older version (wrong content suggestion): re-evaluated once after the rule change
    conn = sqlite3.connect(s.db_path)
    conn.execute("UPDATE mails SET assign_status='suggested', assign_method='similarity', "
                 "suggested_project='BS-2022-0901', suggest_done=1 WHERE id = ?", (r["id"],))
    conn.execute("UPDATE meta SET value = '1' WHERE key = 'assign_rules'")
    conn.commit()
    run_index(s, ai)
    r = mail(s, "neu.eml")
    assert (r["assign_status"], r["suggested_project"]) == ("unassigned", None)

    (s.projects_dir / "BS-2026-0001 Tiefgarage Nord").mkdir()
    run_index(s, ai)
    r = mail(s, "neu.eml")
    assert (r["project"], r["assign_status"], r["assign_method"]) == ("BS-2026-0001", "assigned", "subject")


def test_reasons_are_plain_german(demo):
    """DEMO-8: reasons are sentences in plain language, no developer jargon or bare percentages."""
    _, expected, rows = demo
    for key, m in expected.items():
        reason = rows[key]["assign_reason"]
        assert reason[0].isupper() and reason.endswith("."), reason
        assert "%" not in reason and "Abschnitte" not in reason, reason
        if m["category"] == "subject":
            assert reason == f"Projektnummer {m['project']} im Betreff."
        if m["category"] == "content" and rows[key]["suggested_project"]:
            assert reason.startswith("Keine Projektnummer in der Mail. Inhaltlich am ähnlichsten: "
                                     f"{rows[key]['suggested_project']} ")
            assert "Gemeinsame Begriffe: " in reason
    newsletter = next(r for r in rows.values() if r["subject"].startswith("Newsletter"))
    assert "Sieht nach Newsletter oder Werbung aus" in newsletter["assign_reason"]


def test_newsletters_and_advertising_get_no_suggestion(tmp_path, fake_ai):
    """A8: topic-specific advertising and newsletters (header, sender address, wording) get no
    suggestion; a known correspondent writing about the same topic still does."""
    generate(tmp_path / "demo")
    s = fake_settings(fake_ai, tmp_path, DEMO_MODE=1)
    ai = AIServices.from_settings(s)
    run_index(s, ai)
    kita = mail_by_subject(s, "Regenbogenweg 3 – Rauchwarnmelder im Schlafraum")
    add_eml(s, "werbung.eml", "Rauchwarnmelder für Kitas zum Sonderpreis",
            "Funkvernetzte Rauchwarnmelder nach DIN 14676 für Schlafräume und Gruppenräume in Kitas – jetzt "
            "20 % Rabatt!", sender="Melder-Shop <verkauf@melder-shop.example>")
    add_eml(s, "liste.eml", "Fachartikel Tiefgaragen",
            "Maschinelle Entrauchung von Tiefgaragen mit 12-fachem Luftwechsel, CO-Warnanlagen und Rampen.",
            sender="Redaktion <redaktion@fachblatt.example>",
            headers=[("List-Unsubscribe", "<mailto:abmelden@fachblatt.example>")])
    add_eml(s, "news.eml", "Fachartikel Tiefgaragen",
            "Maschinelle Entrauchung von Tiefgaragen mit 12-fachem Luftwechsel, CO-Warnanlagen und Rampen.",
            sender="Fachblatt <newsletter@fachblatt.example>")
    add_eml(s, "kita.eml", "Rauchwarnmelder im Gruppenraum",
            "für die Kindertagesstätte am Regenbogenweg fragt die Leitung, ob die funkvernetzten "
            "Rauchwarnmelder im Gruppenraum auch im Büro der Leitung Alarm geben.",
            sender=f"{kita['sender_name']} <{kita['sender']}>")
    run_index(s, ai)
    for name, why in (("werbung.eml", "„Sonderpreis“"), ("liste.eml", "List-Unsubscribe"),
                      ("news.eml", "newsletter@fachblatt.example")):
        r = mail(s, name)
        assert (r["assign_status"], r["suggested_project"]) == ("unassigned", None), r["assign_reason"]
        assert "Newsletter oder Werbung" in r["assign_reason"] and why in r["assign_reason"]
    assert mail(s, "kita.eml")["suggested_project"] == "BS-2025-0019"
    # the same for plMail's suggestion API (nothing indexed)
    conn = sqlite3.connect(s.db_path)
    conn.row_factory = sqlite3.Row
    common = dict(vector_key=str(s.db_path), pattern=s.project_number_pattern, attachments=[])
    assert suggest_for_text(conn, ai, subject="Rauchwarnmelder für Kitas zum Sonderpreis",
                            body="Funkvernetzte Rauchwarnmelder für Kitas – jetzt 20 % Rabatt!",
                            sender="verkauf@melder-shop.example", **common) == []
    assert suggest_for_text(conn, ai, subject="Fachartikel Tiefgaragen",
                            body="Maschinelle Entrauchung von Tiefgaragen mit 12-fachem Luftwechsel.",
                            sender="redaktion@fachblatt.example",
                            headers={"List-Unsubscribe": "<mailto:x@fachblatt.example>"}, **common) == []


def mail_by_subject(s, subject):
    conn = sqlite3.connect(s.db_path)
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM mails WHERE subject = ?", (subject,)).fetchone()


def test_promotion_rules():
    assert promotion("x", "y", "a@b.example", bulk="List-Unsubscribe", sender_known=True) == \
        "Kopfzeile „List-Unsubscribe“"
    for address in ("newsletter@x.example", "no-reply@x.example", "noreply.de@x.example", "angebot@x.example"):
        assert promotion("x", "y", address, bulk=None, sender_known=True), address
    assert promotion("Angebot", "Jetzt 10 % Rabatt", "a@b.example", bulk=None, sender_known=False)
    # one phrase from a known correspondent is fine, two are not
    assert promotion("Termin", "Ich muss mich leider abmelden.", "a@b.example", bulk=None, sender_known=True) is None
    assert promotion("Webinar", "Frühbucherrabatt", "a@b.example", bulk=None, sender_known=True)
    assert promotion("Prüfbericht", "Anbei der Bericht.", "info@b.example", bulk=None, sender_known=False) is None


def test_judge_needs_substantial_evidence():
    """A8: the share of the votes alone is not enough – absolute evidence is required too."""
    ev = Evidence(ranked=[("A", 0.8), ("B", 0.1)], shared={"A": ["Regenbogenweg", "Schlafraum"]},
                  similarity={"A": 0.4})
    kw = dict(min_share=0.5, min_lead=0.2, names={"A": "Kita"})
    assert judge(ev, **kw).project is None  # two shared words are too few
    ev.shared["A"].append("Rauchwarnmelder")
    ok = judge(ev, **kw)
    assert ok.project == "A" and ok.reason == ("Inhaltlich am ähnlichsten: A Kita, deutlich vor B. Gemeinsame "
                                               "Begriffe: Regenbogenweg, Schlafraum, Rauchwarnmelder.")
    assert judge(ev, new_sender=True, **kw).project is None  # stricter without project history
    assert judge(ev, min_similarity=0.5, **kw).project is None  # ASSIGN_MIN_SIMILARITY
    assert judge(ev, min_similarity=0.3, **kw).project == "A"
    close = Evidence(ranked=[("A", 0.55), ("B", 0.4)], shared={"A": ["a1", "a2", "a3"]})
    assert "B kommt ebenso in Frage" in judge(close, **kw).reason


def test_min_similarity_setting_is_applied(demo):
    s, _, _ = demo
    conn = sqlite3.connect(s.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("UPDATE mails SET suggest_done = 0 WHERE assign_status IN ('suggested', 'unassigned')")
    conn.commit()
    stats = assign_mails(conn, s.project_number_pattern, min_share=0.5, min_lead=0.2,
                         vector_key=str(s.db_path), min_similarity=0.999)
    assert stats["suggested"] == 0
    assert conn.execute("SELECT COUNT(*) FROM mails WHERE assign_status = 'suggested'").fetchone()[0] == 0


def _reevaluate_everything(s):
    conn = sqlite3.connect(s.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("UPDATE meta SET value = 'anders' WHERE key = 'assign_mode'")  # mode switch: all mails
    conn.commit()
    return conn


def test_evidence_is_computed_once_per_run(demo, monkeypatch):
    """ops-01: the evidence set is read once per run, not once per mail."""
    s, _, rows = demo
    conn = _reevaluate_everything(s)
    calls = []
    real = assign.evidence_chunk_ids
    monkeypatch.setattr(assign, "evidence_chunk_ids", lambda c: calls.append(1) or real(c))
    stats = assign_mails(conn, s.project_number_pattern, min_share=0.5, min_lead=0.2, vector_key=str(s.db_path))
    assert len(calls) == 1
    # every mail was evaluated again – with the same rules nothing changes (and nothing is written)
    assert stats["unchanged"] == len(rows) and sum(v for k, v in stats.items() if k != "unchanged") == 0


def test_inbox_can_write_while_suggestions_are_computed(demo, monkeypatch):
    """ops-01/BUG-2: no write transaction is open while a suggestion is computed."""
    s, _, _ = demo
    conn = _reevaluate_everything(s)
    results = []
    real = assign.vote_projects

    def vote_and_write(*args, **kwargs):
        other = sqlite3.connect(s.db_path, timeout=0)
        try:
            other.execute("INSERT INTO meta(key, value) VALUES('probe', 'x') ON CONFLICT(key) DO UPDATE "
                          "SET value = excluded.value")
            other.commit()
            results.append("ok")
        except sqlite3.OperationalError as exc:
            results.append(str(exc))
        finally:
            other.close()
        return real(*args, **kwargs)

    monkeypatch.setattr(assign, "vote_projects", vote_and_write)
    assign_mails(conn, s.project_number_pattern, min_share=0.5, min_lead=0.2, vector_key=str(s.db_path))
    assert results and set(results) == {"ok"}


def test_long_assignment_run_commits_regularly(demo, monkeypatch):
    """ops-01: a decision in the inbox waits at most briefly, even during a long run."""
    s, _, rows = demo
    conn = _reevaluate_everything(s)
    real = assign._certain

    def slow(*args, **kwargs):
        time.sleep(0.04)  # 76 mails ≈ 3 s in phase 1
        return real(*args, **kwargs)

    monkeypatch.setattr(assign, "_certain", slow)
    outcome = {}

    def decide_meanwhile():
        time.sleep(0.6)
        other = sqlite3.connect(s.db_path, timeout=2)
        started = time.time()
        try:
            other.execute("INSERT INTO meta(key, value) VALUES('probe', 'x') ON CONFLICT(key) DO UPDATE "
                          "SET value = excluded.value")
            other.commit()
            outcome["waited"] = time.time() - started
        except sqlite3.OperationalError as exc:
            outcome["error"] = str(exc)
        finally:
            other.close()

    t = threading.Thread(target=decide_meanwhile)
    t.start()
    assign_mails(conn, s.project_number_pattern, min_share=0.5, min_lead=0.2, vector_key=str(s.db_path))
    t.join()
    assert "error" not in outcome and outcome["waited"] < 1.5


def test_words_found_almost_everywhere_do_not_vote(tmp_path):
    """ops-01: words in more than half of all sections carry no BM25 weight but made every vote
    score nearly all sections – they are left out of the query and of the shared-word evidence."""
    from projektakte import db
    from projektakte.documents import add_fts

    path = tmp_path / "x.sqlite3"
    db.init_db(path)
    conn = db.connect(path)
    for i in range(60):
        text = "Brandschutz Nachweis" + (" am Regenbogenweg" if i < 3 else f" Notiz {i}")
        cid = conn.execute("INSERT INTO chunks(doc_id, project, ord, text) VALUES(NULL, 'P', ?, ?)",
                           (i, text)).lastrowid
        add_fts(conn, cid, "", text)
    freq = assign.TermFrequency(conn)
    common = freq.common("Brandschutz-Nachweis für die Kita am Regenbogenweg")
    assert common == {"brandschutz", "nachwei"}
    assert assign.without_terms("Brandschutz-Nachweis am Regenbogenweg", common) == "am Regenbogenweg"
    assert list(assign.specific_terms("Brandschutz am Regenbogenweg", common).values()) == ["Regenbogenweg"]
