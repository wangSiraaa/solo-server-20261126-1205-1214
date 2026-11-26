# 家庭戏水馆 · 中途替班本地后台

一套**零第三方依赖**的 Python + SQLite + 原生 HTTP 后台，用来复演题目中的经营约定：

- 园区三片水区由馆员建档；
- 水区之间只承认明确填写的“口岸”，不会因为相邻就可跨过；
- “能不能看见孩子”是独立的视线关系，和口岸/相邻关系分开；
- 成人容量、家庭间接手/借用许可、实际入区回条共同决定能否放行；
- 孩子可同时有两名陪同成人；
- 同一个成人只能有一个当前实际位置；
- 每个成人的物理名额按 `(场次, 成人, 槽位)` 唯一占用，卖给/分配给别组后不能重复使用；
- 预备交接、名额预留、签发许可、实际过闸、过闸后实际接手分别落账；
- SQLite 文件关机后可重开，位置、实际接手人和离开事件不丢。

> 所有家庭、腕带、口岸和交易均为虚构场次的经营演示。

## 1. 30 秒验收

环境只需 Python 3.11+，不需要 `pip install`。

```bash
./demo.sh
```

脚本会：

1. 删除并重建 `data/demo.db`；
2. 真实启动 HTTP 服务；
3. 播种 **3 个区域、5 条腕带**；
4. 跑完整复演和反例，共 33 项断言；
5. 关掉服务；
6. 用同一个 SQLite 文件重新开机；
7. 再次核对甲在闸外、乙在浅区、乙实际接下妹妹、甲的 `gate_exit` 已入账。

手动跑测试：

```bash
python3 -m unittest discover -s tests -v
```

## 2. 启动后台和页面

```bash
python3 -m family_pool serve \
  --db data/family-pool.db \
  --host 127.0.0.1 \
  --port 8787 \
  --seed-demo
```

或：

```bash
PORT=8787 HOST=0.0.0.0 DB=data/family-pool.db ./start.sh --seed-demo
```

打开：

- 后台页面：<http://127.0.0.1:8787/>
- 健康检查：<http://127.0.0.1:8787/api/health>
- 主复演答案：<http://127.0.0.1:8787/api/answer?session_id=SESS-MAIN>
- 全部状态：<http://127.0.0.1:8787/api/state?session_id=SESS-MAIN>
- 流水：<http://127.0.0.1:8787/api/ledger?session_id=SESS-MAIN>

另一个终端可运行 HTTP 剧本：

```bash
python3 -m family_pool.replay --base-url http://127.0.0.1:8787
# 服务重启后：
python3 -m family_pool.replay --base-url http://127.0.0.1:8787 --verify-restart
```

## 3. 最小复演阵容

种子 ID 定义在 `family_pool/seed.py`：

| 类型 | ID | 名称 | 关键设定 |
|---|---|---|---|
| 区域 | `Z-SHALLOW` | 浅区 | 甲、乙、妹妹初始实际入区处 |
| 区域 | `Z-OTHER-SIDE` | 滑梯另一侧 | 与浅区有标明口岸，但**彼此看不见** |
| 区域 | `Z-WARM` | 暖池区 | 与浅区有视线关系 |
| 口岸 | `P-SHALLOW-OTHER` | 浅区—滑梯侧水口 | 可实际过口，但不产生可见关系 |
| 口岸 | `P-SHALLOW-WARM` | 浅区—暖池水口 | 标明口岸 |
| 家庭 | `F-JIA` | 甲家 | 甲、乙、妹妹 |
| 家庭 | `F-LIN` | 林家 | 林成人、小豆；两家允许互相接手/借用 |
| 腕带 | `P-A-JIA` / `WB-A` | 甲 | 成人，容量 2 |
| 腕带 | `P-B-YI` / `WB-B` | 乙 | 成人，容量 **1**，所以 baseline 恰有一个空名额 |
| 腕带 | `P-SISTER-MEI` / `WB-SISTER` | 妹妹 | 儿童 |
| 腕带 | `P-C-LIN` / `WB-C` | 林成人 | 成人；主剧本结尾前无入区回条 |
| 腕带 | `P-DOU` / `WB-DOU` | 小豆 | 儿童；容量反例中更早占用乙的唯一槽位 |

视线规则：同区恒可见；跨区只查 `visibility_edges`。因此：

