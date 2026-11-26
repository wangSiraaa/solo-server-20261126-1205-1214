# 远程配戏棚本地 HTTP 服务

零第三方依赖的本地服务。录音用短词数组表示；录音号、发送号、交片单号均由服务端分配。

## 一分钟验证

```bash
python3 demo.py
```

`demo.py` 会自动启动真实 HTTP 服务、跑完整场景、杀掉并重启服务，最后输出 `DEMO_OK {...}`。

单独启动服务：

```bash
python3 server.py --host 127.0.0.1 --port 8787 --db data/studio.sqlite3
# 或 npm start
```

运行状态规则测试：

```bash
python3 -m unittest -v test_service.py
# 或 npm test
```

## 哪段对答能进交片单？

不能按演员口头说“接的是第二遍”判断，也不能按导演的“已发送第二遍”记录判断。唯一依据是：

1. 本地播放器对某条耳返回传过词段确认；
2. 确认发生在该应答录音 `startSeq` 之前，并已进入这段录音的 `listeningSnapshot`；
3. 词段和服务端分配的具体 `recordingId` 对应；
4. 区间覆盖台本要求的 `[startIndex,endIndex)`，或签单前存在仍有效的导演互认。

演示中的三句单最终引用：

```text
甲第二遍 A2（尾词“别”）
  -> 乙第二遍 B2（开录前已由播放器确认实听 A2 的尾段 [2,4)）
    -> 丙第二遍 C2（开录前已确认实听 B2 的指定区间 [1,3)）
```

因此能签的是完整链条 `[A2, B2, C2]`，不是只看最后一对。`[A2, B1, C1]` 在演示中曾因导演互认 `A1[2,4) ≈ A2[2,4)` 变成 `ready`；撤掉这项互认后，未签候选单立即回到 `pending`。已签出的 `[A2,B2,C2]` 不依赖该互认，且签单 JSON 冻结，之后重启仍原样可查。

## 核心状态规则

- 每幕/每个台本最多 **4 句**：创建第 5 句返回 `422 too_many_lines`。
- 每句最多 **3 遍**：第 4 次开录返回 `409 take_limit_exceeded`。
- 区间一律为零基、半开区间 `[startIndex,endIndex)`。
- 导演 `POST /api/deliveries` 只证明“发送过”。
- 演员 `POST /api/self-reports` 永远 `admissible:false`，只留存陈述。
- 只有 `POST /api/playback-confirmations` 产生实听证据。
- 播放器确认必须带 `Idempotency-Key` 或 body 中的 `confirmationKey`。同一 key、同一词段重复送达返回同一个 `confirmationId/receivedSeq`，不会多一个词。
- 应答调用 `POST /api/recordings/start` 时，服务端写入不可变快照 `listeningSnapshot`，其中列出本会话在 `cutoffSeq` 前已确认的词段及各自 `recordingId`。
- 开录后才到的确认仍可留存/展示，但进入 `lateConfirmationsNotCounted`，不能补成开录前经历，也不会改写旧快照。
- 两遍文字即使完全相同，也不会由服务端自动判定等价；必须有导演在签单前登记的 mutual recognition。

## 中途换词的耳返策略

导演对同一会话再次 `POST /api/deliveries` 时：

- 旧 delivery 变为 `switched`，并记录 `supersededBy`；
- 旧 delivery 已确认的词段仍绑定旧 `recordingId`，可作为旧演法的证据；
- 新 delivery 在播放器确认前只是“已发送通知”；
- 切换后才用新 key 对旧 delivery 补送的确认会标为 `rejected/delivery_switched`，不进入证据；
- 若交片单选新前词、乙快照里只有旧前词，则核验新前词失败。要么实听新遍后重录乙，要么由导演在签单前登记明确区间互认。

## 版本什么时候锁定？

- 每遍录音一旦 `complete`，词数组和 ID 不可改写。
- 未签候选单的状态会随互认撤销等事实重新核验，因此可能从 `ready` 回到 `pending`。
- 同棚连排调用 `POST /api/rehearsals/{id}/close` 后封场，不能再以该连排名义开新录音。
- 导演调用 `POST /api/sheets/{id}/sign` 时最终锁定：服务端保存按台本顺序的具体录音 ID、完整核验报告、签出序号和 canonical hash。
- 已签单 `GET /api/sheets/{id}` 永远返回签出时冻结的 snapshot；之后撤销互认或重启服务都不改变它。
- 可调用 `GET /api/sheets/{id}/verify` 重算 hash，检查冻结内容是否一致。

## 两种收片棚

