#!/bin/sh
# Builds the image, starts a container (demo data, fake AI) and checks /health, /health/deps and,
# once the background indexing is done, a question with sources via the API.
set -eu
cd "$(dirname "$0")/.."
IMAGE="${IMAGE:-projektakte:smoke}"
PORT="${SMOKE_PORT:-18000}"
PW="smoke-passwort-123"
USERS="smoke:$(printf '%s\n' "$PW" | python3 -m projektakte.passwd smoke --stdin | cut -d: -f2-)"
docker build -q -t "$IMAGE" . >/dev/null
START=$(date +%s)
CID=$(docker run -d --rm -p "$PORT:8000" -e AI_MODE=fake -e DEMO_MODE=1 -e INDEX_INTERVAL=0 \
      -e AUTH_USERS="$USERS" -e SECRET_KEY=smoke "$@" "$IMAGE")
trap 'docker stop "$CID" >/dev/null 2>&1 || true' EXIT
for _ in $(seq 1 100); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then break; fi
  sleep 0.1
done
echo "health:  $(curl -fsS "http://127.0.0.1:$PORT/health")  (nach $(( $(date +%s) - START )) s)"
echo "deps:    $(curl -sS -u "smoke:$PW" "http://127.0.0.1:$PORT/health/deps" | python3 -c \
  'import json,sys; d=json.load(sys.stdin); print(d["status"], {k: v.get("format") for k, v in d["services"].items()})')"
echo "ohne Login: $(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/projects")"
for _ in $(seq 1 100); do
  RUNS=$(curl -sS -u "smoke:$PW" "http://127.0.0.1:$PORT/api/status" | python3 -c \
    'import json,sys; d=json.load(sys.stdin)["indexing"]; print(d["runs"] if not d["running"] else 0)')
  [ "$RUNS" -ge 1 ] && break
  sleep 0.2
done
curl -sS -u "smoke:$PW" -H 'Content-Type: application/json' "http://127.0.0.1:$PORT/api/ask" \
  -d '{"question": "Was fordert Auflage 7 der Baugenehmigung?", "project": "BS-2024-0137"}' | python3 -c \
  'import json,sys; d=json.load(sys.stdin); print("frage:  ", d["status"], "|", d["answer"][:90], "|", d["sources"][0]["label"])'
