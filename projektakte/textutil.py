"""Small German text helpers shared by search, assignment and the fake AI services."""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("projektakte.textutil")

_WORD = re.compile(r"[^\W_]+", re.UNICODE)

STOPWORDS = frozenset(
    """
    a ab aber alle allem allen aller alles als also am an ander andere anderen auch auf aus bei beim
    bin bis bitte bzw da dabei damit dann das dass dein deine dem den denn der deren des dessen die
    dies diese diesem diesen dieser dieses doch dort du durch ein eine einem einen einer eines einige
    er es etwa etwas euch euer fur gegen gibt hab habe haben hat hatte hatten hier hin hinter ich ihm
    ihn ihnen ihr ihre im in ins ist ja jede jedem jeden jeder jedes jetzt kann kein keine konnen
    konnte machen man mehr mein meine mit muss mussen nach nicht nichts noch nun nur ob oder ohne sehr
    sein seine sich sie sind so soll sollen sondern sowie uber um und uns unser unter vom von vor
    wann war waren warum was weil welche welchem welchen welcher welches wenn wer werden wie wieder
    will wir wird wo wurde wurden zu zum zur zwar zwischen
    gilt gelten steht stehen gibt geben laut bitte sowie hallo guten tag viele grusse freundlichen
    mfg sehr geehrte geehrter damen herren liebe lieber
    """.split()
)

# Words that only shape a question and carry no content (used by the fake LLM and "not found").
QUESTION_WORDS = frozenset(
    "welche welcher welches welchen wer wie was wann wo warum wieso weshalb gibt es ist sind hat "
    "haben wurde wurden werden kann muss mussen soll sollen darf durfen "
    "worum wofur wozu woraus womit wodurch wonach worauf woruber wovon wohin woher wieviel wieviele".split()
)

# Filler words of typed questions ("In welchen Projekten ist … vorgesehen?"): light verbs, generic
# nouns and forms of address. They carry no topic, so question-side matching (full-text expansion,
# fake reranker and fake chat) ignores them. Not used for mails/documents (assignment).
QUERY_FILLER = frozenset(
    """
    projekt projekte projekten projekts projektes bauvorhaben bauvorhabens vorhaben objekt objekte
    objekten unterlagen dokument dokumente dokumenten quelle quellen akte information informationen
    vorgesehen vorsehen sieht geplant planen plant gefordert fordert fordern verlangt gibt geben
    gegeben steht stehen genannt erwahnt beschrieben drin denn eigentlich genau jeweils bitte uns
    braucht brauchen benotigt benotigen notig bekommt bekommen erhalt erhalten kommt kommen bauen baut
    gebaut errichtet errichten
    geht gehen sagt sagen gesagt wissen weiss gilt gelten funktionieren funktioniert durchhalten
    beachten beachtet beachtung tun macht machen gemacht lassen worden wird werden sein seien hatte
    passiert passieren geschehen getan gelaufen verfugung
    anforderung anforderungen vorgabe vorgaben regelung regelungen bestimmung bestimmungen massnahme massnahmen
    gelost geregelt gestaltet bewertet bewerten beurteilt beurteilen eingeschatzt einschatzen
    nummer
    festlegung festgehalten festgelegt vereinbart beschlossen besprochen notiert dokumentiert protokolliert
    festlegungen angabe angaben thema themen punkt punkte details detail infos info sache situation
    art sorte typ frau herr herrn dr dipl ing jemand irgendwas irgendwelche
    kurz kurze kurzen kurzer kurzes knapp knappe knappen grob mal einmal gern gerne schnell eben
    mir mich dir dich gib gebt zeig zeige zeigen erzahl erzahle erzahlt erzahlen nenn nenne nennen
    ruckmeldung ruckmeldungen
    ihren ihrem ihrer ihres seinen seinem seiner seines unsere unseren unserem unserer unseres meinen
    meinem meiner meines eure euren eurem
    """.split()
)

# Words asking for the latest state ("Was ist der letzte Stand …?", "Wurde … schon …?", "Was gibt es
# Neues …?", "Was hat … zuletzt geschrieben?").
TEMPORAL_WORDS = frozenset(
    """
    letzt letzte letzter letzten letztes zuletzt letztens aktuell aktuelle aktueller aktuellen aktuelles
    neueste neuester neuesten neuestes neuste neusten neues neuigkeit neuigkeiten neuerdings neulich
    kurzlich jungst jungste jungsten stand stande sachstand zwischenstand inzwischen mittlerweile schon
    bereits derzeit momentan gerade jetzt update
    """.split()
)


def fold(text: str) -> str:
    """Lowercase and strip diacritics (ä→a, ß→ss) – matches FTS5 'remove_diacritics'."""
    text = text.lower().replace("ß", "ss")
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c))


def words(text: str) -> list[str]:
    return _WORD.findall(fold(text))


_SUFFIXES = ("ungen", "ern", "em", "en", "er", "es", "e", "n", "s")
# separable verbs with "zu" infix: abzunehmen → abnehmen, aufzuschalten → aufschalten
_ZU_INFIX = re.compile(r"^(ab|an|auf|aus|bei|ein|fest|frei|her|hin|mit|nach|vor|weg|zuruck|zusammen)zu(\w{3,}n)$")


# feminine forms of persons: Prüfingenieurin → Prüfingenieur, Ansprechpartnerinnen → Ansprechpartner
def _keeps_final_s(word: str) -> bool:
    return word.endswith(("haus", "ss", "os")) or (word.endswith("us") and not word.endswith("aus"))
_FEMININE = re.compile(r"^(\w{4,}(?:er|eur|rat|ent|ant|ist|or|ar|at|kt|eg|og))in(?:nen)?$")


@lru_cache(maxsize=131072)
def stem(word: str) -> str:
    """Very light German stemmer: enough to match Rettungsweg/Rettungswege/Rettungswegs."""
    if word.isdigit():
        return word
    m = _ZU_INFIX.match(word)
    if m:
        word = m.group(1) + m.group(2)
    m = _FEMININE.match(word)
    if m:
        word = m.group(1)
    for suf in _SUFFIXES:
        if suf == "s" and _keeps_final_s(word):  # Hochhaus, Status – no genitive -s (but Umbaus → Umbau)
            continue
        if len(word) - len(suf) >= 4 and word.endswith(suf):
            if suf == "ungen":
                return word[: -len(suf)] + "ung"
            return word[: -len(suf)]
    return word


def content_terms(text: str, extra_stop: frozenset[str] = frozenset()) -> list[str]:
    """Stemmed content words without stop words, in order, duplicates removed."""
    seen: dict[str, None] = {}
    for w in words(text):
        if (len(w) < 2 and not w.isdigit()) or w in STOPWORDS or w in extra_stop:
            continue
        seen.setdefault(stem(w), None)
    return list(seen)


def term_in(term: str, doc_terms: set[str], doc_text_folded: str | None = None) -> bool:
    """Does a (stemmed) query term occur in a document?

    Accepts exact stems, compounds that start with the term (Rauchableitung → Rauchableitungsöffnung)
    and, for longer words, the head of a compound term (Baubesprechung → Besprechung). German
    compounds carry their meaning in the last part: a Löschwasserteich is a Teich, so "Löschwasser"
    alone does not match it, and "Projektbezug" does not match "Projekt".
    """
    if term in doc_terms:
        return True
    if len(term) >= 5:
        for t in doc_terms:
            if t.startswith(term) or (len(t) >= 6 and len(term) - len(t) >= 3 and term.endswith(t)):
                return True
    return False


_DATE_DE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def find_date(text: str) -> date | None:
    """First plausible date in a text (German 12.03.2024 or ISO 2024-03-12)."""
    candidates = []
    for m in _DATE_ISO.finditer(text):
        candidates.append((m.start(), int(m.group(1)), int(m.group(2)), int(m.group(3))))
    for m in _DATE_DE.finditer(text):
        candidates.append((m.start(), int(m.group(3)), int(m.group(2)), int(m.group(1))))
    for _, y, mo, d in sorted(candidates):
        try:
            if 1990 <= y <= 2100:
                return date(y, mo, d)
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- dates
try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo("Europe/Berlin")
except Exception:  # noqa: BLE001 – no tz database available: fall back to UTC
    LOCAL_TZ = timezone.utc


