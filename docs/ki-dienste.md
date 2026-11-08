# KI-Dienste im lokalen Netz

Alle drei laufen auf dem Unraid-Server `192.168.178.62`. Adressen und Modellnamen kommen aus
Umgebungsvariablen; die Werte unten sind Standardwerte. **Formate vor dem ersten Einsatz mit `curl`
prüfen** und die tatsächliche Antwortform in diese Datei eintragen.

## Sprachmodell – `LLM_URL=http://192.168.178.62:8000/v1`

OpenAI-kompatibel (Chat Completions), Modell `LLM_MODEL=qwen3.8-flash-next` (wird gegen eine
multimodale „Omni“-Variante getauscht).

```bash
curl -s http://192.168.178.62:8000/v1/models
curl -s http://192.168.178.62:8000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.8-flash-next","messages":[{"role":"user","content":"Sag OK."}],"max_tokens":8}'
```

Hinweise: Antworten können `<think>…</think>` enthalten – vor der Anzeige entfernen. Kontextfenster
sparsam nutzen (nur die besten Abschnitte in den Prompt). Bilder nur senden, wenn
`LLM_VISION=1` gesetzt ist (Inhalt als `image_url` mit `data:`-URL).

## Embeddings – `EMBED_URL=http://192.168.178.62:8001`

Qwen3-Embedding-4B. Erwartet wird die OpenAI-Form `POST {EMBED_URL}/v1/embeddings`:

```bash
curl -s http://192.168.178.62:8001/v1/embeddings -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-embedding-4b","input":["Rauchableitung im Treppenraum"]}'
# → {"data":[{"embedding":[...],"index":0}], ...}
```

Falls der Server stattdessen das TEI-Format spricht (`POST {EMBED_URL}/embed` mit
`{"inputs":[...]}` → `[[...]]`), beide Formate unterstützen und beim Start erkennen.
Qwen3-Embedding liefert bessere Treffer, wenn **Suchanfragen** mit einer Instruktion versehen werden
(`"Instruct: Finde Abschnitte aus Brandschutz-Projektunterlagen, die die Frage beantworten\nQuery: …"`),
Dokumentabschnitte dagegen ohne. Vektoren normalisieren, Ähnlichkeit = Skalarprodukt.

## Reranker – `RERANK_URL=http://192.168.178.62:4343/rerank`

Qwen3-Reranker-4B. Zwei verbreitete Formate – beide unterstützen:

```bash
# TEI-Stil
curl -s http://192.168.178.62:4343/rerank -H 'Content-Type: application/json' \
  -d '{"query":"Rauchableitung","texts":["Text A","Text B"]}'
# → [{"index":1,"score":0.93},{"index":0,"score":0.12}]

# Cohere/Jina-Stil
curl -s http://192.168.178.62:4343/rerank -H 'Content-Type: application/json' \
  -d '{"query":"Rauchableitung","documents":["Text A","Text B"],"top_n":2}'
# → {"results":[{"index":1,"relevance_score":0.93}, ...]}
```

Reranker nur auf die besten ~30 Kandidaten der hybriden Suche anwenden. Fällt er aus, mit der
Reihenfolge der hybriden Suche weiterarbeiten (und das in `/health/deps` melden).

## Tests

Keine Tests gegen die echten Dienste in der normalen Testsuite. Für jeden Dienst gibt es einen Fake
(z. B. deterministische Pseudo-Embeddings aus einem Hash der Wörter), sodass Retrieval-Tests
reproduzierbar sind.

## Stand der Formatprüfung

Aus der Entwicklungsumgebung sind die Dienste nicht erreichbar; die tatsächlichen Antwortformate
sind daher **noch nicht eingetragen**. Projektakte erkennt sie zur Laufzeit selbst:

| Dienst | unterstützt | erkannt wird über |
|---|---|---|
| Sprachmodell | OpenAI Chat Completions, `<think>…</think>` wird entfernt | `GET /v1/models` + Mini-Chat |
| Embeddings | OpenAI `/v1/embeddings` und TEI `/embed` (Reihenfolge: OpenAI, dann TEI) | erster Aufruf |
| Reranker | Anfrage TEI (`texts`) oder Cohere/Jina (`documents`); Antwort Liste oder `results` | erster Aufruf |

Nach dem Umschalten (README, Abschnitt „Echte KI-Dienste anbinden“) zeigt `GET /health/deps` das
erkannte Format je Dienst; bitte hier eintragen. Zusätzlich gibt es einen ausdrücklich markierten
Integrationstest gegen die echten Dienste:

