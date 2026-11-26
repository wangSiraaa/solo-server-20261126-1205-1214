# 连载译刊 · 众筹开译服务

一个纯标准库（SQLite + `http.server`）实现的小型服务：认捐、撤回、开译、
读者查单。所有状态落在一个 SQLite 文件里，进程重启后按「期号」仍能查到
谁承担哪篇、哪笔钱扣了多少；同一个 `request_id` 重发不会二次扣款。

## 产品模型

- **期 (issue)**：`joint`（联合截单成刊）或 `rolling`（按篇滚动开译）。
- **篇 (piece)**：开译所需点数 `points_required`、工期 `duration`（连续档期数）、
  可接该篇的译者 `eligible`。`wish_points` 只是广告愿望额，**永远不是可扣的钱**。
- **译者档期**：`(translator, 档期标签)` 是**全局独占**资源，跨期号也只可被占用一次。
- **认捐 (pledge)**：支持者、点数、`accepts`（接受用于哪些篇，留空=任意篇）、
  `requires`（只有这些篇也开译才生效的条件）。支持者的每期上限
  `supporter_cap` 是对**整期合计**的上限，不是每篇各一份。
- **开译**：一次落定「篇 + 独占档期 + 可实际扣下的点数」。档期用精确回溯匹配，
  资金用最大流（支持者→认捐→篇）求解，两者同时满足才算可行。

两种开译方式：

- `open_joint`：**一个动作里**把选中篇目及其相互有条件的认捐一起兑现
  （互为前提的两笔认捐因此能成立）；截单前不对译者作任何承诺。
- `open_piece`：滚动开译，只用无条件、或前提**已经开出来**的钱落下一篇；
  抢到的档期立刻锁定，后续不许拿尚未开出的篇目作担保。

多个组合都可行时，服务抛 `NeedsEditorDecision` 并列出候选（每个候选都带
篇、译者、档期、每笔扣款），由编辑用 `candidate_id` 或篇目列表选定，选择
记进本期回执。4 线程并发重发同一请求只扣一次。

## 本地调用入口

CLI（每步打印 JSON）：

```bash
python3 -m journal.cli --db desk.db create-issue island --mode rolling
python3 -m journal.cli --db desk.db add-translator t1 --slots 周二
python3 -m journal.cli --db desk.db add-piece island island \
        --title 海岛 --points 6 --eligible t1 --wish 40
python3 -m journal.cli --db desk.db pledge island p1 alice --points 6 --accepts island
python3 -m journal.cli --db desk.db candidates island
python3 -m journal.cli --db desk.db open-piece island --request-id req1 --piece island
python3 -m journal.cli --db desk.db query island      # 重启后依旧可查
python3 -m journal.cli --db desk.db receipt rcpt_xxx
```

HTTP（同样的语义，无额外业务逻辑）：

```bash
python3 -m journal.http_server --db desk.db --port 8080
curl -XPOST localhost:8080/issues -d '{"issue_id":"island","mode":"rolling"}'
curl localhost:8080/issues/island
curl -XPOST localhost:8080/issues/island/open-piece -d '{"request_id":"r1","piece":"island"}'
```

## 验证

```bash
python3 -m unittest discover -s tests -v   # 16 个场景/并发测试
python3 scripts/demo.py                    # 逐场景演示（含冷重启）
```

`demo.py` 覆盖：单篇无条件开译并给出扣款/开工回执；条件版（另一篇无人可译）
无法开；恢复无条件但周二档期已被占无法开；互为前提的两笔认捐在联合截单里
同时兑现；冷重启后按篇查译者与扣款；三篇两译者需编辑取舍；五点上上限并非每篇各五点。

## 布局

- `journal/db.py` —— SQLite 存储、写事务串行化、每线程连接。
- `journal/engine.py` —— 档期回溯匹配 + 资金最大流 + 候选枚举。
- `journal/service.py` —— 服务 API：认捐/撤回/开译/查单/幂等。
- `journal/cli.py`、`journal/http_server.py` —— 本地入口。
- `tests/`、`scripts/demo.py` —— 验证。
