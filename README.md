# 发廊预约与拆卷调度服务

一个零外部依赖、可从空目录启动的 Node.js 服务。它把“预约、签到、分区卷发、卷齐后的等待窗、短活插单、师傅交接、椅位、替班、结账和重启核对”放在同一份持久化客单里处理。

## 先回答柜台问题

题目中的最小案例：

- 第一位客人最后一区在第 `0` 分实际完工；
- 拆卷开始窗为第 `[3, 4]` 分，拆卷占师傅 `2` 分钟；
- 刘海客只能在第 `[0, 3]` 分接受服务，剪刘海占师傅 `3` 分钟；
- 第 `2` 张椅在 `[0, 3)` 可用。

系统给出的可执行安排是：

```text
0──────3 分：林师傅在 chair2 剪刘海
3──────5 分：林师傅回到 chair1 拆卷（选择最早开始 3）
```

所以：**原始承诺可以同时成交。**

两种变异都会被系统拒绝：

1. 实际卷齐提前两分：拆卷窗变成 `[1, 2]`，林师傅最晚第 `2` 分必须回来；三分钟刘海 `[0, 3)` 会占住第 `1、2` 分，不能再承诺。
2. 工序回条恢复为卷齐第 `0` 分，但刘海椅到第 `2` 分才空：刘海客只有 `[0, 3]`，三分钟服务必须第 `0` 分开始，椅位不成立，不能再承诺。

以上判断由 `test/salon.test.js` 固定，`npm run demo` 会打印完整结果。

## 运行

要求 Node.js 20+，不需要安装依赖。

```bash
npm start
# 默认 http://localhost:3000
# 数据默认写入 ./data/salon.json
```

柜台/员工网页入口：<http://localhost:3000/>

指定数据文件和端口：

```bash
PORT=3210 SALON_DATA_FILE=/tmp/salon.json npm start
```

从完全空的数据状态开始：

```bash
rm -rf data
npm start
```

运行测试：

```bash
npm test
```

运行题目场景演示：

```bash
npm run demo
```

## 两种班表

### 1. 主理人班表：`mode: "principal"`

- 预约必须有 `principalStylistId`。
- 客人签一个名字：`signature`。
- 预约写入时就生成一个受保护的“回这位主理人拆卷”的窗口约束。
- 系统不会把主理人在整个烫卷项目期间粗暴封死；它只保护：
  - 实际或计划卷齐时间；
  - 最短/最长等待；
  - 拆卷开始窗；
  - 拆卷持续时间；
  - 必需椅位。
- 窗口前或窗口内仍可插入经过可行性求解的短活。

如果临时换其他师傅拆卷，必须额外满足：

- 替代师傅有拆卷资格；
- 本人 `confirmState: true`；
- 确认接收当前所有已完成分区及其状态哈希；
- 能在窗口内留出可用椅位；
- 客人明确同意替班并留下同意签名。

### 2. 工序班表：`mode: "process"`

- 预约时不锁定唯一拆卷人。
- 拆卷师傅在交接时选择。
- 接手请求必须包含：
  - `stylistId`；
  - `confirmState: true`；
  - `acceptCompletedSections: true` 或显式 `acceptedSectionIds`；
  - `chairId`。
- 服务端为当前分区内容生成 `processStateHash`。
- 若交接后分区时间、完工状态被补录或修改，哈希失效；必须由新师傅重新确认，才能开始拆卷。

## 插单承诺策略

系统不保存会过期的“口头报价占位”。

- `POST /api/quotes/insert` 只计算可行性，不占资源。
- 报价会枚举整数分钟开始点和可用椅位，并联合求解所有已保护的拆卷窗。
- `POST /api/insertions` 在写入时重新执行同一套检查；只有能原子写单才作出承诺。
- 返回的 `promise.latestFeasibleStart` 是这张短单仍可承诺的最晚整数开始分钟。
- 一旦确认，短单成为固定师傅/椅位区间；后续预约和插单都必须绕开它，而不是移动已承诺客单。

