# 社区活动咨询台 · 热线换班话务后台

一个零外部依赖的 Node.js 本地服务。**不接真实电话线路**，用来模拟来电人、坐席甲/乙、等待音乐、私聊咨询支路、陪同转接、挂机通知、乐观锁冲突和进程重启后的责任恢复。

本实现选择的业务模式是：

> **陪同转接（attended / accompanied transfer）**：甲先暂存来电人，与乙私聊；随后把乙桥接给来电人。只有“音频上确实连通”和“工作台资格确实满足”同时对齐，责任接办人才换成乙；甲之后才离场。

回呼接棒模式没有实现；当前模型把“来电人实际在场且听见”作为答复可办结的音频条件。

---

## 1. 一键验收

需要 Node.js 18+（开发环境为 Node 20）。无需 `npm install`。

```bash
npm test
```

验收脚本会自动：

1. 用临时数据文件在 `127.0.0.1:3210` 启动服务；
2. 验证甲只能答日期、乙能答日期和取件；
3. 用两个相同 `expectedRevision` 的并发暂存请求验证旧画面冲突；
4. 模拟乙忙线、咨询通话失败、甲继续跟进；
5. 验证私聊中的取件答复不会结清取件项；
6. 验证撤销乙的取件资格不会改变音频通路，但会阻止交班/办结；
7. 完成陪同转接；
8. 重发运营商挂机通知并验证幂等；
9. 停止进程、用同一 JSON 文件重启，再从来电号码恢复完整交接链；
10. 验证 CLI 可调用。

最近一次本地运行结果：**30 个断言场景全部通过**。

---

## 2. 启动服务

```bash
npm start
# 默认 http://127.0.0.1:3000
```

可选环境变量：

```bash
PORT=4000 HOST=127.0.0.1 HOTLINE_DATA=/tmp/hotline.json npm start
```

数据在每次变更后原子写入 `HOTLINE_DATA`；默认文件是 `./data/hotline.json`。

另开终端使用 CLI：

```bash
npm run cli -- help
# 或
node cli.js help
```

下面示例默认号码为 `13800001111`，也可用 `--phone=139...` 覆盖，或设置 `CALLER_PHONE`。

---

## 3. 三个终端怎么用

服务是普通 HTTP/JSON，因此可以由本机多个终端同时调用。推荐分三个角色终端；服务本身可在第一个终端前台运行，也可放到后台。

### 终端 A：服务/看板

```bash
npm start
```

另一个窗格可轮询工作台：

```bash
watch -n 1 'node cli.js board'
```

### 终端 B：模拟来电人 + 甲

```bash
B=http://127.0.0.1:3000
P=13800001111

node cli.js inbound $P --base $B
node cli.js answer a --phone $P --base $B
node cli.js ask date --phone $P --base $B
node cli.js say-date a --phone $P --base $B
node cli.js ask pickup --phone $P --base $B

# 甲暂存来电人：来电人进入等待音乐
node cli.js hold a --phone $P --base $B

# 乙忙线时可模拟失败，甲仍可取回
node cli.js presence b busy --base $B
node cli.js consult a b --simulate busy --phone $P --base $B   # 预期失败
node cli.js retrieve a --phone $P --base $B
node cli.js presence b available --base $B
```

### 终端 C：乙坐席

```bash
B=http://127.0.0.1:3000
P=13800001111

# 甲重新暂存并另拨乙后：
node cli.js hold a --phone $P --base $B
node cli.js consult a b --phone $P --base $B
node cli.js answer-consult b --phone $P --base $B

# 乙在甲—乙私聊支路念取件方式；这不会办结取件项
node cli.js say-pickup b --phone $P --base $B
node cli.js caller $P --base $B
```

然后回到任意终端继续桥接、交接和结案：

```bash
# 工单可先转给乙准备，但 returnableAgentId 仍为甲
node cli.js assign a b --phone $P --base $B

# 把来电人拉入同一音频桥
node cli.js bridge --phone $P --base $B
node cli.js audio --phone $P --base $B

# 交班核对连通关系 + 未答事项资格
node cli.js complete a b --phone $P --base $B

# 甲离场；只断甲的支路，乙—来电人仍通
node cli.js hangup a --phone $P --base $B

# 乙当来电人面重新答复后才结清
node cli.js say-pickup b --phone $P --base $B

# 来电人挂机，全案结案
node cli.js hangup caller --phone $P --base $B

# 重启服务后仍可按号码查回
node cli.js caller $P --base $B
```

