# 街区云联欢 · 三乘三连线贴后台

一个只用 Python 标准库的本地后台：一条封存报号带、两张模拟终端卡、一个小奖箱。
不收钱、不接外部直播，服务只绑定 `127.0.0.1`。

本实现选择承办 **电子场（每卡一次换角机会）**。

## 先回答：换角前盖的章，能不能接到 2-5-7 斜线上？

要看章所在格是否仍是当前有效卡面上的同一格：

- **不能**把被换掉的旧角上的章接到斜线上。旧角在换角成功时失效，旧章保留为
  `voided` 审计记录，但不算有效章；已盖格不能搬到新角或其他位置。
- 被换下的号即使以后才由报号带揭出，也不能回旧角补章；后台会回
  `RETIRED_CORNER_NUMBER`。
- 如果换的是与主对角 `D1 = (0,0),(1,1),(2,2)` 无关的角，那么换角前盖在
  `2`、`5` 等未移动格上的章仍然有效。七号揭出后，只要当前有效版三格章号一致、
  受理/发奖窗开着、这条线尚未兑奖锁定，就可以喊 `D1`。
- 如果换掉的正是主对角端点角（例如 TL 的 `2` 或 BR 的 `7`），即使旧角上以前盖过章，
  第七号揭出时 `D1` 也不成立；必须等换入的新角号码揭出并在新角上盖章后才行。

`tests/test_bingo.py` 中同时锁定了“无关换角仍可连线”和“被换斜角不能凑线”两个方向。

## 一键验证

```bash
cd /workspace

# 领域规则测试 + 真实 HTTP 服务/并发/重启 E2E
./scripts/check.sh
```

