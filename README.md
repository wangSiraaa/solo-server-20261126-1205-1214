# 沿河声景本地导览后台

面向录音志愿者、站点编辑与游客的**纯本地**声景导览后台，无外部依赖（Python 3.11 标准库），
不采集真实声音，只用四站、十声段的标记文件演示全部规则。

## 营业服务的选择：离线声景包借出

题目给了两种服务，本实现选 **出发站借出离线声景包** 对客营业：

- 签出时一次性校验：路线全部相邻通路、各站开放日、播放方式（外放/耳机）、
  可听日期与**借用期限**；
- 只装入许可证能承诺到**归还日**的声部——收听日与归还日双重校验，
  不允许赌“路中补授权”；
- 签出后包是不可变快照。当事人撤回同意、站点改道都**不远程改动已借出的包**
  （离线环境没有这条信道），新状态只约束此后的新签出与重领；
- 同一游客对同一请求重领同一包，按内容指纹返回**原发放号**，不产生新片段账。

另一种“按到达站实时发短片”的服务依赖联网，其“撤回后未签出片按当时真实路径处理”
的语义在本系统中对应为：撤回/改道后新签出立即失败或改配（见场景 B、C）。

## 领域模型（`data/markers.json`）

| 概念 | 说明 |
| --- | --- |
| Station 站点 | T1 望江台、T2 河湾（拐弯处）、T3 柳岸、T4 古渡；各有开放日与必讲旁白位 |
| Route 路线 | R1 短路 T1→T2→T3、R2 全线四站、R3 古渡折返 |
| Segment 声段（10 个） | 挂在某条轨 track 上，含 narration/music/ambience，带时码与权利人 |
| Leak 漏入 | **S04 河湾音乐轨 MIC-MUSIC 在 2–4 秒漏入人声 P**。漏入随源轨播放，单独静音 MIC-P 消不掉 |
| License 许可 | 按当事人 × 站点 × 开放日 × 播放方式（speaker/headphone）× 有效期授予；`*` 为通配 |

关键许可差异：R（柳岸旁白）**只授权 headphone**，广场外放签不出含 T3 的路线；
P 在 T1、T2 的许可拆成两张，可单独撤回河湾那张。

## 消音规则（`soundguide/rendering.py`）

给定静音轨集合渲染游客实际听到的人声时码：

- 静音某轨 → 该轨直录与挂在该轨上的漏入都不播；
- 只静音 MIC-P：S05 直录消失，但 S04 的 2–4 秒漏入仍在（via=leak）；
- 静音 MIC-MUSIC：漏入消失，S05 直录仍在；
- 两轨都静音：P 完全不可闻，河湾必讲旁白须由备选 S06（Q）顶替。

## 规划器（`soundguide/planner.py`）

在三个自由度上穷举搜索，静音最少的可行方案优先：

1. 每站原声/备选旁白；
2. 游客或委托**允许**的静音集合内选哪些轨静音；
3. 总耳机时长预算。

每个实际可闻的人都必须有覆盖到归还日的许可；必讲旁白位不能由静音顶替。
无法兑现时逐站给出原因（不开放、许可缺失、漏入无法消除、预算超限、通路关闭、
委托指定原声不可得等）。

## 账目纪律（`soundguide/player.py`）

- 只认发放号快照里的站；路线外站点播放/回查一律按“走错路”拒绝且不落账；
- A 站播过的旁白不会记到 B 站——按 `(发放号, 站号)` 回查只返回本站真实收到的片段；
- 重复播放幂等，重领同一包返回原发放号。

## 场景验证

```bash
python -m soundguide.demo                      # 端到端断言演示（A-E 五个场景）
python -m unittest discover -s tests -v        # 16 条规则单元测试
```

演示覆盖题目要求的全部关键情形：

- **A 基准行程**：完整许可下 R1 短路签出，河湾交付 P 的原声 S05；小样明示仅静音
  MIC-P 仍听到 2–4 秒漏入；逐站模拟播放给出真实时码；
- **B 仅撤回 P 在河湾的许可**：原声委托无法兑现并指明原因；只允许静音 MIC-P 时
  明确报“漏入源轨不在允许集合”；允许静音 MIC-MUSIC 后改用备选 S06，重放 P 时码为空；
- **C 恢复许可、仅关闭 T1→T2 通路**：原路线签出被拒，恢复通路后可签；
- **D 回查与防串账**：走错路 T4 拒绝且无账；河湾 S05、柳岸 S08 各归各站；
  重复播放与重领幂等（同发放号、同指纹）；
- **E 离线语义与边界**：撤回后已借出的旧包仍按快照播放（不假定远程删除）；
  耳机预算超限显式报错；R 的耳机专属许可拦住外放；归还日超许可有效期拒签。

## CLI 用法

```bash
python -m soundguide.cli markers
python -m soundguide.cli licenses
python -m soundguide.cli plan     --visitor V1 --route R1 --mode headphone \
    --listen-day 2026-11-28 --loan-from 2026-11-26 --loan-to 2026-11-29 \
    [--mute MIC-MUSIC] [--budget 60] [--require-original T2]
python -m soundguide.cli checkout <同上参数>
python -m soundguide.cli walk <发放号>
python -m soundguide.cli play <发放号> <站号>
python -m soundguide.cli query <发放号> <站号>
python -m soundguide.cli timeline <发放号> <站号>
python -m soundguide.cli withdraw L-P-T2 | restore L-P-T2
python -m soundguide.cli close-edge T1 T2 --reason 施工 | reopen-edge T1 T2
```

运行态写入 `data/state.json`（许可撤回、通路关闭、借出账、播放账、发放序号），
标记文件 `data/markers.json` 保持为编辑维护的基础数据。
