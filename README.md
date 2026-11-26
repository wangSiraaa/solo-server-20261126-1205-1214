# 街坊舞会后台（Dance Party Back Office）

一个零依赖、事件溯源（event-sourced）的双人舞会后台：领舞/跟舞、按节奏的邀舞许可、
分区同时开舞、手机确认、关轮结算、补舞（绝配/改约）、名额判断、以及按来客号重放的会面记录。

运行时只需要 Node ≥ 18（用 `node:test` 自带测试运行器，无需联网装包）。

```
npm test         # 20 个测试
npm run demo     # 可读的端到端演示
npm run serve    # 启动 HTTP 后台（默认 8080，事件日志 data/events.jsonl）
```

---

## 1. 选择的经营方式：**每轮关轮后再公布下一支**（路线 B）

题目要求两种经营方式只做一种。本实现做的是 **mode B（逐轮 / 动态公布）**：

- 每一轮内：领舞方发起邀约（`proposeInvitation`），被邀方在手机上确认（`confirmInvitation`）。
- `closeRound` 关轮时，依据「两人是否实到、区域是否开放、双方是否仍接受该轮节奏、
  同一人/同一区是否被重复占用」判定哪些确认**真正兑现成一支舞**，其余记为
  `absent / zoneClosed / invalidRhythm / conflict / expired`。
- 关轮后基于「这一轮谁真的跳完、谁同意再跳、谁还在场」再排下一轮（`autoPlanRound` /
  `autoMakeup`）。

这条路线**无法事先向客人报定下一位舞伴**（我们本来就只在关轮后才公布），但可以用
**动态接替/补舞**兑现更多相遇——这正是需求里说的取舍，本项目明确站在这一侧。

> 字段 `source: 'program'`（节目单）依然保留：即使走动态路线，也可以把某些邀约标成
> 「节目单已排」，并在会面记录里单独列出 `scheduled`，与真正 `partners` 区分。

---

## 2. 核心规则：换角色再跳 **不算** 新舞伴

「不同舞伴」按 **对方来客号** 去重，**与角色无关**。

- `metPartners(sessionId, number)` 返回该客人**实际跳完**的、去重后的对方来客号。
- `meetingRecord()` 仍然区分四条线，互不混淆：
  - `partners`：真正遇到的舞伴（含每人每次的 `roleSwapped` / `repeat` 标记）；
  - `scheduled`：只在「节目单」里、还没实际跳的搭档；
  - `noShows`：中途没到的邀约（`absent`，带 `absentBy`）；
  - `blocked`：因节奏不允许 / 区域关闭 / 冲突而未结成的邀约；
  - `pending`：还在等对方确认的邀约。

因此：甲与乙跳完第一支、乙换角色又与甲跳第二支，**甲仍只有乙这 1 位不同舞伴**；
再安排丙并实跳，才变成 2 位。

---

## 3. 关键验收场景与对应实现

| 需求 | 实现 | 测试 |
|---|---|---|
| 换角色不算新舞伴 | `metPartners` 按对方来客号去重；`meetingRecord` 标 `roleSwapped` | `role swap...` |
| 再安排丙实跳达到 2 人 | 同上 | `then arranging 丙...` |
| 甲拒绝丙的节奏 → 不能结作 2 人（保持空场原样） | `setConsent(... accept:false)` + `closeRound` → `invalidRhythm` | `rhythmBlock...` |
| 恢复许可、只关闭唯一能开舞的区 → 仍不能结作 2 人 | `setZone(open:false)` → `zoneClosed` | `zoneBlock...` / 单区版本 |
| 6 人内、3 轮，报名→确认→开舞→关轮→补舞 | `autoPlanRound` / `autoMakeup` / `closeRound` | `happyPath...`, `noShow...` |
| 两领队同时登记同一来客在两区开舞 | 确认时抢占式 `conflict`，`effectiveAssignment` 给出真正生效的一边 | `twoLeads...`（含并发 HTTP 版） |
| 重启后按来客号重放实际认识的人 | JSONL 事件日志 + `#apply` 纯折叠 | `restart...` |
| 导出安排表不能销单 | `exportSchedule` 只读；`redeem` 是独立显式动作 | `happyPath...`, `program-book...` |

