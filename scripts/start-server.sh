#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 -m bingo serve --host 127.0.0.1 --port "${BINGO_PORT:-8765}" --state "${BINGO_STATE:-data/state.json}"
