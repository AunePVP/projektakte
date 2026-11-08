#!/bin/sh
# Builds the image, starts a container (demo data, fake AI, APP_ENV=production from the image) and
# checks /health, /health/ready, the HEALTHCHECK command, /health/deps, the access control, security
# headers, the access log (no search terms), a question with sources, the backup and the refused
# starts (AUTH_DISABLED outside dev, data directory not writable).
set -eu
cd "$(dirname "$0")/.."
IMAGE="${IMAGE:-projektakte:smoke}"
PORT="${SMOKE_PORT:-18000}"
PW="smoke-passwort-123"
KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
USERS="smoke:$(printf '%s\n' "$PW" | python3 -m projektakte.passwd smoke --stdin | cut -d: -f2-)"
fail() { echo "FEHLER: $*" >&2; exit 1; }

docker build -q -t "$IMAGE" . >/dev/null
[ "$(docker run --rm --entrypoint sh "$IMAGE" -c 'find /app -name __pycache__ -o -name tests -o -name "*.md" | wc -l')" = 0 ] \
  || fail "Image enthält __pycache__, Tests oder Doku"

START=$(date +%s)
CID=$(docker run -d --rm -p "127.0.0.1:$PORT:8000" -e AI_MODE=fake -e DEMO_MODE=1 -e INDEX_INTERVAL=0 \
      -e AUTH_USERS="$USERS" -e SECRET_KEY="$KEY" "$@" "$IMAGE")
trap 'docker stop "$CID" >/dev/null 2>&1 || true' EXIT
for _ in $(seq 1 100); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then break; fi
  sleep 0.1
done
echo "health:  $(curl -fsS "http://127.0.0.1:$PORT/health")  (nach $(( $(date +%s) - START )) s)"
echo "ready:   $(curl -fsS "http://127.0.0.1:$PORT/health/ready")"
echo "check:   $(docker exec "$CID" python -m projektakte.healthcheck)"
echo "deps:    $(curl -sS -u "smoke:$PW" "http://127.0.0.1:$PORT/health/deps" | python3 -c \
  'import json,sys; d=json.load(sys.stdin); print(d["status"], {k: v.get("format") for k, v in d["services"].items()})')"
echo "ohne Login: $(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/projects")"
curl -sS -D - -o /dev/null "http://127.0.0.1:$PORT/login" | grep -qi '^x-frame-options: DENY' \
  || fail "Sicherheits-Header fehlen"
[ "$(curl -sS -o /dev/null -w '%{http_code}' -u "smoke:$PW" -X POST -H 'Sec-Fetch-Site: cross-site' \
     "http://127.0.0.1:$PORT/api/reindex")" = 403 ] || fail "fremde POST-Anfrage nicht abgelehnt"
echo "Header:  Sicherheits-Header gesetzt, fremde POST-Anfrage abgelehnt"
for _ in $(seq 1 100); do
  RUNS=$(curl -sS -u "smoke:$PW" "http://127.0.0.1:$PORT/api/status" | python3 -c \
    'import json,sys; d=json.load(sys.stdin)["indexing"]; print(d["runs"] if not d["running"] else 0)')
  [ "$RUNS" -ge 1 ] && break
  sleep 0.2
done
curl -sS -u "smoke:$PW" -H 'Content-Type: application/json' "http://127.0.0.1:$PORT/api/ask" \
  -d '{"question": "Was fordert Auflage 7 der Baugenehmigung?", "project": "BS-2024-0137"}' | python3 -c \
  'import json,sys; d=json.load(sys.stdin); print("frage:  ", d["status"], "|", d["answer"][:90], "|", (d["sources"] or d.get("context") or [{}])[0].get("label"))'
curl -sS -o /dev/null -u "smoke:$PW" "http://127.0.0.1:$PORT/api/search?q=Zebrafink-Suchbegriff"
sleep 0.5
docker logs "$CID" 2>&1 | grep -q 'GET /api/search HTTP' || fail "Zugriff fehlt im Log"
if docker logs "$CID" 2>&1 | grep -q 'Zebrafink'; then fail "Suchbegriff steht im Log"; fi
echo "Log:     Zugriffe ohne Query-String"
docker exec "$CID" python -m projektakte.backup /data/sicherungen | sed 's/^/backup:  /'

# refused starts: clear German message, exit code 2
OUT=$(docker run --rm -e AUTH_DISABLED=1 "$IMAGE" 2>&1) && fail "Start mit AUTH_DISABLED in production"
echo "$OUT" | grep -q 'AUTH_DISABLED=1 ist nur mit APP_ENV=dev erlaubt' || fail "Meldung fehlt: $OUT"
echo "Abbruch: AUTH_DISABLED in production abgelehnt"
HOSTDIR=$(mktemp -d)  # belongs to the calling user, not to uid 10001
OUT=$(docker run --rm -v "$HOSTDIR:/data" -e APP_ENV=dev -e AUTH_DISABLED=1 "$IMAGE" 2>&1) \
  && fail "Start mit nicht beschreibbarem DATA_DIR"
rmdir "$HOSTDIR" 2>/dev/null || true
echo "$OUT" | grep -q 'ist nicht beschreibbar' || fail "Meldung fehlt: $OUT"
echo "Abbruch: nicht beschreibbares DATA_DIR erkannt ($(echo "$OUT" | grep -o 'chown -R [0-9:]*' | head -1))"
