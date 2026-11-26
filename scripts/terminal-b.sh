#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 -m bingo shell \
  --url "http://127.0.0.1:${BINGO_PORT:-8765}" \
  --game "${BINGO_GAME:-main}" \
  --participant pB \
  --card card-B \
  --terminal terminal-B
