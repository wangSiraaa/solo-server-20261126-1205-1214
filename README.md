# 三乡轮市服务

一个零外部依赖的本地轮市服务：村委申报集期/摊类/相邻格并召集轮次，街坊看“今天去哪赶集”，摊主领取写明地点与邻摊的摆位单并到场报开。数据落在 SQLite，重复回单、跨月查询和重启后都能审计“哪一乡到底用掉了哪一轮”。

## 运行

需要 Python 3.10+（只用标准库）。

```bash
# 1. 跑完整本地演示（会重建 data/lunshi.sqlite3）
python3 demo.py

# 2. 跑不变量自检
python3 selftest.py

# 3. 启动网页/JSON 服务
python3 -m lunshi.server --db data/lunshi.sqlite3 --host 127.0.0.1 --port 8765
# 也可：python3 lunshi/server.py --port 8765
```

浏览器打开 <http://127.0.0.1:8765/>：

- `/street.html`：街坊街讯；
- `/vendor.html`：摊主摆位单、报开、撤摊/恢复；
- `/committee.html`：村委/联席会召集、核算、雨撤、墙隔、借地和审计。

`demo.py` 已把完整成功轮市写入默认库，因此直接启动服务即可在页面看到甲、乙、丙的轮次账和 V-WEN 等摊主摆位。

## 本实现选择的办轮办法

选择题目允许的第二种：**按实际开张与欠轮情况现场召集下一乡**。没有实现“集期前互签完整轮值表”的 signed schedule；因此也不涉及已签表内切换，但借地唯一许诺约束仍然保留。

实现决定：

1. **轮次号不随状态变化。** 第 1 轮固定优先甲，第 2 轮按 `甲→乙→丙` 循环固定给乙，依次类推。只有 `round_usage.status='effective'` 的记录才算该轮被用掉。
2. **前驱无效，后继不能入账。** 甲的书摊、修伞摊真实报开并满足开局约后，第 1 轮才有 effective usage；此后 `call_next` 才会找乙的合用场。若甲回执撤摊导致 usage 被冲正，即使乙已经真实开张，结算第 2 轮也会得到 `PREDECESSOR_NOT_COMPLETE`，不能把乙记成继任。
3. **欠轮保留同一轮号和优先补集权。** 雨撤、部分开张、回执冲正都会写 `owed_rounds`；下一次 `call_next` 找到的是甲的 `round_no=1 / kind=makeup` 合用日，而不是跳到乙。
4. **后面的集日照常开放。** 乙日、丙日的街讯和报名不必因甲的回执更正而删除；它们可以继续交易、保留摆位和回单，只是在轮次链修复前不能形成新的 effective successor usage。前驱补齐后，已满足真实开张条件的后续 call 可再结算。
5. **同一摊主同一日序同一时段只能有一个 live 回单。** 数据库部分唯一索引 `receipts_one_live_place` 兜底；两地登记不影响该约束。
6. **重复回单号幂等。** 相同 `receipt_no` / `Idempotency-Key` 重放返回原回单和 `duplicate:true`，不会新增开张或轮次。
7. **借地与完成轮次是两件事。** `venue_loans` 只记录地皮许诺，同一格/日/时段有部分唯一索引防止二次许诺。转场迁移同一张回单，并保留 `host_township_id`（主办乡/轮次归属）与 `spot_township_id`（地皮乡）；借地或转场本身绝不写轮次账。
8. **审计不物理删除。** 错误的“已办成”记录置为 `reversed`，链上后继可置为 `suspended_chain`；恢复后可补记新的 effective usage 或重激活链记录。

## 题目指定场景如何复现

`python3 demo.py` 会依次验证以下全部情节；每个 `✓` 都是断言。

1. **开约与申报**
   - 甲 A、乙 B、丙 C 的 `seq=1,2,3`；
   - 甲申报整数日序 D1/D4，乙 D2/D5，丙 D3；
   - 公历日期设为 D1=`2026-11-30`、D2=`2026-12-01`、D3=`2026-12-02`，用于跨月查询；
   - 各格声明允许摊类，`A1-A2`、`B1-B2`、`C1-C2` 初始相邻。
2. **甲 D1 同场报开**
   - V-WEN 文墨书摊、V-GU 骨青修伞都在甲、乙等乡登记；
   - 签发 A1/A2 摆位单，单上有地点和邻摊；
   - 两张回单 `RCPT-A-BOOK`、`RCPT-A-UMB` 报开后，第 1 轮 effective usage 归 A。
3. **乙 D2 相邻双摊**
   - `call_next(from_day=2)` 返回 `round=2, B, day=2`；
   - `GRP-B-PAIR` 受理明确要求相邻的双摊；
   - `issue_group_permits` 校验 B1/B2 相邻，签发两张互为邻摊的摆位单。难办的相邻双摊就在第 2 日，不推到最后日期。