def local_date(value: str | None) -> date | None:
    """Calendar date of an ISO date/datetime in the office's time zone (Europe/Berlin).

    Mails are stored in UTC ("2024-06-17T22:30:00+00:00"); shown as a date they must use the local
    day (18.06.2024). Plain dates ("2024-06-18") are returned unchanged.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(LOCAL_TZ)
    return dt.date()


def de_date(value: str | None, default: str = "?") -> str:
    """German date (TT.MM.JJJJ) of an ISO date/datetime, in local time."""
    d = local_date(value)
    return d.strftime("%d.%m.%Y") if d else default


# --------------------------------------------------------------------------- word variants
# Separable verb prefixes: "abgenommen" → "ab" + "genommen", "einzubauen" → "ein" + "zu" + "bauen".
_SEP_PREFIX = ("zuruck", "zusammen", "statt", "durch", "fest", "frei", "nach", "teil", "uber", "auf", "aus", "bei",
               "ein", "her", "hin", "mit", "vor", "weg", "ab", "an", "um", "zu")
_PARTICIPLE = re.compile(r"^(%s)?ge(\w{2,}?)(t|et|en)$" % "|".join(_SEP_PREFIX))
_ZU_ANY = re.compile(r"^(%s)zu(\w{3,}en)$" % "|".join(_SEP_PREFIX))
_DENOMINAL = re.compile(r"^(?:be|ver)(\w{5,})$")
_WEAK_PARTICIPLE = re.compile(r"^((be|ge|er|ver|zer|ent|emp)\w{3,}t|\w{3,}(iert|igt))$")
# Ablaut series of strong verbs common in project files (Abnahme/abnehmen/abgenommen …).
_ABLAUT = (("nehm", "nahm", "nomm", "nimm"), ("schliess", "schloss"), ("zieh", "zog", "zug"),
           ("find", "fand", "fund"), ("steh", "stand"), ("halt", "hielt"), ("geb", "gab"),
           ("schreib", "schrieb"), ("treff", "traf", "troff"), ("fehl", "fiehl", "fahl", "fohl"))


@lru_cache(maxsize=65536)
def word_variants(word: str) -> frozenset[str]:
    """Stems of a folded word plus base forms of participles and strong-verb forms.

    ``abgenommen`` → {abgenomm, abnomm, abnehm, abnahm}; ``eingebaut`` → {eingebaut, einbau};
    ``Abnahme`` → {abnahm, abnehm, abnomm}. Enough to match questions and texts that use different
    word forms of the same verb or noun.
    """
    return frozenset({stem(word)} | set(verb_bases(word)))


@lru_cache(maxsize=65536)
def verb_bases(word: str) -> tuple[str, ...]:
    """Verb stems hidden in a folded word: participles, zu-infinitives and ablaut forms."""
    bases: set[str] = set()
    m = _PARTICIPLE.match(word)
    if m and len(word) >= 5:
        bases.add((m.group(1) or "") + m.group(2))
    m = _ZU_ANY.match(word)
    if m:
        bases.add(stem(m.group(1) + m.group(2)))
    if word.endswith("ern") and len(word) >= 6 and not word.endswith("tern"):  # sichern → sicher (stem: sich)
        bases.add(word[:-1])
    if _WEAK_PARTICIPLE.match(word):  # genehmigt → genehmig, beantragt → beantrag, installiert → installier
        bases.add(word[:-1])
        m = _DENOMINAL.match(word[:-1])  # bemängelt → mangel, beantragt → antrag
        if m:
            bases.add(m.group(1))
    plain = {stem(word)} | ({word[:-1]} if word.endswith("t") and len(word) >= 5 else set())  # nimmt → nimm
    for b in list(bases or plain):
        for series in _ABLAUT:  # only as the last morpheme: Abnahme, abgenommen – not Gebäude
            form = next((f for f in series if b.endswith(f)), None)
            if form:
                bases.update(b[: -len(form)] + other for other in series)
    bases.discard(stem(word))
    return tuple(sorted(b for b in bases if len(b) >= 2))


def verbal_noun(word: str) -> str | None:
    """Nominalisation of a verb form: 'prüfen' → 'prufung', 'geübt' → 'ubung', 'evakuieren' →
    'evakuierung'. Only for infinitives and participles."""
    m = _PARTICIPLE.match(word)
    if m and len(word) >= 5:
        if m.group(3) == "en":  # strong verbs change their stem (genommen ≠ Nahme)
            return None
        base = (m.group(1) or "") + m.group(2)
    elif word.endswith("en") and len(word) >= 6:
        base = word[:-2]
    else:
        return None
    if base.endswith(("ung", "e")) or len(base) < 2:
        return None
    return base + "ung"


def verbal_nouns(word: str) -> list[str]:
    """All nominalisations of a verb form: 'prüfen' → [prufung], 'kompensieren' → [kompensierung,
    kompensation] (-ieren verbs also form nouns on -ation: installieren → Installation)."""
    vn = verbal_noun(word)
    if not vn:
        return []
    out = [vn]
    if vn.endswith("ierung") and len(vn) > 9:
        out.append(vn[: -len("ierung")] + "ation")
    return out


# --------------------------------------------------------------------------- synonyms
BUILTIN_SYNONYMS = """
# Abkürzungen und Fachsynonyme aus dem Brandschutz und dem Bauwesen. Eine Gruppe je Zeile:
#   "A = B = C"  gleichbedeutend (in beide Richtungen)
#   "A -> B"     A wird zu B erweitert, aber nicht umgekehrt (für mehrdeutige Kürzel und Wörter)
# Die Liste erweitert auch die Volltextsuche der App: nur Wörter, die in jedem Satz dasselbe meinen –
# keine Lesart einer bestimmten Frageform (z. B. "geschrieben" als "Verfasser:" in "Wer hat … geschrieben?",
# die der Fake-Leser selbst kennt).
BMA = Brandmeldeanlage
BMZ = Brandmelderzentrale
RWA = Rauch- und Wärmeabzugsanlage = Rauch- und Wärmeabzug = Rauchabzugsanlage = Rauchabzug = Rauchableitung = Entrauchung = Entrauchungsanlage
SAA = Sprachalarmanlage = ELA
SiBe = Sicherheitsbeleuchtung = Notbeleuchtung = Notlicht
RDA = DBA = Druckbelüftungsanlage = Druckbelüftung = Überdruckanlage = Überdruckbelüftung = Rauchschutzdruckanlage
FSA = Feststellanlage
BSK = Brandschutzklappe
RWM = Rauchwarnmelder = Rauchmelder
FSD = Feuerwehrschlüsseldepot
FBF = Feuerwehrbedienfeld
FAT = Feuerwehranzeigetableau
Treppenraum = Treppenhaus
Rettungsweg = Fluchtweg
OG = Obergeschoss
DG = Dachgeschoss
TG = Tiefgarage = Garage
GK = Gebäudeklasse
EG -> Erdgeschoss
UG -> Untergeschoss
KG -> Kellergeschoss
PI -> Prüfingenieur
PSV -> Prüfsachverständiger
BSB -> Brandschutzbeauftragter
Feuerwehr = Brandschutzdienststelle
Bauaufsicht = Bauaufsichtsbehörde = Bauamt = Bauordnungsamt = Baubehörde
Drehleiter = Hubrettungsfahrzeug
Bedenken = Einwände = Einwand = Einwendungen
Räumung = Evakuierung
Material = Baustoff
Löschwasserbedarf = Löschwassermenge
Neuigkeiten = Neues = Neuigkeit = Sachstand = aktueller Stand
FW = Feuerwehr
BSD = Brandschutzdienststelle
FwA = FWA = Feuerwehraufzug
FIZ = Feuerwehrinformationszentrale
ÜE = Übertragungseinrichtung
DEA = Druckerhöhungsanlage
WH = Wandhydrant
TSL = trockene Steigleitung
NSL = nasse Steigleitung
STR = Sicherheitstreppenraum
TRH -> Treppenraum
NRA = NRWG = natürliche Rauchabzugsanlage = Rauchabzugsgeräte
MRA = maschinelle Rauchabzugsanlage
RST = Rauchschutztür
BSH = Brandschutzhelfer
BSO = Brandschutzordnung
BSN = Brandschutznachweis
BSK -> Brandschutzkonzept, Brandschutzklappe
BG -> Baugenehmigung
PB -> Prüfbericht
LV = Leistungsverzeichnis
SV -> Sachverständiger
DLK = Drehleiter
Kat = Kategorie
Az = Aktenzeichen
KW = Kalenderwoche
MHHR = Muster-Hochhaus-Richtlinie
IndBauRL = Industriebaurichtlinie
MBO = Musterbauordnung
LBO = BauO = Landesbauordnung = Bauordnung
GarVO = GaVO = Garagenverordnung
BeVO = BStättV = Beherbergungsstättenverordnung
SchulbauR = SchulbauRL = MSchulbauR = Schulbaurichtlinie
PrüfVO = PrüfVV = Prüfverordnung
FwP = FWP = Feuerwehrplan
PrüfIng = Prüfingenieur
HM = Handmelder = Handfeuermelder = Handtaster
ÜH = Überflurhydrant
UH -> Unterflurhydrant
TH -> Treppenhaus
BH -> Bauherr
BL -> Bauleitung
AG -> Auftraggeber
AN -> Auftragnehmer
WE -> Wohneinheit, Wohnung
NE -> Nutzungseinheit
SB -> Sicherheitsbeleuchtung
NB -> Notbeleuchtung
RM -> Rauchmelder
LW -> Löschwasser
EP -> Einstellplatz
Stpl = Stellplatz
TGA = technische Gebäudeausrüstung
Std -> Stunde
Stk -> Stück
Kita = Kindertagesstätte = Kindergarten
BS -> Brandschutz
BV -> Bauvorhaben
Abn -> Abnahme
Abw -> Abweichung
Genehm -> Genehmigung
Notausgang = Ausgang ins Freie
Adresse = Anschrift
unterschreiben = unterzeichnen = Unterschrift
Adresse -> Grundstück
# allgemeine Bau- und Alltagswörter
Geschoss = Stockwerk = Etage
Stock -> Obergeschoss = Geschoss
Fassade = Außenwand = Außenwandbekleidung
Lüfter = Ventilator = Gebläse
Stellplatz = Einstellplatz = Parkplatz
Ansprechpartner = Ansprechperson = Kontaktperson = Vertreter = vertreten
Feueralarm = Brandalarm = Alarm
benachrichtigen = alarmieren = verständigen
Inbetriebnahme = Nutzungsaufnahme
Mitarbeiter = Beschäftigte = Personal
anfangen = beginnen = Beginn = starten
aufmachen = eröffnen = Eröffnung
Löschfahrzeug = Einsatzfahrzeug = Feuerwehrfahrzeug
ansaugen -> Saugstelle
ausschreiben -> Leistungsverzeichnis, Ausschreibung
Ausschreibung -> Leistungsverzeichnis
Bewohner -> Pflegeplatz
"""


@dataclass(frozen=True)
class Synonyms:
    """Synonym groups as phrases of folded words: ('rauch', 'warmeabzug') for 'Rauch- und Wärmeabzug'."""

    groups: tuple[tuple[tuple[str, ...], ...], ...] = ()
    one_way: tuple[tuple[tuple[str, ...], tuple[tuple[str, ...], ...]], ...] = ()

    def alternatives(self, term: str) -> list[tuple[str, ...]]:
        """Other phrasings of a stemmed term as tuples of stems (a phrase has several).

        A term matches a group word exactly or as the first part of a compound; then the rest of the
        compound is kept: ``entrauchungsoffnung`` → (``rauchableitung``, ``offnung``), …
        """
        return list(_alternatives(self, term))

    def _alternatives(self, term: str) -> list[tuple[str, ...]]:
        """Other phrasings of a stemmed term as tuples of stems (a phrase has several).

        A term matches a group word exactly or as the first part of a compound; then the rest of the
        compound is kept: ``entrauchungsoffnung`` → (``rauchableitung``, ``offnung``), …
        """
        out: list[tuple[str, ...]] = []

        def rest_of(key: tuple[str, ...]) -> str | None:
            """'' for an exact match, the remaining compound part, or None."""
            if len(key) != 1:
                return None
            word = key[0]
            if term == word or term == stem(word):
                return ""
            for c in sorted({word, stem(word)}, key=len, reverse=True):
                if len(c) >= 5 and term.startswith(c) and len(term) - len(c) >= 4:
                    rest = term[len(c):]
                    return rest[1:] if rest.startswith("s") and len(rest) > 4 else rest
            return None

        def stems(key: tuple[str, ...]) -> tuple[str, ...]:
            return tuple(stem(w) for w in key)

        for group in self.groups:
            for key in group:
                rest = rest_of(key)
                if rest is None:
                    continue
                for other in group:
                    if other is key:
                        continue
                    out.append(stems(other) + ((rest,) if rest else ()))
                break
        for key, targets in self.one_way:
            rest = rest_of(key)
            if rest is not None:
                out.extend(stems(t) + ((rest,) if rest else ()) for t in targets)
        return [k for k in dict.fromkeys(out) if k != (term,)]

    def expanded(self, term: str) -> list[tuple[str, ...]]:  # noqa: D401
        """:meth:`alternatives` plus the alternatives of single-word alternatives (one more step:
        Stock → Obergeschoss → OG)."""
        first = self.alternatives(term)
        out = list(first)
        for alt in first:
            if len(alt) == 1:
                out.extend(a for a in self.alternatives(alt[0]) if a != (term,))
        return list(dict.fromkeys(out))


@lru_cache(maxsize=65536)
def _alternatives(syn: Synonyms, term: str) -> tuple[tuple[str, ...], ...]:
    return tuple(syn._alternatives(term))


def _phrase_key(phrase: str) -> tuple[str, ...]:
    return tuple(w for w in words(phrase) if w not in STOPWORDS and (len(w) >= 2 or w.isdigit()))


def parse_synonyms(text: str) -> Synonyms:
    """Parse the synonym format (see BUILTIN_SYNONYMS). Lines without a group are ignored."""
    groups, one_way = [], []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "->" in line:
            left, right = line.split("->", 1)
            key = _phrase_key(left)
            targets = tuple(k for k in (_phrase_key(x) for x in re.split(r"[=,;]", right)) if k)
            if key and targets:
                one_way.append((key, targets))
            continue
        keys = tuple(dict.fromkeys(k for k in (_phrase_key(p) for p in re.split(r"[=,;]", line)) if k))
        if len(keys) >= 2:
            groups.append(keys)
    return Synonyms(tuple(groups), tuple(one_way))


def merge_synonyms(*items: Synonyms) -> Synonyms:
    return Synonyms(tuple(g for s in items for g in s.groups), tuple(o for s in items for o in s.one_way))


@lru_cache(maxsize=1)
def builtin_synonyms() -> Synonyms:
    return parse_synonyms(BUILTIN_SYNONYMS)


@lru_cache(maxsize=16)
def _file_synonyms(path: str, mtime: float) -> Synonyms:
    try:
        return parse_synonyms(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("Synonymdatei nicht lesbar (%s): %s", path, exc)
        return Synonyms()


def load_synonyms(path: str | None = None) -> Synonyms:
    """Built-in list plus an optional file (SYNONYMS_FILE, same format); re-read when it changes."""
    base = builtin_synonyms()
    if not path:
        return base
    try:
        mtime = Path(path).stat().st_mtime
    except OSError:
        log.warning("Synonymdatei nicht gefunden: %s", path)
        return base
    return _merged_synonyms(path, mtime)


@lru_cache(maxsize=16)
def _merged_synonyms(path: str, mtime: float) -> Synonyms:
    """One object per file version – the word caches keyed by it stay warm."""
    return merge_synonyms(builtin_synonyms(), _file_synonyms(path, mtime))


# --------------------------------------------------------------------------- question analysis
_WORD_CASED = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True)
class QueryTerm:
    """One content word of a question with everything it may match."""

    term: str  # stem of the folded word
    word: str  # folded word as typed
    noun: bool  # capitalised (or an abbreviation/number) – nouns carry the topic in German
    syns: tuple[tuple[str, ...], ...] = ()  # abbreviations/synonyms (phrases as tuples of stems)
    forms: tuple[str, ...] = ()  # other word forms (participle bases, ablaut, verbal noun)
    attributive: bool = False  # lower-case word right before a noun ("nasse Steigleitung")
    weak: bool = False  # set by callers for words that mainly shape the question ("wie lange")
    attr: str | None = None  # value type asked for by the compound head ("Löschwassermenge" → quantity)
    heads: tuple[str, ...] = ()  # nouns that match as a word or compound head ("hoch" → Höhe, Gebäudehöhe)
    umlaut: bool = False  # typed with ä/ö/ü ("Lüfter" is no plural of "Luft": no compounds by its stem)

    @property
    def weight(self) -> float:
        """Nouns carry the topic; attributes narrow it; verbs and adverbs vary most between a
        question and the text that answers it."""
        if self.weak:
            return 0.25
        if self.noun:
            return 1.0
        return 0.75 if self.attributive else 0.4

    @property
    def alts(self) -> tuple[tuple[str, ...], ...]:
        return self.syns + tuple((f,) for f in self.forms) + tuple((h,) for h in self.heads)


# Compound heads that ask for a value of the thing named before them: "Löschwassermenge" asks for the
# amount of Löschwasser, "Abnahmetermin" for the date of the Abnahme, "Baukosten" for money.
ATTRIBUTE_HEADS = {
    "menge": "quantity", "bedarf": "quantity", "anzahl": "count", "zahl": "count", "dauer": "duration",
    "hohe": "measure", "breite": "measure", "lange": "measure", "tiefe": "measure", "grosse": "measure",
    "kosten": "money", "preis": "money", "preise": "money", "miete": "money", "honorar": "money",
    "gebuhr": "money", "gebuhren": "money", "datum": "date", "termin": "date", "zeitpunkt": "date",
    "temperatur": "temperature", "gewicht": "weight", "farbe": "color", "hersteller": "name",
}


def attribute_head(word: str) -> tuple[str, str] | None:
    """(stem of the thing or "", value type) of a folded attribute noun ("kosten" → ("", "money"),
    "loschwassermenge" → ("loschwass", "quantity"), "baukosten" → ("", "money")), else None."""
    for head in sorted(ATTRIBUTE_HEADS, key=len, reverse=True):
        if word == head or word == head + "n" or word == head + "en":
            return "", ATTRIBUTE_HEADS[head]
        if word.endswith(head) and len(word) - len(head) >= 2:
            modifier = word[: -len(head)]
            if modifier.endswith("s") and len(modifier) > 5:  # Fugen-s: Betriebsdauer
                modifier = modifier[:-1]
            return (stem(modifier) if len(modifier) >= 4 else ""), ATTRIBUTE_HEADS[head]
    return None


_ADJ_ENDINGS = ("e", "en", "er", "es", "em")
_COMPARATIVE = re.compile(r"^(\w{4,}?)er(e|en|es|em|er)?$")
# what a measuring adjective is called as a noun: "wie hoch" ↔ "Höhe: 68 m", "Gebäudehöhe 21,5 m"
ADJECTIVE_NOUNS = {
    "hoch": ("hohe",), "hoh": ("hohe",), "lang": ("lange",), "lange": ("lange", "dauer"), "breit": ("breite",),
    "tief": ("tiefe",), "gross": ("grosse", "flache"), "weit": ("weite", "abstand", "entfernung"),
    "schwer": ("gewicht",), "alt": ("alter",), "dick": ("dicke",), "heiss": ("temperatur",),
    "warm": ("temperatur",), "kalt": ("temperatur",), "teuer": ("kost", "preis"), "stark": ("starke",),
    "klein": ("grosse",), "niedrig": ("hohe",), "schmal": ("breite",),
}


def query_terms(text: str, synonyms: Synonyms | None = None, *,
                drop: frozenset[str] = frozenset()) -> list[QueryTerm]:
    """Content words of a question with word-form variants and synonyms.

    Question words, filler words and words in ``drop`` (folded) are left out. A word typed with a
    capital letter counts as noun unless the question is written entirely in lower case.
    """
    syn = synonyms if synonyms is not None else builtin_synonyms()
    raw = _WORD_CASED.findall(text)
    any_upper = any(w[:1].isupper() for w in raw[1:])
    verb_at, particle = _separable_verb(raw)
    out: dict[str, QueryTerm] = {}
    for i, w in enumerate(raw):
        f = fold(w)
        if i == verb_at:  # "Wer nimmt die Anlage ab?" → abnimmt (matches Abnahme, abnehmen)
            f = particle + f
        elif verb_at is not None and i == len(raw) - 1:
            continue  # the particle itself ("… teil?", "… statt?") is part of the verb
        if (len(f) < 2 and not f.isdigit()) or f in STOPWORDS or f in QUESTION_WORDS or f in QUERY_FILLER \
                or f in drop:
            continue
        t = stem(f)
        if t in out:
            continue
        noun = (w[:1].isupper() and (i > 0 or w.isupper())) or not any_upper or f.isdigit() \
            or (len(w) >= 2 and w.isupper())
        attributive = not noun and i + 1 < len(raw) and raw[i + 1][:1].isupper() and f.endswith(_ADJ_ENDINGS)
        forms = list(verb_bases(f))
        if not noun and len(f) >= 5 and f.endswith("t") and not f.endswith(("ung", "ist")):
            base = f[:-2] if f.endswith("et") else f[:-1]  # "bittet" → bitt, "prüft" → prüf
            if len(base) >= 4 and base not in forms and base != t:
                forms.append(base)
        for vn in ([] if noun else verbal_nouns(f)):  # verbs only: "geprüft" also finds "Prüfung"
            if vn not in forms:
                forms.append(vn)
        m = None if noun else _COMPARATIVE.match(f)  # "längeren" → "lang", "breiteren" → "breit"
        if m and m.group(1) not in forms:
            forms.append(m.group(1))
        syns: dict[tuple[str, ...], None] = {}
        bases = [t, *forms]
        if len(w) >= 3 and w[:-1].isupper() and w.endswith("s"):  # plural of an abbreviation: "BMAs"
            bases.append(f[:-1])
        for base in bases:
            for a in syn.expanded(base):
                if a != (t,) and not (len(a) == 1 and a[0] in forms):
                    syns.setdefault(a, None)
        attr = None
        head = attribute_head(f) if noun else None
        if head:  # "Löschwassermenge": the text may name only "Löschwasser" next to the value
            attr = head[1]
            if len(head[0]) >= 4:
                syns.setdefault((head[0],), None)
                for a in syn.expanded(head[0]):
                    syns.setdefault(a, None)
        heads = () if noun else ADJECTIVE_NOUNS.get(f, ADJECTIVE_NOUNS.get(t, ()))
        out[t] = QueryTerm(t, f, noun, tuple(syns), tuple(x for x in forms if x != t), attributive, attr=attr,
                           heads=heads, umlaut=any(c in w.lower() for c in "äöü"))
    return list(out.values())


def _separable_verb(raw: list[str]) -> tuple[int | None, str]:
    """Position of the finite verb whose separable particle ends the question, and the particle.

    German questions put the verb second (W-questions) or first (yes/no questions) and the particle
    last: 'Wer nimmt die Druckbelüftung ab?', 'Hält die Feuerwehr die Fläche frei?'."""
    if len(raw) < 3:
        return None, ""
    particle = fold(raw[-1])
    if particle not in _SEP_PREFIX:
        return None, ""
    at = 1 if fold(raw[0]) in QUESTION_WORDS or fold(raw[0]) in ("wer", "wie", "was", "wann", "wo") else 0
    verb = raw[at]
    if not verb[:1].islower() and at == 1:
        return None, ""
    if fold(verb) in STOPWORDS or fold(verb) in QUESTION_WORDS:  # modal/auxiliary: "Wo soll … hin?"
        return None, ""
    return at, particle


@lru_cache(maxsize=4096)
def text_terms(text: str) -> frozenset[str]:
    """All stems and word-form variants of a text (the document side of matching)."""
    terms: set[str] = set()
    for w in set(words(text)):
        if len(w) < 2 and not w.isdigit():
            continue
        terms |= word_variants(w)
    return frozenset(terms)


# Endings that keep a short stem the same word ("Flur" → "Flurs", "Alarm" → "alarmiert"); a stem of
# 4–5 letters does not match longer compounds ("Lüfter" → "luft" must not match "Luftwechsel").
_SAME_WORD_ENDINGS = frozenset("e en er es n s ern ens nen em ier iert ieren".split())
SHORT_STEM = 6  # stems shorter than this match compounds only by their head, not by their beginning
_INFLECTED = frozenset("e en er es ern ens".split())  # … unless the question word was clearly inflected
# endings that derive another word (offen → öffentlich, Brand → brandig): no compound
_DERIVATION = ("lich", "tlich", "ig", "isch", "bar", "keit", "heit", "ung", "schaft", "los", "sam", "haft", "tum")
PARTIAL_CREDIT = 0.5  # only the first part of a compound question word is named ("Löschwasser" for
#                       "Löschwasserteich", "Abweichung" for "Abweichungsantrag")


def word_credit(a: str, doc: frozenset[str] | set[str], *, reverse: bool = True, word: str | None = None) -> float:
    """How well a (stemmed) question word ``a`` occurs in a text's term set: 1.0, PARTIAL_CREDIT, 0.

    Full: the same stem; a compound starting with the word (Rauchableitung → Rauchableitungsöffnung,
    only for stems of SHORT_STEM letters or more); a compound ending with it (Genehmigung →
    Baugenehmigung); the word inside a longer one (Wasser → Löschwasserbedarf); with ``reverse`` the
    text naming the head of a compound question word (Baubesprechung → Besprechung). Partial: the text
    names only the first part of the question word (Löschwasserteich → Löschwasser). A short stem
    starts compounds only when the question word was inflected (``word``): "Löschen" → Löschwasser,
    "Kinder" → kindgerecht – but not "offen" → Offenhalten."""
    if a in doc:
        return 1.0
    if a.isdigit() or len(a) < 2:
        return 0.0
    best = 0.0
    compound_prefix = len(a) >= SHORT_STEM or (word is not None and word != a and word.startswith(a)
                                                and word[len(a):] in _INFLECTED)
    for d in doc:
        if d.startswith(a) and len(a) >= 4 and (d[len(a):] in _SAME_WORD_ENDINGS or (
                compound_prefix and not d[len(a):].startswith(_DERIVATION))):
            return 1.0
        if len(a) >= 5 and len(d) - len(a) >= 2 and d.endswith(a):
            return 1.0
        if len(a) >= 6 and a in d:
            return 1.0
        if reverse and len(d) >= 6 and len(a) - len(d) >= 3 and a.endswith(d):
            return 1.0
        if reverse and len(d) >= 5 and len(a) - len(d) >= 3 and a.startswith(d):
            best = PARTIAL_CREDIT
    return best


def _alt_in(a: str, doc: frozenset[str] | set[str], reverse: bool = True) -> bool:
    return word_credit(a, doc, reverse=reverse) >= 1.0


def _phrase_in(phrase: tuple[str, ...], doc: frozenset[str] | set[str]) -> bool:
    """A synonym/word form – only exact, prefix and compound-head matches (a synonym such as
    'Brandschutzdienststelle' must not match its mere beginning 'Brandschutz')."""
    return all(_alt_in(p, doc, reverse=False) for p in phrase)


def _whole_word(part: str, doc: frozenset[str] | set[str], *, head: bool) -> str | None:
    """A text word that *is* ``part`` (same stem or word form; with ``head`` also a compound ending
    with it). Returns the matching text word."""
    for d in doc:
        if d == part or (d.startswith(part) and d[len(part):] in _SAME_WORD_ENDINGS):
            return d
        if head and len(part) >= 4 and d.endswith(part) and len(d) - len(part) >= 3:
            return d
    return None


@lru_cache(maxsize=16384)
def _splits(t: str, synonyms: Synonyms) -> tuple[tuple[str, tuple[str, ...], tuple, tuple], ...]:
    """Ways to split a compound question word: (right part, left parts, their synonyms)."""
    out = []
    for i in range(4, len(t) - 3):
        left, right = t[:i], t[i:]
        lefts = (left[:-1], left) if left.endswith("s") and len(left) > 5 else (left,)
        out.append((right, lefts, tuple(synonyms.alternatives(right)),
                    tuple(tuple(synonyms.alternatives(lf)) for lf in lefts)))
    return tuple(out)


def _decompound(t: str, doc: frozenset[str] | set[str], synonyms: Synonyms) -> bool:
    """Compound written together in the question, apart in the text: 'Entrauchungsventilatoren' ↔
    'Ventilatoren der Entrauchung', 'Drehleiterfläche' ↔ 'Aufstellfläche für das Hubrettungsfahrzeug',
    'Flurlänge' ↔ 'Länge des Flurs'. Both parts must be words of their own in the text (not just the
    beginning of another word: 'Stellplatz' is not 'Stellung' + 'Marktplatz')."""
    if len(t) < 8:
        return False
    for right, lefts, right_alts, left_alts in _splits(t, synonyms):
        r_word = _whole_word(right, doc, head=True) or _whole_word(stem(right), doc, head=True)
        if r_word is None and not any(_phrase_in(a, doc) for a in right_alts):
            continue
        for lf, alts in zip(lefts, left_alts):
            l_word = _whole_word(lf, doc, head=False) or (_whole_word(stem(lf), doc, head=False)
                                                           if len(stem(lf)) >= 4 else None)
            if l_word is not None and l_word != r_word:
                return True
            if l_word is None and any(_phrase_in(a, doc) for a in alts):
                return True
    return False


_SEPARABLE_FORM = re.compile(r"^(%s)(\w{4,})$" % "|".join(sorted(_SEP_PREFIX, key=len, reverse=True)))


def _separated_verb(form: str, doc: frozenset[str] | set[str]) -> bool:
    """A separable verb of the question written apart in the text: 'zustimm' (zugestimmt) ↔
    'stimme ich … zu', 'abnehm' ↔ 'nimmt … ab'."""
    m = _SEPARABLE_FORM.match(form)
    return bool(m) and m.group(1) in doc and _alt_in(m.group(2), doc, reverse=False)


@lru_cache(maxsize=65536)
def query_term_credit(q: QueryTerm, doc: frozenset[str], synonyms: Synonyms | None = None) -> float:
    """Credit of a question word in a text's term set: 1.0 (the word, a word form, a synonym or the
    compound written apart), PARTIAL_CREDIT (only the first part of a compound) or 0."""
    # "Kinder" → kindgerecht, but "Lüfter" (an agent noun, not the plural of Luft) ↛ Luftwechsel
    # only nouns start compounds by a short stem: "offenen Punkte" ↛ Offenhalten
    agent_noun = q.umlaut and q.word.endswith("er") and len(q.term) < SHORT_STEM
    credit = word_credit(q.term, doc, word=q.word if q.noun and not agent_noun else None)
    if credit >= 1.0 or any(_phrase_in(a, doc) for a in q.syns):
        return 1.0
    if any(word_credit(f, doc, reverse=False, word=f + "en") >= 1.0 for f in q.forms):
        return 1.0  # verb stems start compounds: "prüft" → Prüfsachverständiger, "löschen" → Löschwasser
    if q.noun and 3 <= len(q.term) < 5 and not q.term.isdigit():
        short = {q.term, q.term[:-1] if q.term.endswith("e") and len(q.term) == 4 else q.term}
        if any(d.endswith(x) and len(d) - len(x) >= 4 for x in short for d in doc):
            return 1.0  # short noun as compound head: "Weg" → Rettungsweg, "Betten" → Gastbetten
    if q.word != q.term and len(q.word) >= 6 and _alt_in(q.word, doc):  # stem too short: wass(er)
        return 1.0
    if any(_separated_verb(f, doc) for f in (q.term, *q.forms)):
        return 1.0
    heads = {*q.heads, *(stem(x) for x in q.heads), *(x[:-1] for x in q.heads if x.endswith("e") and len(x) > 3)}
    if heads and any(d == h or (d.endswith(h) and len(d) - len(h) >= 3) for h in heads for d in doc):
        return 1.0
    if _decompound(q.term, doc, synonyms if synonyms is not None else builtin_synonyms()):
        return 1.0
    return credit


def query_term_in(q: QueryTerm, doc: frozenset[str], synonyms: Synonyms | None = None) -> bool:
    """Does a question word (or one of its variants/synonyms) occur in a text's term set?"""
    return query_term_credit(q, doc, synonyms) >= 1.0


