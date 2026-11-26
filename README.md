# 联合掩星值守网络服务

一个零外部依赖的 Node.js 服务，模拟天文社多站点联合掩星值守。所有“天象”都是输入的自造半开区间秒数 `[start,end)`，不连接望远镜或任何真实仪器。

服务同时服务三类角色：

- **站点**：登记两次对时、整数偏差、本地拍摄区间、丢帧空段、背景标定、失联/在线和重复交帧。
- **活动主持人**：创建委托，按当前实到记录评估/结案，处理确认窗口、竞争派班、接力指挥和补看。
- **读片员**：从委托号追踪共同秒段、逐站背景、实到帧、派班/接力回执和封交原版报告。

## 运行与测试

```bash
npm start
# 默认 http://localhost:3000
# PORT=3100 npm start

npm test
```

- 值守控制台：<http://localhost:3000/>
- 读片员入口：<http://localhost:3000/reader.html?commissionId=C-DONE>
- API 目录：<http://localhost:3000/api>
- 健康检查：<http://localhost:3000/health>

服务默认播种完整剧本。可用下面的接口恢复初始剧本：

```bash
curl -X POST http://localhost:3000/api/admin/reset \
  -H 'content-type: application/json' \
  -d '{"seed":true}'
```

## 题目数字剧本

内置开放委托 `C-LAB` 正好对应可反复操作的验算台：

- 天象区间：`[4,8)`
- 要求：3 秒双站共视
- S1 合格目标曝光：`[4,8)`
- S2 合格目标曝光：`[5,8)`
- S1 背景：`[3,4)`
- S2 背景：`[4,5)`
- 两站偏差均为 `0`，各有两次以上对时/约定校准锚点

### 1. 初始：恰好 3 秒

```bash
curl -s -X POST http://localhost:3000/api/commissions/C-LAB/evaluate \
  -H 'content-type: application/json' -d '{}'
```

结果包含：

```json
{
  "commonIntervals": [{ "start": 5, "end": 8 }],
  "commonSeconds": 3,
  "backgroundSupportedSeconds": 3,
  "qualified": true
}
```

### 2. 只修正第二站对时：曝光变 `[6,9)`，交回 2 秒

```bash
curl -X PUT http://localhost:3000/api/stations/S2/clock \
  -H 'content-type: application/json' \
  -d '{"agreedOffset":1,"reason":"correct second sync"}'

curl -X POST http://localhost:3000/api/commissions/C-LAB/evaluate \
  -H 'content-type: application/json' -d '{}'
```

S2 的 nominal common interval 变为 `[6,9)`，但与 S1 `[4,8)` 及天象段相交后只剩 `[6,8)`：

```json
{ "commonIntervals": [{ "start": 6, "end": 8 }], "commonSeconds": 2 }
```

背景区间也随同一时钟平移：S2 背景从 `[4,5)` 变为 `[5,6)`，仍紧邻修正后的曝光起点。

### 3. 恢复时钟

```bash
curl -X PUT http://localhost:3000/api/stations/S2/clock \
  -H 'content-type: application/json' \
  -d '{"agreedOffset":0}'
```

重新评估会再次得到 3 秒。服务不保存“上次合格”的结论，每次都从当前对时和实到帧重算。

### 4. 单独把要求改成 4 秒

```bash
curl -X PATCH http://localhost:3000/api/commissions/C-LAB \
  -H 'content-type: application/json' \
  -d '{"requiredCommonSeconds":4}'

curl -X POST http://localhost:3000/api/commissions/C-LAB/evaluate \
  -H 'content-type: application/json' -d '{}'
```

结果仍是实际共同秒 3，但 `requiredCommonSeconds` 为 4，因此：

```json
{ "commonSeconds": 3, "requiredCommonSeconds": 4, "qualified": false }
```

如果委托已有封交报告，修改条件会留下 amendment；旧报告快照和校验值不变，新条件必须重新评估并可形成新版本报告。

## 读片员追踪

打开：

```text
http://localhost:3000/reader.html?commissionId=C-DONE
```

或请求机器可读追踪：

```bash
curl http://localhost:3000/api/commissions/C-DONE/trace
```

