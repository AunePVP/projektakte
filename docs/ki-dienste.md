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
