#!/usr/bin/env bash
# A copy-pasteable end-to-end walkthrough.  It starts and restarts the server,
# so it proves that the handover number and actual owners survive disconnect.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${DEMO_PORT:-18080}"
BASE="http://127.0.0.1:${PORT}"
DB="${DEMO_DB:-/tmp/disk-handover-curl-demo.db}"
rm -f "${DB}" "${DB}-wal" "${DB}-shm"

python3 "${ROOT_DIR}/server.py" --host 127.0.0.1 --port "${PORT}" --db "${DB}" \
  >/tmp/disk-handover-demo.log 2>&1 &
PID=$!
cleanup() { kill "${PID}" 2>/dev/null || true; }
trap cleanup EXIT

for _ in $(seq 1 50); do
  if curl -sf "${BASE}/health" >/dev/null 2>&1; then break; fi
  sleep 0.1
done

hr() { printf '\n\033[1;36m== %s ==\033[0m\n' "$1"; }
show() { python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin), ensure_ascii=False, indent=2))'; }

hr '1. 重新建盘：甲的 3 格稿 + 2 格稿 + 返回祖先目录的入口'
curl -sS -X POST "${BASE}/v1/admin/rebuild" \
  -H 'Content-Type: application/json' \
  -d '{"scenario":"standard"}' | show

hr '2. 建报价/完整接手单 HO-CURL-1（含版本、失效时刻；此时尚未换主人）'
curl -sS -X POST "${BASE}/v1/handovers" \
  -H 'Content-Type: application/json' \
  -d '{
    "id":"HO-CURL-1",
    "from_account_id":"jia",
    "to_account_id":"yi",
    "root_entry_id":"ent_project",
    "ttl_seconds":3600
  }' | show

hr '3. 乙确认报价：归属和容量在这一个事务里生效'
curl -sS -X POST "${BASE}/v1/handovers/HO-CURL-1/confirm" \
  -H 'Content-Type: application/json' \
  -d '{"account_id":"yi"}' | show

hr '4. 普通上传入口：甲上传 5 格应成功；乙上传 1 格应被额度拒绝'
curl -sS -X POST "${BASE}/v1/uploads" \
  -H 'Content-Type: application/json' \
  -H 'X-Account-Id: jia' \
  -d '{"name":"甲用回五格","size_cells":5}' | show || true
curl -sS -X POST "${BASE}/v1/uploads" \
  -H 'Content-Type: application/json' \
  -H 'X-Account-Id: yi' \
  -d '{"name":"乙不能再添一格","size_cells":1}' | show || true

hr '5. 重试同一次确认：幂等回放，不再次转移/扣费'
curl -sS -X POST "${BASE}/v1/handovers/HO-CURL-1/confirm" \
  -H 'Content-Type: application/json' \
  -d '{"account_id":"yi"}' | show

hr '6. 模拟断线/重启服务：凭交接号查每份文件的实际主人'
kill "${PID}"; wait "${PID}" 2>/dev/null || true
python3 "${ROOT_DIR}/server.py" --host 127.0.0.1 --port "${PORT}" --db "${DB}" \
  >>/tmp/disk-handover-demo.log 2>&1 &
PID=$!
for _ in $(seq 1 50); do
  if curl -sf "${BASE}/health" >/dev/null 2>&1; then break; fi
  sleep 0.1
done
curl -sS "${BASE}/v1/handovers/HO-CURL-1/ownership" | show

hr '7. 别名盘：另起两张单；两格稿入口只是通往三格稿的别名'
curl -sS -X POST "${BASE}/v1/admin/rebuild" \
  -H 'Content-Type: application/json' \
  -d '{"scenario":"alias_draft"}' >/dev/null
curl -sS "${BASE}/v1/scan?root_entry_id=ent_project" | show
for hid in HO-ALIAS-1 HO-ALIAS-2; do
  curl -sS -X POST "${BASE}/v1/handovers" \
    -H 'Content-Type: application/json' \
    -d "{\"id\":\"${hid}\",\"from_account_id\":\"jia\",\"to_account_id\":\"yi\",\"root_entry_id\":\"ent_project\"}" \
    >/dev/null
done
curl -sS -X POST "${BASE}/v1/handovers/HO-ALIAS-1/confirm" \
  -H 'Content-Type: application/json' -d '{"account_id":"yi"}' | show
echo '--- 第二张单也可结清：同一物理文件已归乙，本单不再转移/扣费 ---'
curl -sS -X POST "${BASE}/v1/handovers/HO-ALIAS-2/confirm" \
  -H 'Content-Type: application/json' -d '{"account_id":"yi"}' | show

hr '8. 独立两稿 + 乙提前占 1 格：整盘报价失败；清理后用新单办完'
curl -sS -X POST "${BASE}/v1/admin/rebuild" \
  -H 'Content-Type: application/json' \
  -d '{"scenario":"blocked_upload"}' >/dev/null
curl -sS -X POST "${BASE}/v1/handovers" \
  -H 'Content-Type: application/json' \
  -d '{"id":"HO-FAIL","from_account_id":"jia","to_account_id":"yi","root_entry_id":"ent_project"}' >/dev/null
curl -sS -X POST "${BASE}/v1/handovers/HO-FAIL/confirm" \
  -H 'Content-Type: application/json' -d '{"account_id":"yi"}' | show || true
curl -sS -X DELETE "${BASE}/v1/files/file_yi_blocker" \
  -H 'X-Account-Id: yi' | show
curl -sS -X POST "${BASE}/v1/handovers" \
  -H 'Content-Type: application/json' \
  -d '{"id":"HO-NEW","from_account_id":"jia","to_account_id":"yi","root_entry_id":"ent_project"}' >/dev/null
curl -sS -X POST "${BASE}/v1/handovers/HO-NEW/confirm" \
  -H 'Content-Type: application/json' -d '{"account_id":"yi"}' | show

printf '\nDemo complete. Server log: /tmp/disk-handover-demo.log\n'
