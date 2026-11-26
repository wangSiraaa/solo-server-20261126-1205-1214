# 本机社群论坛后台

零第三方依赖的 Python 标准库 + SQLite 服务。帖子有稳定 ID 与回复关系；移帖只改当前地址并记录去向；已读回单精确到“某张屏幕分页中真实出现的帖子版本”。

## 启动与一键复现

```bash
cd /workspace
PORT=8787 python3 server.py
# 或 npm start
```

另开终端运行六帖以内的完整网络复现。脚本会自己使用 `8799` 端口、临时重建 `demo-forum.db`，中途杀掉并重启服务：

```bash
./demo.sh
# 或 npm run demo
```

并发/同刻到达一致性检查：

```bash
python3 verify_concurrency.py
# 或 npm run test:concurrency
```

## 这个场景里旧的“全部读完”会漏什么

旧页面的 `page_token` 只记录该屏幕真实展示过的 `(post_id, displayed_version)`。因此旧页面晚来的“全部读完”不会确认：

- **丙 `p3`**：搬迁后才出现、从未展示的新回复；
- **甲帖 v2 的补充说明**：旧回单只有 `p1@v1`，当前是 `v2`，状态为 `updated_after_read`；
- **折叠的乙 `p2`**：折叠元数据不是屏幕正文，不写入 `page_items`；
- **没有翻到的后一页**：没有对应的持久化分页项和回单。

乙在“跟分枝”订阅中仍可追踪；若目的私组后来对读者不可读，接口只给申请入口，不返回私组标题和正文。对“只收本版”订阅，乙搬出后从本版待阅消失；丁/戊/己分枝搬入后，以搬迁事件序号作为本版到达位置出现。

## 主要实际网络调用

下面变量由登录接口取得；完整可执行版本见 [`demo.sh`](./demo.sh)。

```bash
BASE=http://127.0.0.1:8787

# 1) 登录
curl -sX POST $BASE/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"bob","password":"pass1234"}'

# 2) 建两个小组、主题和甲/乙两层回复
curl -sX POST $BASE/groups -H "Authorization: Bearer $MOD" -H 'Content-Type: application/json' \
  -d '{"id":"g_home","slug":"home","title":"老街拼单版","visibility":"public"}'
curl -sX POST $BASE/groups -H "Authorization: Bearer $MOD" -H 'Content-Type: application/json' \
  -d '{"id":"g_lab","slug":"lab","title":"私组","visibility":"private"}'

curl -sX POST $BASE/posts -H "Authorization: Bearer $ALICE" -H 'Content-Type: application/json' \
  -d '{"id":"p1","topic_id":"t_home","body":"甲 v1"}'
curl -sX POST $BASE/posts -H "Authorization: Bearer $ALICE" -H 'Content-Type: application/json' \
  -d '{"id":"p2","parent_id":"p1","body":"乙（默认折叠，不进阅读回单）"}'

# 3) Bob 的屏幕只展示甲；collapsed_replies 里有乙，但 page_token 只含 p1@v1
curl -s "$BASE/posts/p1/screen" -H "Authorization: Bearer $BOB"

curl -sX POST $BASE/read-receipts -H "Authorization: Bearer $BOB" \
  -H 'Content-Type: application/json' \
  -d '{"page_token":"pg_...","intent":"old_page_mark_all_read"}'

# 4) 两类订阅；可把先前真实回单作为 commitment_receipt_id
curl -sX POST $BASE/subscriptions -H "Authorization: Bearer $BOB" \
  -H 'Content-Type: application/json' \
  -d '{"id":"sub_branch","kind":"branch","anchor_post_id":"p1",
       "baseline_receipt_id":"rcp_..."}'
curl -sX POST $BASE/subscriptions -H "Authorization: Bearer $BOB" \
  -H 'Content-Type: application/json' \
  -d '{"id":"sub_board","kind":"board","group_id":"g_home",
       "baseline_mode":"all","baseline_receipt_id":"rcp_..."}'

# 5) 版主把乙和当时子树搬到另一私组/主题
curl -sX POST $BASE/posts/p2/move -H "Authorization: Bearer $MOD" \
  -H 'Content-Type: application/json' \
  -d '{"target_group_id":"g_lab","target_topic_id":"t_lab"}'

# 旧地址可查去向；无权看目的私组时不返回其标题
curl -s "$BASE/old-addresses/posts/p2" -H "Authorization: Bearer $BOB"

# 6) 旧页面拿 p2 当父帖发帖：即使带了旧 topic_id，也挂到 p2 当前真正所在的组/主题
curl -sX POST $BASE/posts -H "Authorization: Bearer $ALICE" \
  -H 'Content-Type: application/json' \
  -d '{"id":"p4","topic_id":"t_home","parent_id":"p2","body":"丁"}'

# 7) 翻阅；page_token 是本屏回单依据，next_cursor 可跨搬迁和重启
curl -s "$BASE/subscriptions/sub_board/pages?limit=2&cursor=cur_..." \
  -H "Authorization: Bearer $BOB"
curl -sX POST $BASE/read-receipts -H "Authorization: Bearer $BOB" \
  -H 'Content-Type: application/json' -d '{"page_token":"pg_..."}'

# 8) 登录用户的继续阅读位置，以及来自哪个订阅/回单依据
curl -s "$BASE/me/continue" -H "Authorization: Bearer $BOB"
```

