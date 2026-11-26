# 三人工作室网盘交接后台

一个只依赖 Python 标准库的本机服务，用 SQLite 持久化目录、文件主人、额度、
授权、报价单和交接台账。

本实现选择题目中的 **整盘报价** 产品：

1. 先创建一张有交接号、失效时刻、文件版本清单的完整报价单；
2. 接收者在报价有效期内调用确认；
3. **确认事务提交的一刻**才是所有权和空间占用换主人的边界；
4. 确认接口按交接号幂等，断线重试不会把同一份文件扣两次；
5. 报价后若文件被编辑或主人变化，旧单不会偷偷交接新版本，而是返回
   `quote_stale`，再用一张新单按当前状态办完。

## 启动

```bash
./start.sh
# 或：npm start
# 或显式指定：
python3 server.py --host 127.0.0.1 --port 8080 --db ./data/diskhandover.db
```

健康检查：

```bash
curl -sS http://127.0.0.1:8080/health
```

一键演示（会自动使用 18080 端口和临时数据库，并演示重启后查询）：

```bash
./scripts/demo.sh
```

自动化不变量测试：

```bash
python3 -m unittest test_server -v
```

---

## 核心领域规则

### 1. 入口是图，物理文件只算一次

- 文件夹、文件入口、快捷方式/别名都放在 `entries` 表。
- 多个入口可以引用同一个 `files.id`。
- 快捷方式可以指向文件，也可以指回祖先文件夹。
- 扫描时按入口图遍历，并用 `visited` 集合防止返回祖先造成死循环。
- 报价按物理 `file_id` 去重；别名不会重复占空间。
- 两份独立文件即使大小相同，也是两行 `files`，各占各的格数。
- “返回祖先目录”的入口会被真实跟随：祖先目录中的其他可达文件也会进入清单并按主人/许可分类；
  `visited` 只负责防止目录环导致死循环，不会把同一物理文件错误合并。

### 2. 接手项目夹时的四类文件

从 `root_entry_id` 实际遍历到的物理文件会被分类：

| 分类 | 条件 | 是否转移/占乙额度 |
|---|---|---:|
| `transfer` | 当前主人是交出人，且 `transferable=true` | 是 |
| `borrowed` | 第三人所有 | 否，只列作借阅 |
| `blocked` | 交出人所有但明确不允许交接 | 否 |
| `recipient_owned` | 本来就已经是接收者所有 | 否 |

### 3. 浏览、改内容、接用量是三件事

- `POST /v1/grants` 可分别授予 `can_read`、`can_write`。
- 可浏览/下载不代表可改；可改也不代表能发起交接或转移所有权。
- 交接必须由文件主作为 `from_account_id` 发起报价，并由报价指定的
  `to_account_id` 确认。
- 可传 `X-Account-Id: bing` 模拟身份；不传时视为本机受信调用，方便 curl 建盘。

### 4. 额度按当前实际主人实时求和

账号的 `used_cells` 等于其名下未删除物理文件大小之和。所有上传和交接确认都在
`BEGIN IMMEDIATE` 写事务内重新计算：

- 普通上传：`used + size > quota` 就拒绝，不插入文件。
- 交接确认：乙的剩余额度小于整单大小时拒绝，整单不转移任何文件。
- 两个并发写事务由 SQLite 写锁串行化，不会同时花掉同一格余额。

### 5. 报价后的编辑与并发上传

报价单保存每个文件的 `quoted_version`、`quoted_owner_id` 和 `size_cells`。
确认时执行 compare-and-check：

- 版本变了：`version_changed`；
- 主人变成无关第三人：`ownership_changed`；
- 主人已通过另一张单变成同一接收者：不重复转移/扣费，只把本单对应行结清；
- 大小/交接许可变了：`size_changed` / `transfer_revoked`。

任一变化都会使旧单成为 `stale`，并记录事件；请重新报价。这样甲在签出前仍可继续
编辑，但服务不会把未报价的新版本静默交出去。

报价时不预占乙的空间。若乙的其他上传先提交并占用了空间，确认时会返回
`quota_exceeded` 和占用文件清单；乙删除/移走文件或提高额度后，未过期的原单可重试，
也可以另开新单办完。

---

## 调用样例

下面的固定 ID 可直接复制。示例由重建接口生成：

