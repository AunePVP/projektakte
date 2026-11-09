# Übergabe – Projektakte

Stand: 9. November 2026, Version 0.5.0 plus „Unveröffentlicht“ (siehe CHANGELOG.md).
Diese Datei fasst alles zusammen, was man zum Weiterarbeiten braucht – in einem neuen Chat, mit
einem anderen Werkzeug oder selbst. Ausführliches steht in den verlinkten Dateien.

---

## 1. Worum es geht

Projektakte ist ein internes Werkzeug für ein Brandschutz-Ingenieurbüro. Es liest die Projektordner
(Dateiserver) und die Mails (plMail oder `.eml`-Ordner) nur lesend, ordnet alles der Projektnummer zu
und beantwortet Fragen mit nummerierten Quellen („Welche Auflagen gelten für den Rettungsweg?“ →
Antwort mit [1], [2] und Ausschnitt). Grundsätze: keine Cloud, keine Telemetrie, nie Mails senden,
Quellordner nie verändern, lieber „nichts gefunden“ als eine erfundene Antwort.

Vision und Spezifikation: `VISION.md`, `ROADMAP.md`, `docs/ki-dienste.md`, `docs/plmail-jmap.md`.

## 2. Wo der Code liegt

- Repository `aunepvp/projektakte`, Branch **`claude/lucid-cerf-h2058c`** (alles gepusht, kein Pull
  Request angelegt).
- Python 3.12, FastAPI, SQLite mit FTS5, Jinja2/HTMX, ein Docker-Container. Abhängigkeiten nur
  gepinnt in `requirements.txt` (+ `constraints.txt` für das Image).
- Testsuite: `sh scripts/test.sh` (pytest-xdist, gut 1.750 Tests, etwa 75 s). Docker-Probelauf:
  `sh scripts/docker-smoke.sh`.

## 3. Stand in Zahlen

| Was | Ergebnis |
|---|---|
| Phase A (14 Roadmap-Schritte), Zuordnung in plMail, Nachbesserungen | fertig (ROADMAP 1–16a) |
| Testsuite | grün (zuletzt 1752 bestanden, 4 Integrationstests übersprungen, 1 bewusst „xfail“) |
| Docker-Probelauf | grün |
| Eigener Evaluationssatz mit Fake-KI | 103/103 |
| Zurückgehaltener Prüfsatz (unabhängiger Autor) mit Fake-KI | 19/28 beantwortbare richtig (0,68), 12/12 Absagen, 0 erfundene Antworten |
| Mit den **echten** lokalen KI-Diensten | **noch nicht gemessen** – Schritt 17, Anleitung unten in Abschnitt 7 |

Die Fake-KI ist nur für Demo und Tests. Sie versteht frei formulierte Fragen etwa in 7 von 10 Fällen
und lehnt im Zweifel ab. Über die Antwortqualität des echten Modells sagt sie nichts.

## 4. Aufbau in Kürze

```
Projektordner ─┐
Mails/plMail ──┼─► Indexer ─► Leseprozess ─► Abschnitte ─► SQLite
               │   (Hintergrund)  (mit Grenzen)              (Volltext + Vektoren)
               └────────── Zuordnung Mail → Projekt ◄──────────────┤
                                                                   ▼
            Weboberfläche / API ◄── Fragen mit Quellen ◄── Suche (Volltext + Vektor + Reranker)
```

| Bereich | Module (`projektakte/`) |
|---|---|
| Start, Einstellungen | `main.py`, `config.py` (alles aus Umgebungsvariablen), `startup.py`, `db.py` |
| Einlesen | `registry.py` (Projektordner), `structure.py` (Nummernschema erkennen), `documents.py`, `extract.py` + `extract_worker.py` (eigener Leseprozess), `chunking.py`, `vectors.py`, `indexer.py`, `background.py` |
| Mails | `mails/` (`eml_source`, `jmap_source`, `store`, `model`) |
| Zuordnung | `assign.py`, `inbox.py`, `integration.py` (API für plMail) |
| Suche und Antworten | `search.py`, `ask.py`, `textutil.py` |
| KI-Anbindung | `ai/` (`llm`, `embeddings`, `rerank`, `http`) |
| Oberfläche | `web.py`, `webtext.py`, `templates/`, `api.py` |
| Sicherheit | `auth.py`, `websecurity.py`, `logsetup.py` |
| Betrieb | `backup.py`, `healthcheck.py`, `demo_cleanup.py` |
| Demo und Messung | `demo/` (8 erfundene Projekte), `fake_ai/`, `evaluate.py` |