时间区间采用整数分钟、左闭右开：`[0, 3)` 在第 `3` 分结束，因此可以紧接 `[3, 5)`。

## 迟到后的救单顺序

`POST /api/schedule/recover` 按以下顺序决定先救哪份仍能兑现的预约：

1. 已经超过最晚拆卷开始时间的单不再伪装成可救，标记为 `window_closed`。
2. 对仍在窗内的单，按 **最晚可开始拆卷时间 `latestStart` 升序**，即先救截止时间最硬、最不可拖延的单。
3. 截止时间相同，优先主理人保护客。
4. 仍相同，按更早的预约顺序。
5. 先保证高优先级单可与既有固定任务共存，再尝试下一优先级；不会为了后到的单破坏更早的承诺。
6. 如果主理人已无法兑现，但存在有资格的替代师傅，返回 `needs_substitute_acceptance` 和替代方案；替班仍需师傅本人确认分区状态、椅位以及主理人班表下的客人同意。

## 持久化与重启核对

每次写操作都同步写入 JSON 文件，并使用临时文件 + `rename` 原子替换。

重启后：

```bash
curl http://localhost:3000/api/appointments/perm/receipt
```

回条中的 `segments` 会区分：

- `realized`：已经实际发生并落账的段，如签到、某区完工、卷齐、交接、实际拆卷、结账；
- `protected`：尚未发生但预约时必须保住的拆卷窗；
- `pending_acceptance`：工序班表等待师傅接手；
- `planned` / `pending` / `missing`。

`processStateHash` 用来核对“师傅接手的分区回条”和当前实际分区状态是否一致。

## 最小 API 走查

### 录入员工和椅位

```bash
curl -X POST http://localhost:3000/api/staff \
  -H 'content-type: application/json' \
  -d '{"id":"lin","name":"林师傅","canUnroll":true}'

curl -X POST http://localhost:3000/api/chairs \
  -H 'content-type: application/json' \
  -d '{"id":"chair1","name":"烫卷椅"}'

curl -X POST http://localhost:3000/api/chairs \
  -H 'content-type: application/json' \
  -d '{"id":"chair2","name":"刘海椅"}'
```

员工资格由店里录入：`canUnroll: true` 才会进入可拆卷/可替班候选；未录入时默认不能拆卷。

员工挂时段/请假：

```bash
curl -X POST http://localhost:3000/api/staff/lin/schedule \
  -H 'content-type: application/json' \
  -d '{"timeOff":[{"start":3,"end":5,"reason":"临时离开"}]}'
```

椅位封闭：

```bash
curl -X PATCH http://localhost:3000/api/chairs/chair2 \
  -H 'content-type: application/json' \
  -d '{"unavailable":[{"start":0,"end":2,"reason":"前客第2分才走"}]}'
```

### 主理人预约

```bash
curl -X POST http://localhost:3000/api/appointments \
  -H 'content-type: application/json' \
  -d '{
    "id":"perm",
    "customerName":"第一位客人",
    "signature":"第一位客人",
    "mode":"principal",
    "principalStylistId":"lin",
    "sections":[{"id":"last","name":"最后一区"}],
    "plannedRollCompleteAt":0,
    "unroll":{"durationMinutes":2,"minWaitMinutes":3,"maxWaitMinutes":4}
  }'
```

### 签到和逐区完工

```bash
curl -X POST http://localhost:3000/api/appointments/perm/check-in \
  -H 'content-type: application/json' \
  -d '{"at":0,"chairId":"chair1"}'

curl -X POST http://localhost:3000/api/appointments/perm/sections/last/complete \
  -H 'content-type: application/json' \
  -d '{"at":0,"stylistId":"lin"}'
```

### 插单报价与确认