- 甲 `jia`：额度 5 格，已用满；
- 乙 `yi`：额度 5 格，余额 5 格；
- 丙 `bing`：额度 5 格；
- 项目夹：`ent_project`；
- 三格稿：`file_draft_san` / `ent_draft_san`；
- 两格稿：`file_draft_er` / `ent_draft_er`；
- 返回盘根的快捷入口：`ent_back_to_root`。

### A. 重新建盘

```bash
curl -sS -X POST http://127.0.0.1:8080/v1/admin/rebuild \
  -H 'Content-Type: application/json' \
  -d '{"scenario":"standard"}'
```

可选场景：

- `{}`：清空成空盘；
- `"standard"`：三格独立稿 + 两格独立稿 + 返回入口；
- `"alias_draft"`：两格稿位置变成通往三格稿的别名；
- `"blocked_upload"`：标准两稿 + 乙已提前上传一格；
- `"borrowed"`：再加入丙的借阅件和甲的禁止交接件。

查看图遍历结果：

```bash
curl -sS 'http://127.0.0.1:8080/v1/scan?root_entry_id=ent_project'
```

### B. 报价

```bash
curl -sS -X POST http://127.0.0.1:8080/v1/handovers \
  -H 'Content-Type: application/json' \
  -d '{
    "id": "HO-DEMO-1",
    "from_account_id": "jia",
    "to_account_id": "yi",
    "root_entry_id": "ent_project",
    "ttl_seconds": 3600
  }'
```

返回中关注：

```json
{
  "handover_number": "HO-DEMO-1",
  "status": "quoted",
  "expires_at": "...",
  "totals": {
    "transfer_cells": 5,
    "transferable_unique_files": 2
  }
}
```

### C. 确认交接

```bash
curl -sS -X POST \
  http://127.0.0.1:8080/v1/handovers/HO-DEMO-1/confirm \
  -H 'Content-Type: application/json' \
  -d '{"account_id":"yi"}'
```

成功后：

- 三格稿、两格稿的 `actual_owner_id` 都变成 `yi`；
- 甲 `used_cells=0, free_cells=5`；
- 乙 `used_cells=5, free_cells=0`。

### D. 用普通上传入口验证新归属下的额度

甲可以再传一个 5 格文件：

```bash
curl -sS -X POST http://127.0.0.1:8080/v1/uploads \
  -H 'Content-Type: application/json' \
  -H 'X-Account-Id: jia' \
  -d '{"name":"甲用回五格","size_cells":5}'
```

乙连 1 格也不能再添：

```bash
curl -sS -X POST http://127.0.0.1:8080/v1/uploads \
  -H 'Content-Type: application/json' \
  -H 'X-Account-Id: yi' \
  -d '{"name":"乙多一格","size_cells":1}'
```

第二个调用返回 `409` / `quota_exceeded`。

重试上次确认是幂等回放：

```bash
curl -sS -X POST \
  http://127.0.0.1:8080/v1/handovers/HO-DEMO-1/confirm \
  -H 'Content-Type: application/json' \
  -d '{"account_id":"yi"}'
```

响应含 `"idempotent_replay": true`，不会再次转移或扣费。

### E. 查询交接单和每份文件的实际主人

```bash
curl -sS http://127.0.0.1:8080/v1/handovers/HO-DEMO-1
curl -sS http://127.0.0.1:8080/v1/handovers/HO-DEMO-1/ownership
```

断开客户端、甚至重启服务进程后，只要仍使用同一个 `--db` 文件，交接号和当前主人都在
SQLite 中：

```bash
# 停止 server 后重新执行 ./start.sh，然后：
curl -sS http://127.0.0.1:8080/v1/handovers/HO-DEMO-1/ownership
```

---

## 题目中的两个边界案例

### 别名盘：乙接完只占 3 格，余额 2 格；两张单不会扣两次

```bash
curl -sS -X POST http://127.0.0.1:8080/v1/admin/rebuild \
  -H 'Content-Type: application/json' \
  -d '{"scenario":"alias_draft"}'

for HID in HO-ALIAS-1 HO-ALIAS-2; do
  curl -sS -X POST http://127.0.0.1:8080/v1/handovers \
    -H 'Content-Type: application/json' \
    -d "{\"id\":\"$HID\",\"from_account_id\":\"jia\",\"to_account_id\":\"yi\",\"root_entry_id\":\"ent_project\"}"
done

curl -sS -X POST http://127.0.0.1:8080/v1/handovers/HO-ALIAS-1/confirm \
  -H 'Content-Type: application/json' -d '{"account_id":"yi"}'
```