返回内容包括：

- `commission`：委托条件和站点组合；
- `commonSegments`：每个共同秒段及逐站背景判定；
- `stationEvidence`：本地区间、公共秒映射、丢帧挖空后的可用段、背景帧；
- `backgrounds` / 背景检查；
- `dispatchReceipts`：确认窗口或接力指挥回执；
- `arrivals`：实到/收帧记录；
- `reports`：封交原版报告全文与 SHA-256 校验；
- `makeupPlan`：不足时可派谁补看的计划窗口。

也支持友好路径 `/reader/C-DONE`。

## 错开的两份三秒观测

内置 `C-STAGGER`：

- S1：`[31,34)`，3 秒；
- S2：`[36,39)`，3 秒；
- 两站背景均紧邻各自曝光；
- 交集为空。

评估中：

```json
{
  "individualTotalSeconds": 6,
  "commonSeconds": 0,
  "additiveSecondsRejected": true,
  "qualified": false
}
```

主持人结案结果为 `pending_makeup`。各站自己的曝光秒数不会相加凑成联测。

## 播种样例

| ID | 用途 |
| --- | --- |
| `C-DONE` / `RPT-C-DONE-V1` | 已凭实到帧封交的双站 3 秒实测，带窗口派班回执 |
| `C-LAB` | 可反复做对时修正、恢复、改 4 秒要求的开放实验委托 |
| `C-MAKEUP` | S3 失联且无目标帧，主持人判定 `pending_makeup` |
| `C-STAGGER` | 两份各 3 秒但彼此错开，共同秒为 0 |
| `C-FOUR` / `RPT-C-FOUR-V1` | 四站活动；S4 重复交同一帧，原版报告仍可读 |
| `C-RACE-A/B`、`W-RACE-50-54` | 两个竞争委托抢同一已确认窗口 |
| `C-RELAY` / `D-RELAY-DEMO` | 接力指挥台在线预报、延迟、回执和转向时间样例 |

## 时间、对时、丢帧和背景规则

### 半开区间

所有区间按 `[start,end)` 处理，秒数为 `end-start`。例如 `[5,8)` 长度为 3。

### 本地钟到公共秒

站点时钟保存：

```text
commonSecond = localSecond + agreedOffset
```

- `agreedOffset` 必须是整数；
- 每个站点保存 `syncs[]`，校准要求至少两次对时/约定锚点；
- `PUT /api/stations/:id/clock` 可修正整数偏差并保存 correction；
- `POST /api/stations/:id/sync` 可追加真实对时；传 `"adopt":true` 时采用该次对时推算出的整数偏差。

请求也可通过 body 的 `logicalClock` 或 `x-logical-clock` 请求头携带远端 Lamport 钟。

### 丢帧挖空

交帧示例：

```json
{
  "stationId": "S1",
  "mode": "target",
  "localInterval": [4, 8],
  "droppedIntervals": [[5, 6]]
}
```

真实可用段会变成：

```json
[{ "start": 4, "end": 5 }, { "start": 6, "end": 8 }]
```

共同秒只取挖空后的可用段交集。

### 背景距离

委托可指定：

```json
{ "background": { "before": 10, "after": 20 } }
```

也支持 `maxDistance` / `radius` 同时限制前后。背景在曝光前时，距离为 `exposure.start - background.end`；在曝光后时，距离为 `background.start - exposure.end`。重叠背景不能冒充前后标定。

## 派班承诺

### 已确认站对窗口

1. `POST /api/windows` 可预划指定两站窗口。
2. 窗口必须经 `POST /api/windows/:id/confirm` 变成 `confirmed`。
3. `POST /api/dispatch/window` 只接受已确认窗口。
4. 同一站、时间重叠的 `held/active/at_risk` 时段不能重复派发；竞争委托中恰好一个成功，另一个得到 `409 SLOT_CONFLICT`。
5. 回执带 `nonTransferable:true`。没有直接转让接口；`POST /api/dispatches/:id/transfer` 返回 `NON_TRANSFERABLE_DISPATCH`。必须先取消原派班并留审计，再按窗口/接力规则重派。

### 接力指挥台