```text
can_see(浅区, 滑梯另一侧) = false   # 虽有口岸/相邻
can_see(浅区, 暖池区)     = true
```

## 4. 题目情节如何被保证

主剧本采用**预编家庭组制**；系统也实现了**现场互助团**的两步确认和测试。

### 4.1 甲、乙都在浅区，甲申请独自离开

调用顺序：

```text
POST /api/sessions
POST /api/plan-segments                 # 主陪甲、备陪乙、跨区窗口
POST /api/scans/gate-entry              # 工作人员逐腕带确认甲/乙/妹妹实际入浅区
POST /api/care/assignments              # 甲实际陪同妹妹
POST /api/handovers/prepare             # 预编安排 + 乙在场可见 + 空槽位 -> prepared
POST /api/permits                       # 签发甲的 gate_exit 许可
POST /api/permits/PM-MAIN/consume       # 工作人员过闸时再次校验并消费许可
```

放行时事务完成：

1. 写甲的 `gate_exit` 事件和馆外回条；
2. 许可变为 `consumed`；
3. 交接变为 `effective`；
4. 乙的槽位由 `reserved` 变 `consumed`；
5. 新增乙—妹妹的 `actual` 陪同关系；
6. 结束甲—妹妹的旧陪同关系并释放甲的旧槽位；
7. 分别写 `gate.crossed`、`handover.effective`、`slot.consumed` 流水。

所以 `/api/answer?session_id=SESS-MAIN` 能回答：

- 甲：`outside`，已真正出闸；
- 乙、妹妹：`inside / Z-SHALLOW`；
- 妹妹的 `actual_receiver_ids` 为 `[P-B-YI]`；
- 哪一个 `crossing_event_id` / `permit_id` 是甲的离开。

### 4.2 只把乙的实际回条改到看不见的另一侧

反例场次 `SESS-INVISIBLE` 中，委托和槽位均已存在，然后调用：

```text
POST /api/receipts/correct
{ "wristband_id":"P-B-YI", "zone_id":"Z-OTHER-SIDE",
  "reason":"只改实际入区回条" }
```

再次兑现同一许可时，`consume` 会重新读取**最新实际回条**，返回：

```text
409 gate_not_released
receiver_not_visible
```

同时：

- 许可仍是 `issued`，没有被消费；
- 交接仍是 `prepared`，委托不丢；
- 乙的槽位仍是 `[1]`，人数不变；
- `permit_attempts` 和 `gate.denied` 留下拒绝账。

把乙的回条恢复到浅区后，同一许可可以成功兑现。

### 4.3 乙的最后名额更早被另一名儿童占用

容量反例 `SESS-CAPACITY` 把受理顺序倒过来：

1. 林成人委托乙接小豆；
2. 现场确认并预备，乙的唯一槽位 `slot_no=1` 被小豆占用；
3. 林成人过闸，乙成为小豆的实际接手人；
4. 甲再请乙接妹妹。

此时返回 `409 no_free_slot`；因为甲仍有妹妹的 active 陪同关系，试图不提交接直接签发离区许可会返回 `409 handover_required`。甲始终留在浅区，也没有 `gate_exit`。

系统不是在妹妹已占槽后强行把同一槽再给小豆——数据库的部分唯一索引会阻止这种重复占用。因此该反例按“较早受理”的真实顺序重放。

### 4.4 闸机外的成人不能被已售票假定为在场

`SESS-PRESENCE` 中乙的腕带存在，但没有任何入区回条。预备交接返回：

```text
409 receiver_not_in_water
```

只有工作人员实际调用 `/api/scans/gate-entry` 后，乙才成为有效接替人。

### 4.5 甲出闸后，另一家借用甲原来的陪同时间

主剧本末尾：

```text
POST /api/time-accounts                 # 甲原陪同时间，1 个可借用单位
POST /api/time-loans                    # 林家/小豆申请
POST /api/time-loans/LOAN-LIN-1/decision { approve:true }
```

批准只代表商业名额已卖出并扣减库存；实际陪同状态是 `awaiting_present_adult`。

随后：

- 指定甲兑现：甲最新回条在馆外，返回 `adult_not_in_water`；
- 指定林成人兑现：林成人无入区回条，同样拒绝；
- 第二单再借同一个单位：返回 `loan_quota_exhausted`，不会重复使用；
- 林成人和小豆都实际刷卡入区后，`/time-loans/.../fulfill` 才创建跨家庭槽位和实际陪同关系。