# --------------------------------------------------------------------------- question intent
_ENUMERATION = re.compile(
    r"\b(in\s+welchen|welche[n]?|alle[n]?|wie\s+viele[n]?|in\s+wie\s+vielen)\s+(\w+\s+)?"
    r"(projekte[n]?|bauvorhaben|vorhaben|objekte[n]?|gebäude[n]?|baustellen)\b"
    r"|\bwo\s+(überall|alles)\b", re.I)
_PROJECT_QUESTION = re.compile(
    r"\b(welche[mrsn]?|in\s+welche[mrn]?|für\s+welche[sn]?|bei\s+welche[mrn]?)\s+(\w+\s+)?"
    r"(projekt\w*|bauvorhaben\w*|vorhaben\w*|objekt\w*|gebäude\w*|baustelle\w*)\b|^\s*wo\b", re.I)


def is_enumeration_question(question: str) -> bool:
    """'In welchen Projekten …?', 'Welche Projekte …?' – the answer is a list over projects."""
    return bool(_ENUMERATION.search(question))


def is_project_question(question: str) -> bool:
    """Asks which project(s) something belongs to ('In welchem Projekt …?', 'Wo wird …?')."""
    return bool(_PROJECT_QUESTION.search(question)) or is_enumeration_question(question)


def is_temporal_question(question: str) -> bool:
    """Asks for the latest state ('letzter Stand', 'inzwischen', 'schon', 'aktuell', 'Neues')."""
    return any(w in TEMPORAL_WORDS for w in words(question))


