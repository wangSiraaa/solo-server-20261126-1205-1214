#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m beadworks.server \
  --db "${BEADWORKS_DB:-data/beadworks.json}" \
  --host "${HOST:-0.0.0.0}" \
  --port "${PORT:-8000}"
