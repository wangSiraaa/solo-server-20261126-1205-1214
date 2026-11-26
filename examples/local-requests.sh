#!/usr/bin/env bash
# 本地 HTTP 请求全流程：重置样条 -> 开模 -> 入样/浇注 -> 回放 -> 询单 -> 装盒 -> 取货 -> 余料 -> 并发。
# 用法：
#   npm start
#   bash examples/local-requests.sh
# 可设 BASE_URL=http://127.0.0.1:4310。

set -euo pipefail
BASE_URL="${BASE_URL:-http://127.0.0.1:4310}"
JSON="Content-Type: application/json"

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$1"; }

say '0. 健康检查'
curl -sS "$BASE_URL/api/health"; echo

say '1. 重置为题目样条：8 格、两处蓝边黄心、红点一左一右，并预切成两片'
curl -sS -X POST -H "$JSON" -d '{}' "$BASE_URL/api/admin/reset" >/tmp/soap-reset.json
python3 - <<'PY'
import json
d=json.load(open('/tmp/soap-reset.json'))
print('seeded=', d['seeded'], 'bar=', d['bar']['code'], 'pieces=', [p['code'] for p in d['pieces']])
PY

say '2. 柜员换带方向样卡询单：允许左右翻转时应有两片'
curl -sS -X POST -H "$JSON" \
  -d '{"barCode":"BAR-DEMO","cardId":"CARD-REDL","piecesPerBox":2,"boxCount":1,"orientationPolicy":"horizontal-ok"}' \
  "$BASE_URL/api/quotes" >/tmp/soap-quote.json
python3 - <<'PY'
import json
q=json.load(open('/tmp/soap-quote.json'))
print('feasible=', q['feasible'], 'maxBoxes=', q['maxBoxes'])
for a in q['maxAssignments']:
    m=a['match']
    print(a['pieceCode'], m['face'], 'cell', m['cellIndex'], m['transform'])
PY

say '2b. 柜员问：今天能照哪张样卡装盒？'
curl -sS "$BASE_URL/api/recommendations?barCode=BAR-DEMO&piecesPerBox=2&orientationPolicy=horizontal-ok" \
  | python3 -c 'import sys,json; [print(r["cardId"], "physical", r["physicalMatchCount"], "boxes", r["maxCompleteBoxes"]) for r in json.load(sys.stdin)]'

say '3. 开模：新建一根 9 格长条模（先不合模）'
curl -sS -X POST -H "$JSON" \
  -d '{"code":"BAR-LOCAL","length":9,"rows":5,"cols":5,"standardWidth":3,"kerfWidth":1,"autoClose":false}' \
  "$BASE_URL/api/bars" | python3 -c 'import sys,json; b=json.load(sys.stdin); print(b["code"], "length", b["length"], "status", b["status"])'

say '4. 入样/浇注：按层提交真实颜色，空格用 . 或 null'
curl -sS -X POST -H "$JSON" \
  --data-binary @- "$BASE_URL/api/bars/BAR-LOCAL/pours" <<'JSON' | python3 -c 'import sys,json; d=json.load(sys.stdin); print("poured layers:", len(d) if isinstance(d,list) else d)'
{
  "by": "master-local",
  "layers": [
    {"index": 0, "pattern": ["R....", ".BBB.", ".BYB.", ".BBB.", "....."]},
    {"index": 1, "pattern": [".....", ".....", ".....", ".....", "....."]},
    {"index": 2, "pattern": [".....", "..G..", ".GGG.", "..G..", "....."]}
  ]
}
JSON

say '5. 合模；可选 normal / invert（倒扣）/ shift_one（平移一格合模）'
curl -sS -X POST -H "$JSON" -d '{"closeMode":"normal"}' \
  "$BASE_URL/api/bars/BAR-LOCAL/close" | python3 -c 'import sys,json; b=json.load(sys.stdin); print(b["code"], b["status"], b["closeMode"])'

say '6. 师傅凭条坯号回放实际截面（文本图）'
curl -sS "$BASE_URL/api/bars/BAR-LOCAL/replay" >/tmp/soap-replay.json
python3 - <<'PY'
import json
r=json.load(open('/tmp/soap-replay.json'))
for layer in r['layers'][:3]:
    print(f"-- layer {layer['index']} --")
    print(layer['text'])
PY

say '7. 切前查询余料'
curl -sS "$BASE_URL/api/bars/BAR-LOCAL/remnants" | python3 -m json.tool

say '8. 从条头切一片：保留 0..2，第 3 格为切口，余料从第 4 格开始'
curl -sS -X POST -H "$JSON" \
  -d '{"from":"head","width":3,"kerfWidth":1,"clerk":"local-request"}' \
  "$BASE_URL/api/bars/BAR-LOCAL/cut" | python3 -c 'import sys,json; p=json.load(sys.stdin); print(p["code"], p["start"], p["end"], "kerf", p["kerfIndexes"])'

