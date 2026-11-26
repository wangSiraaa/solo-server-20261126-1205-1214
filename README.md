# 绣样接力 · 局域网编作台

一个**零外部依赖**的 Node.js 服务 + 原生网页，用来模拟窄绣带的样段登记、针图签发、逐格报工、接缝锁定和主编改样。只模拟针图与报工，不连接任何刺绣设备。

## 运行

```bash
npm start
# 或：PORT=8080 STUDIO_DATA=./data/studio-state.json node server.js
```

打开：

- <http://localhost:3000/>
- 服务也会尝试监听 `8000`、`8080`、`5000`，便于不同验题入口访问。
- 局域网内其他终端访问 `http://<本机IP>:3000/`。

一键验题脚本：

```bash
npm test
```

脚本会自动启动临时服务，覆盖三段新样、红带两个改样现场、双终端并发、迟到交回、下游工单不倒退以及重启持久化。

状态默认原子写入 `data/studio-state.json`；可用环境变量 `STUDIO_DATA` 修改路径。`POST /api/reset` 可清空。

---

## 办社选择：默认并行小绷

本实现把题目中的两种办社都保留为可选项，但默认并明确采用 **并行小绷（`mode: "parallel"`）**：

1. **领取时先做全局规划**：服务在现有锁、订单端点、留白、徽记和接缝之间找一条可行完整针路。
2. **签发即锁接缝与针路**：针单包含每色线的有序相邻格、入口、出口、全局/本地坐标、相邻接缝行和签发版本；返回的 `diagram.offline = true`，领绷人可脱离服务照做。
3. **未交回工单的已签发针位不可挪动**：主编若改写该段，服务返回 `409 active_ticket_locked`。
4. **交回只释放未刺部分**：当前“下一格”连同后续未刺针单可交回；已确认针迹成为不可变前缀。换发的新针单从实际落针处接回。
5. **下游已签发则不可倒退**：旧版本迟到交回会得到 `409 version_conflict`；已完成且无未刺余单的工单不能再次交回。下游针单 ID、状态和版本保持不变。
6. **未签发尾段可改**：只要替换内容不碰锁针，且新边界仍相容、订单仍可达成，改样申请就受理并生成新设计版本。

如创建订单时传 `"mode": "relay"`，则会启用接力限制：下一段必须等前段完成或交回后才能领取；未发出的部分仍可修改。

---

## 数据模型

### 坐标

默认使用 `[row, col]`，行从上到下为 `0..rows-1`。也可在创建订单时设置 `"coordOrder": "xy"`，此时数组按 `[x, y]` 解释。推荐直接用对象：

```json
{ "row": 1, "col": 0 }
```

### 样段

样段最多 4 行。颜色可以是 `red`、`blue` 或中文 `"红"`；`红/红色/红线` 会规范化为 `red`。

```json
{
  "id": "first",
  "rows": 2,
  "cols": 2,
  "allowFlip": true,
  "threads": {
    "red": {
      "routes": [
        { "id": "straight", "kind": "straight", "path": [[0,0],[0,1]] },
        { "id": "switch", "kind": "switch", "path": [[0,0],[1,0],[1,1]] }
      ]
    }
  }
}
```

也支持 `colors/lines/paths/cells/stitches/walks` 等常见字段名，以及一段内多色路线的笛卡尔组合或整段 `walks`。

翻面取值：

- `original` / `none` / `"原放"`
- `flip-v` / `vertical` / `"上下翻面"`：`row -> rows-1-row`
- `flip-h` / `horizontal`
- `both` / `180`

翻面会一起变换实际路径、入口和出口。

### 订单

```json
{
  "id": "red-demo",
  "name": "两行红带",
  "mode": "parallel",
  "rows": 2,
  "left":  { "red": { "row": 0, "col": 0 } },
  "right": { "red": { "row": 0 } },
  "forbidden": [{ "row": 0, "col": 2 }],
  "marks": [{ "row": 1, "col": 0, "color": "red" }],
  "segments": []
}
```

校验内容包括：

- 每根线非空、格位在样段内、相邻格必须边相邻；
- 显式入口/出口必须是路径首尾；
- 针迹不能穿过 `forbidden/blanks/留白`；
- 所有 `marks/emblems/徽记` 必须被指定颜色或任意颜色经过；
- 左端入口、右端出口必须符合订单；
- 相邻段同色出口/入口必须在同一行；
- DP 规划会综合接缝行与徽记覆盖，返回完整针路或具体 `violations`。

---

## 主要 HTTP API

所有响应均为 JSON，成功对象含 `ok: true`。错误使用合适的 `4xx/5xx`，并含 `error/code/message/details`。

### 健康、重置、内置现场

```bash
curl http://localhost:3000/api/health
curl -X POST http://localhost:3000/api/reset \
  -H 'content-type: application/json' -d '{}'

# 红带三单：red-order-a、red-site-straight、red-site-switch
curl -X POST http://localhost:3000/api/scenarios/red-band

# 三段直走新样
curl -X POST http://localhost:3000/api/scenarios/three-segments
```

### 订单与换样

```bash
curl -X POST http://localhost:3000/api/orders \
  -H 'content-type: application/json' -d @order.json

curl http://localhost:3000/api/orders/red-site-straight
curl http://localhost:3000/api/orders/red-site-straight/full-path
```

保存主编图样（允许保存不可行草稿，响应中看 `completable`）：

