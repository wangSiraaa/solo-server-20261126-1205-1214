# 展陈工房 · 量具出租与返工服务

整数长度的量尺出租与返工后台：起点片 / 延长片的固定偏置会带入读数，补入
校准后要按**真实量法**判断每块展板是否还能满足装配净隙，并通过租户端派出
实际可完成的复核 / 换件 / 放行 / 召回工单。

纯标准库（Python 3.11+），SQLite 持久化，HTTP JSON API + 领域层 + 可复现的
场景脚本与测试。

## 复核路线（已选定）

采用 **随件复核 (accompanying recheck)**：租户接单时每块板保留一个仍可触达的
参照点。补入校准后逐块用真实量法重算真长：

| 情况 | 处置 |
| --- | --- |
| 真长仍满足净隙 | **放行 release**（正常交付） |
| 真长不满足、参照点可触达 | **现场复核 recheck**（占用复核时段，逐块重测缩小范围） |
| 真长不满足、参照点已被盖住 | **召回 recall**（不能当场放行，回工房重测） |
| 偏置片被不合格板实际使用 | **换件 replace**（片级工单，不整箱召回） |

工房 / 现场重测的新读数会重新判定并**改变具体工单**（如 recheck → release）。

## 量法约定

* 所有长度、偏置均为**整数**。
* 一次读数偏置 = 起点片偏置 + （跨接延长段时的）延长片偏置。
* 直接读数：`真长 = 读数 - 偏置`。
* 同一把尺两次读数相减：`真长 = (b - bias_b) - (a - bias_a)`。
  同一配置下偏置精确抵消 —— 相减法对片偏置**不敏感**，直接法敏感。
* 合格性只看**物理真长**，不看原始读数，也不看自称/标称偏置。

## 持箱 ≠ 全部用过

箱里有哪些片、租期内某片装在哪把尺，由归还 / 封箱 / 开盒 / 现场换片回执
（`piece-events` 与每次 `usage-receipt`）确定；但某块板是否受影响只取决于
**它自己回执里的尺、片、量法**。所以客服不会整箱召回 —— 见
`tests/test_scenarios.py::BoxScopeTests` 与 `scripts/demo.py` 第 6 步。

## 幂等申领 / 重启

`POST /work-orders/{id}/claim` 以 `screen_token`（旧屏幕）幂等：同一 work order
至多一个**有效处置**；同屏两名工人、跨重启都收敛到同一处置及其测量链。见
`tests/test_http.py::test_claim_idempotent_and_restart_survives`。

## 运行

```bash
# 启动 HTTP 服务（默认 var/service.db）
PYTHONPATH=. python3 -m app.http_api --db var/service.db --port 8000

# 完整场景演示（打印每一步租户视图）
PYTHONPATH=. python3 scripts/demo.py

# 测试
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

## API 速览

| 方法 & 路径 | 作用 |
| --- | --- |
| `POST /pieces` | 登记片（`nominal_bias` 标称 / `true_bias` 真实） |
| `POST /rulers` / `POST /boxes` | 量尺 / 工具箱 |
| `POST /boards` | 建板（要求净隙、参照点、是否可触达） |
| `POST /usage-receipts` | 登记下单使用回执（尺 + 片 + 量法 + 读数） |
| `POST /boards/{id}/usage-receipt` | 现场换片：替换该板使用回执 |
| `POST /piece-events` | 归还 / 封箱 / 开盒 / 换片来源事件 |
| `POST /calibrations` | 补入校准（揭示片真实偏置） |
| `POST /calibrations/{id}/dispatch` | 按真实量法评估并派工单 |
| `GET  /calibrations/{id}/plan` | 租户视图：须重测 / 复核时段 / 证据 |
| `GET  /work-orders` | 工单列表（可按 tenant / calibration / status 过滤） |
| `POST /work-orders/{id}/claim` | 租户端申领（幂等） |
| `GET  /work-orders/{id}/measurement-chain` | 该工单有效的测量链 |
| `POST /boards/{id}/recheck` | 现场复核重测（可改变工单） |
| `POST /boards/{id}/workshop-remeasure` | 工房复测重测（可改变工单） |
| `GET  /dispositions` | 租户端有效处置 |
| `GET  /boards/{id}/evaluation` | 单板真长 / 判定 / 测量链 |

## 代码结构

```
app/model.py    纯领域：偏置数学、合格性、处置决策、测量链
app/store.py    SQLite 快照 + 事件日志
app/engine.py   服务编排：回执、校准回溯、派工、申领幂等、复测改单
app/http_api.py 标准库 JSON HTTP 接口
tests/          model / scenarios / http 三层测试
scripts/demo.py 逐步旁白式演示
```