## 接口速览

| 类别 | 接口 |
| --- | --- |
| 用户/登录 | `POST /users`, `POST /auth/login`, `GET /me` |
| 小组/成员/申请 | `POST /groups`, `PATCH /groups/{id}`, `GET /groups`, `POST /groups/{id}/members`, `DELETE /groups/{id}/members/{uid}`, `POST /groups/{id}/access-requests`, `POST /access-requests/{id}/approve` |
| 主题/帖子 | `POST /topics`, `GET /topics/{id}`, `POST /posts`, `GET /posts/{id}`, `PATCH /posts/{id}`, `GET /posts/{id}/screen` |
| 搬迁/旧地址 | `POST /posts/{id}/move`, `GET /old-addresses/posts/{id}` |
| 订阅/翻阅 | `POST /subscriptions`, `GET /subscriptions`, `GET /subscriptions/{id}`, `GET /subscriptions/{id}/pages`, `GET /subscriptions/{id}/unread`, `GET /me/continue` |
| 阅读回单 | `POST /read-receipts`, `GET /read-receipts/{id}` |

## 一致依据

1. **全局单调事件序号 + SQLite `BEGIN IMMEDIATE` 事务**  
   创建、编辑、搬迁、回单都写 `events.seq`。同刻到达的编辑和搬迁由写事务串行化；演示中的并发测试验证二者都提交且不丢正文。

2. **版本化回单**  
   `receipt_items(post_id, displayed_version)` 保存真实展示版本。已读 v1 后编辑到 v2，会得到 `updated_after_read`，而不是被旧回单吞掉。用包含 v2 的新屏幕回单后，未读数恰好减少。

3. **稳定帖子 ID 与逻辑父链**  
   搬迁不改帖子 ID/`parent_id`，只改当前 `group_id/topic_id`，并写 `moves`。旧地址因此永远能查去向；旧客户端回复 `parent_id=p2` 时，服务端按 p2 当前真实位置落帖。

4. **两种订阅采用不同归属规则**
   - `branch`：递归逻辑父链，身份跟帖跨组；私组无权时输出 `access_required` 遮罩。
   - `board`：只看 `posts.group_id = 当前版` 且 `board_arrival_seq > baseline_seq`。搬出立即消失；搬入事件刷新 `board_arrival_seq`，按本版承诺出现。

5. **持久化分页**  
   `page_tokens/page_items` 存在 SQLite，记录本屏真实帖子版本；`next_cursor` 编码稳定 `path` 或 `(board_arrival_seq,path)`。服务重启后旧游标和旧屏幕回单仍可用，跨搬迁不会跳过后续帖。

6. **读时计算，不做全站广播**  
   未读不是给所有用户复制标题，而是按“当前用户的订阅集合 → 当前帖子/ACL → 该用户的最高版本回单”查询。私组失权后，直接取帖返回 403，分页和订阅元数据均遮罩标题/正文。