# Parties of a project ("Bauherr", "Prüfingenieurin", "Feuerwehr"): who wrote or decided something.
ROLE_WORDS = ("bauherr", "architekt", "prufingenieur", "fachplaner", "bauleitung", "errichter", "betreiber",
              "brandschutzplanung", "sachverstandig", "feuerwehr", "brandschutzdienststell", "bauaufsicht",
              "bauamt", "bauordnungsamt", "baubehord", "mieter", "planer")
_ROLE_ENDINGS = ("", "e", "en", "n", "s", "es", "in", "innen", "ern")


def role_key(t: QueryTerm) -> str | None:
    """The party a question word names ("Bauherrn" → bauherr, "PI" → prufingenieur, "FW" → feuerwehr)
    – not a thing whose name merely starts with one ("Feuerwehrschlüsseldepot", "Feuerwehrzufahrt")."""
    keys = [t.term, t.word] + [a[0] for a in t.syns if len(a) == 1]
    for k in keys:
        for r in ROLE_WORDS:
            if k.startswith(r) and k[len(r):] in _ROLE_ENDINGS:
                return "feuerwehr" if r == "brandschutzdienststell" else r
    return None


def named_roles(question: str) -> list[QueryTerm]:
    """Question words that name a party of the project."""
    return [t for t in query_terms(question) if role_key(t)]