say '9. 切后查询余料'
curl -sS "$BASE_URL/api/bars/BAR-LOCAL/remnants" | python3 -m json.tool

say '10. 样卡入档：新增一张“左上红点”方向卡（设计图以后可 PATCH 改版）'
curl -sS -X POST -H "$JSON" \
  -d '{"code":"CARD-LOCAL-REDL","name":"本地入样卡","directional":true,"matchMode":"mask","pattern":["R....",".BBB.",".BYB.",".BBB.","....."]}' \
  "$BASE_URL/api/cards" | python3 -c 'import sys,json; c=json.load(sys.stdin); print(c["code"], "v"+str(c["version"]))'

say '11. 办理一盒两片订单：原子领片、装盒，返回 BOX-0001'
curl -sS -X POST -H "$JSON" \
  -d '{"barCode":"BAR-DEMO","cardId":"CARD-REDL","piecesPerBox":2,"boxCount":1,"orientationPolicy":"horizontal-ok","clerk":"clerk-li","customer":"手作教室","destination":"教室自提柜 A1"}' \
  "$BASE_URL/api/orders" >/tmp/soap-order.json
python3 - <<'PY'
import json
d=json.load(open('/tmp/soap-order.json'))
print(d['order']['code'], d['order']['status'])
for b in d['boxes']:
    print(b['code'], b['pieceIds'])
    for s in b['slots']:
        print(' ', s['pieceCode'], s['outwardFace'], s['orientationTransform'], 'cell', s['actualCellIndex'])
PY

say '12. 办理取货并登记确切去向'
curl -sS -X POST -H "$JSON" \
  -d '{"receivedBy":"wang","destination":"上海展会柜台 B12"}' \
  "$BASE_URL/api/boxes/BOX-0001/pickup" | python3 -c 'import sys,json; b=json.load(sys.stdin); print(b["code"], b["status"], b["pickup"])'

say '13. 按盒号查原片和两面；重启服务后此请求仍应成功'
curl -sS "$BASE_URL/api/boxes/BOX-0001" >/tmp/soap-box.json
python3 - <<'PY'
import json
b=json.load(open('/tmp/soap-box.json'))
print('box', b['code'], b['status'], 'pieces', b['pieceIds'])
for f in b['pieceFaces']:
    print(f['pieceId'], 'outward=', f['outwardFace'], f['orientationTransform'], 'headCell=', f['headFace']['cellIndex'], 'tailCell=', f['tailFace']['cellIndex'])
print('destination=', b['pickup']['destination'])
PY

say '14. 两个柜员同时领同一片：只能各领实际存在状态，其中一个收到 409'
curl -sS -X POST -H "$JSON" \
  -d '{"code":"BAR-CLAIM","length":8,"rows":3,"cols":3,"autoClose":true,"precut":{"width":3,"kerfWidth":1}}' \
  "$BASE_URL/api/bars" >/dev/null
(curl -sS -o /tmp/claim-a.json -w 'A HTTP %{http_code}\n' -X POST -H "$JSON" -d '{"clerk":"A"}' "$BASE_URL/api/pieces/PIECE-0004/claim") &
(curl -sS -o /tmp/claim-b.json -w 'B HTTP %{http_code}\n' -X POST -H "$JSON" -d '{"clerk":"B"}' "$BASE_URL/api/pieces/PIECE-0004/claim") &
wait
python3 - <<'PY'
import json
for who,path in [('A','/tmp/claim-a.json'),('B','/tmp/claim-b.json')]:
    d=json.load(open(path))
    print(who, d.get('code') or d.get('error',{}).get('code'), d.get('clerk') or d.get('error',{}).get('message'))
PY

say '15. 两个柜员同时从同一条切取：格段不重叠，切口格不二次出现'
curl -sS -X POST -H "$JSON" \
  -d '{"code":"BAR-CONCURRENT","length":12,"rows":3,"cols":3,"autoClose":true}' \
  "$BASE_URL/api/bars" >/dev/null
(curl -sS -X POST -H "$JSON" -d '{"clerk":"A","width":3,"kerfWidth":1}' "$BASE_URL/api/bars/BAR-CONCURRENT/cut") &
(curl -sS -X POST -H "$JSON" -d '{"clerk":"B","width":3,"kerfWidth":1}' "$BASE_URL/api/bars/BAR-CONCURRENT/cut") &
wait
curl -sS "$BASE_URL/api/bars/BAR-CONCURRENT/remnants" | python3 -c 'import sys,json; r=json.load(sys.stdin); print("remainder", [(s["start"],s["end"]) for s in r["segments"]])'

cat <<'TIP'

完成。现在停止服务（Ctrl+C），用同一个 DATA_FILE 重新启动，例如：
  DATA_FILE=./data/db.json PORT=4310 npm start
然后执行：
  curl -sS http://127.0.0.1:4310/api/boxes/BOX-0001
仍可按盒号找到原片、两面和去向。
TIP