> 注意：如果先执行了“撤销乙取件资格”的负向验收，需要重新 `node cli.js grant b pickup`，并由乙**重新当来电人面说一次**。资格撤销期间说出的旧话不能追溯办结。

---

## 4. 关键负向演示

### 4.1 私聊答复不算送达

```bash
node cli.js say-pickup b --phone $P --base $B
```

返回中的关键字段：

```json
{
  "formalAnswer": false,
  "blockers": ["utterance_not_audible_to_caller"],
  "utterance": {
    "audibleToCaller": false,
    "audienceAgentIds": ["a"]
  }
}
```

此时：

```bash
node cli.js caller 13800001111 --base http://127.0.0.1:3000
```

`cases[0].items.pickup.status` 仍是 `"open"`。

### 4.2 撤资格不断通路，同一句话不能办结

```bash
node cli.js bridge --phone $P
node cli.js revoke b pickup
node cli.js audio --phone $P          # callerHears.b 仍为 true
node cli.js say-pickup b --phone $P   # audibleToCaller=true，但 formalAnswer=false
```

返回的 `blockers` 包含：

```text
agent_not_qualified_at_delivery
```

即使随后恢复资格，那句在无资格状态下说出的话仍不能补办；必须由当前合格的乙重新向来电人送达。

### 4.3 相同旧画面并发

所有写接口接受可选的 `expectedRevision`。先读取 `/board` 的 `board.revision`，两个终端用同一个 revision 提交时，只有一个成功，另一个返回：

```json
{
  "ok": false,
  "error": {
    "code": "stale_screen",
    "message": "画面已过期：另一位坐席可能已经操作，请刷新后重试"
  }
}
```

### 4.4 重发运营商挂机通知

先查到甲咨询支路的 `providerLegId`：

```bash
node cli.js caller 13800001111
```

然后发送同一 `eventId` 两次：

```bash
curl -sS -X POST http://127.0.0.1:3000/telephony/events \
  -H 'content-type: application/json' \
  -d '{
    "eventId":"evt-a-hangup-001",
    "type":"leg.disconnected",
    "providerLegId":"prov-leg-123"
  }'

# 完全相同的请求再发一次；第二次 duplicate=true，不重复改状态
```

`processed provider eventId` 会持久化，所以进程重启后重发仍被幂等拦截。

---

## 5. “谁此刻能听到谁”的数据模型

系统没有用“工单在谁手里”推断声音，而是单独维护通话支路和音频房间：

- 一通来电是一条 `kind: "inbound"` 的 `calls` 记录；
- 另拨的咨询电话是另一条 `kind: "consult"` 的 `calls` 记录，有独立 `id/providerCallId/legs`；
- 每个坐席/来电人是独立 `leg`，有 `ringing | ringback | connected | held | disconnected | failed` 状态；
- 音频房间包括：
  - `customer-bridge`：甲—来电人；
  - `hold-music`：来电人等待音乐；
  - `private-consult`：甲—乙私聊；
  - `accompanied-bridge`：来电人—甲—乙三方陪同桥。

查询当前真实听觉关系：

```bash
curl http://127.0.0.1:3000/calls/<primaryCallId>/audio
```

重点字段：

- `whoHearsWhom`：有向听觉关系；
- `audiblePairs`：同一连通音频房间中的双方；
- `callerHears`：来电人此刻能听到哪些坐席；
- `agentHearsCaller`：哪些坐席能听到来电人；
- `holdMusic`：来电人是否在等待音乐中。

每次拓扑变化都会在 `audioSnapshots` 中留快照，因此重启后仍可复盘交接当时的连通关系，也可用：

```bash
curl 'http://127.0.0.1:3000/calls/<callId>/audio?at=2026-11-26T04:50:00.000Z'
```