_SUMMARY_QUESTION = re.compile(
    r"\bfass\w*\b.*\bzusammen\b|\b(zusammenfass\w*|zusammengefasst|überblick\w*|übersicht\w*|worum\s+geht|"
    r"um\s+was\s+geht|beschreib\w*|kurzbeschreibung|steckbrief)\b|"
    r"^\W*erzähl\w*\b|"  # "Erzähl mir was über das Projekt"
    r"\bwas\s+(ist|sind)\s+(das|dies)\s+für\b|"  # "Was ist das für ein Projekt?"
    # "Was wird hier gebaut?", "Was wird beim Hotel Seeblick gebaut?" – not "Was wird als Rettungsweg gebaut?"
    r"\bwas\s+(wird|werden|soll|ist)\s+(?:(?:hier|dort|da|denn|eigentlich|überhaupt|genau)\s+)*"
    r"(?:(?:bei|beim|in|im|am|an|auf|für)\s+(?:\S+\s+){1,4}?)?gebaut\s*[?.!]*\s*$", re.I)
SUMMARY_WORDS = re.compile(r"^(fass\w*|zusammen\w*|uberblick\w*|ubersicht\w*|worum|beschreib\w*|kurzbeschreibung|"
                           r"steckbrief|geht|erzahl\w*|gebaut)$")