**Ablauf einer Frage:** Die Frage wird eingebettet (1 Embedding-Aufruf). Volltext- und Vektorsuche
laufen im Projekt (oder über alle Projekte) mit je bis zu 50 Kandidaten. Beide Listen werden
zusammengeführt, der Reranker bewertet die besten 30. Die besten 6 Abschnitte (zusammen höchstens
etwa 8.000 Zeichen, rund 2.300 Token) gehen nummeriert an das Sprachmodell. Bei Bedarf kommen eine
Überblickssuche (Zusammenfassungen) oder die genannten Beteiligten dazu. Das Modell darf nur
daraus antworten und muss zitieren. Projektakte prüft Zitate und Absagen. `ASK_DEADLINE`
(180 s) begrenzt die ganze Frage. Es wird nie ein ganzes Projekt in den Kontext geladen (warum:
Abschnitt 9).

## 5. Wichtige Eigenschaften (Kurzfassung von DECISIONS.md)

- **Eigener Leseprozess:** PDF, Word und Mails (auch Anhänge) werden in einem eigenen Prozess mit
  Speicher- (2 GB) und Rechenzeitgrenze (120 s CPU je Datei) gelesen. Eine kaputte oder aufgeblähte
  Datei wird vermerkt, die Anwendung bleibt stabil.
- **Zeitbudget je Frage** (`ASK_DEADLINE`), jeder KI-Aufruf nach Gesamtdauer begrenzt. Ungültige
  Zeitlimits fallen mit Warnung auf den Standard zurück.
- **Absagen echter Modelle** werden erkannt (auch „weder [1] noch [2] nennen ihn“), belegte Fakten
  bleiben Antworten.
- **Mail-Zuordnung:** Mit plMail ordnet der Mensch beim Lesen zu (`ASSIGN_MODE=manual`). Ohne plMail
  gibt es Vorschläge und einen Eingang zum Entscheiden. Die plMail-Seite beschreibt
  `docs/plmail-prompt.md` (Prompt für die KI im plMail-Projekt) und `docs/plmail-integration.md`.
- **Projektordner vom Dateiserver per SMB:** nur lesend eingebunden, Anleitung in
  `deploy/README.md`, Abschnitt „Projektordner vom Dateiserver (SMB)“.
- **Projektnummern und Ordnerstruktur:** `PROJECT_NUMBER_PATTERN` und `PROJECT_FOLDER_DEPTH` von Hand,
  mit Hilfe von `python -m projektakte.structure`. Oder `PROJECT_NUMBER_PATTERN=auto`: Projektakte
  schlägt vor, eine Person bestätigt auf der Statusseite („Projektordner“ → „Übernehmen“). Vorher
  wird nichts als Projekt gelesen.
- **Sicherheit:** eigene Konten je Person (`python -m projektakte.passwd`), Anmeldebremse, CSP,
  CSRF-Schutz, Log ohne Suchbegriffe und Dateinamen, `APP_ENV=production` mit strengen Startprüfungen.

## 6. Lokal ausprobieren (ohne echte KI)

```bash
docker build -t projektakte .
docker run --rm -p 127.0.0.1:8000:8000 -e APP_ENV=dev -e DEMO_MODE=1 -e AI_MODE=fake -e AUTH_DISABLED=1 projektakte
```

Dann `http://localhost:8000/` öffnen. Ohne Docker: `python3 -m venv .venv &&
.venv/bin/pip install -r requirements.txt`, dann `APP_ENV=dev DEMO_MODE=1 AI_MODE=fake
AUTH_DISABLED=1 .venv/bin/python -m projektakte`.

## 7. Test mit der lokalen KI (Schritt 17) – Anleitung

Das geht nur auf einem Rechner im Büronetz, der den KI-Server erreicht. Aus der Cloud (auch aus
einem Chat mit einer KI) ist der KI-Server nicht erreichbar. Deshalb: Befehle hier ausführen und die
Ausgaben in den neuen Chat kopieren (was genau, steht in 7.6).

### 7.1 Dienste von Hand prüfen

```bash
curl -s http://192.168.178.62:8000/v1/models
curl -s http://192.168.178.62:8000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.8-flash-next","messages":[{"role":"user","content":"Sag OK."}],"max_tokens":8}'
curl -s http://192.168.178.62:8001/v1/embeddings -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-embedding-4b","input":["Rauchableitung im Treppenraum"]}'
curl -s http://192.168.178.62:4343/rerank -H 'Content-Type: application/json' \
  -d '{"query":"Rauchableitung","texts":["Kantine","Rauchableitung im Treppenraum"]}'
```

Adressen und Modellnamen anpassen, falls sie anders sind. Antwortet einer der Dienste nicht, zuerst
das klären.

