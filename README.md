# 宽带合作社：迁址与服务补时后台

一个仅依赖 Python 标准库 + SQLite 的本地虚构系统，处理宽带迁址、双接入点交叠、
SLA 补偿分钟、时券兑现、续期回执幂等，以及晚到证据引发的跨期更正和差额挂账。

经营方式选择 **可抵扣时券**：补偿案件确认应付分钟后发券；住户选择一笔可办理的
续期套餐抵扣 synthetic tariff。晚到证据改判时，未用分钟注销，已用于续期的分钟
按原兑换资费回到 **同一笔续期单** 挂账；已经履行的上网段保留，不做负数回滚。

## 1. 运行

```bash
./scripts/test.sh     # 6 个领域/幂等/持久化测试
./scripts/demo.sh     # 完整双客户端端到端演示，并重启服务两次
```

`demo.sh` 会重建：

- SQLite 数据库：`demo/coop-demo.sqlite3`
- 完整调用/响应/分钟级账据：`demo/demo-output.json`
- 服务日志：`demo/server.log`
- 文本演示记录：可手动保存 `PYTHONPATH=. python3 -m coop.demo | tee demo/demo-run.txt`

也可以启动固定端口服务后继续查询：

```bash
./scripts/serve.sh                     # 默认 127.0.0.1:8931
./scripts/client-1 health
./scripts/client-2 call get_customer_ledger \
  '{"customer_id":"C-MAIN"}'
./scripts/client-2 call trace_evidence \
  '{"evidence_id":"EV-MAIN-LATE-FULL"}'
```

两个 wrapper 分别是稳定的本地客户端身份 `terminal-1`、`terminal-2`。
也可以直接运行：

```bash
python3 -m coop.cli --client terminal-1 call list_renewal_options \
  '{"customer_id":"C-MAIN","voucher_id":"VCH-CASE-MAIN-BASE-V1"}'
```

## 2. 分钟判定规则

所有时间都是整数分钟，所有区间均为半开区间 `[start,end)`；第 `t` 分钟指 `[t,t+1)`。

对每个分钟 `t`：

1. 找出当时 **承诺可用** 的接入点：
   - 已实际开通：`opened_at <= t`；
   - 尚未实际归还：`returned_at IS NULL OR t < returned_at`；
   - 存在生效中的 `kind='promised'` 承诺区间；
   - `kind='test'` 的测试接入不计入承诺点，因此测试上线不会提前消灭旧屋权益。
2. 在承诺点中找出当时中断的点，中断事实来自 canonical `outages`，不是直接把
   故障报告条数相加。
3. 只要至少有一个承诺点，并且 **所有承诺点都中断**，该分钟产生一份补偿。
4. 同一线路的重复报告通过 `fault_key`/报告号幂等和区间并集去重；两个接入点同时
   中断也只产生一份分钟补偿。

核心实现在 `coop/engine.py`，并把每分钟的 promised/down/decision/reason 落到
`compensation_minute_rows`，可逐分钟追问。

## 3. 题目三个基线结果

主例时间线：

- 旧接入点：`[4,8)` 中断；`[0,9)` 承诺，第 9 分钟归还。
- 新接入点：`[6,7)` 中断；原始双点合同下 `[4,12)` 承诺。

| 情形 | 第 4、5 分钟 | 第 6 分钟 | 第 7 分钟 | 补偿 |
|---|---|---|---|---|
| 原始双点合同 | 旧断、新可用 | 两点均断 | 旧断、新已修复 | `[6,7)` = **1 分钟** |
| 新址 `[4,8)` 仅供测试 | 仅旧点被承诺且中断 | 同左 | 同左 | `[4,8)` = **4 分钟** |
| 晚到证据证明新址全程可用 | 旧断、新可用 | 旧断、新可用 | 旧断、新可用 | **0 分钟** |

测试-only 更正会把原承诺区间拆成 `[4,8)` test 与 `[8,12)` promised，旧行标记
`superseded` 但不删除。全程可用更正把原 outage 标记为 `void` 并关联证据，原报告
和旧事实仍保留可查。