```bash
curl -X POST http://localhost:3000/api/quotes/insert \
  -H 'content-type: application/json' \
  -d '{
    "stylistId":"lin",
    "durationMinutes":3,
    "availableFrom":0,
    "availableUntil":3,
    "chairId":"chair2"
  }'

curl -X POST http://localhost:3000/api/insertions \
  -H 'content-type: application/json' \
  -d '{
    "customerName":"刘海客",
    "stylistId":"lin",
    "durationMinutes":3,
    "availableFrom":0,
    "availableUntil":3,
    "chairId":"chair2",
    "targetAppointmentId":"perm"
  }'
```

### 工序/替班交接

```bash
curl -X POST http://localhost:3000/api/appointments/perm/handoffs \
  -H 'content-type: application/json' \
  -d '{
    "at":3,
    "stylistId":"zhou",
    "confirmState":true,
    "acceptCompletedSections":true,
    "chairId":"chair1",
    "customerConsent":true,
    "consentSignature":"第一位客人"
  }'
```

工序班表省略 `customerConsent` / `consentSignature`；主理人班表的临时替班需要这两项。

### 拆卷、完成、结账

```bash
curl -X POST http://localhost:3000/api/appointments/perm/unroll/start \
  -H 'content-type: application/json' \
  -d '{"at":3,"stylistId":"zhou"}'

curl -X POST http://localhost:3000/api/appointments/perm/unroll/complete \
  -H 'content-type: application/json' \
  -d '{"at":5,"stylistId":"zhou"}'

curl -X POST http://localhost:3000/api/appointments/perm/checkout \
  -H 'content-type: application/json' \
  -d '{"at":6,"amount":680,"method":"card"}'
```

## 椅位与移动

- 签到后写入实际椅位 episode。
- 未获移动确认时，拆卷必须使用当前椅位。
- 员工可通过 `/mobility` 记录“当前分区状态下客人是否可移动”。
- `/movable-chairs?at=...` 列出仍能容纳剩余操作的目标椅。
- `/move` 会关闭旧椅 episode 并打开新 episode；旧椅从移动分钟起释放，不会把客人同时算进两张椅。

## 主要接口

| 方法与路径 | 用途 |
| --- | --- |
| `POST /api/staff` / `POST /api/chairs` / `POST /api/services` | 录入店里的员工、椅位、服务约束 |
| `POST /api/staff/:id/schedule`、`PATCH /api/chairs/:id` | 挂班次/请假/封闭椅位 |
| `POST /api/appointments` | 主理人或工序预约，预约时做可行性校验 |
| `POST /api/appointments/:id/check-in` | 签到并占椅 |
| `POST /api/appointments/:id/sections/:sectionId/complete` | 逐区完工，推导实际卷齐时间和拆卷窗 |
| `POST /api/appointments/:id/mobility`、`POST /api/appointments/:id/move` | 移动许可与换椅 |
| `POST /api/appointments/:id/handoffs` | 师傅本人确认分区状态、接收范围和椅位 |
| `POST /api/quotes/insert`、`POST /api/insertions` | 插单报价、原子确认 |
| `POST /api/schedule/recover` | 迟到/扰动后的救单顺序和替班方案 |
| `POST /api/appointments/:id/unroll/start|complete` | 窗内拆卷及完工 |
| `POST /api/appointments/:id/checkout` | 结束结账并释放椅位 |
| `GET /api/appointments/:id/receipt` | 按客单核对已做实段和当前状态 |
| `GET /api/state` | 读取整份持久化状态 |

## 代码结构

```text
src/engine.js    纯调度内核：区间冲突、窗口推导、整数分钟回溯求解、恢复排序
src/service.js   客单事务、约束校验、JSON 原子持久化、回条
src/server.js    零依赖 HTTP 服务和静态柜台页面
public/          柜台网页与 API 控制台
scripts/demo.js  题目案例、两种变异、替班和重启持久化演示
test/            领域规则与真实 HTTP 重启测试
```