### 7.2 Demo-Daten mit den echten Diensten starten

Die Demo-Daten sind frei erfunden. Ihre Ergebnisse darf man also bedenkenlos in einen Chat kopieren.

```bash
docker build -t projektakte .
docker run --rm -p 127.0.0.1:8000:8000 -v projektakte-test:/data --name projektakte-test \
  -v "$PWD/tests/eval_holdout.json:/app/tests/eval_holdout.json:ro" \
  -e APP_ENV=dev -e AUTH_DISABLED=1 -e DEMO_MODE=1 \
  -e LLM_URL=http://192.168.178.62:8000/v1 -e LLM_MODEL=qwen3.8-flash-next \
  -e EMBED_URL=http://192.168.178.62:8001 -e EMBED_MODEL=qwen3-embedding-4b \
  -e RERANK_URL=http://192.168.178.62:4343/rerank \
  -e 'LLM_EXTRA_BODY={"chat_template_kwargs":{"enable_thinking":false}}' \
  projektakte
```

- Ohne `AI_MODE` nutzt Projektakte die echten Dienste.
- `LLM_EXTRA_BODY` schaltet das Denken von Qwen3 ab. Das macht es schneller und verhindert
  abgebrochene Antworten.
- Kann das Modell Bilder lesen, zusätzlich `-e LLM_VISION=1` setzen (Scans, Pläne).
- Die Zeile mit `eval_holdout.json` braucht man nur für Schritt 7.4. Der Prüfsatz ist nicht im
  Image enthalten.

Dann im Browser:
- `http://localhost:8000/status`: Dienste `ok`, erkannte Formate, Indexierung abgeschlossen.
- `http://localhost:8000/health/deps`: dasselbe als JSON.
- Ein paar Fragen stellen, z. B. im Projekt „Grundschule Am Lindenhof“: „Welche Auflagen gelten
  für die Rettungswege?“, „Was ist der aktuelle Stand?“, „Fasse das Projekt zusammen.“

### 7.3 Antwortqualität messen

In einem zweiten Terminal, während der Container läuft:

```bash
docker exec projektakte-test python -m projektakte.evaluate --real --json /data/eval-ergebnis.json > ki-test-eval.txt
```

Das indexiert die Demo-Daten mit den echten Diensten in einen eigenen Index und stellt die 103
Fragen des eigenen Satzes. Die Ausgabe enthält:
- je Frage, ob die erwartete Quelle unter den ersten 5 Treffern ist, ob Fakt und Quelle in der
  Antwort stehen, den Status und die Reranker-Werte;
- am Ende Summen, die Laufzeit je Frage und eine **Schwellwerttabelle**.

Ziele: mindestens 80 % der beantwortbaren Fragen richtig, mindestens 90 % der unbeantwortbaren
abgelehnt. Exit-Code 0 heißt erreicht, 1 verfehlt, 2 Dienst nicht erreichbar.

### 7.4 Zurückgehaltener Prüfsatz (nur Summen)

```bash
docker exec projektakte-test python -m projektakte.evaluate --real --set holdout --summary-only > ki-test-holdout.txt
```

Wichtig: Die Fragen dieses Satzes **nie öffnen oder in einen Chat kopieren**, nur die Summen. Sonst
taugt er nicht mehr als unabhängige Messung.

### 7.5 Schwellwerte einstellen

Aus der Schwellwerttabelle in `ki-test-eval.txt` die Werte wählen. Gesucht ist die Stelle, an der
kaum erwartete Quellen unter der Schwelle liegen, aber möglichst wenige Fragen ohne Antwort sie
erreichen.
- `ASK_MIN_RERANK`: Standard 0,01. Ab hier geht eine Fundstelle an das Modell.
- `SEARCH_MIN_RERANK`: Standard 0,05. Ab hier zeigt die Suche einen Treffer.
- `ASSIGN_MIN_SIMILARITY` (Mail-Vorschläge): aus.

Die Standardwerte stammen aus der Entwicklung mit der Fake-KI. Echte Reranker haben andere
Wertebereiche.

Danach die erkannten Formate (aus `/health/deps`) in `docs/ki-dienste.md`, Abschnitt „Stand der
Formatprüfung“ eintragen.

### 7.6 Was in den neuen Chat kopieren

- `ki-test-eval.txt` (ganz oder ab „Zusammenfassung“ plus die nicht bestandenen Fragen)
- `ki-test-holdout.txt` (nur Summen)
- der Inhalt von `http://localhost:8000/health/deps`
- die Startzeile aus `docker logs projektakte-test 2>&1 | grep Konfiguration` und die Warnungen,
  die mit „Konfiguration:“ beginnen