## 4. 兑现、回执重到与重启

主演示中：

1. `CASE-MAIN` 第一版算出 1 分钟，发 `VCH-CASE-MAIN-BASE-V1`。
2. `list_renewal_options` 展示合成套餐；客户选择 30 分钟、1 synthetic coin/分钟：
   - 套餐费：`+30`
   - 时券抵扣 1 分钟：`-1`
   - 应收/回执支付：`29` / `-29`
   - 余额：`0`
   - 上网服务段：`[10,40)`
3. 同一 `receipt_id=RCPT-MAIN-2026-11` 再到：
   - `delivery_count` 增加；
   - 返回当前同一笔 `RNW-MAIN-001`；
   - 不新增抵扣、支付或服务段。
4. 杀掉并重启服务进程后，SQLite 中的案件、券、续期单、回执次数和账务仍可查询。

## 5. 已兑现后再次更正

晚到证据 `EV-MAIN-LATE-FULL` 与迟到修复/遥测复核表明新址第 6 分钟并未中断。
系统执行：

```text
evidence EV-MAIN-LATE-FULL
  -> compensation case CASE-MAIN version 2
  -> expected_minutes 1 -> 0
  -> voucher VCH-CASE-MAIN-BASE-V1 clawed_back_minutes = 1
  -> voucher_redemptions 指向 RNW-MAIN-001
  -> account_ledger 新增 voucher_clawback_charge +1
  -> notifications 通知 C-MAIN
```

该证据 **只改变使用该券的那一期 `RNW-MAIN-001`**，不会落到别的续期期。
若一券多笔兑换，追回顺序是：

1. 先注销券中尚未使用的分钟；
2. 不足部分按 FIFO 追溯 `voucher_redemptions`；
3. 按兑换时锁定的 `unit_amount_minor` 对原续期单补记差额。

演示在 `as_of_minute=12` 更正。续期段是 `[10,40)`，因此已履行区间为
`[10,12)` 共 2 分钟。账据 `account_ledger.detail_json` 与通知明确记录：

- 已履行的 2 分钟保留；
- 整个 `[10,40)` 服务段不删除、不重写成负数分钟；
- 已兑现的 1 分钟券值转换为 `+1` synthetic coin 应收挂账。

随后同一回执第三次重到，`delivery_count=3`，账务仍只有原始三条加一条追回挂账，
余额仍为 1；再用 synthetic payment `PAY-MAIN-CLAWBACK` 结清后余额为 0。
第二次重启后，用回执号仍可查到上述完整结果。

`trace_evidence` 是专门的追账入口，返回证据、补偿版本、券流水、账务分录、
受影响续期单和通知之间的完整链接。

## 6. 主要 RPC

所有方法通过 `POST /rpc/<method>` 调用，请求体为 JSON object；可用请求头
`X-Client-Id` 和 `Idempotency-Key`。

主数据/迁移：

- `create_customer`
- `create_line`（含 `opened_at`、`returned_at`）
- `add_commitment`（`promised` 或 `test`）
- `add_evidence`
- `correct_commitment_to_test`

故障事实：

- `record_fault_report`
- `record_repair`
- `correct_outage`

补偿：

- `run_compensation`
- `recalculate_compensation`
- `get_case`

续期/账务：

- `list_renewal_options`
- `receive_renewal_receipt`
- `get_renewal`
- `pay_outstanding_balance`
- `get_customer_ledger`
- `list_notifications`
- `trace_evidence`

## 7. 测试覆盖

`tests/test_coop.py` 覆盖：

- 双点合同只欠 `[6,7)` 一分钟；
- 新址四分钟测试-only 时升为四分钟；
- 新址全程可用时为零并注销未用券；
- 券兑现后更正，差额回到同一续期单；
- 同一续期回执重复到达不重复入账；
- 迟到修复记录缩短故障区间；
- 关闭/重连 SQLite 后的持久化查询。
