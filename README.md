# Projektakte

Internes KI-Werkzeug für ein Brandschutz-Ingenieurbüro: führt **E-Mails und Projektdateien je
Projektnummer** zusammen und beantwortet **Fragen zu Projekten mit Quellenangaben**. Läuft komplett
im lokalen Netz mit lokalen Modellen.

- Was gebaut werden soll und warum: [VISION.md](VISION.md)
- Reihenfolge: [ROADMAP.md](ROADMAP.md)
- KI-Dienste (LLM, Embeddings, Reranker): [docs/ki-dienste.md](docs/ki-dienste.md)
- Anbindung des Mailprogramms plMail: [docs/plmail-jmap.md](docs/plmail-jmap.md)

## Stand

Roadmap-Schritt 1: Konfiguration aus Umgebungsvariablen, vollständige Clients für alle drei
KI-Dienste, Fake-Dienste (`python -m projektakte.fake_ai`) und `GET /health/deps`.
Getroffene Entscheidungen: [DECISIONS.md](DECISIONS.md), Änderungen: [CHANGELOG.md](CHANGELOG.md).

## Starten

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
PORT=8000 .venv/bin/python -m projektakte
```

Oder als Container:

```bash
docker build -t projektakte . && docker run -p 8000:8000 -v $PWD/data:/data projektakte
```

## Tests

```bash
sh scripts/test.sh
```

Das Skript legt eine virtuelle Umgebung (zwischengespeichert nach Inhalt der requirements-Dateien)
an und führt `pytest` aus. Tests brauchen kein Netz und keine KI-Dienste.

## Konfiguration (Umgebungsvariablen)

| Variable | Standard | Bedeutung |
|---|---|---|
| `PORT` | `8000` | HTTP-Port |
| `DATA_DIR` | `./data` | Index, Datenbank, Demo-Daten |
| `APP_ENV` | `dev` | `dev`, `staging` oder `production` |
| `PROJECTS_DIR` | `$DATA_DIR/projekte` (mit `DEMO_MODE=1`: `$DATA_DIR/demo/projekte`) | Projektordner (read-only) |
| `MAIL_DIR` | `$DATA_DIR/mails` (mit `DEMO_MODE=1`: `$DATA_DIR/demo/mails`) | Ordner mit `.eml`-Dateien (read-only) |
| `PROJECT_NUMBER_PATTERN` | `BS-\d{4}-\d{4}` | Regulärer Ausdruck für Projektnummern |
| `DEMO_MODE` | `0` | `1` = Demo-Datensatz verwenden (und beim Start erzeugen, falls er fehlt) |
| `AI_MODE` | `real` | `fake` = Fake-KI-Dienste im selben Prozess starten, URLs werden ignoriert |
| `LLM_URL` | `http://192.168.178.62:8000/v1` | Sprachmodell (OpenAI-kompatibel) |
| `LLM_MODEL` | `qwen3.8-flash-next` | Modellname |
| `LLM_VISION` | `0` | `1` = Modell kann Bilder lesen (Rückfall für Scans/Pläne) |
| `LLM_TIMEOUT` / `LLM_MAX_TOKENS` | `120` / `800` | Sekunden / Antwortlänge |
| `LLM_EXTRA_BODY` | – | JSON, das in jede Chat-Anfrage gemischt wird, z. B. `{"chat_template_kwargs":{"enable_thinking":false}}` |
| `EMBED_URL` | `http://192.168.178.62:8001` | Embedding-Dienst (ohne `/v1`) |
| `EMBED_MODEL` | `qwen3-embedding-4b` | Modellname |
| `EMBED_FORMAT` | `auto` | `auto`, `openai` (`/v1/embeddings`) oder `tei` (`/embed`) |
| `EMBED_TIMEOUT` / `EMBED_BATCH` | `60` / `32` | Sekunden / Texte pro Anfrage |
| `EMBED_QUERY_INSTRUCTION` | Qwen3-Instruktion | Präfix für Suchanfragen |
| `RERANK_URL` | `http://192.168.178.62:4343/rerank` | Reranker (vollständige URL) |
| `RERANK_FORMAT` | `auto` | `auto`, `tei` (`texts`) oder `cohere` (`documents`) |
| `RERANK_MODEL` | – | nur falls der Server ein `model`-Feld verlangt |
| `RERANK_TIMEOUT` / `RERANK_TOP_K` | `30` / `30` | Sekunden / Kandidaten für das Reranking |
| `HEALTH_TIMEOUT` | `3` | Timeout je Dienst für `/health/deps` |
| `ASSIGN_MIN_SHARE` / `ASSIGN_MIN_LEAD` | `0.5` / `0.2` | Mail-Vorschlag nur, wenn das beste Projekt mindestens diesen Anteil der ähnlichsten Abschnitte hat und so weit vor dem zweiten liegt |
| `LLM_API_KEY`, `EMBED_API_KEY`, `RERANK_API_KEY` | – | optionale Bearer-Tokens (Secrets) |

