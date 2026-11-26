# 共同收听服务 (Co-listening service)

一个供本地播客读书会使用的 HTTP 服务。它解决的核心问题：**「两人都播到了第 13 秒」并不等于「两人听完了同一句」**——因为每个人的清单版本不同，导听长短不同，同一逻辑秒数对应的正文位置也不同。

服务端不信任「播放到第几秒」，只信任播放端上报的**实际播放区间**；拖过（seek）一段不会计入区间。

```bash
npm test          # 19 个用例：领域逻辑 + HTTP 集成
npm run demo      # 起服务并扮演主持端与两台播放器跑完整场景
npm start         # 只起 HTTP 服务（PORT=8787）
```

Node ≥ 18，无第三方依赖。

---

## 1. 领域模型

### 正文与片段号
- **正文 (episode flow)**：有序的 `main` 片段，每个有稳定 `segmentId` 和时长。
- **同一片段可重复出现**：`segmentId` 相同但出现次序不同，是两次不同的 *occurrence*（`甲#1`、`甲#2`）。
- **接缝 (seam)**：相邻正文之间的边界。稳定键：`intro`（第一段之前）、`seam:<i>`（第 i 段与第 i+1 段之间）、`outro`（最后一段之后）。导听插在接缝处。

### 个人清单版本 (manifest)
每个成员订到的是自己的一版清单 = 正文流 + 自己的导听注入。成员的导听可不同：
- **时长不同** `duration`
- **必听性不同** `mustListen`

清单里每一项叫 **item**，用稳定的 `idx`（出现次序）寻址：
- 正文项：`{kind:"main", segmentId, occurrence}` —— 身份跨版本稳定。
- 导听项：`{kind:"guide", slotId, guideId}` —— 身份 = 内容（slot+guideId+时长+必听性）。

### 实际播放区间 (played)
播放端按 item 上报 `{idx, from, to}`（item 内相对秒）。服务端对同一身
份的区间做并集：
- **进度 = 并集的长度**，不是最大秒数。
- **位置 = 从 0 起连续覆盖的前缀**，遇到缺口即停。
- **拖过一段不算听完**：缺失 = 并集在 `[0, duration]` 内的补集。

### 旧版本的回报
编辑替换某段导听后，清单版本号 +1。旧版本回报**仍然接收**，但因为：
- 正文项按 `(segmentId, occurrence, offset)` 记录——跨版本稳定；
- 导听项按内容 key 记录——新版导听是不同的 key；

所以旧版回报能正确贡献正文覆盖，却**绝不会把旧版的逻辑秒数当成新版正文进度**，旧导听的秒数也不会冒充新导听。

---

## 2. 房间规则

### 全组齐步 (rule = `sync`)
- 快到讨论点的人先停；`continue` 许可进入 `waiting`。
- 本轮**所有成员**都补齐（听到绑定的那次正文 + 其前所有必听导听）后，许可才自动变为 `granted`。
- 若之后有人缺听使本轮不再齐，已发的许可被 `revoked`（audit 有记录）。
- 主持人可把离席者 `defer` 到下一轮，并随即返回其**恢复后怎样补听**（`catchUp`）。

### 允许先行 (rule = `early`)
- 快的人可 `prelisten`，许可立即发放，可预听下一节。
- 慢的人仍从自己位置走。
- 开讨论仍须各人听到对齐点（绑定正文+必听导听）。
- 开讨论时给每个先行者下达指令：**回到那次讨论的正文**（`returnTo`），并记住预听进度（`resumeAfterDiscussion`）。讨论结束后恢复其预听进度。

---

## 3. 身份、连接、续接

- **成员进度**存于成员，不随连接丢失。播放端每次上报携带 `connId`（本次连接编号）；服务端校验连接存活。
- **离开又加入**：`POST /members` 对已存在的成员是「重新加入」，保留其清单版本、回报与许可；`POST /members/leave` 关闭其所有连接但保留状态。
- **交还主持权**：`POST /authority` 使 `authorityEpoch` +1。之后所有主持操作必须携带新 epoch；**旧 epoch 迟到也会被拒**（`STALE_AUTHORITY`），即使来自已掉线的连接。
- **哪些许可已发给播放端**：服务端从不把「请求」当「生效」。重连时 `GET /snapshot`（或 `POST /connections` 的返回）给出：
  - `effectivePermits`：此刻真正生效的许可；
  - `stalePermits`：曾发放但已失效的（含原因）；
  - `relisten`：真正生效的回听位置与缺失内容。
  `GET /permits` 同时暴露原始 `permits`（含 waiting/denied/revoked）供审计。