---

## 4. 名额判断 / 提示休息 / 征询改约（`quota()`）

「还能兑现多少位不同舞伴」用**紧密上界**，不是粗略计数：

1. 每位客人每轮最多新增 1 位不同舞伴；
2. 在「剩余可跳轮次 × 未遇过且到场、角色相容、双方接受该轮节奏、在其购买轮数内」的
   二分图上求**最大匹配** `maxBipartiteMatching`，得到 `maxAdditional`；
3. `projected = delivered + maxAdditional`，`canFulfil = projected >= target`。

并据此给出 `prompt`：

- `fulfilled`：已跳足，无需再排；
- `rest`：下一轮该客人登记了休息——提示「先歇一轮，回来后仍差 N 位」；
- `reschedule`：即使全排满也兑现不了名额——**征询改约/加轮**；
- `onTrack`：还差 N 位，给出剩余可跳轮次与候选舞伴数。

---

## 5. 竞争与"真实生效的一边"

- **确认是抢占式的**：同一人同一轮若已有一条 `confirmed`，后确认的邀约记为
  `conflict`（带 `conflictWith`），**先确认者生效**。
- `effectiveAssignment({number, round})`：关轮前返回真正生效的 `confirmed-invitation`，
  关轮后返回真实 `dance`，并把落败/冲突的邀约列进 `conflicts`。
- 两领队同时登记同一来客、两个手机近同时确认时，答案确定：
  **事件流里先落盘的那条确认生效**（单进程单写者，事件循环天然串行）。

---

## 6. 数据模型与持久化

- **纯函数式 reducer**：所有状态都可由事件日志折叠得到（`#apply`）。
- **追加式 JSONL + `fsync`**：`--emit` 先持久化再推进内存状态，崩溃不会让状态领先于日志。
- 重启：`new Engine({ filePath })` 自动 `load()` 所有事件并重放；
  `metPartners('s','001')` 等查询结果与崩溃前一致。

事件类型：`SESSION_CREATED`、`GUEST_REGISTERED`、`GUEST_REST_SET`、`ZONE_SET`、
`RHYTHM_CONSENT_SET`、`INVITATION_PROPOSED/CONFIRMED/DECLINED/CANCELLED/CONFLICT`、
`ROUND_CLOSED`、`ORDER_REDEEMED`。

---

## 7. HTTP 接口（`src/server.js`）

```
POST /sessions
POST /sessions/:sid/guests                     { number,name,roles,rhythms,targetPartners,roundsPurchased }
POST /sessions/:sid/rest                       { number,round,away }
POST /sessions/:sid/zones                      { zone,open }
POST /sessions/:sid/consent                    { number,round,rhythm,accept }
POST /sessions/:sid/invitations                { round,from,to,fromRole,toRole,zone,source }
POST /sessions/:sid/invitations/:id/confirm    { by }
POST /sessions/:sid/invitations/:id/decline    { by }
POST /sessions/:sid/invitations/:id/cancel     { reason }
POST /sessions/:sid/rounds/:round/close
POST /sessions/:sid/rounds/:round/plan         { source }
POST /sessions/:sid/makeup
GET  /sessions/:sid/guests/:number/record
GET  /sessions/:sid/guests/:number/met
GET  /sessions/:sid/guests/:number/quota
GET  /sessions/:sid/guests/:number/effective?round=
GET  /sessions/:sid/schedule
POST /sessions/:sid/guests/:number/redeem
```

领域错误映射为 400（`NO_SESSION`/`NO_GUEST` 为 404）。

---

## 8. 文件结构

```
src/store.js    追加式 JSONL 事件存储（写后 fsync）
src/engine.js   领域引擎：命令校验 + 纯 reducer + 查询（metPartners/quota/…）
src/server.js   零依赖 HTTP 封装 + 可执行入口
test/*.test.js  node:test 测试（引擎 + HTTP + 重启重放）
scripts/demo.js 端到端演示
```
