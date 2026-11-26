# 灯罩工坊 · 资格与落款后台 (Lampshade Workshop Backend)

一个只依赖 Python 3.8+ 标准库的实现（SQLite + HTTP），把“桌面纸模练习、后台授予花样制作与带教资格”的工坊规则落成可查询、可审计、可重启的数据模型。

```
lampshop/
  timeutil.py   时间/时长工具（统一 UTC，文本序 = 时间序）
  db.py         SQLite schema 与事务（BEGIN IMMEDIATE 串行化写入）
  errors.py     带稳定错误码的领域异常
  service.py    领域引擎：证据、师承、资格、落款、证期、审计
  api.py        JSON-over-HTTP 薄封装
  __main__.py   python -m lampshop 启动服务
tests/          35 个测试，逐条复现规格里的场景
demo.py         把叙述从头走一遍并打印追踪
run_tests.py    无需设置 PYTHONPATH 的测试入口
```

## 跑起来

```bash
python3 run_tests.py -v     # 全部测试
python3 demo.py             # 叙述式演示
python3 -m lampshop --db lampshop.db --port 8080
```

## 数据模型要点

- **报名册 ≠ 证据**：`register_evidence` 只在人到之前登记时段/工艺项/导师/学员/共同完成的步骤，状态 `registered`，**不构成证据**。只有 `complete_evidence` 产生 `completed` 的见习证据（“完工签认才产生见习证据”）。
- **资格从不倒推**：带教资格是一条显式、带有效期的记录（`qualifications`），绝不由“某人带过人”反推。一条见习只有在导师于**整段见习时段**都持有效带教资格时才算“有效监督”。
- **同台冲突**：同一人重叠时段出现在不同台位时，时长按**区间并集**计量（不把两边都算足），并可用 `disputed` 查受争议分钟。
- **不可抹除的更正**：撤回见习 = 追加 `retract` 事件并保留原 `complete` 事件与完成时间；“更正”不会被伪装成“从未发生过”。

## 选定的运营制度

规格要求**二选一**并决定师承合并的处理方式。本项目**默认 `endorsement`（随单背书制）**，同时完整实现 `periodic`（按期发证制）作为可切换的备选。

### 随单背书制（默认）

- 每签一件成品，沿**实际师承**从签收人一路走到仍有效的根（甲），逐环验证：
  - 见习证据在**签署时刻**仍可用（已完成、未被撤回、独立指导已被查实、且当时被有效监督）；
  - 该环导师**此刻**仍持有效带教资格。
- 旧证据被撤回后：**旧件已落款的依据快照不变**；**新件**无法再用那条断掉的师承背书（`NO_VALID_BASIS`）。
- **导师缺席的代签**：只有当代理人是**同一条见习上、确实指导过相关步骤、且此刻仍合格**的导师才可担保。做法是查“全工坊随便一个有证者”会失败。

### 按期发证制（备选）

- 开期时核**有效经历**与**已认证导师**，并对本期名单签发**带起止日期**的证书；沿途师承会在此刻被**重验到根**。
- 签署时只验证书与当班岗位，不再逐环走师承。
- 若事后发现旧证据有误：证书按本期 `review_policy` 处理——
  - `honor_through_period`：**已发出的证在有效期内照用**（只记复核决定，不吊销）；
  - `revoke_on_retraction`：**吊销**该证书，之后无法凭它落款。
- **下一期必须重验**：被撤回的依据使资格不再满足，新一期开期时不会续发。

无论哪种制度，**已落款的成品与它的依据快照永不被回溯性抹除**；制度变化只影响“新件能否落款 / 证书状态”。

### 多条师承汇到同一人

当多条有效师承汇聚到同一人时：

1. **全部保留**在依据快照里（`chains` 列表）；
2. 另选一条**主师承**，规则确定且可复现：**跳数最少 → 最靠根的那条证据完成时间最早 → 证据 id 最小**；
3. 该规则写在每份依据的 `lineage_policy` 字段里，店主可照着复算。

## 落款规则

`sign_product(product_id, signer_id, request_id, at_ts)`：

1. **同一件只有一个最终签收人**：`signatures.product_id` 在库层 `UNIQUE`；任何“已签后再签”的请求都返回**同一条**签名。
2. **Idempotency**：`request_id` 唯一。**服务重启后重送同一请求**，返回先前结果（`replayed: true`），不会产生第二位签收人；即便换个新 `request_id` 也只会得到原签名（`duplicate_request: true`）。
3. **权限**：签收人必须在**实际日期与时刻**处于**有签收权的当班岗位**（营业台按实际日期、工艺组合的授权和当班岗位决定）。把丙从签收台换到**无签收权的学徒台**，其先前资格仍可查询，但**当次不能落款**（`POST_NOT_AUTHORIZED`）。
4. 多人（老师与两名学员）可从**不同客户会话**发起，但一件成品只会落下**一个**最终签收人。