## Indexieren

```bash
python -m projektakte.index      # ein Durchlauf im Vordergrund (Projekte, später Dokumente/Mails)
```

Unterstützte Formate: TXT, Markdown, CSV, PDF (pypdf, je Seite), DOCX (python-docx, mit Überschriften
und Tabellen), EML. Bilder (PNG/JPG/TIFF) und PDF-Seiten ohne Textebene werden nur mit `LLM_VISION=1`
über das multimodale Modell gelesen, sonst als „übersprungen“ vermerkt. Mails werden aus `MAIL_DIR`
(alle `*.eml`, auch in Unterordnern) gelesen; Anhänge landen als Kopie in `$DATA_DIR/cache` und
werden wie Dokumente indexiert. Quellordner werden nie verändert. Unveränderte Dateien
(Größe/Änderungszeit, dann SHA-256) werden nicht erneut gelesen.

### Zuordnung der Mails

1. **Sicher**: bekannte Projektnummer im Betreff, sonst im Text, sonst im Anhangnamen.
2. **Vorschlag**: Sonst dient die Mail als Suchanfrage (Volltext + Vektor) gegen alle Abschnitte der
   Projekte (Projektdateien und sicher zugeordnete Mails). Die besten Treffer stimmen für ihr Projekt;
   liegt eines klar vorn und teilt Begriffe mit der Mail, wird es vorgeschlagen. Absender, die bisher
   nur zu einem Projekt geschrieben haben, zählen zusätzlich.
3. **Zuordnungs-Eingang**: Vorschläge bestätigt oder verwirft ein Mensch; Entscheidungen bleiben
   auch nach einem Neuaufbau des Index erhalten.

## API (Auszug)

| Methode und Pfad | Zweck |
|---|---|
| `GET /health` | Lebenszeichen, ruft nie KI-Dienste auf |
| `GET /health/deps` | Zustand der KI-Dienste (erreichbar, Format, Antwortzeit) |
| `GET /api/projects?q=` | Projektliste, optional gefiltert nach Nummer/Name |
| `GET /api/projects/{nummer}` | ein Projekt |
| `GET /api/projects/{nummer}/documents` | Dokumente eines Projekts mit Status (ok/übersprungen/Fehler) |
| `GET /api/documents/{id}` | ein Dokument mit seinen Abschnitten (Seite, Überschrift, Text) |
| `GET /api/mails?project=&status=&q=` | Mails (neueste zuerst), filterbar |
| `GET /api/mails/{id}` | eine Mail mit Text und Anhängen |
| `GET /api/inbox` | Zuordnungs-Eingang: Mails ohne sichere Zuordnung, mit Vorschlag und Begründung |
| `POST /api/mails/{id}/assign` | Entscheidung: `{"action": "confirm"}`, `{"action": "assign", "project": "BS-…"}`, `{"action": "reject"}` (Vorschlag verwerfen), `{"action": "none"}` (kein Projektbezug) |

## Demo-Datensatz

```bash
python -m projektakte.demo            # schreibt nach $DATA_DIR/demo (Standard ./data/demo)
python -m projektakte.demo --target /tmp/demo
python -m projektakte.demo --eval tests/eval.json   # Evaluationssatz neu schreiben
```

