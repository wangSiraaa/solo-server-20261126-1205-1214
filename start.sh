#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

export HOST="${HOST:-127.0.0.1}"
export PORT="${PORT:-8080}"
export DB_PATH="${DB_PATH:-$ROOT_DIR/data/diskhandover.db}"

exec python3 server.py --host "$HOST" --port "$PORT" --db "$DB_PATH"
