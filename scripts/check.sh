#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}" python3 -m unittest discover -s tests -v
PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}" python3 -m bingo.e2e