Erzeugt deterministisch (fester Seed, ohne Zeitstempel in den Dateien) 8 frei erfundene
Brandschutz-Projekte (Schule, Pflegeheim, Bürogebäude, Tiefgarage, Hotel, Industriehalle, Kita,
Wohnhochhaus) unter `demo/projekte/<Nummer> <Name>/` mit Baugenehmigung (PDF, nummerierte
Auflagen), Brandschutzkonzept (DOCX), Prüfbericht (Markdown), Besprechungsprotokoll (TXT) und
einem Bild-„Scan“ eines Fluchtwegplans, außerdem 76 Mails (`demo/mails/*.eml`, 15 mit Anhängen):
Projektnummer im Betreff (Mehrheit), nur im Text, nur im Anhangnamen, ohne Nummer aber inhaltlich
eindeutig und ohne Projektbezug (Newsletter, Rechnung …). `demo/manifest.json` hält fest, welche Mail
zu welchem Projekt gehört; `tests/eval.json` enthält Fragen mit bekannter Antwort und Quelle. Alle Namen, Firmen und Orte sind erfunden; Mailadressen
nutzen die reservierte Domain `.example`.

## Fake-KI-Dienste

Für Entwicklung und Tests gibt es deterministische Fakes aller drei Dienste auf einem Port:

```bash
python -m projektakte.fake_ai --port 8999
LLM_URL=http://127.0.0.1:8999/v1 EMBED_URL=http://127.0.0.1:8999 \
  RERANK_URL=http://127.0.0.1:8999/rerank python -m projektakte
```

Kürzer: `AI_MODE=fake` startet dieselben Fakes im Prozess der Anwendung. Optionen des Fake-Servers:
`--embed-format openai|tei|both`, `--rerank-format tei|cohere|both`, `--no-think`, `--delay`.

## Echte KI-Dienste anbinden

Keine Codeänderung nötig – nur Umgebungsvariablen setzen (`AI_MODE` weglassen oder `real`):

```bash
LLM_URL=http://192.168.178.62:8000/v1 LLM_MODEL=qwen3.8-flash-next \
EMBED_URL=http://192.168.178.62:8001 EMBED_MODEL=qwen3-embedding-4b \
RERANK_URL=http://192.168.178.62:4343/rerank python -m projektakte
```

Danach `GET /health/deps` aufrufen: Dort steht je Dienst `ok`, das **erkannte Format**
(`openai-chat`; `openai`/`tei`; `tei`/`cohere`), die Antwortzeit und ggf. der Fehler. Die Formate
vorher von Hand prüfen (siehe [docs/ki-dienste.md](docs/ki-dienste.md)):

```bash
curl -s http://192.168.178.62:8000/v1/models
curl -s http://192.168.178.62:8000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.8-flash-next","messages":[{"role":"user","content":"Sag OK."}],"max_tokens":8}'
curl -s http://192.168.178.62:8001/v1/embeddings -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-embedding-4b","input":["Rauchableitung im Treppenraum"]}'
curl -s http://192.168.178.62:8001/embed -H 'Content-Type: application/json' \
  -d '{"inputs":["Rauchableitung im Treppenraum"]}'
curl -s http://192.168.178.62:4343/rerank -H 'Content-Type: application/json' \
  -d '{"query":"Rauchableitung","texts":["Text A","Text B"]}'
curl -s http://192.168.178.62:4343/rerank -H 'Content-Type: application/json' \
  -d '{"query":"Rauchableitung","documents":["Text A","Text B"],"top_n":2}'
```

Erkennt der Client das Format nicht oder rät falsch, lässt es sich mit `EMBED_FORMAT` bzw.
`RERANK_FORMAT` festlegen. Fällt der Reranker aus, sucht Projektakte ohne ihn weiter; fällt das
Sprachmodell aus, antwortet die Fragefunktion mit „KI-Dienst nicht erreichbar“.

## Datenschutz

Es werden keine echten Firmendaten für Entwicklung und Tests verwendet; der Demo-Datensatz ist frei
erfunden. Projektordner und Postfächer werden nur gelesen, nie verändert.