def is_summary_question(question: str) -> bool:
    """'Fasse das Projekt … zusammen.', 'Worum geht es bei …?', 'Gib mir einen Überblick über …'."""
    return bool(_SUMMARY_QUESTION.search(question))


# Reference codes such as project numbers ("BS-2024-0137"), file numbers ("63-0137/24", "BA-019/2025"),
# invoice numbers ("2024-1187"): they pick a project or file but say nothing about the topic of a
# question. ISO dates are no reference codes.
_IDENTIFIER = re.compile(r"\b(?:[A-ZÄÖÜ]{1,6}[-/ ]?|[a-zäöü]{1,6}[-/])?\d{2,6}(?:[-/]\d{1,6}){1,3}\b")
_ISO_DAY = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")


def strip_identifiers(text: str) -> str:
    return _IDENTIFIER.sub(" ", text)


def find_identifiers(text: str) -> list[str]:
    """Reference codes in a text, as written ('BS-2024-0233', '63-0137/24'), without ISO dates."""
    out: dict[str, None] = {}
    for m in _IDENTIFIER.finditer(text or ""):
        ident = m.group(0).strip()
        if not _ISO_DAY.match(ident):
            out.setdefault(ident, None)
    return list(out)


@lru_cache(maxsize=1024)
def identifier_pattern(ident: str) -> re.Pattern:
    """Regex for a reference code in any spelling of its separators ('BS 2024-0233', 'bs-2024-0233'),
    never as part of a longer number ('2024-0233' does not match '2024-02331')."""
    parts = re.findall(r"[^\W_]+", fold(ident))
    return re.compile(r"(?<![^\W_])" + r"[-/\s.]?".join(re.escape(p) for p in parts) + r"(?![^\W_])")


def identifier_in(ident: str, text: str | None) -> bool:
    return bool(text) and bool(identifier_pattern(ident).search(fold(text)))


# --------------------------------------------------------------------------- clean passages
_MD_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
_MD_SEPARATOR_CELL = re.compile(r"^:?-{2,}:?$")
_MD_BULLET = re.compile(r"^\s*(?:[-*+•]|\d{1,2}[.)])\s+")
_MD_EMPHASIS = re.compile(r"(\*\*|__|\*|_|`)(?=\S)(.+?)(?<=\S)\1")
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")


def _table_cells(line: str) -> list[str] | None:
    s = line.strip()
    if not (s.startswith("|") or " | " in s):
        return None
    cells = [c.strip() for c in s.strip("|").split("|")]
    return cells if len(cells) >= 2 else None


def _header_like(cells: list[str]) -> bool:
    return all(c and len(c) <= 30 and not any(ch.isdigit() for ch in c) for c in cells)


