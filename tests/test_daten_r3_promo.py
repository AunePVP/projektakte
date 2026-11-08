"""Round 3: newsletter/advertising detection on a broad matrix of realistic variants, both ways and on
both paths (plMail suggestion API and inbox).

The rule under test (assign.promotion / promo_verdict): strong signals – mass-mail header,
marketing address – decide alone; weak signals – advertising wording, sales/editorial/automatic
address, shop domain – count only for senders without project history and only when the mail names
no project specifically (project name, street address, contact person from its files). Two weak
signals make it advertising; a single one still withholds a content suggestion, since then only
generic trade vocabulary connects the mail to a project."""
import sqlite3
from email.message import EmailMessage

import pytest

from projektakte.ai.services import AIServices
from projektakte.assign import ProjectReferences, promo_verdict, suggest_for_text
from projektakte.demo import generate
from projektakte.indexer import run_index

from .conftest import fake_settings

HOTEL, KITA, SCHULE, HEIM = "BS-2024-0233", "BS-2025-0019", "BS-2024-0137", "BS-2023-0412"
GARAGE, HOCHHAUS, BUERO, HALLE = "BS-2022-0901", "BS-2024-0311", "BS-2024-0058", "BS-2023-0777"

# (name, expected project or None, sender, subject, body, headers)
PROJECT_MAILS = [
    ("Errichter-Angebot mit Rabatt und Sonderpreis, nennt Objekt", HOTEL, "vertrieb@stahlbau-kurz.example",
     "Hotel Seeblick – Fluchtbalkon am Stichflur",
     "Guten Tag, für den Fluchtbalkon am Stichflur des Hotel Seeblick machen wir Ihnen einen Sonderpreis: 5 % "
     "Rabatt bei Beauftragung bis Monatsende. Die Bauabschnitte stimmen wir mit dem Hotelbetrieb ab.", ()),
    ("Portal-Benachrichtigung noreply@ mit Anschrift", HEIM, "noreply@vergabe-portal.example",
     "Neue Nachricht im Projektraum",
     "Im Projektraum zum Bauvorhaben Am Sonnenhang 21 liegt eine neue Nachricht: Die Feststellanlagen an den "
     "Rauchschutztüren der Wohnbereiche und die Brandmeldeanlage werden in Rauchabschnitten geprüft. Die "
     "Räumungsübung in der Nachtschicht ist zu dokumentieren.", ()),
    ("Schulung mit Gutschein, nennt Schule und Straße", SCHULE, "kontakt@brandschutz-akademie.example",
     "Brandschutzhelfer Grundschule Am Lindenhof",
     "Hallo, für die Brandschutzhelfer der Grundschule Am Lindenhof, Lindenhofweg 4, liegt der Gutschein für die "
     "Unterweisung bei. Wir üben die Räumung über die Außentreppe an der Ostseite und die Alarmierung mit der "
     "Sprachalarmanlage.", ()),
    ("Kostenloses Webinar, nennt Ansprechpartnerin und Anschrift", GARAGE, "s.berg@lueftung-berg.example",
     "Entrauchung Garage – Abstimmung",
     "Sehr geehrte Frau Vogt, wie im kostenlosen Webinar angekündigt prüfen wir die maschinelle Entrauchung der "
     "Tiefgarage am Marktplatz 1 mit der CO-Warnanlage. Die Rampe und die Zuluftnachströmung nehmen wir mit auf.",
     ()),
    ("Nur der Bauherr als Ansprechpartner genannt, mit Preisliste", HOTEL, "info@balkon-technik.example",
     "Fluchtbalkon und Sicherheitsbeleuchtung",
     "Sehr geehrter Herr Rehberg, anbei unsere Preisliste für die Stahlteile des Fluchtbalkons am Stichflur im "
     "3. Obergeschoss und für die Sicherheitsbeleuchtung im Restaurant. Den Einbau planen wir in den "
     "Bauabschnitten während des Hotelbetriebs, abgestimmt mit der Beherbergungsstättenverordnung.", ()),
    ("noreply@ eines Planservers nur mit Anschrift", HOCHHAUS, "no-reply@planserver.example",
     "Neuer Plan hochgeladen: Parkallee 50",
     "Für das Bauvorhaben Parkallee 50 wurde ein neuer Plan hochgeladen: Feuerwehraufzug mit Vorraum, nasse "
     "Steigleitungen mit Wandhydranten, Druckerhöhungsanlage und die Aufstellfläche an der Parkallee.", ()),
    ("Errichter mit Stückpreis und Staffelpreis, nennt Kita und Leitung", KITA, "m.weber@melder-weber.example",
     "Rauchwarnmelder Kita Regenbogen",
     "Guten Tag, für die Kita Regenbogen am Regenbogenweg 3 bieten wir die funkvernetzten Rauchwarnmelder für "
     "Schlafraum und Gruppenräume mit Alarmweiterleitung ins Leitungsbüro an – Stückpreis 79 EUR, Staffelpreis "
     "ab 20 Stück.", ()),
]
ADVERTISING = [
    ("Personenadresse, nur ein Werbewort (Gutschein)", None, "a.klein@schulungen-klein.example",
     "Brandschutzschulung für Pflegeheime",
     "Für Pflegeheime mit Rauchabschnitten, Feststellanlagen und Brandmeldeanlage bieten wir Schulungen der "
     "Pflegekräfte zur horizontalen Evakuierung an – mit Gutschein für Ihr Team.", ()),
    ("info@, nur ein Werbewort (Webinar)", None, "info@entrauchung-info.example", "Webinar Tiefgaragen",
     "Maschinelle Entrauchung von Tiefgaragen, CO-Warnanlage, Rampen und Zuluftnachströmung – die Grundlagen "
     "für Planer.", ()),
    ("Seminaradresse mit Frühbucher", None, "seminar@brandschutz-forum.example", "Fachtagung Schulbau",
     "Rauchableitung in Treppenräumen von Grundschulen, Sprachalarmanlagen und Außentreppen – Frühbucher bis "
     "Ende des Monats.", ()),
    ("Shop-Domain und versandkostenfrei", None, "service@melder-shop24.example", "Rauchwarnmelder für Kitas",
     "Funkvernetzte Rauchwarnmelder für Schlafräume und Gruppenräume in Kindertagesstätten, mit "
     "Alarmweiterleitung – versandkostenfrei.", ()),
    ("Newsletter-Adresse", None, "newsletter@fachverlag-bau.example", "Ausgabe November",
     "Feststellanlagen an Rauchschutztüren, Brandmeldeanlagen in Pflegeheimen und Räumungsübungen.", ()),
    ("List-Unsubscribe von unbekanntem Absender", None, "post@verband-sicherheit.example", "Mitgliederinfo",
     "Druckbelüftungsanlagen für Sicherheitstreppenräume in Bürogebäuden: Prüfung durch Prüfsachverständige.",
     (("List-Unsubscribe", "<mailto:abmelden@verband-sicherheit.example>"),)),
    ("Personenadresse, nur Produktneuheit", None, "t.lang@tuertechnik-lang.example", "Feststellanlagen",
     "Unsere Produktneuheit: Feststellanlagen für Rauchschutztüren in Schulfluren, mit Rauchschalter und "
     "Freilauftürschließer.", ()),
    ("Versandbestätigung von noreply@, kein Werbewort", None, "noreply@melder-versand.example",
     "Ihre Lieferung ist unterwegs",
     "20 funkvernetzte Rauchwarnmelder für Schlafräume und Gruppenräume mit Alarmweiterleitung sind unterwegs.",
     ()),
    ("Werbung nennt eine Stadt, kein Projekt", None, "k.ott@melder-ott.example", "Rauchwarnmelder in Beispielstadt",
     "Funkvernetzte Rauchwarnmelder für Kitas in Beispielstadt: Schlafraum, Gruppenraum, Alarmweiterleitung – "
     "Sonderpreis für Träger.", ()),
    ("Fachartikel mit Gattungswort im Plural", None, "redakteur@bauzeitung.example", "Tiefgaragen",
     "Fachartikel: Entrauchung von Tiefgaragen unter Marktplätzen mit CO-Warnanlage und Rampen.", ()),
]
CASES = PROJECT_MAILS + ADVERTISING