4. **甲修伞回执改撤摊**
   - `POST /api/receipts/RCPT-A-UMB/withdraw`；
   - 原 R1/A usage 变 `reversed`，欠轮写为 `owed`；
   - `call_next(from_day=2)` 返回 `R1/A/makeup/D4`；
   - 乙即便报开，结算也被 `PREDECESSOR_NOT_COMPLETE` 阻止；
   - 场地、双摊组、申请和原摆位单均保留。
5. **恢复甲 + 乙墙隔**
   - 在独立故障库中先撤再恢复 `RCPT-A-UMB`，产生新的 R1/A effective usage；
   - `POST /api/spots/separate` 将 B1/B2 改为墙隔开；
   - `venue_readiness(B,D2)` 仍为 `qualified_to_host=true`，但 `adjacent_required_pair_available=false`；
   - 对原组指定 `["B1","B2"]` 签发时返回 `ADJACENCY_REQUIRED`，且不产生任何摆位单或占格。
6. **重复回单、跨月、重启**
   - 同一回单跨月重放为 duplicate；
   - `/api/street?month=2026-11` 与 `month=2026-12` 返回不同墟日；
   - 重新 `LunshiService(same_db_path)` 等同重启，effective rounds 仍为 R1=A、R2=B、R3=C。

## 主要 HTTP 接口

| 角色 | 方法与路径 | 用途 |
|---|---|---|
| 村委 | `POST /api/admin/seed-standard` | 写入标准三乡世界 |
| 村委 | `POST /api/townships` / `POST /api/sessions` / `POST /api/spots` | 申报乡、日子、格位与摊类 |
| 村委 | `POST /api/spots/link` / `POST /api/spots/separate` | 相邻成组或砌墙隔开 |
| 村委 | `POST /api/conventions` | 开局约定轮序和办成条件 |
| 村委 | `POST /api/calls/next` | 按有效轮次/欠轮召集下一乡 |
| 村委 | `POST /api/calls/{id}/settle` | 按真实 open 回单核算 |
| 村委 | `POST /api/sessions/rain-out` | 雨撤并把 open call 记为欠轮 |
| 村委 | `POST /api/loans` | 许诺借地，防止同格同日许两次 |
| 村委 | `POST /api/transfers` | 转场、迁移原回单和摆位单 |
| 街坊 | `GET /api/street/today` | 当前日去哪赶集 |
| 街坊 | `GET /api/street?day_no=2` / `?month=2026-12` | 按整数日序、公历日或月份查 |
| 摊主 | `POST /api/vendors` | 登记摊主和可赶集的乡 |
| 摊主 | `POST /api/applications` | 报名单摊 |
| 摊主 | `POST /api/groups/adjacent-pair` | 受理相邻双摊 |
| 摊主 | `POST /api/groups/{id}/issue` | 校验相邻并开连摊摆位单 |
| 摊主 | `GET /api/vendors/{id}` | 我的报名、摆位单、回单 |
| 摊主 | `POST /api/receipts` | 凭摆位单号到场报开 |
| 摊主/村委 | `POST /api/receipts/{no}/withdraw` · `/restore` | 撤摊/恢复并重算轮次 |
| 审计 | `GET /api/ledger?convention_id=LUN-SANXIANG` | effective、冲正、欠轮、召集全账 |
| 审计 | `GET /api/audit?limit=200` | 操作事件流 |

重复 POST 可带请求头 `Idempotency-Key: ...`；回单本身的 `receipt_no` 也天然幂等。

### 命令行 API 小例

```bash
curl -s http://127.0.0.1:8765/api/street/today | python3 -m json.tool
curl -s 'http://127.0.0.1:8765/api/ledger?convention_id=LUN-SANXIANG' | python3 -m json.tool

curl -s -X POST http://127.0.0.1:8765/api/receipts/RCPT-A-UMB/withdraw \
  -H 'Content-Type: application/json' \
  -d '{"reason":"雨撤"}' | python3 -m json.tool
```

## 代码结构

```
lunshi/
  service.py    # SQLite schema + 领域服务/不变量
  server.py     # 标准库 ThreadingHTTPServer、JSON API、静态网页
  scenario.py   # 题目标准甲乙丙世界
  web/          # 三个角色的原生 HTML/JS 页面
demo.py         # 端到端本地演示与断言
selftest.py     # 领域不变量 + HTTP 冒烟测试
data/           # SQLite 数据库位置
```

## 设计说明：什么才算“真办成”

默认开局约 `LUN-SANXIANG` 的规则是：

```json
{
  "same_day": true,
  "same_slot": true,
  "distinct_vendors": true,
  "required_categories": ["book", "umbrella"]
}
```

因此，只有同一天同一时段内，书摊和修伞摊分别由不同摊主持有 `status='open'` 的真实回单，才会形成 effective round usage。报名、受理、摆位单、场地借出或口头“已办成”都不算数；回单撤摊后，系统会重新核算并冲正轮次账。