def plain_lines(text: str) -> list[str]:
    """Lines of an indexed text without markdown syntax: headings without '#', table rows as
    'Spalte: Wert' (header rows and separator rows dropped), list marks and emphasis removed."""
    out: list[str] = []
    lines = (text or "").replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line:
            continue
        cells = _table_cells(line)
        if cells is not None:
            rows = [cells]
            while i < len(lines) and (c := _table_cells(lines[i])) is not None:
                rows.append(c)
                i += 1
            data = [r for r in rows if not all(_MD_SEPARATOR_CELL.match(c.replace(" ", "")) for c in r if c)]
            separator = len(data) < len(rows)
            if data and (separator or (len(data) >= 2 and out)) and _header_like(data[0]):
                data = data[1:]  # header row ("| Angabe | Inhalt |", "Kennwert | Wert")
            for r in data:
                r = [c for c in r if c]
                if len(r) >= 2:
                    out.append(f"{r[0]}: {', '.join(r[1:])}")
                elif r:
                    out.append(r[0])
            continue
        m = _MD_HEADING.match(line)
        if m:
            line = m.group(1)
        line = _MD_BULLET.sub("", line)
        line = _MD_LINK.sub(r"\1", line)
        line = _MD_EMPHASIS.sub(r"\2", line)
        line = line.lstrip("> ").strip()
        if line:
            out.append(line)
    return out


def plain_text(text: str) -> str:
    """Clean text of :func:`plain_lines`, one line per paragraph, heading or table row."""
    return "\n".join(plain_lines(text))


_PASSAGE_ABBREV = re.compile(r"(\b(Nr|Abs|ca|bzw|ggf|inkl|vgl|Dipl|Ing|Str|Tel|Dr|Prof|gem|max|min|mind|S|u|z|d|e)"
                             r"|\b\d{1,2}|\b[A-ZÄÖÜ])\.$")


def split_sentences(line: str) -> list[str]:
    """Sentences of a line (abbreviations, ordinals and dates do not end a sentence)."""
    out: list[str] = []
    buf = ""
    for piece in re.split(r"(?<=[.!?])\s+", line):
        buf = f"{buf} {piece}".strip()
        if not _PASSAGE_ABBREV.search(buf):
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")

# Signature lines at the end of a mail or letter: "Brandoberinspektor Uwe Kessler", "Dipl.-Ing. Sabine
# Wendehals", "Benedikt Sauer, Wohnbau Stadtpark eG", "Feuerwehr Musterstadt, Vorbeugender Brandschutz",
# "Prüfingenieurin für Brandschutz", "Ihre Papeterie".
_SIG_RANK = (r"(?:Dipl\.-Ing\.(?:\s*\(FH\))?|Dr\.|Prof\.|[A-ZÄÖÜ][a-zäöüß]*(?:meister|meisterin|inspektor|inspektorin|"
             r"rat|rätin|direktor|direktorin|führer|führerin))")
_SIG_PERSON = re.compile(r"^(?!(?:Kein|Keine|Keinen|Die|Der|Das|Den|Dem|Ein|Eine|Einen|Alle|Gegen|Mit|Ohne|Bei|Im|Am|"
                         r"Zum|Zur|Nach|Vor|Für|Laut|Siehe|Neue|Neuer|Neues|Weitere|Nächste|Nächster)\b)"
                         r"(?:" + _SIG_RANK + r"\s+)*[A-ZÄÖÜ][a-zäöüß]+(?:-[A-ZÄÖÜ][a-zäöüß]+)?"
                         r"(?:\s+[A-ZÄÖÜ][a-zäöüß]+(?:-[A-ZÄÖÜ][a-zäöüß]+)?){1,2}(?:\s*,\s*[^.!?:;]{2,60}\.?)?$")
_SIG_ORG = re.compile(r"^[^.!?:;]{0,60}\b(GmbH|gGmbH|mbH|eG|AG|KG|e\.\s?V\.|Partner|Architekt\w*|Ingenieurbüro\w*|"
                      r"Ingenieurgesellschaft|Feuerwehr|Brandschutzdienststelle|Jugendamt|Stadt|Amt|Bauaufsicht|"
                      r"Bauordnung\w*|Prüfingenieur\w*|Prüfsachverständig\w*|Abteilung|Büro|Atelier|Gefahrenvorbeugung|"
                      r"Vorbeugender\s+Brandschutz)\b[^.!?:;]{0,60}$|^Ihre?\s+[A-ZÄÖÜ][\w&.\- ]{1,40}$")
_CLOSING_LINE = re.compile(r"^(mit\s+freundlichen\s+grüßen|freundliche\s+grüße|viele\s+grüße|beste\s+grüße|herzliche\s+grüße|"
                           r"liebe\s+grüße|gruß\b|grüße\b|mfg\b)", re.I)


def is_signature_line(line: str) -> bool:
    """Does a line look like (part of) a signature – a name with rank/title or an organisation?"""
    line = line.strip()
    if not line or len(line.split()) > 9 or re.search(r"\d{3,}|@|https?:", line):
        return False
    return bool(_SIG_PERSON.match(line) or _SIG_ORG.match(line))


def signature_start(lines: list[str]) -> int:
    """Index of the first signature line of a text's lines (len(lines) if there is none): the lines
    after a closing ("Mit freundlichen Grüßen"), or name/organisation lines that end the text after at
    least one sentence."""
    for i, line in enumerate(lines):
        if _CLOSING_LINE.match(line.strip()):
            return i + 1 if i + 1 < len(lines) else len(lines)
    end = len(lines)
    while end > 1 and is_signature_line(lines[end - 1]):
        end -= 1
    if end < len(lines) and not re.search(r"[.!?:;]\s*$|\?", "\n".join(lines[:end])):
        return len(lines)  # no sentence before: a title block, not a signature
    return end


def split_signature(sentences: list[str]) -> tuple[list[str], str]:
    """A signature written into the last sentence line ("… behalten. Benedikt Sauer, Wohnbau Stadtpark
    eG"): (sentences without it, the signature)."""
    if len(sentences) >= 2 and is_signature_line(sentences[-1]) and not sentences[-1].endswith(("?", "!")):
        return sentences[:-1], sentences[-1]
    return sentences, ""


# what the reader puts in front of a quote ("BS-… Name – laut Mail von X vom …:", "Laut Baugenehmigung (S. 2),
# Auflage 3:") names the source, not the statement – it must not make a signature line the "best" passage
_ANSWER_LEAD = re.compile(r"(?:\b[A-Z]{1,4}-\d{4}-\d{3,4}\b[^–\n]{0,80}?–\s*)?\b[Ll]aut\s+[^:!?\n]{1,140}?:\s")


_PASSAGE_NOISE = re.compile(r"^(Betreff|Von|An|Cc|Datum|Gesendet):\s|^(sehr\s+geehrte|hallo|guten\s+tag|liebe[rs]?\s)"
                            r"[^.!?]{0,60}[,!]?$|^(mit\s+freundlichen|viele|beste|freundliche)\s+grüße", re.I)
PASSAGE_LEAD = 150  # at most this much text before the matching sentence (besides its section heading)
# a section heading in plain text: a numbered section ("7 Organisatorischer Brandschutz", "3.2 Rettungswege")
# or an agenda item ("TOP 2 – Rauchwarnmelder"); markdown headings ("# 7 …") are known from the text itself
_PASSAGE_SECTION = re.compile(r"^(?:TOP\s+\d{1,2}\b|\d{1,2}(?:\.\d{1,2})*\.?\s+[A-ZÄÖÜ])")
_PASSAGE_AGENDA = re.compile(r"^TOP\s+\d{1,2}\b")
_PASSAGE_BARE_NUMBER = re.compile(r"^\d{1,3}\s+\S")  # "2 Pulverlöscher", "7 Organisatorischer Brandschutz"
# … but a number that counts or measures is no section number: "12 Monate nach Inbetriebnahme", "3 Stück"
_PASSAGE_QUANTITY = re.compile(r"^\d+(?:[.,]\d+)?\s*(?:Stück|Stk\b|Monate?n?\b|Jahre?n?\b|Wochen?\b|Tage?n?\b|"
                               r"Stunden?\b|Minuten?\b|Sekunden?\b|Mal\b|Euro\b|EUR\b|€|%|m\b|m²|m³|cm\b|mm\b|km\b|kg\b)")