- auffällige Antworten aus der Oberfläche (Frage, Antwort, Quellen)

Keine echten Projektinhalte, Namen oder Mails in einen Cloud-Chat kopieren. Für echte Daten nur
zusammenfassen: „Frage zu X wurde mit der richtigen Quelle beantwortet / nicht gefunden“.

### 7.7 Danach mit echten Projektdaten

1. Freigabe nur lesend einbinden (`deploy/README.md`, „Projektordner vom Dateiserver (SMB)“). Zum
   ersten Ausprobieren reicht auch eine **Kopie** einiger Projektordner.
2. Schema prüfen: `python -m projektakte.structure /pfad/zu/den/projekten`. Die ausgegebenen Zeilen in
   die Einstellungen übernehmen, oder `PROJECT_NUMBER_PATTERN=auto` setzen und auf der Statusseite
   bestätigen.
3. Starten wie in 7.2, aber ohne `DEMO_MODE`, mit
   `-v /srv/projekte:/quellen/projekte:ro -e PROJECTS_DIR=/quellen/projekte`. Für den Dauerbetrieb das
   Beispiel unter `deploy/` mit Anmeldung und HTTPS verwenden.
4. Der erste Lauf liest jede Datei einmal über das Netz. Bei großen Ablagen dauert das Stunden,
   also nachts starten.
5. Nach dem ersten Einlesen zeigt `python -m projektakte.structure --data-dir /data` je Projekt die
   Textmenge in Token und ob das Projekt ganz in 32k/128k/1M passen würde.
6. Fragen stellen, die ihr aus eurer Arbeit kennt, und prüfen: Stimmt die Quelle? Stimmt der Fakt?

### 7.8 Typische Probleme

| Zeichen | Ursache / Abhilfe |
|---|---|
| „Antwort abgebrochen“ (Status `truncated`) | Qwen3 denkt zu lange → `LLM_EXTRA_BODY` wie oben oder `LLM_MAX_TOKENS` erhöhen |
| Dienst „nicht erreichbar“ in `/health/deps` | Adresse/Port, Firewall; in Docker ist `localhost` der Container selbst |
| falsches Format erkannt | `EMBED_FORMAT=openai|tei`, `RERANK_FORMAT=tei|cohere` festlegen |
| „Zeitbudget der Frage … erreicht“ | Dienste zu langsam → Denken abschalten, `ASK_DEADLINE`/Timeouts prüfen (Startwarnungen) |
| Scans/Pläne „übersprungen“ | `LLM_VISION=1` nur, wenn das Modell Bilder lesen kann |
| nach Modellwechsel Suche schlecht | Projektakte rechnet Vektoren bei geändertem Embedding-Dienst automatisch neu; Indexierung abwarten |
| Projekte werden nicht gefunden | `python -m projektakte.structure`, Statusseite „Projektordner“ |

## 8. Offene Punkte, nach Wichtigkeit

1. **Schritt 17: mit der lokalen KI messen und Schwellwerte einstellen** (Abschnitt 7). Erst danach
   ist die Antwortqualität im Echtbetrieb belegt. Danach Version 1.0.
2. **Projektstruktur, letzte Runde:** Behoben werden
   - eine einzelne Datei mit falsch kodiertem Namen, die heute das Einlesen späterer Projekte
     blockiert;
   - eine von Hand gesetzte `PROJECT_FOLDER_DEPTH`, die den bestätigten Vorschlag still überstimmt;
   - zusätzlich kommen „Bitte prüfen“-Hinweise bei Kunden- und Angebotsordnern.

   Der Stand steht im CHANGELOG unter „Unveröffentlicht“.
3. **Nicht gelesene Dateitypen:** `.doc`, `.xls`/`.xlsx`, `.msg` (Outlook), `.pptx`, `.odt`, `.dwg`.
   Der Strukturbefehl zeigt, wie viele davon in euren Projekten liegen. `.msg` und `.xlsx` sind
   vermutlich die wichtigsten Kandidaten zum Ergänzen.
4. **plMail-Seite:** Die Änderungen an plMail beschreibt `docs/plmail-prompt.md`. Das ist der Prompt
   für die KI im plMail-Projekt.
