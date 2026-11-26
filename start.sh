#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

DB="${LOSTFOUND_DB:-lostfound.db}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

exec python3 app.py \
  --db "$DB" \
  --host "$HOST" \
  --port "$PORT" \
  --seed-demo \
  "$@"