_PASSAGE_ENDS_STATEMENT = re.compile(r"[.!?)]$")
PASSAGE_HEADING_WORDS = 8  # a heading is a short line


def _heading_shaped(line: str) -> bool:
    return len(line.split()) <= PASSAGE_HEADING_WORDS and ":" not in line and not re.search(r"[.!?;,]$", line)


def _passage_headings(text: str, lines: list[str]) -> set[int]:
    """Indexes of the (plain) lines that are section headings: markdown headings, agenda items ("TOP 2 –
    Räumungsübung") and numbered section headings that stand on their own – a short line with a section
    number, no quantity ("2 Pulverlöscher" after "je Geschoss:" is a list line, "12 Monate nach
    Inbetriebnahme" a phrase), after a line that ends a statement or is short itself (not after "…
    zwar je Geschoss:" or in the middle of a wrapped sentence), not continued by the next line ("… ist
    die erste Prüfung fällig.") and not one of several numbered lines in a row ("3 Wandhydranten / 2
    Pulverlöscher")."""
    md = {h for raw in (text or "").replace("\r\n", "\n").split("\n") if _MD_HEADING.match(raw.strip())
          for h in plain_lines(raw)}
    out = set()
    for i, ln in enumerate(lines):
        if ln in md:
            out.add(i)
            continue
        if not _PASSAGE_SECTION.match(ln) or not _heading_shaped(ln):
            continue
        if _PASSAGE_AGENDA.match(ln):
            out.add(i)
            continue
        if _PASSAGE_QUANTITY.match(ln):
            continue
        prev = lines[i - 1] if i > 0 else ""
        if prev and (re.search(r"[:,;–-]$", prev) or not (
                _PASSAGE_ENDS_STATEMENT.search(prev) or len(prev.split()) <= PASSAGE_HEADING_WORDS)):
            continue  # the line continues a list or a sentence of the line before
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if nxt[:1].islower():
            continue  # the next line continues this one
        if _PASSAGE_BARE_NUMBER.match(ln) and any(
                _PASSAGE_BARE_NUMBER.match(x) and _heading_shaped(x) for x in (prev, nxt)):
            continue  # "3 Wandhydranten / 2 Pulverlöscher": lines of a list, not sections without text
        out.add(i)
    return out


def relevant_passage(text: str, question: str = "", answer: str = "", max_chars: int = 400) -> str:
    """The passage of ``text`` that matters for a question/answer, as clean text (no markdown).

    Sentences are scored by the words and numbers they share with the answer (the statement quoted
    from this source, without the "Laut <Quelle>:" lead) and by the question words they cover; the
    best sentence is taken with the following sentences (and a little of what precedes it) as long as
    they fit into ``max_chars``. What precedes it never reaches back over the heading of its section
    (a chunk may hold several short sections): the passage starts at that heading when the heading and
    the text up to the sentence fit, otherwise inside the section. Mail header lines and greetings are
    left out; signature lines (after the closing, or a name/organisation ending the text) and the mail
    subject only count when the answer quotes them ("Wer hat … unterschrieben?") and never extend a
    passage. A passage that does not start the text begins with "… "."""
    all_lines = plain_lines(text)
    kept = [(i, ln) for i, ln in enumerate(all_lines) if not _PASSAGE_NOISE.match(ln)] or list(enumerate(all_lines))
    lines = [ln for _, ln in kept]
    sig_all = signature_start(all_lines)  # with the closing line, which the passage leaves out
    sig_from = next((k for k, (i, _) in enumerate(kept) if i >= sig_all), len(kept))
    sents: list[tuple[int, str, bool]] = []
    subject = next((ln for ln in all_lines[:3] if ln.startswith("Betreff: ")), None)
    if subject and answer and lines and lines[0] != subject:  # quoted only when the answer cites it
        lines = [subject] + lines
        sig_from += 1
        sents.append((0, subject, True))
    for li, line in enumerate(lines):
        if li == 0 and line is subject:
            continue
        parts = split_sentences(line)
        signature = ""
        if li == sig_from - 1:
            parts, signature = split_signature(parts)
        sents += [(li, x, li >= sig_from) for x in parts]
        if signature:
            sents.append((li, signature, True))
    if not sents:
        return ""
    q_terms = query_terms(question) if question.strip() else []
    q_total = sum(t.weight for t in q_terms)
    answer = _ANSWER_LEAD.sub(" ", answer or "")
    a_terms = set(content_terms(answer, QUESTION_WORDS)) if answer.strip() else set()
    a_numbers = set(_NUMBER.findall(answer)) if answer else set()

    def score(sent: str, signature: bool) -> float:
        terms = text_terms(sent)
        val = 0.0
        own = set(content_terms(sent, QUESTION_WORDS))
        if q_total:
            val += sum(t.weight * query_term_credit(t, terms) for t in q_terms) / q_total
        if a_terms:
            if own:
                val += 2.0 * len(own & a_terms) / len(own)
            val += 0.5 * len(a_numbers & set(_NUMBER.findall(sent)))
        if signature and not (own and a_terms and len(own & a_terms) >= 0.6 * len(a_terms)):
            val *= 0.25  # a name under the text (or the subject) is no statement – unless the answer quotes it
        return val

    def quoted(sent: str) -> bool:
        own = set(content_terms(sent, QUESTION_WORDS))
        return bool(own) and len(own & a_terms) >= 0.7 * len(own)

    scores = [score(s, sig) for _, s, sig in sents]
    best = max(range(len(sents)), key=lambda i: (scores[i], -i))
    if scores[best] <= 0:
        best = 0
    while best > 0 and a_terms and not sents[best][2] and not sents[best - 1][2] and quoted(sents[best - 1][1]):
        best -= 1  # the answer quotes several sentences: start with the first of them
    lo = hi = best
    size = len(sents[best][1])
    lead = 0
    headings = _passage_headings(text, lines)
    heading = next((i for i in range(best, -1, -1) if sents[i][0] in headings and not sents[i][2]), None)
    floor = heading or 0  # the passage never starts before the heading of the sentence's section
    if heading is not None and heading < best:  # "7 Organisatorischer Brandschutz / Nachts mindestens …"
        body = sum(len(sents[i][1]) for i in range(floor + 1, best))  # the section's text before the sentence
        need = sum(1 + len(sents[i][1]) for i in range(floor, best))
        if size + need <= max_chars and body <= PASSAGE_LEAD and (
                sents[best][2] or not any(sents[i][2] for i in range(floor, best))):
            lo, size, lead = floor, size + need, body
    while True:  # following sentences first (they continue the statement), then a little before
        if hi + 1 < len(sents) and size + 1 + len(sents[hi + 1][1]) <= max_chars and (
                sents[best][2] or not sents[hi + 1][2]):
            hi += 1
            size += 1 + len(sents[hi][1])
            continue
        if lo > floor and size + 1 + len(sents[lo - 1][1]) <= max_chars \
                and lead + len(sents[lo - 1][1]) <= PASSAGE_LEAD and (sents[best][2] or not sents[lo - 1][2]):
            lo -= 1
            size += 1 + len(sents[lo][1])
            lead += len(sents[lo][1])
            continue
        break
    parts: list[str] = []
    for i in range(lo, hi + 1):
        li, sent, _ = sents[i]
        if parts:
            parts.append("\n" if sents[i - 1][0] != li else " ")
        parts.append(sent)
    passage = "".join(parts).strip()
    if len(passage) > max_chars:  # one very long sentence: cut at a word boundary
        passage = passage[:max_chars].rsplit(" ", 1)[0] + " …"
    if lo > 0 or lines[0] != all_lines[0]:
        passage = "… " + passage
    return passage