@pytest.fixture(scope="module")
def demo(tmp_path_factory, fake_ai):
    tmp = tmp_path_factory.mktemp("promo")
    generate(tmp / "demo")
    s = fake_settings(fake_ai, tmp, DEMO_MODE=1)
    ai = AIServices.from_settings(s)
    run_index(s, ai)
    return s, ai


def _conn(s):
    conn = sqlite3.connect(s.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def test_matrix_both_ways_on_both_paths(demo):
    s, ai = demo
    conn = _conn(s)
    wrong = []
    for name, expected, sender, subject, body, headers in CASES:
        res = suggest_for_text(conn, ai, str(s.db_path), s.project_number_pattern, subject=subject, body=body,
                               attachments=[], sender=sender, headers=dict(headers))
        if expected is None:
            if res:
                wrong.append(("API", name, res))
        else:
            top = [r["number"] for r in res if r["confidence"] == "hoch"]
            if top != [expected]:
                wrong.append(("API", name, res))
    # inbox: the same mails as .eml files, one indexing run
    for i, (name, expected, sender, subject, body, headers) in enumerate(CASES):
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = sender, "buero@ib-feuerfest.example", subject
        m["Date"] = "Wed, 05 Nov 2025 10:00:00 +0100"
        m["Message-ID"] = f"<matrix-{i}@test.example>"
        for k, v in headers:
            m[k] = v
        m.set_content(body)
        (s.mail_dir / f"2025-11-05_matrix_{i:02d}.eml").write_bytes(bytes(m))
    run_index(s, ai)
    for i, (name, expected, *_rest) in enumerate(CASES):
        r = conn.execute("SELECT project, suggested_project, assign_status, assign_reason FROM mails WHERE "
                         "source_key = ?", (f"eml:2025-11-05_matrix_{i:02d}.eml",)).fetchone()
        if (r["project"], r["suggested_project"]) != (None, expected):
            wrong.append(("Eingang", name, dict(r)))
        # the rule decides – not merely too little shared vocabulary
        if ("Werbung" in r["assign_reason"]) != (expected is None):
            wrong.append(("Eingang-Begründung", name, r["assign_reason"]))
    assert not wrong, "\n".join(map(str, wrong))


def test_reasons_say_why(demo):
    s, _ai = demo
    conn = _conn(s)
    refs = ProjectReferences(conn)
    # the advertising reason names the signals; a single signal explains why there is no suggestion
    two = promo_verdict("Fachtagung", "Frühbucher bis Ende des Monats", "seminar@forum.example", bulk=None,
                        sender_known=False, references=refs.find("Fachtagung Frühbucher"))
    assert two.promotion == "Absender seminar@forum.example, Wort „Frühbucher“"
    one = promo_verdict("Webinar Tiefgaragen", "Entrauchung", "info@x.example", bulk=None, sender_known=False,
                        references={})
    assert (one.promotion, one.hint) == (None, "Wort „Webinar“")
    # history beats every weak signal, strong signals stay strong
    assert promo_verdict("Webinar", "Sonderpreis, jetzt bestellen", "info@x.example", bulk=None,
                         sender_known=True) == promo_verdict("x", "y", "a@b.example", bulk=None, sender_known=False)
    assert promo_verdict("Hotel Seeblick", "Uferpromenade 15", "newsletter@x.example", bulk=None,
                         sender_known=False, references={HOTEL: ["Seeblick"]}).promotion


def test_project_references(demo):
    s, _ai = demo
    refs = ProjectReferences(_conn(s))
    assert refs.find("Angebot für die Kita Regenbogen") == {KITA: ["Regenbogen"]}
    assert refs.find("Kindertagesstätte am Regenbogenweg 3") == {KITA: ["Regenbogenweg 3"]}
    assert refs.find("Sehr geehrte Frau Lindqvist,") == {KITA: ["Frau Lindqvist"]}
    assert refs.find("Seniorenresidenz am Sonnenhang") == {HEIM: ["Sonnenhang"]}
    assert refs.find("Halle am Nordring, Löschwasserteich") == {HALLE: ["Nordring"]}
    assert refs.find("Marktplatz-Garage") == {GARAGE: ["Marktplatz"]}
    assert refs.find("Bürogebäude Hafenkante 7")[BUERO]
    # no reference: building types, plurals of common nouns, towns and office staff found everywhere,
    # numbers that are no street address
    for text in ("Rauchwarnmelder für Kitas und Kindertagesstätten", "Tiefgaragen unter Marktplätzen",
                 "Grundschulen und Hotels in Musterstadt", "Sehr geehrter Herr Brenner",
                 "Brandmeldeanlage Kategorie 2 nach DIN 14675", "Seite 3 von 7"):
        assert refs.find(text) == {}, text