`POST /api/relay/plan` 只根据调用方当场提供的在线预报计算，不读取后续实际轨迹：

```json
{
  "forecast": { "sentAt": 10, "predictedInterval": [14, 20] },
  "notificationDelaySeconds": 1,
  "receiptDelaySeconds": 1,
  "stationId": "S2",
  "slewSeconds": 2,
  "availableInterval": [10, 30]
}
```

计算为：

```text
orderReceivedAt       = forecast.sentAt + notificationDelay
stationReadyAt        = orderReceivedAt + slewSeconds
commanderReceiptAt    = orderReceivedAt + receiptDelay
catchableInterval     = predictedInterval ∩ availableInterval ∩ [readyAt, ∞)
```

`POST /api/relay/dispatch` 在能赶上时生成不可转让回执；赶不上时返回 `NO_TIMELY_CANDIDATE`。

### 委托竞争和一站失联时守住什么

- **不重复占用**：Node 服务内的派班检查和写入在同一事件循环事务段中完成；HTTP 并发测试验证两个竞争请求恰好一个 `201`、一个 `409`。
- **不以计划冒充实测**：窗口、派班回执和补看计划只进入 trace，不进入 `commonSeconds`。
- **失联不删证据**：站点标记 lost 后，已收到的帧和已封交报告保留。
- **失联派班标风险**：相关 held/active 回执变为 `at_risk` 并生成 incident；在主持人取消前，该时段仍阻止别的委托抢占，避免“失联后被系统悄悄转让”。
- **结案只认真实记录**：失联且无实到帧的站会进入 `missingStations`，委托为 `pending_makeup`。
- **封交报告不可变**：重复同号帧按幂等重复交帧处理并增加 `deliveries`，但不改写原报告快照和 checksum；后续新条件/新证据只能形成新评估或新版本。

## 主要 HTTP 接口

| 方法与路径 | 说明 |
| --- | --- |
| `GET /health` | 健康检查和当前逻辑钟/实体计数 |
| `POST /api/admin/reset` | 重置内存数据，可重新播种 |
| `GET /api/events` | Lamport 逻辑钟事件流 |
| `GET /api/incidents` | 失联等值守事件及受影响派班 |
| `GET/POST /api/stations` | 站点列表/注册 |
| `GET/PATCH /api/stations/:id` | 站点详情/更新 |
| `POST /api/stations/:id/sync` | 追加对时，可同时采用整数偏差 |
| `PUT/POST /api/stations/:id/clock` | 修正整数偏差或批量设置对时 |
| `POST /api/stations/:id/lost` · `/online` | 失联/恢复 |
| `POST /api/stations/:id/frames` | 交目标帧或背景帧；同帧号重复提交幂等计数 |
| `GET /api/frames` | 实到帧列表 |
| `POST/GET /api/arrivals` | 实到/签到记录 |
| `GET/POST /api/commissions` | 委托列表/新建 |
| `GET/PATCH /api/commissions/:id` | 详情/修改条件 |
| `POST /api/commissions/:id/evaluate` | 总是重新计算共同秒和背景 |
| `POST /api/commissions/:id/close` | 主持人结案：成功封交或待补 |
| `GET /api/commissions/:id/trace` | 读片员追踪 JSON |
| `GET /reader.html?commissionId=:id` | 读片员页面 |
| `GET/POST /api/windows` | 站对窗口 |
| `POST /api/windows/:id/confirm` | 确认窗口 |
| `POST /api/dispatch/window` | 凭确认窗口派班 |
| `POST /api/relay/plan` · `/api/relay/dispatch` | 接力试算/出回执 |
| `GET /api/dispatches` | 派班回执 |
| `GET /api/reports` · `GET /api/reports/:id` | 报告列表/原版全文 |

重复交帧可使用相同 `id`/`frameId`，也可传 `Idempotency-Key` 请求头。

## 代码结构

```text
src/time.js       半开区间、交集、丢帧挖空、背景距离
src/domain.js     逻辑钟、站点、委托、评估、派班、封交报告和播种剧本
src/server.js     零依赖 Node HTTP 服务与静态文件
public/           值守控制台和读片员页面
test/             node:test 领域与 HTTP 测试
```