### 同棚连排

`POST /api/studios/{studioId}/rehearsals` 时圈定：

- `roster`：甲/乙/丙各自的 `sessionId`；
- `scriptId`；
- 可选 `initialCue`：本场前词策略或具体前词录音。

录音时带 `rehearsalId`。封场前可按每句三遍上限重录；封场后不能再开录。建单时传同一个 `rehearsalId`，服务端会检查所有中选录音确实属于这条连排。签单后连排状态变为 `delivered`。

### 跨场补录

`POST /api/pickup-plans` 输入：

```json
{
  "studioId": "studio_...",
  "scriptId": "scr_...",
  "baselineRecordingIds": ["来自R2的甲", "来自R1的乙", "来自R1的丙"],
  "rerecordCosts": [80, 20, 100]
}
```

服务端枚举“保留基线片或逐句补录”的可行组合，选择：

1. 补录总成本最低；
2. 成本相同则补录句数最少；
3. 仍相同则优先保留最晚的后句/最长可用后缀。

如果保留旧应答句、只重录它前面的 cue，旧应答快照中必须已有一段可证明的实听区间；方案会要求在补录取得服务端录音号后，由导演登记一项搭桥互认。这样能尽量保留仍然适用的后句，而不是机械地整段重录。

- `POST /api/pickup-plans/{id}/simulate`：用脚本会话模拟送词、播放器确认、开录和完成补录，并登记方案列出的搭桥互认。
- 未录齐时 `POST /api/pickup-plans/{id}/sheet` 返回 `409 pickups_incomplete`。
- 录齐并生成可签单后，仍需导演显式 `/sign`。

## 如何向导演解释落选片段

调用：

```bash
curl -H "X-Session-Token: $DIRECTOR_TOKEN" \
  http://127.0.0.1:8787/api/scripts/$SCRIPT_ID/casting-matrix
```

返回每句每遍录音对每一条前词 take 的核验矩阵，包括：

- 乙开录快照里实际有哪些 `recordingId + interval`；
- 缺了哪些词下标；
- 是否通过导演互认；
- 哪些播放器确认因晚于开录被排除；
- 演员自报记录在 `selfReportsIgnored` 中，但明确不参与判定。

交片单自身的 `validation.lineChecks/hearingChecks` 也会给出同样的落选原因。

## 主要 HTTP 接口

所有业务接口使用 JSON；鉴权头为 `X-Session-Token` 或 `Authorization: Bearer <token>`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/sessions` | 创建 `director/jia/yi/bing` 会话 |
| POST | `/api/studios` | 建棚 |
| POST | `/api/studios/{id}/script` | 建最多四句台本和听词区间 |
| POST | `/api/studios/{id}/rehearsals` | 圈定同棚连排参演者/前词 |
| POST | `/api/rehearsals/{id}/close` | 封场 |
| POST | `/api/recordings/start` | 服务端分配录音号并冻结听词快照 |
| POST | `/api/recordings/{id}/complete` | 提交短词数组，完成录音 |
| POST | `/api/deliveries` | 导演送耳返；新发送会切换旧 active delivery |
| POST | `/api/playback-confirmations` | 模拟播放器确认词段，需幂等 key |
| POST | `/api/self-reports` | 演员自报，留档但不作证据 |
| POST | `/api/studios/{id}/mutual-recognitions` | 登记两遍指定区间互认 |
| POST | `/api/mutual-recognitions/{id}/revoke` | 撤销互认并重算未签单 |
| POST | `/api/sheets/validate` | 只核验候选句序 |
| POST | `/api/sheets` | 创建候选/待处理交片单 |
| POST | `/api/sheets/{id}/sign` | 可签时冻结并签出 |
| GET | `/api/sheets/{id}` | 查询交片单；已签单返回冻结 JSON |
| GET | `/api/sheets/{id}/verify` | 校验 canonical hash |
| POST | `/api/pickup-plans` | 计算最低成本跨场补录清单 |
| POST | `/api/pickup-plans/{id}/simulate` | 脚本模拟录齐补录 |
| POST | `/api/pickup-plans/{id}/sheet` | 录齐后生成交片单 |
| GET | `/api/scripts/{id}/casting-matrix` | 落选片段原因矩阵 |

## 持久化

数据保存在 SQLite 文件（默认 `data/studio.sqlite3`）。停止并重启 `server.py --db同一个文件` 后：

- 录音号、交片单号不变；
- 幂等确认 key 不重复生效；
- 已签单和 hash 不变；
- 开录快照不变，迟到确认仍不能回溯。