5. **Ideen, besprochen, nicht gebaut:**
   - **Gründliche Suche:** Für Vollständigkeitsfragen wie „alle Mängel seit 2022“ wird das Projekt in
     Portionen durchgelesen, dann zusammengeführt. Etwa 40 Modellaufrufe je Projekt, einige Minuten.
   - **Projektüberblick:** einmal je Dokument eine kurze Zusammenfassung beim Einlesen, als
     Orientierung für Überblicksfragen.
   - **16-Bit-Vektoren:** halber Arbeitsspeicher für die Suche, ohne messbaren Qualitätsverlust,
     sinnvoll ab einigen hunderttausend Abschnitten.
   - **Prefix-Caching auf dem KI-Server** (vLLM `--enable-prefix-caching`).
   - **Verworfen:** pxpipe (Text als Bilder) und Vektoren statt Text an das Sprachmodell. Bringt bei
     deutschem Text nichts und gefährdet exakte Zitate (Abschnitt 9).

## 9. Bekannte Grenzen und Antworten auf häufige Fragen

- **Wird das ganze Projekt in den Kontext geladen?** Nein, das Verfahren heißt RAG: Projektakte sucht
  die passenden Stellen und gibt nur die an das Modell. Ein durchschnittliches Brandschutzprojekt
  sind grob 250.000 Token, ein lokales Modell hat oft 32.000. Selbst wenn es passt, wird es
  langsamer (das Modell liest alles bei jeder Frage neu) und ungenauer. Projektgröße begrenzt die
  Antworten daher nicht. Vollständige Listen liefert die Suchseite (oder später die gründliche
  Suche).
- **Laufen Embedding und Reranker auch in einem Projekt?** Ja, immer. Das Projekt schränkt nur den
  Suchraum ein.
- **Was wird gespeichert?** Text, Abschnitte und Vektoren in SQLite (`DATA_DIR`), Kopien von
  Mail-Anhängen und Sicherungen. Antworten werden nicht gespeichert. Originale bleiben auf dem
  Dateiserver.
- Die Fake-KI beantwortet frei formulierte Fragen nur zu etwa 70 %. Für Vorführungen mit Fragen aus
  dem Publikum die echten Dienste anbinden.
- Markierungen in langen Komposita sind bewusst vorsichtig: lieber keine Markierung als eine falsche.
- Mehrere Geschosse in einer Frage („im 3. und 4. OG“) prüft die Fake-KI nicht einzeln.

## 10. Arbeitsregeln für die Weiterarbeit

- Nach jeder Änderung `sh scripts/test.sh`. Die Suite muss grün sein. Bei Änderungen am Image
  zusätzlich `sh scripts/docker-smoke.sh`.
- Ein Commit je zusammenhängendem Thema, aussagekräftige deutsche Commit-Titel.
- README.md, CHANGELOG.md und DECISIONS.md mitpflegen (DECISIONS: *warum* etwas so gelöst ist).
- Code-Kommentare und Docstrings auf Englisch, alles für Nutzer Sichtbare auf Deutsch.
- Keine Cloud-Dienste, keine Telemetrie, Geheimnisse nur aus Umgebungsvariablen. Keine echten
  Firmendaten ins Repository.
- **`tests/eval_holdout.json` nie öffnen oder zum Abstimmen benutzen**, nur
  `--set holdout --summary-only`.
- Fake-KI: lieber ablehnen als falsch antworten (keine falschen grünen Antworten). Neue Lockerungen
  nur mit Test, dass sie keine falschen Antworten erzeugen.
- Quellordner und Postfächer nur lesen. Nie Mails senden.

## 11. Wo was steht

| Datei | Inhalt |
|---|---|
| `README.md` | Bedienung, alle Einstellungen, Betrieb, Aufbau |
| `deploy/README.md` | Server: Docker Compose, Caddy, SMB, Konten, Zeitlimits, Sicherung |
| `deploy/projektakte.env.example` | Vorlage der Einstellungen |
| `DECISIONS.md` | Entscheidungen mit Begründung |
| `CHANGELOG.md` | Änderungen je Version |
| `ROADMAP.md` | Schritte, offen: 17 |
| `docs/ki-dienste.md` | Anbindung und Formate der KI-Dienste |
| `docs/plmail-*.md` | plMail-Anbindung und Prompt für die plMail-Seite |
| `docs/letzter-testlauf.txt` | Ausgabe eines Testlaufs |

## 12. Einstieg für einen neuen Chat

In Claude Code liest die Sitzung `CLAUDE.md` automatisch, die auf diese Datei verweist. Für einen
anderen Chat diesen Text vorweg schicken:

> Wir arbeiten am Repository aunepvp/projektakte, Branch claude/lucid-cerf-h2058c. Lies zuerst
> UEBERGABE.md, dann README.md und DECISIONS.md. Halte dich an die Arbeitsregeln in UEBERGABE.md,
> Abschnitt 10. Als Nächstes möchte ich: …

Beim Test mit der lokalen KI die Ausgaben aus Abschnitt 7.6 anhängen.
