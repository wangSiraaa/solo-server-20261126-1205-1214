# 儿童自然拼读 · 离线复习室服务

一个零外部依赖的 Node.js HTTP + JSON 持久化服务，用来处理离线练习回执、学习回合身份、提示污染、每日上限、关卡机会和补练。

## 先回答：昨天那张卡算两次，还是同一次？

判据不是“交回了几个请求”，而是**是否属于同一个 canonical learning round（正式学习回合）**：

- 同一个 `roundId`、同一份 `worksheetId` 的重印件、两台设备、重复网络投递，都合并为**一个回合**；相同事实的新副本返回 `duplicate: true`，不会增加熟练度。
- 第 1 天和第 4 天是两个不同、服务端已登记的回合，并且都有“最终读音正确 + 该知识点在答对前未使用提示”的证据，才贡献**两个不同回合**。
- 同一回合内，只要某个知识点的提示在正确回忆前被揭开，之后换另一张考同一知识点的卡，也不算该回合独立回忆。
- 上一回合用过提示不会永久污染；下一个新回合仍可独立答对。

可通过下面的接口查看开发者解释：

```http
GET /api/rounds/:roundId/evidence
```

它会列出设备副本、每张卡的首次猜测、最终短字符串发音、提示时序、是否独立、冲突项以及是否生成补练。

## 运行

```bash
npm start
# 默认 http://127.0.0.1:3000
# 数据默认写入 ./data/phonics-review-room.json，原子 rename 保存
```

环境变量：

- `PORT`：监听端口，默认 `3000`
- `HOST`：默认 `0.0.0.0`
- `DATA_FILE`：JSON 数据文件；设为 `:memory:` 时仅内存运行

一键演示完整叙事，包括真实重启子进程、旧设备迟到副本、冲突补练、第 8 天关卡和重启后 worksheet 对账：

```bash
npm run demo
```

运行测试：

```bash
npm test
```

## 核心领域规则

### 1. 字形不等于知识点

同一字形在不同单元/课次可以考不同音。知识点绑定在老师布置的 `assignment`（某单元内的一张卡）上，而不是只绑定 `cardId` 或字形。

例如演示数据中：

- 字形 `a` 在 Unit 1 对应 `/æ/`、`kp-a`
- 同一张字形卡 `card-glyph-a` 在 Unit 2 对应 `/eɪ/`、`kp-long-a`

封闭卡包发出时会把 `items`、期望发音、提示和 `kpIds` 快照进 pack/round。之后老师修改映射，或学员临时加入别的卡，都不能改写原卡包的对账口径。

### 2. 什么证据足以贡献一个回合

服务用短字符串模拟发音，并按快照中的 `expected` 校验：

```js
independentCorrect =
  finalAnswer 与 expected 相同 &&
  在该知识点答对之前没有揭开相关提示
```

`firstGuess` 会被标准化保存，并记录 `firstGuessCorrect`，用于解释和设备副本冲突比对。

同一回合的知识点状态包括：

| state | 含义 | 本回合计数 |
| --- | --- | --- |
| `independent` | 有正确且未被提示污染的回忆 | 一个候选回合 |
| `hint_assisted` | 答对，但提示已在正确答案前揭开/污染同一 KP | 否 |
| `incorrect` | 没有正确发音 | 否 |
| `conflicted` | 两台设备对同一回合的关键事实给出不一致回执 | 否，生成补练 |
| `not_answered` | 未练习该 KP | 否 |

跨卡污染按回执中答案和提示的顺序判断：第 1 张卡先揭开某 KP 的提示，第 2 张同 KP 卡即使没有再揭提示，也会标成 `hintBeforeAnswer: true`。

### 3. 两台设备与冲突

