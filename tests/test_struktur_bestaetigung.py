"""PROJECT_NUMBER_PATTERN=auto as a proposal a person confirms once (structure.py, status page, CLI):
a pending proposal is never used; confirming on the status page (POST, login, same site) or with
--uebernehmen; rejecting; a later, different detection next to the confirmed scheme; the evidence on
the status page and in /api/status (escaped); folder and file names that are no valid UTF-8; guards
that would drop projects; and the cases of the second adversarial review that now give a correct
proposal – or a wrong one that the evidence makes visible."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import projektakte.auth as auth_mod
from projektakte import db, structure
from projektakte.config import load_settings
from projektakte.indexer import run_index
from projektakte.main import create_app
from projektakte.passwords import hash_password
from projektakte.registry import loose_pattern, printable, scan_projects
from projektakte.structure import analyse, main

YEAR_NUM = r"\d{4}-\d{3}"
FOUR = r"(?!(?:19|20)\d\d(?!\d))\d{4}"
PW = "Brandschutz-2025!"
NAMES = ["Kita Regenbogen", "Grundschule Nord", "Sporthalle Süd", "Rathaus Umbau", "Wohnanlage 12 WE",
         "Pflegeheim St. Anna", "Feuerwehrhaus", "Bürogebäude Müller", "Hotel Löwen", "Tiefgarage Markt"]
SUBS = ["01_Schriftverkehr", "02_Pläne", "03_Brandschutzkonzept"]


def mk(root: Path, *paths: str) -> Path:
    for p in paths:
        (root / p).mkdir(parents=True, exist_ok=True)
    return root


def name(i: int) -> str:
    return f"{NAMES[i % len(NAMES)]} {i}"


def settings_for(tmp_path: Path, root: Path, **extra):
    mail = tmp_path / "mails"
    mail.mkdir(parents=True, exist_ok=True)
    env = {"DATA_DIR": str(tmp_path / "data"), "PROJECTS_DIR": str(root), "MAIL_DIR": str(mail),
           "APP_ENV": "test", "AUTH_DISABLED": "1", "PROJECT_NUMBER_PATTERN": "auto"}
    env.update({k: str(v) for k, v in extra.items()})
    return load_settings(env)


def year_share(root: Path, years=(2023, 2024), per_year: int = 8) -> Path:
    for y in years:
        for i in range(1, per_year + 1):
            folder = mk(root, f"{y}/{y}-{i:03d} {name(i)}/Pläne") / f"{y}/{y}-{i:03d} {name(i)}"
            (folder / "Bericht.txt").write_text(f"Brandschutzbericht {y}-{i:03d}: Rettungswege geprüft.",
                                                encoding="utf-8")
    return root


def eml(path: Path, subject: str, body: str = "Hallo") -> None:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "Bauamt <bauamt@stadt.example>", "buero@ib.example", subject
    m["Date"] = "Tue, 18 Jun 2024 10:30:00 +0200"
    m["Message-ID"] = f"<{path.stem}@stadt.example>"
    m.set_content(body)
    path.write_bytes(bytes(m))


def meta(s, key):
    with db.session(s.db_path) as conn:
        raw = db.get_meta(conn, key)
    return json.loads(raw) if raw else None


def active(s) -> list[str]:
    with db.session(s.db_path) as conn:
        return [r[0] for r in conn.execute("SELECT number FROM projects WHERE active = 1 ORDER BY number")]


def age_check(s, hours: int = 2) -> None:
    with db.session(s.db_path) as conn:
        rec = json.loads(db.get_meta(conn, structure.CHECK_KEY))
        when = datetime.now(timezone.utc) - timedelta(hours=hours)
        db.set_meta(conn, structure.CHECK_KEY, json.dumps({**rec, "checked_at": when.isoformat(timespec="seconds")}))


def pending_id(s) -> str:
    return structure.current_layout(s).proposal["id"]


def confirm(s, user="pruefer"):
    with db.session(s.db_path) as conn:
        return structure.confirm_proposal(conn, s, pending_id(s), user)


def client_for(s) -> TestClient:
    return TestClient(create_app(s, background=False))


def section(page: str) -> str:
    """The Projektordner card of the status page."""
    start = page.index('id="projektordner"')
    return page[start:page.index("</section>\n<section", start)]


# ============================================================================ pending is never used
def test_pending_proposal_reads_nothing_as_a_project(tmp_path):
    root = year_share(tmp_path / "p")
    s = settings_for(tmp_path, root)
    eml(s.mail_dir / "a.eml", "Unterlagen zu 2024-003")
    stats = run_index(s)
    assert stats["structure"]["proposal"] == YEAR_NUM and stats["structure"]["state"] == "pending"
    assert stats["projects"]["warning"].startswith("Vorschlag zur Projektstruktur wartet auf Bestätigung")
    assert stats["documents"]["note"] == "nicht abgeglichen – Vorschlag zur Projektstruktur wartet auf Bestätigung"
    assert stats["assignment"]["note"] == "nicht ausgeführt – Vorschlag zur Projektstruktur wartet auf Bestätigung"
    assert active(s) == [] and structure.number_pattern(s) == structure.NO_MATCH
    with db.session(s.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert conn.execute("SELECT assign_status FROM mails").fetchone()[0] == "pending"
    # another pass changes nothing either (and does not read the share again within the interval)
    assert run_index(s)["projects"]["added"] == 0 and active(s) == []
    client = client_for(s)
    page = section(client.get("/status").text)
    assert "Vorschlag zur Projektstruktur wartet auf Bestätigung" in page and "noch kein bestätigtes Schema" in page
    layout = client.get("/api/status").json()["layout"]
    assert layout["state"] == "pending" and layout["pattern"] is None and layout["proposal"]["pattern"] == YEAR_NUM
    # confirmed: projects, documents and the assignment follow at once
    confirm(s)
    stats = run_index(s)
    assert stats["projects"]["added"] == 16 and stats["documents"]["new"] == 16
    with db.session(s.db_path) as conn:
        assert tuple(conn.execute("SELECT project, assign_status FROM mails").fetchone()) == ("2024-003", "assigned")


def test_legacy_record_without_confirmation_is_not_used(tmp_path):
    """A scheme stored by the development state before the confirmation (nobody confirmed it) is
    ignored: the next pass makes a proposal instead."""
    root = year_share(tmp_path / "p")
    s = settings_for(tmp_path, root)
    db.init_db(s.db_path)
    with db.session(s.db_path) as conn:
        db.set_meta(conn, "project_layout_auto", json.dumps({"pattern": YEAR_NUM, "depth": 2,
                                                              "root": os.path.abspath(root)}))
    assert structure.current_layout(s).pattern is None
    run_index(s)
    assert structure.current_layout(s).state == "pending" and meta(s, "project_layout_auto") is None


# ============================================================================ confirming on the status page
def test_confirm_on_the_status_page(tmp_path, caplog):
    root = year_share(tmp_path / "p")
    s = settings_for(tmp_path, root)
    run_index(s)
    client = client_for(s)
    page = section(client.get("/status").text)
    pid = pending_id(s)
    assert f'name="vorschlag" value="{pid}"' in page and "Übernehmen" in page and "Ablehnen" in page
    caplog.set_level(logging.INFO, logger="projektakte.structure")
    r = client.post("/status/projektstruktur/uebernehmen", data={"vorschlag": pid}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/status#projektordner"
    confirmed = meta(s, structure.CONFIRMED_KEY)
    assert confirmed["pattern"] == YEAR_NUM and confirmed["confirmed_via"] == "Statusseite"
    assert confirmed["confirmed_by"] == structure.ANONYMOUS and confirmed["confirmed_at"]
    assert "Projektstruktur bestätigt von unbekannt (ohne Anmeldung) (Statusseite)" in caplog.text
    assert meta(s, structure.PROPOSAL_KEY) is None
    page = section(client.get("/status").text)
    assert r"<code>\d{4}-\d{3}</code> – bestätigt am" in page and "von unbekannt (ohne Anmeldung)" in page
    assert "Übernehmen" not in page
    assert run_index(s)["projects"]["added"] == 16


def test_a_changed_proposal_is_not_confirmed_blindly(tmp_path):
    """The button carries the id of the proposal the page showed: a newer detection meanwhile is
    refused (409), nothing is confirmed."""
    root = year_share(tmp_path / "p")
    s = settings_for(tmp_path, root)
    run_index(s)
    client = client_for(s)
    r = client.post("/status/projektstruktur/uebernehmen", data={"vorschlag": "0123456789abcdef"})
    assert r.status_code == 409 and "Vorschlag nicht mehr aktuell" in r.text and "inzwischen geändert" in r.text
    r = client.post("/status/projektstruktur/uebernehmen", data={})
    assert r.status_code == 409
    assert meta(s, structure.CONFIRMED_KEY) is None and structure.current_layout(s).state == "pending"


def test_confirm_needs_a_login_and_the_same_site(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_mod, "_DUMMY", [hash_password("dummy", iterations=1000)])
    root = year_share(tmp_path / "p")
    s = settings_for(tmp_path, root, AUTH_DISABLED="", SECRET_KEY="test-geheimnis",
                     AUTH_USERS=f"anna:{hash_password(PW, iterations=1000)}")
    run_index(s)
    pid = pending_id(s)
    client = client_for(s)
    # without a login: to the login page (a browser) or 401 (a script), nothing confirmed
    html = {"Accept": "text/html"}
    r = client.post("/status/projektstruktur/uebernehmen", data={"vorschlag": pid}, headers=html,
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    r = client.post("/status/projektstruktur/ablehnen", data={"vorschlag": pid}, follow_redirects=False)
    assert r.status_code == 401
    r = client.post("/status/projektstruktur/neu-pruefen", follow_redirects=False)
    assert r.status_code == 401
    assert meta(s, structure.CONFIRMED_KEY) is None and meta(s, structure.REJECTED_KEY) is None
    login = client.post("/login", data={"username": "anna", "password": PW, "next": "/status"},
                        follow_redirects=False)
    assert login.status_code == 303
    # another site (also a sibling subdomain) may not confirm in the name of the logged-in user
    for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
                    {"Origin": "https://evil.example"}):
        r = client.post("/status/projektstruktur/uebernehmen", data={"vorschlag": pid}, headers=headers,
                        follow_redirects=False)
        assert r.status_code == 403, headers
    assert meta(s, structure.CONFIRMED_KEY) is None
    r = client.post("/status/projektstruktur/uebernehmen", data={"vorschlag": pid},
                    headers={"Sec-Fetch-Site": "same-origin"}, follow_redirects=False)
    assert r.status_code == 303
    confirmed = meta(s, structure.CONFIRMED_KEY)
    assert confirmed["confirmed_by"] == "anna" and confirmed["pattern"] == YEAR_NUM
    assert "von anna" in client.get("/status").text


def test_confirm_with_the_command_line(tmp_path, monkeypatch, capsys):
    root = year_share(tmp_path / "p")
    s = settings_for(tmp_path, root)
    run_index(s)
    monkeypatch.setenv("PROJECTS_DIR", str(root))
    monkeypatch.setenv("DATA_DIR", str(s.data_dir))
    monkeypatch.setenv("PROJECT_NUMBER_PATTERN", "auto")
    assert main(["--uebernehmen", "--ohne-dateien", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["accepted"]["pattern"] == YEAR_NUM and data["accepted"]["confirmed_via"] == "Kommandozeile"
    assert data["accepted"]["confirmed_by"].endswith("(Kommandozeile)")
    layout = structure.current_layout(s)
    assert layout.state == "confirmed" and layout.proposal is None
    assert run_index(s)["projects"]["added"] == 16
    # a share without a clear scheme: nothing is confirmed
    other = mk(tmp_path / "q", "Verwaltung", "Fotos")
    monkeypatch.setenv("PROJECTS_DIR", str(other))
    assert main(["--uebernehmen", "--ohne-dateien"]) == 1
    assert "Nichts übernommen" in capsys.readouterr().out


# ============================================================================ rejecting
def test_reject_keeps_the_state_until_the_detection_changes(tmp_path, caplog):
    root = year_share(tmp_path / "p")
    s = settings_for(tmp_path, root)
    run_index(s)
    client = client_for(s)
    caplog.set_level(logging.INFO, logger="projektakte.structure")
    r = client.post("/status/projektstruktur/ablehnen", data={"vorschlag": pending_id(s)}, follow_redirects=False)
    assert r.status_code == 303
    assert "Vorschlag zur Projektstruktur abgelehnt von unbekannt (ohne Anmeldung)" in caplog.text
    layout = structure.current_layout(s)
    assert layout.state == "rejected" and layout.pattern is None and layout.proposal is None
    page = section(client.get("/status").text)
    assert "Abgelehnt am" in page and "Ablehnung aufheben" in page and 'name="vorschlag"' not in page
    stats = run_index(s)
    assert stats["projects"]["added"] == 0 and "abgelehnt" in stats["projects"]["warning"]
    # the same detection again: not offered again
    age_check(s)
    run_index(s)
    assert structure.current_layout(s).state == "rejected"
    # the detection changes (the office added a numbering of its own): a new proposal
    mk(root, *(f"P{30000 + i} {name(i)}" for i in range(1, 41)))
    age_check(s)
    run_index(s)
    layout = structure.current_layout(s)
    assert layout.state == "pending" and layout.proposal["pattern"] == r"P\d{5}"


def test_undo_a_rejection(tmp_path):
    root = year_share(tmp_path / "p")
    s = settings_for(tmp_path, root)
    run_index(s)
    client = client_for(s)
    client.post("/status/projektstruktur/ablehnen", data={"vorschlag": pending_id(s)})
    assert structure.current_layout(s).state == "rejected"
    r = client.post("/status/projektstruktur/neu-pruefen", data={"ablehnung": "aufheben"}, follow_redirects=False)
    assert r.status_code == 303
    run_index(s)  # detects at once (no waiting for the interval)
    assert structure.current_layout(s).proposal["pattern"] == YEAR_NUM


def test_recheck_button_detects_at_once(tmp_path):
    root = mk(tmp_path / "p", "Verwaltung", "Fotos", "Akquise")
    s = settings_for(tmp_path, root)
    run_index(s)
    assert structure.current_layout(s).state == "none"
    year_share(root)
    run_index(s)
    assert structure.current_layout(s).state == "none"  # within the interval: not read again
    client_for(s).post("/status/projektstruktur/neu-pruefen")
    run_index(s)
    assert structure.current_layout(s).proposal["pattern"] == YEAR_NUM


# ============================================================================ a later, different detection
def test_new_proposal_next_to_the_confirmed_scheme(tmp_path):
    root = year_share(tmp_path / "p")
    s = settings_for(tmp_path, root)
    run_index(s)
    confirm(s)
    run_index(s)
    assert len(active(s)) == 16
    for p in list((root / "2024").iterdir()) + list((root / "2023").iterdir()):
        p.rename(p.parent / p.name.replace(f"{p.parent.name}-", f"P{p.parent.name[2:]}"))  # 2024-001 → P24001
    age_check(s)
    stats = run_index(s)
    layout = structure.current_layout(s)
    assert layout.pattern == YEAR_NUM and layout.proposal["pattern"] == r"P\d{5}"
    assert layout.proposal["current_projects"] == 0 and stats["structure"]["proposal"] == r"P\d{5}"
    # the confirmed scheme stays in use – it finds nothing now, so nothing is changed (unmounted share?)
    assert "keine Projekte mehr" in stats["projects"]["warning"] and len(active(s)) == 16
    page = section(client_for(s).get("/status").text)
    assert "Neuer Vorschlag – weicht vom bestätigten Schema ab" in page and "bleibt das bestätigte" in page
    assert f'value="{layout.proposal["id"]}"' in page and r"<code>P\d{5}</code>" in page
    # rejecting it keeps the confirmed scheme; the same detection is not offered again
    with db.session(s.db_path) as conn:
        structure.reject_proposal(conn, s, layout.proposal["id"], "anna")
    age_check(s)
    run_index(s)
    layout = structure.current_layout(s)
    assert layout.pattern == YEAR_NUM and layout.proposal is None and layout.rejected["rejected_by"] == "anna"
    # confirming a new proposal later replaces the scheme (only then)
    with db.session(s.db_path) as conn:
        structure.request_recheck(conn, s, forget_rejection=True)
    run_index(s)
    confirm(s)
    assert run_index(s)["projects"]["added"] == 16 and structure.current_layout(s).pattern == r"P\d{5}"


def test_a_second_numbering_after_confirming_is_shown(tmp_path):
    """Review (verify2, auto.py switch): new projects in another numbering next to the confirmed one
    are named on the status page – as a further scheme, or as a detection without a clear scheme."""
    root = year_share(tmp_path / "p", years=(2022, 2023, 2024), per_year=30)
    s = settings_for(tmp_path, root)
    run_index(s)
    confirm(s)
    run_index(s)
    mk(root, *(f"2025/P25{i:03d} {name(i)}" for i in range(1, 21)))
    age_check(s)
    run_index(s)
    layout = structure.current_layout(s)
    assert layout.proposal is None and layout.check["hints"][0]["pattern"] == r"P\d{5}"
    page = section(client_for(s).get("/status").text)
    assert "Weitere Nummernschemata" in page and r"<code>P\d{5}</code>" in page and "P25001" in page
    mk(root, *(f"2025/P25{i:03d} {name(i)}" for i in range(21, 61)))
    age_check(s)
    run_index(s)
    layout = structure.current_layout(s)
    assert layout.pattern == YEAR_NUM and layout.check["outcome"] == "none"
    assert any("ergibt kein eindeutiges Schema" in n and "etwa gleich oft" in n for n in layout.notices)


# ============================================================================ evidence
def test_evidence_on_the_status_page_and_in_the_api(tmp_path):
    root = tmp_path / "p"
    for y in (2023, 2024):
        mk(root, *(f"{y}/{y}-{i:03d} {name(i)}/Pläne" for i in range(1, 13)))
    mk(root, "2023/2023-000 <script>alert(1)<!-- & Co", "_Vorlagen", "00_Allgemein", "<img src=x onerror=alert(1)>",
       *(f"Angebote/AN-2024-{i:03d} Anfrage" for i in range(1, 10)))
    s = settings_for(tmp_path, root)
    run_index(s)
    p = structure.current_layout(s).proposal
    assert p["description"] == "4 Ziffern, Bindestrich, 3 Ziffern" and p["pattern"] == YEAR_NUM
    assert p["projects"] == 25 and p["depth"] == 2 and p["levels"] == [[2, 25]]
    assert p["parents"] == [["2023", 13], ["2024", 12]] and len(p["examples"]) == 10
    assert sorted(p["exceptions"]) == ["00_Allgemein", "<img src=x onerror=alert(1)>", "Angebote", "_Vorlagen"]
    assert p["hints"][0]["pattern"] == r"AN-\d{4}-\d{3}" and p["hints"][0]["where"] == "Angebote"
    client = client_for(s)
    page = section(client.get("/status").text)
    for text in ("4 Ziffern, Bindestrich, 3 Ziffern", r"<code>\d{4}-\d{3}</code>", "Ordnertiefe", "2023 (13)",
                 "2024 (12)", "Ausnahmen", "_<wbr>Vorlagen", r"<code>AN-\d{4}-\d{3}</code>", "Angebote",
                 r"PROJECT_NUMBER_PATTERN=(?:\d{4}-\d{3}|AN-\d{4}-\d{3})", "… und 15 weitere"):
        assert text in page, text
    # folder names are escaped
    assert "<script>" not in page and "<img" not in page
    assert "2023-000 &lt;script&gt;alert(1)&lt;!-- &amp; Co" in page and "&lt;img src=x onerror=alert(1)&gt;" in page
    api = client.get("/api/status").json()["layout"]["proposal"]
    assert api["pattern"] == YEAR_NUM and api["hints"][0]["examples"][0] == "AN-2024-001 Anfrage"
    assert api["parents"] == p["parents"] and api["examples"] == p["examples"]


def test_no_clear_scheme_explains_and_lists_candidates(tmp_path):
    root = mk(tmp_path / "p", *(f"2024-{i:03d} {name(i)}" for i in range(1, 31)),
              *(f"P{30000 + i} {name(i)}" for i in range(1, 21)))
    s = settings_for(tmp_path, root)
    stats = run_index(s)
    assert "Kein eindeutiges Projektnummern-Schema" in stats["projects"]["warning"]
    page = section(client_for(s).get("/status").text)
    assert "Kein eindeutiges Schema erkannt" in page and "etwa gleich oft" in page
    assert "Nächste Kandidaten" in page and r"<code>\d{4}-\d{3}</code>" in page and r"<code>P\d{5}</code>" in page
    assert "Von Hand einstellen" in page and "PROJECT_NUMBER_PATTERN" in page
    check = client_for(s).get("/api/status").json()["layout"]["check"]
    assert check["outcome"] == "none" and {c["pattern"] for c in check["candidates"][:2]} == {YEAR_NUM, r"P\d{5}"}


# ============================================================================ names that are no valid UTF-8
def bad_share(root: Path) -> Path:
    root.mkdir(parents=True)
    raw = os.fsencode(root)
    for i in range(1, 6):
        os.makedirs(raw + b"/2024-%03d B\xfcro %d" % (i, i))
        (Path(os.fsdecode(raw + b"/2024-%03d B\xfcro %d" % (i, i))) / "Bericht.txt").write_text(
            "Brandschutz Rettungsweg", encoding="utf-8")
    for i in range(6, 9):
        (mk(root, f"2024-{i:03d} Kita {i}") / f"2024-{i:03d} Kita {i}" / "Bericht.txt").write_text(
            "Brandschutz Rettungsweg", encoding="utf-8")
    return root


def test_sync_with_names_that_are_no_utf8(tmp_path, caplog):
    """Review s11: the index pass stored such names and crashed – with an explicit pattern, too."""
    root = bad_share(tmp_path / "p")
    s = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN=YEAR_NUM)
    caplog.set_level(logging.WARNING)
    stats = run_index(s)
    assert stats["projects"]["added"] == 8 and stats["projects"]["bad_names"]["count"] == 5
    with db.session(s.db_path) as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM projects ORDER BY number")]
    assert names[0] == "B�ro 1" and names[-1] == "Kita 8"
    assert "falsche Zeichenkodierung" in caplog.text and "iocharset=utf8" in caplog.text
    # warned once – not again at the next pass
    caplog.clear()
    run_index(s)
    assert "falsche Zeichenkodierung" not in caplog.text
    client = client_for(s)
    page = section(client.get("/status").text)
    assert "falsche Zeichenkodierung" in page and "B�ro" in page and "iocharset=utf8" in page
    api = client.get("/api/status")
    assert api.status_code == 200 and "B�ro" in api.json()["layout"]["notices"][0]


def test_file_names_that_are_no_utf8_do_not_stop_the_pass(tmp_path):
    root = mk(tmp_path / "p", *(f"2024-{i:03d} Kita {i}" for i in range(1, 6)))
    with open(os.fsencode(root) + b"/2024-002 Kita 2/Bericht \xfcber.txt", "wb") as f:
        f.write(b"Brandschutz Rettungsweg")
    s = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN=YEAR_NUM)
    eml(s.mail_dir / "a.eml", "Frage zu 2024-004")
    stats = run_index(s)
    assert stats["projects"]["added"] == 5 and "iocharset=utf8" in stats["documents"]["warning"]
    assert stats["mails"]["new"] == 1 and "assigned" in json.dumps(stats["assignment"])


def test_detection_and_cli_with_names_that_are_no_utf8(tmp_path):
    """Review (verify2): the CLI crashed with a strict UTF-8 stdout (de_DE.UTF-8), text and --json."""
    root = bad_share(tmp_path / "p")
    a = analyse(root)
    assert a.proposal.pattern == YEAR_NUM and a.bad_names["count"] == 5
    assert any("falsche Zeichenkodierung" in n for n in a.proposal.notes)
    json.dumps(a.to_dict(), ensure_ascii=False).encode("utf-8")  # no surrogates left
    env = {**os.environ, "PYTHONIOENCODING": "utf-8:strict", "PYTHONPATH": str(Path(__file__).parents[1]),
           "DATA_DIR": str(tmp_path / "data"), "PROJECTS_DIR": str(root)}
    for extra in ([], ["--json"]):
        r = subprocess.run([sys.executable, "-m", "projektakte.structure", str(root), *extra], env=env,
                           capture_output=True, timeout=120)
        out = r.stdout.decode("utf-8")
        assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
        assert "B�ro" in out and "Traceback" not in r.stderr.decode("utf-8", "replace")
    assert json.loads(out)["bad_names"]["count"] == 5


def test_auto_mode_with_names_that_are_no_utf8(tmp_path):
    root = bad_share(tmp_path / "p")
    s = settings_for(tmp_path, root)
    run_index(s)
    p = meta(s, structure.PROPOSAL_KEY)
    assert p["examples"][0] == "2024-001 B�ro 1" and p["bad_names"]["count"] == 5
    assert "falsche Zeichenkodierung" in section(client_for(s).get("/status").text)
    confirm(s)
    assert run_index(s)["projects"]["added"] == 8


def test_printable():
    assert printable("2024-001 B\udcfcro") == "2024-001 B�ro"
    assert printable("Straße – Übergang") == "Straße – Übergang"


# ============================================================================ guards never drop projects silently
@pytest.mark.parametrize("pattern, loose", [
    (FOUR, r"\d{4}"),
    (r"(?!(?:19|20)\d\d-(?:19|20)\d\d(?!\d))\d{4}-\d{3,4}", r"\d{4}-\d{3,4}"),
    (r"(?:BS-\d{4}|(?!(?:19|20)\d\d(?!\d))\d{4})", r"(?:BS-\d{4}|\d{4})"),
    (r"BS-\d{4}-\d{4}", None),
])
def test_loose_pattern(pattern, loose):
    assert loose_pattern(pattern) == loose


def test_year_guard_is_left_out_when_the_numbers_reach_the_years(tmp_path):
    """Review N08: flat running numbers 1840–1920 – the guard would drop 1900–1920. No year folder
    holds projects, so the guard is left out (and said so)."""
    mk(tmp_path, *(f"{i} {name(i)}" for i in range(1840, 1921)), "_Vorlagen", "Archiv")
    p = analyse(tmp_path).proposal
    assert (p.pattern, p.projects) == (r"\d{4}", 81) and p.guard["dropped"] and p.guard["count"] == 21
    assert any(n.startswith("Ohne Ausschluss von Jahreszahlen: 21 Ordner") for n in p.notes)
    assert "keine Jahreszahl" not in p.description


def test_year_range_guard_is_left_out_when_it_would_drop_projects(tmp_path):
    """Review Y1/Y2/T5: year and a running number that continues across the years (2021-1950)."""
    mk(tmp_path, *(f"{y}/{y}-{1500 + (y - 2018) * 100 + i} {name(i)}" for y in range(2018, 2024) for i in range(100)))
    p = analyse(tmp_path).proposal
    assert (p.pattern, p.projects) == (r"\d{4}-\d{4}", 600) and p.guard["dropped"]
    assert "kein Jahresbereich" not in p.description


def test_a_few_excluded_folders_are_named(tmp_path):
    mk(tmp_path, *(f"2023-{i:03d} {name(i)}" for i in range(1, 21)), *(f"2024-{i:04d} {name(i)}" for i in range(1, 11)),
       "2017-2018 Altakten")
    p = analyse(tmp_path).proposal
    assert p.projects == 30 and not p.guard["dropped"] and p.guard["count"] == 1
    assert "1 Ordner würde als Jahresbereich ausgeschlossen und nicht gelesen: 2017-2018 Altakten" in p.notes


def test_guard_of_a_confirmed_scheme_checked_at_every_pass(tmp_path, caplog):
    """Review (verify2, auto.py years): the guarded \\d{4} was confirmed for 1801–1880 in year folders;
    1881–1920 added later – 1900–1920 are kept out by the guard, which the status page now says."""
    root = mk(tmp_path / "p", *(f"{y}/{1800 + (y - 2020) * 20 + i} {name(i)}" for y in range(2020, 2024)
                                for i in range(1, 21)))
    s = settings_for(tmp_path, root)
    run_index(s)
    assert structure.current_layout(s).proposal["pattern"] == FOUR
    confirm(s)
    run_index(s)
    mk(root, *(f"2024/{1880 + i} {name(i)}" for i in range(1, 41)))
    caplog.set_level(logging.WARNING)
    stats = run_index(s)
    assert stats["projects"]["projects"] == 99 and stats["projects"]["guard_excluded"]["count"] == 21
    assert "21 Ordner würden als Jahreszahl ausgeschlossen" in caplog.text
    page = section(client_for(s).get("/status").text)
    assert "21 Ordner würden als Jahreszahl ausgeschlossen und nicht gelesen: 2024/1900" in page
    caplog.clear()
    run_index(s)
    assert "ausgeschlossen" not in caplog.text  # said once


def test_guard_of_a_configured_pattern_is_checked_too(tmp_path):
    root = mk(tmp_path / "p", *(f"{y}/{1880 + (y - 2022) * 20 + i} {name(i)}" for y in (2022, 2023) for i in range(1, 21)))
    s = settings_for(tmp_path, root, PROJECT_NUMBER_PATTERN=FOUR)
    stats = run_index(s)
    assert stats["projects"]["projects"] == 19 and stats["projects"]["guard_excluded"]["count"] == 21
    notices = client_for(s).get("/api/status").json()["layout"]["notices"]
    assert notices and notices[0].startswith("21 Ordner würden als Jahreszahl ausgeschlossen")
    excluded: list = []
    scan_projects(root, FOUR, 2, excluded=excluded)
    assert sorted(n for n, _ in excluded)[:2] == ["1900", "1901"]


# ============================================================================ review cases (verify2)
def test_report_folders_inside_projects_are_no_projects(tmp_path):
    """Blocker P2+2/P3+3/N10b: numbered report folders (PB-22-1001 Prüfbericht) inside the projects."""
    k = 1000
    for y in (2022, 2023, 2024):
        for i in range(1, 21):
            reps = []
            for _ in range(2):
                k += 1
                reps.append(f"PB-{y % 100}-{k:04d} Prüfbericht")
            mk(tmp_path, *(f"{y}/{y}-{i:03d} {name(i)}/{sub}" for sub in reps + ["Pläne", "Schriftverkehr"]))
    p = analyse(tmp_path).proposal
    assert (p.pattern, p.depth, p.projects) == (YEAR_NUM, 2, 60)
    assert p.inner["examples"][0].endswith("PB-22-1001 Prüfbericht")


def test_expert_opinions_and_sub_projects_inside_projects(tmp_path):
    """G1 (BV-2024-001 with G30001 Gutachten) and N09c (half of the projects hold only 2024-017-001 Bauteil)."""
    g = 30000
    for i in range(1, 31):
        subs = []
        for _ in range(1 + i % 3):
            g += 1
            subs.append(f"G{g} Gutachten")
        mk(tmp_path / "g", *(f"BV-2024-{i:03d} {name(i)}/{sub}" for sub in subs + ["Pläne", "Korrespondenz"]))
    assert analyse(tmp_path / "g").proposal.pattern == r"BV-\d{4}-\d{3}"
    for i in range(1, 41):
        subs = [f"2024-{i:03d}-{j:03d} Bauteil" for j in (1, 2, 3)] if i % 2 else SUBS
        mk(tmp_path / "n", *(f"2024-{i:03d} {name(i)}/{sub}" for sub in subs))
    p = analyse(tmp_path / "n").proposal
    assert (p.pattern, p.projects) == (YEAR_NUM, 40)


def test_customer_projects_with_one_label_are_still_projects_when_they_have_folders(tmp_path):
    """The record rule needs records: folders of one label without folders of their own. Projects
    with their own structure inside numbered customers stay the projects (verify2 c13)."""
    mk(tmp_path, *(f"K-{1020 + c} Kunde {c}/{2020 + c % 5}-{c * 3 + i:03d} Projekt/Pläne" for c in range(30)
                   for i in range(3)))
    p = analyse(tmp_path).proposal
    assert (p.pattern, p.projects) == (YEAR_NUM, 90)


@pytest.mark.parametrize("office", ["Anfragen", "Honorarangebote", "Ausschreibungen", "Ausgangsrechnungen"])
def test_bigger_numbered_office_sets_do_not_win(tmp_path, office):
    """Review I1/T3: 270 inquiries (AF-2024-001 …) next to 120 projects."""
    mk(tmp_path, *(f"{y}/{y}-{i:03d} {name(i)}/Pläne" for y in (2022, 2023, 2024) for i in range(1, 41)),
       *(f"{office}/{y}/XY-{y}-{i:03d} {name(i)}" for y in (2022, 2023, 2024) for i in range(1, 91)))
    p = analyse(tmp_path).proposal
    assert (p.pattern, p.depth, p.projects) == (YEAR_NUM, 2, 120)
    assert p.hints[0]["pattern"] == r"XY-\d{4}-\d{3}" and p.hints[0]["branch"] == "side"


def test_customer_records_do_not_win(tmp_path):
    """Review K3: Kunden/K-1001 Müller GmbH/{Verträge, Korrespondenz} ×300 next to 120 projects."""
    mk(tmp_path, *(f"{y}/{y}-{i:03d} {name(i)}/Pläne" for y in (2022, 2023, 2024) for i in range(1, 41)),
       *(f"Kunden/K-{1000 + c} Kunde {c}/{sub}" for c in range(1, 301) for sub in ("Verträge", "Korrespondenz")))
    p = analyse(tmp_path).proposal
    assert (p.pattern, p.projects) == (YEAR_NUM, 120) and p.hints[0]["branch"] == "customers"


def test_projects_below_a_customer_folder_are_still_found(tmp_path):
    """'Kunden' only yields to a numbering elsewhere – if all projects lie there, they are found."""
    mk(tmp_path, *(f"Kunden/Kunde {chr(65 + c)}/2024-{c * 10 + i:03d} {name(i)}" for c in range(6) for i in range(5)))
    assert (analyse(tmp_path).proposal.pattern, analyse(tmp_path).proposal.projects) == (YEAR_NUM, 30)


@pytest.mark.parametrize("customers", [30, 400])
def test_share_check_does_not_depend_on_the_size(tmp_path, customers):
    """Review N11b: 4-digit customers holding 5-digit projects – 400 customers gave exit 1 because
    the customers that were never opened counted as 'andere'."""
    n = 20000
    paths = []
    for c in range(customers):
        for _ in range(3):
            n += 1
            paths.append(f"{5000 + c} {NAMES[c % 10]}/{n} {name(n)}/Pläne")
    mk(tmp_path, *paths)
    p = analyse(tmp_path).proposal
    assert (p.pattern, p.projects) == (r"\d{5}", 3 * customers)
    assert p.exceptions_total == 0  # the customers are no exceptions


def test_depth_is_not_raised_by_office_folders_or_copies(tmp_path):
    """Review N02b/N03b/X1/X2: offers in the same format in Angebote/, invoices per project in
    00_Verwaltung/Rechnungen, copies in 00_Austausch and templates do not set the depth."""
    mk(tmp_path, *(f"2024-{i:03d} {name(i)}/Pläne" for i in range(1, 61)),
       *(f"Angebote/2024-{500 + i:03d} Anfrage" for i in range(1, 26)),
       *(f"00_Verwaltung/Rechnungen/2024-{i:03d}" for i in range(1, 30, 3)),
       "00_Austausch/2024-017 Pläne für Bauherr", "_Vorlagen/2024-000 Musterprojekt/01_Schriftverkehr")
    p = analyse(tmp_path).proposal
    assert (p.pattern, p.depth, p.projects) == (YEAR_NUM, 1, 60) and p.duplicates == []


def test_one_archived_project_counts(tmp_path):
    """Review S2: 100 BS projects and 1 in Archiv/ – depth 1 with one, 2 with two archived projects."""
    mk(tmp_path, *(f"BS-2024-{i:04d} {name(i)}" for i in range(1, 101)), "Archiv/BS-2015-0001 Alt")
    p = analyse(tmp_path).proposal
    assert (p.depth, p.projects) == (2, 101)


def test_unreadable_folders_are_named_in_the_reason(tmp_path, monkeypatch):
    """Review U2: every year folder is unreadable – the reason must say so."""
    mk(tmp_path, *(f"{y}/{y}-001 Kita" for y in range(2019, 2025)))
    real = os.scandir

    def scandir(path="."):
        if Path(path).parent == tmp_path:
            raise PermissionError(13, "Permission denied", str(path))
        return real(path)

    monkeypatch.setattr(structure.os, "scandir", scandir)
    a = analyse(tmp_path)
    assert a.proposal is None and a.reason.startswith("6 Ordner waren nicht lesbar (2019, 2020, 2021 …)")


def test_wrong_proposals_are_visible(tmp_path):
    """Layouts the names alone cannot decide: the proposal is wrong, but its evidence shows why –
    offers in the same number format, and customer numbers continued by 2-digit project numbers."""
    mk(tmp_path / "a", *(f"{y}/{y}-{i:03d} {name(i)}/Pläne" for y in (2023, 2024) for i in range(1, 41)),
       *(f"Angebote/{y}-{500 + i:03d} Anfrage" for y in (2023, 2024) for i in range(1, 11)))
    p = analyse(tmp_path / "a").proposal
    assert p.projects == 100 and any("20 gefundene Ordner liegen in einer Nebenablage" in n for n in p.notes)
    mk(tmp_path / "c", *(f"{1020 + c} {NAMES[c % 10]}/{1020 + c}-{i:02d} {name(c * 5 + i)}/Pläne"
                         for c in range(20) for i in range(1, 5)))
    p = analyse(tmp_path / "c").proposal
    assert p.projects == 20 and p.inner["examples"][0] == f"1020 {NAMES[0]}/1020-01 {name(1)}"