所有许可决定（发放、撤销、拒绝、主持权拒绝）都写入 **audit** 事件流，可回查。

---

## 4. 关键举例（见 `test/service.test.js` 与 `scripts/demo.js`）

正文甲 10s、乙 6s，甲→乙之间插入必听导听：p1 两秒、p2 五秒。

| | p1 清单 | p2 清单 | 13 秒时正文位置 |
|---|---|---|---|
| 甲 | 0–10 | 0–10 | 都在甲 |
| 导听 | 10–12 (2s) | 10–15 (5s) | |
| 乙 | 12–18 | 15–21 | **p1 在乙+1s，p2 在乙+0s** |

- 两人都从头上报连续播放到自己的第 13 秒。
- 此时只有 p1 听到了乙的 1 秒；p2 只到乙的起点。**即便两人都提前请求过继续，讨论点仍不能判给全组**（`DISCUSSION_NOT_READY`，`waitingOn=[p1,p2]`）。
- 两人后来都听完乙 → 讨论可开。
- 只把 p2 的旧回报换成「拖过 3 秒导听」→ 房间指出缺口（`MANDATORY_GUIDE_MISSING`，缺口恰好 3 秒）。
- 还原播放记录，再把讨论范围改为包含尚未开听的第三人 → 本轮等待他（`waitingOn=[p3]`）。

---

## 5. HTTP API

所有请求/响应为 JSON。错误体：`{ "error": "<CODE>", "message": "...", ... }`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/rooms` | 建房：`roomId, flow, rule, host, members, assignmentsByMember` |
| GET | `/rooms/:room` | 房间摘要 |
| POST | `/rooms/:room/members` | 加入/重新加入成员：`memberId, name?, assignments?` |
| POST | `/rooms/:room/members/leave` | 成员离席：`memberId` |
| POST | `/rooms/:room/connections` | 开连接（返回快照）：`memberId, connId?` |
| DELETE | `/rooms/:room/connections/:connId?memberId=` | 关连接 |
| GET | `/rooms/:room/snapshot?memberId=&connId=` | 生效许可 + 回听位置 |
| GET | `/rooms/:room/members/:memberId/progress` | 该成员进度/缺失 |
| POST | `/rooms/:room/reports` | 上报播放区间：`memberId, connId, manifestVersion, items[], reset?` |
| DELETE | `/rooms/:room/members/:memberId/reports` | 清空回报 |
| POST | `/rooms/:room/manifest` | 主持编辑清单：`actorMemberId, authorityEpoch, connId, memberId?+assignments? / all?` |
| POST | `/rooms/:room/discussion/point` | 设讨论点：`segmentId, occurrence` |
| POST | `/rooms/:room/discussion/round` | 设本轮成员：`memberIds[]` |
| POST | `/rooms/:room/discussion/defer` | 转入下一轮：`memberId` |
| POST | `/rooms/:room/discussion/open` | 开讨论（全体就绪才可） |
| POST | `/rooms/:room/discussion/end` | 结束讨论并下恢复指令 |
| GET | `/rooms/:room/discussion?memberId=` | 讨论状态 + 各人就绪 |
| GET | `/rooms/:room/who-missing?memberId=` | 谁还缺哪段 |
| POST | `/rooms/:room/permits` | 请求许可：`memberId, connId, kind: continue\|prelisten` |
| GET | `/rooms/:room/permits?memberId=` | 原始许可 + 生效/失效 |
| POST | `/rooms/:room/authority` | 交还主持权：`toMemberId` |
| GET | `/rooms/:room/audit?since=` | 事件流 |

主持类请求需带 `actorMemberId, authorityEpoch, connId`。

### 示例：上报「连续播放到逻辑 13 秒」

```jsonc
POST /rooms/r/reports
{
  "memberId": "p1",
  "connId": "p1-c1",
  "manifestVersion": 1,
  "items": [
    { "idx": 0, "from": 0, "to": 10 },  // 甲全
    { "idx": 1, "from": 0, "to": 2 },   // 导听全 (p1 的 2s)
    { "idx": 2, "from": 0, "to": 1 }    // 乙 1s
  ]
}
```

---

## 6. 文件

- `src/model.js` — 区间并集、前缀位置、缺口、清单构建。
- `src/service.js` — 房间/成员/版本/许可/讨论/主持权。
- `src/server.js` — HTTP 路由与错误映射。
- `scripts/demo.js` — 主持端 + 两台播放器的完整剧本（含迟到的旧主持请求）。
- `test/service.test.js`、`test/http.test.js` — 领域与 HTTP 用例。