查某个时刻最近的快照。

---

## 6. 工单归属与可回访责任是两件事

案件上有两个字段：

- `ticketHolderAgentId` / `workOrder.holderAgentId`：工单当前在谁手里准备；
- `returnableAgentId`：当前对来电人负责、可回访的接办人。

私聊后可以先执行 `assign` 把工单转给乙，但此时：

```text
ticketHolderAgentId = b
returnableAgentId   = a
```

只有 `complete-handoff` 同时确认：

1. 乙的支路与来电人在同一个活跃音频桥，双向可听；
2. 甲仍在场，满足陪同交接；
3. 乙具备所有仍为 `open` 事项的当前班次资格；

才把 `returnableAgentId` 改为乙，并保存资格和音频快照。

若来电人提前挂机且仍有 `open` 事项，案件进入 `pending_followup`。`GET /caller/:phone` 的 `responsibility` 会给出：

- 当前责任人；
- 其是否仍具备所有未答事项资格；
- 合格接棒坐席 `eligibleAgentIds`；
- 值班兜底台 `fallbackQueue`。

答复记录 `utterances` 保存：

- 原话和事项编码；
- 说话时所在房间/支路；
- `audibleToCaller`；
- 听众支路；
- 当时的资格快照；
- 是否构成正式答复及 blockers。

因此工单文本本身不能把一句私聊“洗”成已答复。

---

## 7. 重启后按来电号查责任交接

```bash
# 停止 npm start（Ctrl+C），再重新 npm start
node cli.js caller 13800001111
```

返回中包含：

- `cases[].items`：日期/取件逐项答复与音频证据；
- `cases[].returnableAgentId`：最终可回访责任人；
- `handovers[].phases`：
  - `private_consult_connected`
  - `ticket_assigned_privately`
  - `caller_bridged`
  - `responsibility_transferred`
  - `original_agent_departed`
- `timeline`：全部相关审计事件；
- `providerEvents`：运营商通知幂等记录（可用 `/state` 查看）。

---

## 8. 主要 HTTP / CLI 对照

| 动作 | HTTP | CLI |
| --- | --- | --- |
| 总看板 | `GET /board` | `node cli.js board` |
| 按号码查完整责任 | `GET /caller/:phone` | `node cli.js caller :phone` |
| 查音频关系 | `GET /calls/:id/audio` | `node cli.js audio --phone :phone` |
| 来电进入 | `POST /console/inbound` | `node cli.js inbound :phone` |
| 甲接起 | `POST /console/answer` | `node cli.js answer a` |
| 来电人提问 | `POST /console/ask` | `node cli.js ask date` |
| 坐席说话/答复 | `POST /console/speak` | `node cli.js say-date a` |
| 暂存/取回 | `POST /console/hold` `/retrieve` | `hold` / `retrieve` |
| 另拨咨询 | `POST /console/consult` | `node cli.js consult a b` |
| 接咨询 | `POST /console/answer-consult` | `answer-consult b` |
| 转工单 | `POST /console/assign` | `assign a b` |
| 桥接来电人 | `POST /console/bridge` | `bridge` |
| 完成交班 | `POST /console/complete-handoff` | `complete a b` |
| 挂机 | `POST /console/hangup` | `hangup caller/a/b` |
| 资格授予/撤销 | `POST /agents/:id/qualifications` | `grant` / `revoke` |
| 运营商通知 | `POST /telephony/events` | `node cli.js event ...` |
| 重置本地数据 | `POST /admin/reset` | `node cli.js reset` |

所有 `/console/*` 请求体均为 JSON，字段名与 CLI 透传一致；可加 `"expectedRevision": 12` 做乐观并发控制。

---

## 9. 目录结构

```text
server.js                 # 零依赖 HTTP 服务与路由
cli.js                    # 可在任意终端调用的命令行客户端
lib/store.js              # JSON 原子持久化、HttpError
lib/hotline.js            # 通话支路、音频房间、事项答复、交班责任领域逻辑
scripts/acceptance.js     # 启动/操作/杀进程/重启/重放通知的端到端验收
data/hotline.json         # 默认运行后生成，已 gitignore
```
