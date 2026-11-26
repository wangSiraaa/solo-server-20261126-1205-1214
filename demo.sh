#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
PORT="${PORT:-8787}"
HOST="${HOST:-127.0.0.1}"
DB="${DB:-data/demo.db}"
BASE="http://${HOST}:${PORT}"

rm -f "${DB}" "${DB}-wal" "${DB}-shm"
mkdir -p "$(dirname "${DB}")"

PIDS=()
cleanup() {
  for pid in "${PIDS[@]:-}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
  done
}
trap cleanup EXIT INT TERM

start_server() {
  python3 -m family_pool serve --db "$DB" --host "$HOST" --port "$PORT" >/tmp/family-pool-demo.log 2>&1 &
  local pid=$!
  PIDS+=("$pid")
  for _ in $(seq 1 50); do
    if python3 - "$BASE/api/health" <<'PY' >/dev/null 2>&1
import sys, urllib.request
urllib.request.urlopen(sys.argv[1], timeout=0.5).read()
PY
    then
      echo "server up: $BASE (pid $pid, db $DB)"
      return 0
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "server failed to start; log:" >&2
      cat /tmp/family-pool-demo.log >&2
      exit 1
    fi
    sleep 0.1
  done
  echo "server did not become healthy" >&2
  exit 1
}

stop_last_server() {
  local pid="${PIDS[-1]}"
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}

echo "== 1/4 首次开机：播种、开场、复演全部业务断言 =="
start_server
python3 -m family_pool.replay --base-url "$BASE"

echo "== 2/4 正常关机 =="
stop_last_server

echo "== 3/4 使用同一个 SQLite 文件重新开机 =="
start_server

echo "== 4/4 关机重开后核对位置、实际接手人和离开事件 =="
python3 -m family_pool.replay --base-url "$BASE" --verify-restart

echo
echo "DEMO OK. Persistent database: $DB"
echo "You can keep exploring with: python3 -m family_pool serve --db $DB --port $PORT"
