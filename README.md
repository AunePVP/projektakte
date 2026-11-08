# Projektakte

Internes KI-Werkzeug für ein Brandschutz-Ingenieurbüro: führt **E-Mails und Projektdateien je
Projektnummer** zusammen und beantwortet **Fragen zu Projekten mit Quellenangaben**. Läuft komplett
im lokalen Netz mit lokalen Modellen.

- Was gebaut werden soll und warum: [VISION.md](VISION.md)
- Reihenfolge: [ROADMAP.md](ROADMAP.md)
- KI-Dienste (LLM, Embeddings, Reranker): [docs/ki-dienste.md](docs/ki-dienste.md)
- Anbindung des Mailprogramms plMail: [docs/plmail-jmap.md](docs/plmail-jmap.md)

## Stand

Grundgerüst: FastAPI-Anwendung mit `GET /health` und `GET /api/version`. Alles Weitere entsteht
nach der Roadmap.

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

Weitere Variablen (Projektordner, Mailquelle, KI-Dienste) kommen mit Roadmap-Schritt 1.

## Datenschutz

Es werden keine echten Firmendaten für Entwicklung und Tests verwendet; der Demo-Datensatz ist frei
erfunden. Projektordner und Postfächer werden nur gelesen, nie verändert.