- 同一 `studentId + packId` 的每日封闭包领取会返回同一个 canonical round。
- 只提供 `worksheetId` 时，服务也会按 `studentId + worksheetId` 找到该回合。
- 相同答案事实的重复副本只保存/识别为 duplicate，不产生第二个复习回合。
- 如果不同设备对同一卡的 `correct`、最终发音、提示使用/顺序、首次猜测等事实给出不一致证据，该 KP 标为 `conflicted`：
  - 此冲突 KP 本回合不计数；
  - 自动产生 targeted makeup order；
  - 其它一致且独立的 KP/回合不受影响；
  - 第 1 天等正常的独立回合仍保留，不会因为个别冲突把所有证据打回。

老师也可以选择可信副本裁决：

```http
POST /api/teacher/rounds/:roundId/adjudicate
Content-Type: application/json

{ "submissionId": "sub_xxx", "note": "以教室设备日志为准" }
```

或要求补练：

```json
{ "decision": "repair", "kpIds": ["kp-a"], "day": 5 }
```

### 4. 每日上限

证据账本按 `studentId + kpId + day` 分组。每组最多选择一个独立回合进入熟练度：

```json
{
  "countsTowardMastery": false,
  "reason": "daily_max_one_round"
}
```

因此老师要求“两个不同回合”且“每日最多一个回合”时，实际需要两个可贡献日，例如 `[1, 4]`。

### 5. 两种教室组织方式

主叙事选择**按日封闭卡包**：

1. 老师创建 day pack，发包时快照可积累 KP 和 worksheet。
2. 学生领包，离线练习。
3. 之后按原 `roundId` 或 `worksheetId` 延迟交回。
4. 临时加入但不在原包快照中的卡会标为 `card_not_in_original_round` / extra card，不能积累 KP。
5. 补练是服务端新发出的 adaptive/makeup round，不是离线自编回合。

服务也实现了**逐回合自适应约练**：

- `POST /api/rounds/claim` 传 `mode: "adaptive"`。
- 上一回合仍为 `issued` 时，不允许领下一回合，返回 `409 previous_round_outstanding` 和原回合。
- 上一回合已确认后，服务才根据错误/依赖提示的 KP 选择另一张同 KP 卡。
- 服务未发出的 `roundId` 或 worksheet 在交回时返回 `409 unknown_round_or_worksheet`，客户端不能断线后自编新回合。

## 主要 HTTP 接口

### 老师布课

```http
POST /api/teacher/kps
POST /api/teacher/cards
POST /api/teacher/units
POST /api/teacher/assignments
POST /api/teacher/gates
POST /api/teacher/packs
PATCH /api/teacher/gates/:gateId
POST /api/teacher/clock
```

一个完整最小布课：

```bash
curl -sS -X POST localhost:3000/api/teacher/kps \
  -H 'content-type: application/json' \
  -d '{"id":"kp-a","name":"short a"}'

curl -sS -X POST localhost:3000/api/teacher/cards \
  -H 'content-type: application/json' \
  -d '{"id":"card-a1","glyph":"a"}'

curl -sS -X POST localhost:3000/api/teacher/units \
  -H 'content-type: application/json' \
  -d '{"id":"u1","name":"Unit 1"}'

curl -sS -X POST localhost:3000/api/teacher/assignments \
  -H 'content-type: application/json' \
  -d '{
    "id":"as-a1",
    "unitId":"u1",
    "cardId":"card-a1",
    "kpId":"kp-a",
    "expected":"æ",
    "hints":["hear /æ/"]
  }'

curl -sS -X POST localhost:3000/api/teacher/gates \
  -H 'content-type: application/json' \
  -d '{
    "id":"gate-app",
    "opensOnDay":8,
    "requirements":[{"kpId":"kp-a","independentRounds":2}]
  }'

curl -sS -X POST localhost:3000/api/teacher/packs \
  -H 'content-type: application/json' \
  -d '{
    "id":"pack-d1",
    "day":1,
    "unitId":"u1",
    "assignmentIds":["as-a1"],
    "worksheetId":"ws-d1"
  }'
```

### 学生和可练日

```http
POST /api/students
GET  /api/students/:id/progress
GET  /api/students/:id/opportunities?day=8&gateId=gate-app
POST /api/students/:id/practice-days
```

