#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export COOP_DB="${COOP_DB:-$ROOT/demo/coop-demo.sqlite3}"
exec python3 -m coop.server --db "$COOP_DB" --host 127.0.0.1 --port "${COOP_PORT:-8931}" "$@"