```bash
curl -X PATCH http://localhost:3000/api/orders/ORDER_ID/design \
  -H 'content-type: application/json' \
  -d '{"segmentIndex":1,"straightOnly":true,"note":"尾段只许直走"}'
```

严格提交改样申请；若旧针/接缝/订单不相容，返回 `409 change_request_rejected`，且不会应用：

```bash
curl -X POST http://localhost:3000/api/orders/ORDER_ID/change-requests \
  -H 'content-type: application/json' \
  -d '{"segmentIndex":1,"straightOnly":true}'
```

整组换入三段：

```bash
curl -X POST http://localhost:3000/api/orders/ORDER_ID/segments/replace \
  -H 'content-type: application/json' \
  -d '{"segments":[...三段...]}'
```

直接还原完整尾段会清掉该段之前的 `straightOnly` 策略：

```bash
curl -X POST http://localhost:3000/api/orders/ORDER_ID/segments/replace \
  -H 'content-type: application/json' \
  -d '{"segmentIndex":1,"segment":{ "...原始尾段..." }}'
```

别名包括 `/api/orders/:id/revisions`、`/pattern`、`/samples`、`/changes`；资源前缀也可用 `/api/designs`、`/api/bands`、`/api/projects`。

### 领取针单

```bash
curl -X POST http://localhost:3000/api/orders/ORDER_ID/claim \
  -H 'content-type: application/json' \
  -d '{
    "segmentIndex": 0,
    "workerId": "hoop-1",
    "terminalId": "desk-A",
    "routeId": "first-straight"
  }'
```

可传 `routeId/walkId/kind/allowedRouteIds/allowedKinds/orientation/flip` 来限定“只许直走”或“只许换行”。例如：

```json
{ "segmentIndex": 0, "kind": "straight" }
```

返回的 `ticket` 同时以 `workOrder`、`needleSheet` 别名出现，顶层也展开 `id/version/nextCell` 等字段。

### 逐格报工与断线重连

```bash
curl http://localhost:3000/api/tickets/TICKET_ID

curl -X POST http://localhost:3000/api/tickets/TICKET_ID/report \
  -H 'content-type: application/json' \
  -d '{"expectedVersion":1}'
```

省略 `cell` 时确认 `nextCell`；也可显式传：

```json
{ "expectedVersion": 2, "cell": {"row":0,"col":1}, "color":"red" }
```

支持 `count/stitchedCount` 报告“已经刺到第 N 格”，服务会顺序补齐中间落针，不允许倒退或跳到未来格。重复确认同一已刺格且不带过期版本时按幂等重复返回。

### 交回当前格与未刺余单

```bash
curl -X POST http://localhost:3000/api/tickets/TICKET_ID/return \
  -H 'content-type: application/json' \
  -d '{
    "expectedVersion": 1,
    "cell": {"row":0,"col":1},
    "reason": "主编改尾样"
  }'
```

若该格已在 `stitched` 中，返回 `409 already_stitched`。交回成功后工单状态为 `returned`；随后对同一 `segmentIndex` 再调用 `/claim`，会把旧单标记为 `superseded`、保留落针并换发新单。

### 双终端并发规则

两台终端从完全相同的 `expectedVersion` 发送：

- `POST /report`
- `POST /return`

Node 服务对版本检查和状态推进是同步原子的。第一个请求把 `version` 从 `N` 推进到 `N+1`，第二个得到：

```text
409 version_conflict
details.currentVersion = N+1
```

因此按服务实际受理顺序，至多一个动作生效。

### 查询完整针路和签发版本

```bash
curl http://localhost:3000/api/orders/ORDER_ID/full-path
```

返回：

- `fullPath` / `completeNeedlePath`：规划中的每色完整全局路径；
- `needlePath`：主色路径；
- `actualPath` / `actualPathByColor`：已确认落针；
- `seams`：接缝行及相容状态；
- `issuedVersions`：每段当前设计版本、签发单号、签发时的样段/设计版本和 ticket ID。

---

## 题目关键红带是如何落地的

内置 `POST /api/scenarios/red-band` 建立：

- 首段：
  - 直走：`(0,0) → (0,1)`
  - 换行：`(0,0) → (1,0) → (1,1)`
- 尾段：
  - 上行直走、下行直走、上换下行、下换上。
- 左端要求第 0 行，右端要求第 0 行。

于是：

- 零次换行：首段上直走 + 尾段上直走，可成；
- 两次换行：首段上→下 + 尾段下→上，可成；
- 全带只换行一次：右端会落在第 1 行，不可成。

`red-order-a` 的徽记在 `(1,0)`，只有首段换行路线经过；首段由脚本真实逐格报工后，再把尾段改为 `straightOnly`，规划得到 `seam_row_mismatch`，所以 `completable=false`。

`red-site-straight` 与 `red-site-switch` 的徽记都在两路线共经的首格 `(0,0)`。两单分别用 `routeId: first-straight` 和 `routeId: first-switch` 签出初版领工，没有写入任何伪造落针。随后提交同一份：

```json
{ "segmentIndex": 1, "straightOnly": true, "allowedKinds": ["straight"] }
```

直走现场可受理；换行现场因接缝锁定在第 1 行、直走尾段不能回到第 0 行而被拒绝。

---

## 目录

```text
server.js              零依赖 HTTP 服务、静态网站和兼容别名
src/model.js           坐标、样段规范化、翻面、路线与全局规划
src/studio.js          订单、工单、版本、报工、交回、持久化
public/                原生网页编作台
scripts/verify.js      npm test 端到端验题脚本
data/                  运行后生成的 JSON 状态
```