也可以分开跑：

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -v
PYTHONPATH=. python3 -m bingo.e2e
```

E2E 会自动完成：

1. 阿甲的主对角 `2-5-7` 在第 3 拍喊线，领到 `STK-001`；
2. 阿乙换角、用旧版喊线被拒、给退役号补章被拒，随后用新版领走最后一枚 `STK-002`；
3. 关闭/重启后台后，仍能查出每枚贴发给谁、对应哪通呼叫和兑奖记录；
4. 七号延后场证明当时缺 `7`，窗关后即使线已盖妥也回 `WINDOW_CLOSED`；
5. 两人两终端屏障齐发争一枚 `LAST-001`，后台锁内只放行一笔；
6. 完场后读出封存报号带并校验 SHA-256，重播不增加 beat。

## 家里两台终端怎么操作

### 1. 启动本地后台

```bash
cd /workspace
./scripts/start-server.sh
# 等价于：
# python3 -m bingo serve --host 127.0.0.1 --port 8765 --state data/state.json
```

状态在每次变更后原子写入 `data/state.json`。重启仍用同一个 `--state` 文件即可。

### 2. 封存报号带并布置两张卡

另开一个普通终端：

```bash
cd /workspace
python3 -m bingo seed --scenario main
python3 -m bingo open-window --game main
```

`main` 场的报号带开头固定为：

```text
2, 5, 7, 1, 3, ...
```

卡 A 主对角为 `2,5,7`；卡 B 的 TR 角原为 `2`，可换给给定角块 `CB-3`（新号 `3`）。
奖箱只有 `STK-001`、`STK-002` 两枚真实编号贴。

### 3. 开两个模拟顾客终端

终端 A：

```bash
./scripts/terminal-a.sh
```

终端 B：

```bash
./scripts/terminal-b.sh
```

#### 终端 A 内

```text
reveal          # 第 1 拍揭 2
stamp 2
reveal          # 第 2 拍揭 5
stamp 5
reveal          # 第 3 拍揭 7
stamp 7
call D1
claim <输出中的 call id，例如 C-00004> 2468
```

阿甲会领到 `STK-001`，`D1` 随即在卡 aggregate 上锁定。

#### 终端 B 内

```text
stamp 2
stamp 5
swap TR CB-3
oldcall D2       # 故意拿 v1 旧版试，应收到 CARD_VERSION_STALE
stamp 2          # 退役旧号，应收到 RETIRED_CORNER_NUMBER
reveal           # 第 4 拍揭 1
stamp 1
reveal           # 第 5 拍揭 3
stamp 3
call D2
claim <输出中的 call id> 1357
stickers
```

阿乙在换角前已盖的中宫 `5` 留在原格并继续有效；被换走的 TR 旧角 `2` 章则作废。
阿乙用当前 `v2` 卡领到最后一枚 `STK-002`。

> 交互终端里的 `reveal/open/close` 是给值班员在同一本地终端代操作的命令；
> 正式使用时也可由第三个值班窗口执行 one-shot CLI。

## 窗口、同拍竞争和锁线制度

### 什么时候可以喊“连线/叫停”

- 建场时报号带已封存，后端只保存 SHA-256 承诺和不可由请求改号的顺序；
  `reveal.next` 不接受号码参数，只会弹出带下一号。也可先单独封存：
  `python3 -m bingo seal-tape --tape-id gala-1 --numbers 2,5,7,1,3,...`，
  随后建场传 `tape_id`。
- 值班员用 `window.open` 开放本窗后，呼叫才受理。
- 每通呼叫记录：
  - 后端接收顺序 `seq`；
  - 当前揭号拍 `beat`；
  - 当时已揭号码；
  - 卡版本；
  - 八线之一的格位/号码/章快照；
  - 终端号和可选的顾客端呼叫引用。
- 合格呼叫生成 `verified` 记录，并阻止继续揭号，直到值班员完成发奖或 `call.void`。
- 值班员宣布截止后 `window.close`。窗闭后的新呼叫或领奖均为 `WINDOW_CLOSED`；
  后来盖成的线可以在 `card.audit` 中显示 `complete: true`，但没有 redemption。

### 几人同拍争最后一枚贴

- HTTP 服务用 `ThreadingHTTPServer`，所有状态变更进入同一把 `RLock`，保存采用
  临时文件 + `os.replace` 原子替换。
- 同一 beat 的多通呼叫都保留后端顺序。
- 最后一枚贴在领奖事务中按锁内提交顺序发放：第一笔通过领奖码、窗口、锁线复查的
  事务取走盒中第一枚未发贴；第二笔得到 `STICKER_BOX_EMPTY`，详情写明最后一枚给了谁、
  对应哪笔 redemption，以及预定的同拍顺序规则。
- 未发到贴的合格呼叫状态为 `unfunded`，但不锁线；已发奖的线才锁。

### 已兑奖线如何锁

发奖成功时写入：

- `redemptions`：贴、呼叫、领奖手续、卡版本、线、beat、时间；
- `stickers[].status = given` 和持有人；
- `card.redeemed_lines[line_id] = redemption`。

线锁挂在卡的稳定逻辑线上，而不是只挂某个卡版本。因此同一条横/竖/斜线无论用旧版、
新版还是重复请求再喊，都会得到 `LINE_ALREADY_REDEEMED`。

## 掉线、重播和回窗核查

- `tape.replay` 只返回已有揭号事件并校验封存哈希；它不会增加 `beat`，所以
  “掉线重播不等于又开奖一次”。
- 重连后用 `state` 或 `card.audit` 同步当前有效卡版本、章和锁线。
- 断线时照号记下的号码，回窗后可盖为 `source=offline_note_backfill`；记录会保留
  实际补录 beat 和号码原揭号 beat。它不能把此前后端已驳回的呼叫倒填成合格。
- 对未得贴者，值班员可依次出示：
  - `calls.list` / `attempts.list`：指定喊线时刻的 beat、缺格、窗状态、旧版或锁线原因；
  - `card.audit`：当前版卡面、每一版历史、有效/作废章、八条线状态和 redemption；
  - `stickers.list`：贴的真实编号、持有人和 redemption；
  - `tape.replay`：封存顺序和完场重播。

## 常用 one-shot 命令

```bash
# 建场：main / late7 / race / swapdiag
python3 -m bingo seed --scenario late7 --game-id late7

# 窗口与揭号
python3 -m bingo open-window  --game late7
python3 -m bingo reveal       --game late7
python3 -m bingo close-window --game late7

# 盖章、换角、喊线、领奖
python3 -m bingo stamp --game main --card card-A --participant pA --number 7
python3 -m bingo swap  --game main --card card-B --participant pB \
  --position TR --block CB-3
python3 -m bingo call  --game main --card card-A --participant pA --line D1
python3 -m bingo claim --game main --call C-00004 --participant pA --code 2468

# 核查
python3 -m bingo audit    --game main --card card-A
python3 -m bingo calls     --game main
python3 -m bingo attempts  --game main
python3 -m bingo stickers  --game main
python3 -m bingo replay    --game main
python3 -m bingo finish    --game main
```

任意动作也可直接发 JSON：

```bash
python3 -m bingo do card.audit \
  --params '{"game_id":"main","card_id":"card-A"}'
```

## 代码结构

```text
bingo/core.py       # 纯领域规则、JSON 持久化、进程锁
bingo/server.py     # 127.0.0.1 JSON/HTTP 服务
bingo/client.py     # one-shot CLI 与交互式模拟终端
bingo/e2e.py        # 真实子进程、两终端、并发争贴、重启断言
bingo/scenarios.py  # 两张卡/小奖箱示范数据
tests/test_bingo.py # 领域规则测试
scripts/            # 启动 A/B 终端和一键检查脚本
data/state.json     # 运行后生成；已 gitignore
```