```bash
PROJEKTAKTE_INTEGRATION=1 LLM_URL=http://192.168.178.62:8000/v1 EMBED_URL=http://192.168.178.62:8001 \
  RERANK_URL=http://192.168.178.62:4343/rerank sh scripts/test.sh -m integration
```

Beim ersten Lauf mit den echten Embeddings erkennt Projektakte am Fingerabdruck, dass sich der
Dienst geändert hat, und berechnet alle Vektoren neu.

### Antwortqualität messen und Schwellwerte einstellen

```bash
python -m projektakte.evaluate --real --json ergebnis.json
```

indexiert den Demo-Datensatz mit den echten Diensten in einen eigenen Index (`$DATA_DIR/eval`) und
lässt den Evaluationssatz (79 Fragen) laufen. Die Ausgabe zeigt je Frage, ob die erwartete Quelle
unter den ersten 5 Treffern steht, ob Fakt und Quelle in der Antwort stehen, den Status und die
Rerank-Scores; zusammengefasst die Score-Verteilung und eine Schwellwerttabelle (wie viele
erwartete Quellen unter einer Schwelle lägen gegen wie viele Fragen ohne Antwort sie erreichen).
Damit `ASK_MIN_RERANK` (Standard 0,01), `SEARCH_MIN_RERANK` (0,05) und `ASSIGN_MIN_SIMILARITY`
(aus) für die echten Modelle einstellen. Die Werte stammen aus der Entwicklung mit den Fakes.

`--set holdout --summary-only` misst zusätzlich mit dem zurückgehaltenen Prüfsatz
(`tests/eval_holdout.json`, 40 Fragen eines unabhängigen Autors) und zeigt nur Summen. Vor jedem Lauf
werden die Dienste geprüft. Exit-Code 0 = Ziele erreicht (≥ 80 % beantwortbare Fragen richtig,
≥ 90 % unbeantwortbare abgelehnt; `--min-correct`/`--min-refused`), 1 = verfehlt, 2 = Dienst nicht
erreichbar. Mit den Fakes: eigener Satz 94/94, Prüfsatz 0,57 / 1,00 (siehe README).

### Denkmodelle

Qwen3 denkt standardmäßig in `<think>…</think>`. Reicht `LLM_MAX_TOKENS` nicht, endet die Ausgabe
mitten im Denken (`finish_reason: length`, nur `reasoning_content` oder offenes `<think>`).
Projektakte meldet das als Status `truncated` („Antwort abgebrochen“) mit den Fundstellen statt als
„nicht gefunden“. Abhilfe: `LLM_EXTRA_BODY={"chat_template_kwargs":{"enable_thinking":false}}` oder
ein größeres `LLM_MAX_TOKENS`. Absagen echter Modelle („Dazu liegen keine Informationen vor“, „Die
Quellen enthalten keine Angaben …“) und Quellenformen wie `[Quelle 2]`, `[1–3]` oder „laut Quelle 2“
werden erkannt.

### Abkürzungen und Synonyme

Die Volltextsuche (Fragen und Suche, nicht die Mail-Zuordnung) erweitert Abkürzungen und
Fachsynonyme aus einer eingebauten Liste (`projektakte/textutil.py`, `BUILTIN_SYNONYMS`) und aus
einer eigenen UTF-8-Datei (`SYNONYMS_FILE`, eine Gruppe je Zeile, `BMA = Brandmeldeanlage` in beide
Richtungen, `EG -> Erdgeschoss` nur in eine, `#` Kommentar). Echte Embeddings kennen Synonyme
ohnehin; die Liste hilft dem Volltext.

## Fake-Dienste

`python -m projektakte.fake_ai --port 8999` bildet alle drei Schnittstellen auf einem Port nach
(`--embed-format`, `--rerank-format`, `--no-think`, `--delay`, `--chat-quirk
length|reasoning_only|unterminated_think`, `--no-models`); `AI_MODE=fake` startet dieselben Fakes
im Prozess der Anwendung. Die Tests nutzen sie auf `127.0.0.1` mit zufälligem Port. Der Fake-Chat
ist ein extraktiver Leser (Sätze nach Themenwörtern, Synonymen und IDF bewertet, Projektname zählt
nicht, „letzter Stand“ = neueste Quelle, Tabellen als „Spalte: Wert“, Entscheidungsfragen zitieren
den Satz mit der Entscheidung). Er lehnt im Zweifel ab: Wert-Fragen (Kosten, Anzahl, Datum, Dauer,
Maß, Name, Norm …) beantwortet er nur mit einem Wert dieser Art beim gefragten Thema. Er zeigt den
Ablauf glaubwürdig, versteht frei formulierte Fragen aber nur etwa in 7 von 10 Fällen und sagt nichts
über die Antwortqualität des echten Modells.