## 店主可追溯

`GET /product_basis/<成品号>`：

- `signature.basis` 是**当时实际采用的依据快照**（不可变）；
- `current_status_of_cited_evidence` 额外显示这些证据**现在**的状态（例如“该见习已被撤回”），但**不改写**旧快照。这就是“旧件 / 新件”对照的查询入口。

## 场景 ↔ 测试对照

| 规格要求 | 测试 |
| --- | --- |
| 甲→乙→丙 基线；丙当班首次独立落款 | `test_baseline.py` |
| 报名册不构成证据；完工才产生证据 | `test_baseline.py::test_registration_book_alone_is_not_evidence` |
| 同一人两台位→争议分钟；时长按并集 | `test_disputed.py` |
| 带教资格不能倒推；见习移到资格前→不可用 | `test_temporal.py` |
| 资格时段须覆盖整段见习 | `test_temporal.py::test_qualification_window_must_cover_whole_practicum` |
| 原时序只换岗位→资格可查但不能落款 | `test_post_assignment.py` |
| 旧见习撤回；旧件保留依据 / 新件不能复用 | `test_retraction.py` |
| 另一条独立指导后来才查实的例子 | `test_retraction.py::test_late_verified_independent_guidance_as_alternate_line` |
| 更正不是抹除（审计事件） | `test_retraction.py::test_retraction_is_an_event_not_erasure` |
| 导师缺席由“确实指导过相关步骤”的同见习导师担保 | `test_substitution.py` |
| 不能全工坊找有证者代签 | `test_substitution.py::test_shopwide_qualified_stranger_cannot_substitute` |
| 多条师承汇聚 | `test_substitution.py::test_converging_lineages_all_recorded` |
| 按期发证：开期核经历/导师、有效期、复核约定、下期重验 | `test_periodic.py` |
| 重启后重送不产生第二位签收人 | `test_idempotency.py`, `test_http.py` |
| 并发签名只得一位最终签收人 | `test_idempotency.py::test_concurrent_signers_yield_one_final_signer` |
| 多进程竞争同一文件仍只得一位签收人 | `test_idempotency.py::test_concurrent_processes_yield_one_final_signer` |
| 根导师（创始人）在按期制下凭根资格获证并落款 | `test_periodic.py::test_root_mentor_is_certified_by_root_authority` |

## HTTP API（动作名即路径）

写操作 `POST /<action>`，读操作 `GET`。返回统一为 `{"ok": true, ...}` 或 `{"ok": false, "error": <code>, ...}`。

```
POST /person            {id?, name, role?}
POST /craft             {id, name, min_minutes, required?}
POST /add_step          {craft_id, id, name, seq?, required?}
POST /product_type      {id, name, crafts:[...]}
POST /station           {id, name}
POST /post              {id, name, sign_authority}
POST /shift             {person_id, post_id, start_ts, end_ts}
POST /seed_root         {person_id, crafts:[...], at_ts}
POST /setting           {key, value}
POST /regime            {regime}            GET /regime
POST /evidence_register {craft_id, start_ts, end_ts, mentors[], students[], steps[],
                        station_id?, kind?, registered_at?, mentor_steps?, id?}
POST /evidence_complete {evidence_id, completed_at?}
POST /evidence_verify   {evidence_id, verified_at?}     # 独立指导需二次查实
POST /evidence_retract  {evidence_id, reason, at_ts?}
GET  /evidence/<id>?at_ts=...   GET /evidence?person_id=&craft_id=&at_ts=
POST /disputed          {person_id, at_ts?}
POST /eligibility       {person_id, craft_id, at_ts?}
POST /qualification     {person_id, craft_id, capacity, valid_from, valid_until?,
                        source_kind?, enforce_eligibility?}
POST /qualification_revoke {qualification_id, reason, at_ts?}
GET  /qualifications?person_id=&craft_id=&capacity=&at_ts=
POST /lineage           {person_id, craft_id, at_ts?}
POST /product           {id?, product_type_id, customer_session?}
GET  /product/<id>      GET /product_basis/<id>
POST /sign              {product_id, signer_id, request_id, at_ts?, customer_session?}
POST /period            {id?, start_date, end_date, review_policy?}
POST /period_open       {period_id, roster[], crafts[], issued_at?}
POST /period_review     {period_id, at_ts?}
GET  /certificates?person_id=&craft_id=&on_date=
GET  /audit/<entity_id>
GET  /health            POST /reset
```

## 时间约定

- 所有时间戳统一存为 `YYYY-MM-DDTHH:MM:SSZ`（UTC），因此按文本排序即按时间排序。
- 传日期（`YYYY-MM-DD`）时，起日按 `00:00:00`、止日按 `23:59:59`（UTC）展开，用于有效期边界。
