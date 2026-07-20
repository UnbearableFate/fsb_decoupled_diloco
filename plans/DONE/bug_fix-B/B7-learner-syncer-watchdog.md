# B7：learner 对 syncer 死亡的对称 liveness 兜底

## 1. 元信息

- 来源：review B7（中）。`stop_requested`（learner.py:324-334）在 `global_only` 下只认 stop.json；`local_or_global` + `max_local_steps=null` 同样只有 stop.json 一条退出路径。若 syncer 被 SIGKILL/OOM 而未走到 `publish_stop`，全部 learner 将无告警地训练并发布无人消费的 proposal，直到 PBS walltime 强杀。syncer 侧有 `no_progress_timeout_seconds` 自保，learner 侧没有对称机制。
- 性质：**语义新增**（新的 learner 自主退出路径 + 新 stop reason）。正常路径行为不变。
- 影响文件：`fs_diloco/runtime/learner.py`（full 与 fragment 两个主循环）、`fs_diloco/core/config.py`（如新增独立阈值字段）、测试。
- 前置依赖：无。与 plan 01 §3.6 "learner 独立生命周期"方向一致。

## 2. 规格：watchdog 语义

**信号定义**——learner 视角"syncer 活着"的证据，任一发生即刷新 watchdog（记 monotonic 时间）：

- `latest.json`（或 fragment latest）版本号前进（通常复用现有 poll 结果；只有 deadline 首次到达时额外读一次固定 latest pointer 作触发前确认，避免“syncer 已前进但当前策略尚未 poll”的误报）；
- stop.json 出现（直接走正常停止，不经 watchdog）。

**触发条件**：自上次信号起经过 `threshold` 秒且 stop.json 不存在。新增 `liveness.syncer_unresponsive_timeout_seconds: float | None = None`；null 时沿用 `liveness.no_progress_timeout_seconds`，显式值必须 >0。独立字段是确定规格而非留给实施阶段的可选决策，以便测试小阈值且不改变 syncer 自保阈值。

**触发动作**（有序退出，不是 crash）：

1. 记录 `syncer_unresponsive` 事件（含最后信号时刻、当时 global version、local_step）；
2. 写最终心跳 `stopped`，stop reason 记为 `syncer_unresponsive`；
3. 进程正常退出（exit code 0——这是受控自保，launcher 不应把它当作 learner 崩溃重试；异常性由事件与 summary 承载）。

**边界**：

- watchdog 自首次成功 latest 加载后启动；启动前的等待由现有 `wait_for_json(paths.latest_json, timeout_seconds=1800)` 管辖，不重复计时；
- 训练结束阶段（learner 已进入自己的停止流程）不触发；
- fragment learner 同样接入（信号源为 fragment latest 指针），两个主循环共用同一 watchdog 实现——若 S3/S1 已重构，接入点按其结构调整并在 progress 记录。
- 当前 learner 没有固定“poll 周期”：watchdog 在每个 optimizer step 后以及现有 latest/stop 检查点判定；deadline 到达时先重读一次 latest 再决定。因此退出上界是 `threshold + 一次最长本地 step/现有等待点延迟`，集成测试使用短 step 场景验证；不得把它错误写成 `threshold + scan_interval`。若未来需要与单步时长无关的严格秒级上界，应另行引入定时线程/中断机制。

## 3. 目标与完成谓词

1. syncer 死亡场景下 learner 在 `threshold + 一个测试场景本地 step` 内退出，产生 §2 的三项动作（SWD-02/03）;
2. 正常 run（tiny full + tiny fragment）事件轨迹除新增 watchdog 初始化事件外与基线等价——正常路径无行为变化（SWD-04）；
3. `latest.json` 持续前进但间隔接近阈值时不误触发（SWD-05）；
4. 全量 pytest 通过。

## 4. 范围与非目标

