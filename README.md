# 付费弹幕履约系统（本机排期 + 回放，无视频流）

把屏幕简化为离散的**列**（1..W）、**轨道**（1..K）和**拍数**（beat，从 1 起）。
节目方卖的不是“收到一条消息”，而是买家按约定尺寸播放时**能完整看完一条**：
弹幕必须依次经过每一个可见列，途中不撞字幕、不撞别的弹幕，并且该屏真实
回传的播放进度确实滚过了最后一个可见拍，才算一次完整展示并结清。

## 领域约定

- 一条宽 `width`、速 `speed`（格/拍）的弹幕，若在 `start` 拍把最右格送到
  第 W 列入口，则第 `beat` 拍最右列为 `W - (beat-start)*speed`，占据连续
  `width` 格（见 `danmaku/model.py:build_trajectory`）。
- **完整展示**：恰好 W 个可见拍，第 W..1 列每个都经过（`trajectory_is_complete`）。
  速度导致跳列的参数组直接判不可行。
- 三类不可让的占用：不可遮盖字幕、已锁定的预售轨迹、已收到开播回条且未
  滚完的在途轨迹。两条弹幕同拍同轨同格即冲突，均不得计为完整展示。

## 两种开店方式（任选其一，节目初始化时确定）

1. **预售 `presale`**：开播前卖确定出场的票。排出完整路径立即占住，新单
   挤不掉；临时加/减字幕时按下单先后自动重排所有未实际播出的票，在途
   （已开播）指令物理不可动；合同被改坏的票置 `BROKEN`，`owed` 可直接
   查到具体买家。
2. **现场候播池 `pool`**：买家给窗口（允许换轨、稍晚开头）和购买次数。
   **只有收到该屏真实播放进度后**才派下一条；每次进度请求内循环派到
   再无可行路径为止，让来得及完整展示的单吃掉剩余位置；窗口过期的单
   标记 `EXPIRED`，欠账保留。

## 一次完整展示的生命周期

```
PENDING/QUEUED → LOCKED/DISPATCHED →（出件箱 claim）→ SENT
   → 开播回条 receipt → PLAYING → 进度滚过末可见拍 → DONE/SETTLED
                                  └→ 中途 stop → STOPPED（欠账保留，可续派）
```

- 指令真正播给请求方：进入该屏的**出件箱**，`claim` 才能取走。
- 开播回条按 `receipt_id` 幂等去重，**跨重启重传同一张只结一次**。
- 状态每次变更先追加 `events.jsonl` 再原子写 `state.json`；`replay`
  从空状态纯日志重放，断言与磁盘态逐字段一致。

## 目录

```
danmaku/
  model.py    格点/轨迹/遮挡/订单/指令模型与完整展示判定
  store.py    JSONL 事件日志 + state.json 持久化、重放比对
  engine.py   占用索引、路径搜索、预售占座与字幕重排、候播派单、
              回条幂等、停播、欠账查询、事件重放
  player.py   请求方播放器（领指令、回条、真实进度、停播）与文本回放
  scenario.py 题目点名的四个端到端故事（含断言）
  cli.py      本机命令行
tests/        14 个单元测试
```

## 跑通

```bash
python3 -m unittest tests.test_engine -v   # 单元测试
python3 -m danmaku.scenario                # 四个故事 + 逐拍屏幕回放
```

故事对应关系（`danmaku/scenario.py`）：

| 故事 | 验收点 |
| --- | --- |
| A1 | 1 轨 3 位，宽 1 速 1 连续三拍占 3/2/1 列，照播三拍后结清，欠账为空 |
| A2 | 只加中拍字幕遮第 2 列 → 原约无法交完、买家可查；撤字幕自动重排恢复 |
| A3 | 老指令已发屏并收到开播回条、在途占住唯一三拍 → 预售/候播都不许相撞轨迹 |
| B  | 两轨候播：后到单靠挪轨办成；中途停播后欠账保留并续派；重启后重传回条幂等；日志重放 == 磁盘态 |

## 命令行示例

```bash
D=/tmp/show
python3 -m danmaku.cli init --dir $D --show-id s --screen-id scr \
    --width 3 --tracks 2 --mode pool
python3 -m danmaku.cli buy --dir $D --order-id O1 --buyer Alice \
    --width 1 --speed 1 --window-start 1 --window-end 9 --quantity 2
python3 -m danmaku.cli progress --dir $D --beat 0      # 开播前调度拍派单
python3 -m danmaku.cli claim --dir $D                  # 请求方取指令
python3 -m danmaku.cli receipt --dir $D --receipt-id R1 \
    --instr-id I-0001 --beat 1                         # 开播回条
python3 -m danmaku.cli progress --dir $D --beat 3      # 真实进度：结清/续派
python3 -m danmaku.cli stop --dir $D --instr-id I-0001 --beat 2
python3 -m danmaku.cli owed --dir $D                   # 还欠谁几个完整展示
python3 -m danmaku.cli frame --dir $D --beat 2         # 单拍快照
python3 -m danmaku.cli replay --dir $D                 # 日志重放一致性核对
python3 -m danmaku.cli replay --dir $D --text          # 逐拍文本回放
```

只依赖 Python 3.11 标准库。