## 5. 现场互助团两步流程

除一键 `/api/handovers/prepare` 外，也支持柜台真实分步：

```text
POST /api/aid-groups                    # 组团及家庭范围
POST /api/handovers                     # proposed，写明哪些孩子
POST /api/handovers/{id}/confirm        # 仍在场的指定成人用本人腕带确认
POST /api/handovers/{id}/prepare        # 前台决定把有限槽位留给这一组
POST /api/permits
POST /api/permits/{id}/consume
```

测试 `TwoStepOnsiteTests` 完整覆盖该流程。

## 6. 主要 HTTP API

| 方法/路径 | 作用 |
|---|---|
| `POST /api/demo/reset-and-seed` | 清空业务表并播种三片区域、五条腕带 |
| `POST /api/zones` | 馆员填写水区 |
| `POST /api/ports` | 填写标明口岸 |
| `POST /api/visibility` | 填写跨区可见关系 |
| `GET /api/venue` | 区域、口岸、视线图 |
| `POST /api/families` / `POST /api/family-consents` | 家庭及互相接手/借用许可 |
| `POST /api/wristbands/issue`（同 `POST /api/persons`） | 发腕带、角色、家庭、成人容量；请求里可用人员 ID 或 `WB-A` 这类 `band_code` |
| `POST /api/sessions` | 开场，模式为 `prearranged` / `onsite` / `hybrid` |
| `POST /api/aid-groups` | 现场互助团/组团 |
| `POST /api/plan-segments` | 每段活动主陪、备陪、跨区时间窗 |
| `POST /api/scans/gate-entry` | 确认闸机入区并产生实际回条 |
| `POST /api/crossings/port` | 逐人确认从标明口岸过口 |
| `POST /api/receipts/correct` | 凭实际情况更正最新回条，保留审计链 |
| `POST /api/care/assignments` | 实际陪同/共同陪同关系并占用槽位 |
| `POST /api/handovers/prepare` | 一键确认+容量分配+预备入账 |
| `POST /api/handovers`、`/confirm`、`/prepare` | 现场互助分步流程 |
| `POST /api/permits` | 成人或儿童腕带请求本次离区 |
| `POST /api/permits/{id}/consume` | 工作人员实际过闸并兑现交接 |
| `POST /api/time-accounts` | 可借用的原陪同时间/名额 |
| `POST /api/time-loans`、`/decision`、`/fulfill` | 借用申请、卖出记账、实际在场兑现 |
| `GET /api/answer?session_id=...` | 位置、孩子实际接手人、离开事件 |
| `GET /api/state?session_id=...` | 后台页面所需完整状态 |
| `GET /api/ledger?session_id=...` | 只增审计/经营流水 |

多数 POST 支持 JSON 字段 `idempotency_key`，也支持请求头 `Idempotency-Key`。复演中的固定 `at` 字段让时间窗测试确定可重复；真实调用不传 `at` 时使用当前 UTC 时间。

## 7. 持久化与并发

- 数据库：Python 标准库 `sqlite3`；
- 所有关键写操作使用 `BEGIN IMMEDIATE` 和进程内 `RLock` 串行化；
- 槽位唯一性由部分唯一索引兜底：

```sql
UNIQUE(session_id, adult_id, slot_no)
WHERE status IN ('reserved', 'consumed')
```

```sql
UNIQUE(session_id, adult_id, child_id)
WHERE status IN ('reserved', 'consumed')
```

- 位置不来自“已售票”或请求参数，而来自该腕带最新的 `receipts` 回条；
- 拒绝过闸也会写 `permit_attempts` 和 `ledger_entries`；
- 服务进程重启后重新打开同一个 `.db`/.`db-wal` 文件即可恢复。

## 8. 目录

```text
family_pool/
  __main__.py          # python -m family_pool serve/init-db
  db.py                # SQLite schema
  service.py           # 业务规则与事务
  http_app.py          # 零依赖 HTTP API + 静态页面
  seed.py              # 三区五腕带演示数据
  replay.py            # 33 项剧情/反例/重启断言
  web/index.html       # 本地后台页面
tests/
  test_family_pool.py
demo.sh                # 真实关机/重开验收
start.sh
```