扫描结果只有一个物理文件、两条路径；第一张单成功后乙用 3 格、余 2 格。
再确认 `HO-ALIAS-2` 也可以结清，但服务发现同一文件已经归本单接收者所有，因此
`transferred_file_ids=[]`、不写第二次 ownership ledger、不再扣容量。若第二张单的
接收者是另一个人，或文件版本已变，才会返回 `quote_stale`。报价后另开的新单则会把
该文件分类为 `recipient_owned`，同样可以作为 0 格尾单结清。

### 乙提前占 1 格：整盘接不下；处理空间后用新单办完

```bash
curl -sS -X POST http://127.0.0.1:8080/v1/admin/rebuild \
  -H 'Content-Type: application/json' \
  -d '{"scenario":"blocked_upload"}'

curl -sS -X POST http://127.0.0.1:8080/v1/handovers \
  -H 'Content-Type: application/json' \
  -d '{"id":"HO-FAIL","from_account_id":"jia","to_account_id":"yi","root_entry_id":"ent_project"}'

curl -sS -X POST http://127.0.0.1:8080/v1/handovers/HO-FAIL/confirm \
  -H 'Content-Type: application/json' -d '{"account_id":"yi"}'
# 409 quota_exceeded：需要 5 格，只有 4 格；未转移任何文件。

curl -sS -X DELETE http://127.0.0.1:8080/v1/files/file_yi_blocker \
  -H 'X-Account-Id: yi'

curl -sS -X POST http://127.0.0.1:8080/v1/handovers \
  -H 'Content-Type: application/json' \
  -d '{"id":"HO-NEW","from_account_id":"jia","to_account_id":"yi","root_entry_id":"ent_project"}'

curl -sS -X POST http://127.0.0.1:8080/v1/handovers/HO-NEW/confirm \
  -H 'Content-Type: application/json' -d '{"account_id":"yi"}'
```

---

## 权限与借阅样例

重建 `borrowed` 场景后，报价会把丙的 4 格文件放进 `borrowed_items`，把甲所有但
`transferable=false` 的 1 格文件放进 `blocked_items`；二者都不计入交接容量。报价被
接收者确认后，系统会给接收者补一条丙文件的只读 grant，因此“列为借阅”也能实际浏览，
但不能改、不占乙的空间。

单独授予浏览：

```bash
curl -sS -X POST http://127.0.0.1:8080/v1/grants \
  -H 'Content-Type: application/json' \
  -d '{"account_id":"bing","file_id":"file_draft_san","can_read":true}'

curl -sS 'http://127.0.0.1:8080/v1/files/file_draft_san/content?as=bing'
```

只读身份改稿会被拒绝：

```bash
curl -sS -X POST http://127.0.0.1:8080/v1/files/file_draft_san/versions \
  -H 'Content-Type: application/json' \
  -H 'X-Account-Id: bing' \
  -d '{"content":"try edit"}'
```

即使随后授予写权限，丙仍不能以甲的主人身份发起交接报价。

## 从零建盘的通用接口

```bash
curl -sS -X POST http://127.0.0.1:8080/v1/accounts \
  -d '{"id":"jia","name":"甲","quota_cells":5}'
curl -sS -X POST http://127.0.0.1:8080/v1/folders \
  -d '{"id":"ent_project","name":"项目夹"}'
curl -sS -X POST http://127.0.0.1:8080/v1/uploads \
  -d '{"owner_id":"jia","parent_entry_id":"ent_project","name":"稿","size_cells":3}'
curl -sS -X POST http://127.0.0.1:8080/v1/shortcuts \
  -d '{"parent_entry_id":"ent_project","name":"返回入口","target_entry_id":"ent_root"}'
```

## 主要状态码

- `201`：账号/文件/授权/报价已创建；
- `200`：确认、查询、修改、删除成功；
- `403`：身份不是主人/接收者，或缺少写权限；
- `404`：账号、入口、文件、交接号不存在；
- `409 / quota_exceeded`：额度不足；
- `409 / quote_stale`：报价后的版本、主人或许可已变化；
- `410 / quote_expired`：报价超过 `expires_at`。
