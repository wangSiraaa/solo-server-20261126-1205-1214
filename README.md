# 社区羽球馆双打记分本地服务

四人双打（两队各两人）的本地记分服务，覆盖：登记初始左右区 → 开回合 →
边裁临时报分 → 记分台确认 → 向下一位发球人签发开拍单的完整流程。
纯 Python 标准库实现，零依赖，事件溯源持久化，重启后重放日志即可恢复。

## 裁判制度：逐拍确认制（本馆选定）

- **一张单**：记分台每次只签发一张指明「人 + 站区」的开拍单（permit），
  单号形如 `R3-2`（第 3 回合第 2 次签发，重发会递增序号）。
- **确认界线**：一张单必须经历 `issued → served`，且两位边裁 J1/J2 的
  临时报分一致，记分台才能 `confirm` 入账；确认前不放行下一拍。
- **领单即开拍**：只有单上指明的发球人能持该单开球，其他人持单被拒。
- **报反恢复**：两份报分胜负相反时单子进入 `disputed`，确认被锁；
  边裁和登记球员可在该拍确认前举证（异议），记分台裁定胜方
  （`adjudicated`）后才能确认，比赛从同一拍继续。
- **举证主体**：四位登记球员 + 两位边裁；裁定权只在记分台。
- **异议窗口**：开拍单签发后到该拍确认前。确认即封盘，本局不再受理
  该拍异议（逐拍确认制的明确界线）。
- **错人发球的补救**（开拍前发现）：记分台 `void` 作废旧单并按当前分数
  站位重发新单，旧单留痕；已确认的回合保留双方认领记录，
  `GET rallies` 可查每一拍的**原发球人**。
- **可打完路径**：先到 `target_score`（默认 21，建局可改）即完赛，
  完赛后不再签发新单、拒绝一切开拍。

## 轮换规则（`badminton/rules.py`）

- 每回合一分，赢方获得下一拍发球权。
- 原发球方赢球：该队两人交换左右区；接发方夺分：双方站位都不动。
- 新发球方按本队当前分数选站区：**偶数右区、奇数左区**。
- 开局 0:0 为偶数，只能由发球方右区球员发球；只按号码顺序轮着点人会被
  服务拒绝（`server_id` 与右区不符时建局即报错）。

## 暂停

每队每局一次，只能用在**本队当前有效单**上、且实际开球前由队员领用：

- 领用后单子挂起，即使发球人合法也拒绝开拍，记分台放行后才能开。
- 对旧单的迟报被拒（`旧单上的迟报不占新单`），不消耗新单的暂停额度。

## 角色入口（HTTP 前缀强制隔离）

| 角色 | 入口前缀 | 能做的事 |
|---|---|---|
| 记分台 | `/api/desk/...` | 建局、确认、裁定、作废重发、放行暂停 |
| 边裁 | `/api/judge/...` | 临时报分、举证异议（仅 J1/J2） |
| 球员 | `/api/player/...` | 持单开球、领用暂停、举证异议（仅登记球员） |

所有 POST 均可带 `idempotency_key`：同一键重放返回首次结果、不新增事件，
因此重启后重送同一张边裁记录不会凭空多打一拍。

## 运行

```bash
python3 -m badminton.http_app            # 启动 HTTP 服务，默认 127.0.0.1:8080
python3 demo.py                          # 进程内跑通题目全部情形
python3 -m unittest discover -s tests -v # 14 项测试
python3 -m badminton.replay ./data <局号> # 重放一局的事件日志
```

## HTTP 接口一览

```
POST /api/desk/games                          登记开局
POST /api/desk/games/<g>/confirm              确认当前拍
POST /api/desk/games/<g>/adjudicate           裁定报反争议
POST /api/desk/games/<g>/void                 开拍前作废重发
POST /api/desk/games/<g>/release-timeout      放行暂停
POST /api/judge/games/<g>/reports             边裁临时报分
POST /api/judge/games/<g>/objections          边裁举证
POST /api/player/games/<g>/serve              球员持单开球
POST /api/player/games/<g>/timeouts           球员领用暂停
POST /api/player/games/<g>/objections         球员举证
GET  /api/games/<g>                           整局只读视图
GET  /api/games/<g>/permits/<p>               单张开拍单
GET  /api/games/<g>/rallies                   已打回合（含原发球人）
GET  /api/games/<g>/replay                    事件流（重放依据）
```

## 持久化与重放

每局一个 `data/<game_id>.jsonl` 事件日志，追加写并 `fsync`。
状态不在任何其他地方保存——重放事件流即可逐位还原（见 `projection.py`）。
事件类型：`game.setup / permit.issued / permit.served / permit.voided /
report.submitted / permit.disputed / objection.filed /
permit.adjudicated / timeout.claimed / timeout.released / rally.confirmed`。

## 题目三个开局情形的实测结论（见 demo.py）

1. 甲一右区开局、甲首胜 → 下一张单 `R2-1` 由甲一在**左区**发。
2. 只对调甲队站位（右区甲二）开局、同一胜负记录 → 甲二在**左区**发；
   硬点甲一持单开球被拒。
3. 还原站位、首判给乙 → 双方站位不动，乙分数 1（奇数）由左区 **B2** 发。