- **范围内**：watchdog、事件、stop reason、（可选）新配置字段、full+fragment 两循环接入。
- **非目标**：
  - syncer 崩溃后的**恢复**（learner 等待新 syncer resume 后继续）——现架构 learner 退出即可，resume 语义归 plan 01 既有设计；
  - launcher/PBS 对 `syncer_unresponsive` 的编排反应（如提前结束整个作业）——记录为 follow-up，属于脚本层；
  - B8（syncer 侧 shutdown 等待超时）——独立计划。

## 5. Loop Engineering 实施循环

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | HARDEN、CHECK、PERSIST |
| --- | --- | --- | --- |
| L0 语义冻结 | 敲定阈值字段方案与信号清单（§2）；确认两主循环现有 poll 点足以承载信号刷新（不新增 I/O） | 无实现 | 决策记录 progress.md；基线 commit |
| L1 watchdog 单元 | SWD-01/05 先 RED：信号刷新、超时判定、边界（启动前/停止中不触发） | watchdog 实现（纯逻辑对象，时钟可注入） | 单元测试全绿 |
| L2 主循环接入 | SWD-02 先 RED：模拟 syncer 静默（tiny run 中途 kill syncer 或以假 run 目录静置 latest.json），期望 learner 按 §2 退出 | full 循环接入；fragment 循环接入 | SWD-02/03 GREEN；退出目录人工复核（心跳、事件、无半写文件） |
| L3 无回归验证 | — | — | SWD-04 轨迹等价（tiny full + tiny fragment，小阈值不设）；全量 pytest；证据归档 |

## 6. 测试矩阵与通过条件

| ID | 测试 | 通过条件 |
| --- | --- | --- |
| SWD-01 | watchdog 单元 | 注入时钟下：信号刷新重置计时；超阈值且无 stop.json → 触发；stop.json 存在 → 永不触发 |
| SWD-02 | syncer 静默集成 | kill syncer 后 learner 在阈值 + 一个实测本地 step 延迟内退出，exit code 0 |
| SWD-03 | 退出证据 | `syncer_unresponsive` 事件字段完整；最终心跳 stopped 且 reason 正确 |
| SWD-04 | 正常路径无回归 | tiny full/fragment run 轨迹与基线等价（允许新增的初始化事件在 profile 中显式列出） |
| SWD-05 | 抗误报 | latest 以略小于阈值的间隔前进 → 不触发 |
| SWD-06 | fragment 接入 | SWD-02 在 fragment 模式重复 |

progress.md 每条记录必须列出覆盖的 SWD ID（P8）。

## 7. 验证阶梯

1. **登录节点**：lint、`git diff --check`。
2. **1 节点 compute**：SWD 单元 → 全量 pytest → SWD-02/06 集成（小阈值配置）→ SWD-04 轨迹等价。
3. 2/9 节点：不需要。真实 9 节点上该路径只在故障时走到；下一次长作业的 run_analysis 应确认无 `syncer_unresponsive` 误报（作为被动观察项记入交接清单，不是本计划门禁）。

## 8. 报告、证据与 Checker

报告目录 `reports/imp_plans/bug_fix-B/B7/`。SWD-02 的证据必须含：kill 时刻、learner 退出时刻、事件日志、最终心跳内容、run 目录终态列表（经验文档 §5：不得只看退出码）。`check_plan01_invariants.py` 的 stop-reason 合法值清单若存在，需加入 `syncer_unresponsive`。

## 9. 停止与升级规则

按 AGENTS.md 三连败升级。若 SWD-04 暴露正常路径行为差异（watchdog 引入了额外 I/O 或改变 poll 时序），视为设计缺陷回到 L0，不得靠放宽轨迹 profile 通过。

## 10. 文档同步

- docs 的 learner 生命周期/停止语义章节新增 `syncer_unresponsive` 路径与阈值配置；
- review 报告 B7 条目标注完成 commit；launcher 编排反应作为显式 follow-up 记入 progress。
