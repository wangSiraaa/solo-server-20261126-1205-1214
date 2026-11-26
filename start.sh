#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8787}"
DB="${DB:-data/family-pool.db}"
exec python3 -m family_pool serve --db "$DB" --host "$HOST" --port "$PORT" "$@"