学生可带 `practiceDays: [1,2,3,4,5,6,7,8]`。机会接口返回：

- `practiceDays`：学员全部可练日；
- 每个 KP 的 `earnedDays`、`stillNeeds`；
- `contributableDays`：尚未获得该 KP 独立证据的可练日；
- `nextPracticeDay`：不早于查询日的下一个可贡献日；
- `makeupOrders` 和建议的补练请求体。

### 领练、交回和补练

```http
POST /api/rounds/claim
POST /api/receipts
POST /api/makeup-orders
GET  /api/rounds/:id/evidence
GET  /api/worksheets/:worksheetId
GET  /api/gates/:gateId/access?studentId=alice&day=8
```

领取每日包：

```bash
curl -sS -X POST localhost:3000/api/rounds/claim \
  -H 'content-type: application/json' \
  -d '{"studentId":"alice","packId":"pack-d1","deviceId":"device-home"}'
```

延迟交回：

```bash
curl -sS -X POST localhost:3000/api/receipts \
  -H 'content-type: application/json' \
  -d '{
    "studentId":"alice",
    "roundId":"r_alice_pack-d1",
    "deviceId":"device-home",
    "returnedAt":"2026-11-02T19:00:00Z",
    "answers":[{
      "assignmentId":"as-a1",
      "cardId":"card-a1",
      "firstGuess":"æ",
      "finalAnswer":"æ",
      "revealedHints":[]
    }]
  }'
```

如果是先揭提示再答：

```json
{
  "assignmentId": "as-a2",
  "cardId": "card-a2",
  "firstGuess": "uh",
  "finalAnswer": "æ",
  "revealedHints": ["hear /æ/ again"]
}
```

需要跨卡污染时，按练习顺序放两张同 KP 卡，并把提示放在前一张：

```json
{
  "answers": [
    {
      "assignmentId": "as-a1",
      "cardId": "card-a1",
      "firstGuess": "?",
      "finalAnswer": "æ",
      "revealedHints": ["hear /æ/"]
    },
    {
      "assignmentId": "as-a2",
      "cardId": "card-a2",
      "firstGuess": "æ",
      "finalAnswer": "æ",
      "revealedHints": []
    }
  ]
}
```

服务支持 `Idempotency-Key` 请求头或 body 中的 `requestId`，用于网络重试。

## 三个验收场景

`npm run demo` 和 `npm test` 覆盖以下结论：

1. **基准**：Alice 第 1、4 天均无提示独立答对；第 4 天查询仍是 `scheduled_locked`，第 8 天查询为 `open: true`，`earnedDays: [1,4]`。
2. **替换第 4 天证据**：Ivy 第 4 天换成同 KP 另一张卡且先看提示，该回合为 `hint_assisted`；第 8 天 `open: false`，KP 只有 `earnedDays: [1]`，机会接口给出下一个可练日和补练请求。
3. **新增未学 KP**：回到 Alice 基准（第 1、4 天两次证据已足够），老师只修改 gate，增加一个 `kp-new` 要求；gate 立即变为 `requirements_locked`，旧两次证据仍保留但不能满足新要求。

另外演示：

- Leo 的 `ws-d4` 回合先在新设备交回；
- 服务真实重启并从 JSON 文件恢复；
- 旧离线设备送来相同回合的新副本；
- 服务返回 `duplicate: true`、`submissionCount: 1`；
- Leo 仍只有第 1、4 两个贡献日，第 8 天 gate 正常打开；
- 重启后领取的下一张练习单仍为持久化的 `ws-d5`，快照 KP 与 gate 对齐。

## 代码结构

```text
server.js              # 根目录启动入口
src/server.js          # HTTP 路由、请求校验、持久化触发
src/domain.js          # 回执分析、冲突对账、证据账本、每日上限、gate/机会、自适应选题
src/store.js           # JSON 文件和原子写入
scripts/demo.mjs       # 启动/杀掉/重启真实服务的端到端演示
test/service.test.js   # 全部核心规则的 API 测试
```
