#!/bin/sh
# Builds the image, starts a container and checks GET /health (and /health/deps with AI_MODE=fake).
set -eu
cd "$(dirname "$0")/.."
IMAGE="${IMAGE:-projektakte:smoke}"
PORT="${SMOKE_PORT:-18000}"
docker build -q -t "$IMAGE" . >/dev/null
CID=$(docker run -d --rm -p "$PORT:8000" -e AI_MODE=fake -e DEMO_MODE=1 "$@" "$IMAGE")
trap 'docker stop "$CID" >/dev/null 2>&1 || true' EXIT
START=$(date +%s)
for _ in $(seq 1 50); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then break; fi
  sleep 0.2
done
echo "health:  $(curl -fsS "http://127.0.0.1:$PORT/health")  (nach $(( $(date +%s) - START )) s)"
echo "deps:    $(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/health/deps")"
