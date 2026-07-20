# B2：inner LR 调度语义解耦（累计步进度 + 独立 horizon + LR 下限）

## 1. 元信息

- 来源：review B2（高）/ Q1（高）与 `plans/todo/cosine_scheduler_decoupling.md`。三个叠加缺陷（learner.py:362-368 的 `lr_lambda`）：
  1. scheduler 进度用**重建后相对步数**：replace 模式每次 adoption 重建 optimizer+scheduler（learner.py:1642、1896 等共 7 处调用点），正式配置 `warmup_steps=100 == inner_steps=100` → learner 几乎全程在 warmup 锯齿（0.01×→1.0×base），配置的 cosine 从未生效；
  2. `max_local_steps=null` 时 warmup 后退化为常数 LR；
  3. `progress = min(1.0, step/max_local_steps)` 被 clamp 后 LR 乘子**恰好为 0**：`global_only` + preserve-state 下 learner 超过 horizon 的所有步在零学习率下训练且无告警——run_analysis 建议的下一个实验（rebase-preserve + global_only）正好命中。
- 性质：**语义变更（高影响）**。LR 轨迹全面改变；所有历史 loss 对照在本修复后作废，需要新基线（P6）。
- 影响文件：`fs_diloco/runtime/learner.py`（`build_inner_optimizer_and_scheduler` 及 7 处调用点，覆盖 full 与 fragment 两条路径）、`fs_diloco/core/config.py`、`configs/*.yaml`、测试。
- 前置依赖：无。**本计划阻塞所有后续质量消融**（review R1 第一项）；在它完成前不应再跑任何 reset/preserve 或策略质量对照。

## 2. 文献调研结论（设计依据）

对 DiLoCo 系工作中 inner LR 调度的处理做了调研，结论一致性很高：

1. **调度进度一律以累计步数索引，从不因同步/采纳事件重置**。原始 DiLoCo 与其 scaling laws 研究使用 1000 步 warmup + 覆盖**整个训练时长**的 cosine 衰减；inner AdamW 状态每副本本地持有、跨 round 不重置（同步它需 3× 通信而收益不显著）。
2. **异步/异构场景同样如此**：Async Local-SGD（DeepMind）为每个 data shard（=worker）配一条自己的 schedule，"linear warmup + cosine decay，T 为该 shard 的目标总迭代数"，t 跨 round 累计，warmup 只在训练早期发生一次；异构速度用 DyLU（按 worker 速度缩放每 round 步数 H）处理，而不是改 schedule 语义。
3. **horizon 未知/弹性时的工程实践是 WSD**：INTELLECT-1（生产级 DiLoCo，不可靠算力）采用 warmup→常数→最后约 20% cooldown 的 WSD 调度，理由正是"总训练量取决于算力贡献、事先不定"——与本系统 `global_only` 模式（learner 本地总步数未知、全局目标已知）的处境同构。
4. **Decoupled DiLoCo 论文本身未定义 inner scheduler 语义**（采纳时只重置 step/token 计数器，未提 scheduler）——本系统当前的缺陷正是这个规格空白的直接后果，必须由本计划自行定义。

来源：[DiLoCo](https://arxiv.org/abs/2311.08105)、[Scaling Laws for DiLoCo](https://arxiv.org/abs/2503.09799)、[Async Local-SGD](https://arxiv.org/abs/2401.09135)、[INTELLECT-1 技术报告](https://arxiv.org/abs/2412.01152)、[Decoupled DiLoCo](https://arxiv.org/html/2604.21428v1)、[OpenDiLoCo](https://www.primeintellect.ai/blog/opendiloco)、[Streaming DiLoCo](https://arxiv.org/abs/2501.18512)。

## 3. 设计规格

### 3.1 核心语义（对齐文献共识）

- **进度定义**：scheduler 进度 = **已完成的累计本地 optimizer step 数**（即现有 `local_step`），单调递增，与 adoption 事件无关。第 `k` 个 optimizer step（`k` 从 1 起）使用 `schedule(k-1)`；该步完成后 scheduler 进入 `schedule(k)`，供下一步使用。warmup 只在训练开始发生一次。这一约定消除 PyTorch `LambdaLR` 构造时会执行 initial step 带来的 off-by-one 歧义。
- **重建即恢复**：任何原因重建 optimizer/scheduler（replace 的 moments 重置、fragment adopt 重置）后，scheduler 进度**恢复到 `completed_local_steps=local_step`**，不归零。实现必须让重建后“下一 optimizer step 使用的 LR”与未重建基准逐点相等；不得直接把 `last_epoch=local_step` 传给会在构造期再前进一步的 scheduler。可使用 `initial_lr + last_epoch=local_step-1`，或不依赖 scheduler 内部 epoch 的显式累计步接口；SCH-02 只认结果。
- **cosine 定义**：令 `p=completed_local_steps`。`0 <= p < warmup_steps` 时乘子为 `(p+1)/warmup_steps`；其后令 `q=clamp((p-warmup_steps)/(scheduler_total_steps-warmup_steps), 0, 1)`，乘子为 `max(min_lr_ratio, 0.5*(1+cos(pi*q)))`。因此 warmup 最后一步与 cosine 起点都为 1，`p >= scheduler_total_steps` 时稳定在下限。`warmup_steps=0` 时第一个 optimizer step 使用 base LR。
- **由此实现关键解耦**：optimizer moments 的 reset/preserve 是策略语义，LR 日程与其彻底无关——消除 Q1 混杂因子，reset/preserve 消融自此可归因。

### 3.2 配置面

- 新增 `inner_optimizer.scheduler_total_steps: int | None = None`（cosine 时**必填**且必须 `> warmup_steps`，fail-closed，不再由运行时代码从 `training.max_local_steps` 推导；两者职责彻底分离，todo 验收标准第 1 条）。为使空/局部配置仍是有效配置，`scheduler` 的 dataclass 默认值改为 `none`；所有需要 cosine 的仓库 YAML 都显式写 `scheduler: cosine` 与 horizon；
- 新增 `inner_optimizer.min_lr_ratio: float = 0.1`：warmup 后乘子取 `max(min_lr_ratio, 调度值)`——超过 horizon 的步在 0.1×base 下训练，**永不为 0**（缺陷 3 的直接修复；0.1 为 LLM 预训练常见"decay to 10%"实践）；
- `max_local_steps=null` + cosine 组合自此合法（缺陷 2 消除）；
- in-repo YAML 全量迁移：有非空 `max_local_steps` 的配置沿用该数值；原值为 null 的既有命名基线按 `training.inner_steps * sync.stop_after_outer_steps` 固化（8l=2000、fragment/no-fragment 50x10=500、fragment 50x4=200）。这是一次性迁移决策，不是恢复运行时耦合；后续修改停止目标不会自动改 LR horizon。
- `min_lr_ratio` 必须满足 `0 < ratio <= 1`；`warmup_steps` 必须 `>= 0`。`scheduler=none` 不要求 horizon，且不构造 scheduler。

### 3.3 后续扩展：WSD 调度（本计划不实施）

WSD 的候选语义仍是 warmup → 常数 base lr → 按全局进度 cooldown，但它需要定义 resume、latest 观察延迟和 `stop_after_global_tokens` 的统一锚点。本计划不增加 `wsd` 配置值、不增加相关字段，也不在文档中宣称可用；另立实验计划并配 P7 validation 后再实施。这样避免“可选 Loop 未执行但配置契约看似已交付”的歧义。

## 4. 目标与完成谓词

1. LR 轨迹与 adoption 完全解耦：模拟任意 adoption/重建序列，逐步 LR 与无 adoption 基准**逐点相等**（SCH-02，本计划的定义性测试）；
2. `global_only` 与 `local_or_global` 在相同 scheduler 配置下产生相同逐步 LR（SCH-03，todo 验收标准第 2 条）；
3. 超过 `scheduler_total_steps` 后 LR = `min_lr_ratio × base`，非零（SCH-04）；
4. cosine 未配 `scheduler_total_steps` 被拒绝；scheduler 代码不再读取 `max_local_steps`（SCH-05 静态检查）；
5. tiny replace run：warmup 结束后事件流中的 LR 序列单调非增（锯齿消失的管线级证据，SCH-06）；fragment 路径同语义（SCH-08）；
6. resolved config、心跳/事件中可见 scheduler horizon 与当前 LR（todo 验收标准第 3 条）；
7. 全量 pytest 通过；`wsd` 仍被配置校验拒绝。

## 5. 范围与非目标

- **范围内**：进度语义、horizon 字段、min_lr_ratio、全部重建调用点接线、配置迁移。
- **非目标**：
  - optimizer moments 的 reset/preserve 显式开关与消融实验本身（后续实验计划，本计划是其前置）；
  - outer optimizer LR 策略（Q5 的 terminal merge 缩放另行处理）；
  - DyLU 式异构步数调整（研究方向，归 00 计划矩阵）；
  - warmup_steps 与 inner_steps 数值的重新调参（实验问题；但文档需指出 warmup=100 在累计语义下只影响前 100 步，历史配置的病态耦合已解除）。
  - WSD（§3.3 仅保留后续设计方向）。
- **对照污染声明（P6，最高级别）**：本修复后所有历史 loss/质量数据不可与新 run 对比。合入 commit 必须记入 run_analysis，之后的第一批 run 是新基线。

## 6. Loop Engineering 实施循环

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | HARDEN、CHECK、PERSIST |
| --- | --- | --- | --- |
| L0 基线与曲线冻结 | 把 §3.1/3.2 语义写成 golden LR 曲线表（若干 (配置, 步序列, 期望 LR) 三元组，含 adoption 注入点）；基线 commit；基线 tiny replace run 留档（锯齿证据） | 无实现 | golden 表与锯齿证据入 artifacts |
| L1 纯函数与单元 | SCH-01/02/03/04 先 RED（现实现上 SCH-02 必然 RED——锯齿即证据） | 进度改累计步；`scheduler_total_steps` + `min_lr_ratio`；重建即恢复机制 | 单元全绿；golden 明确首步、warmup/cosine 接点、horizon 与 horizon 后一步 |
| L2 调用点接线与配置 | SCH-05 先 RED（cosine 缺 horizon 当前被接受） | 所有初建/重建点传入当前 `local_step`（初建传 0）；配置校验；in-repo YAML 迁移 | 全量 pytest；SCH-05 静态检查；禁止无累计步参数的重建调用 |
| L3 管线证据 | SCH-06/08 场景定义 | `inner_step_summary`、周期 metrics 与 heartbeat 补当前 LR 和 horizon；resolved config 自动携带配置 | tiny replace/rebase/fragment run：LR 序列断言通过；resolved config 与最终 heartbeat 核对 |

## 7. 测试矩阵与通过条件

| ID | 测试 | 通过条件 |
| --- | --- | --- |
| SCH-01 | golden 曲线 | warmup 一次、cosine 形状、边界步取值与 golden 表逐点一致 |
| SCH-02 | adoption 不变性 | 注入任意重建序列后逐步 LR 与无重建基准逐点相等 |
| SCH-03 | 模式不变性 | `global_only` 与 `local_or_global` 相同配置 → 相同逐步 LR |
| SCH-04 | 过 horizon | 步数 > total_steps → LR = min_lr_ratio×base，恒非零 |
| SCH-05 | 配置与静态 | cosine 缺 horizon 拒绝；scheduler 路径无 `max_local_steps` 引用 |
| SCH-06 | replace 管线 | tiny run warmup 后 LR 单调非增（锯齿消失） |
| SCH-07 | WSD 边界 | `scheduler=wsd` 仍被拒绝，避免未交付功能静默按常数 LR 运行 |
| SCH-08 | fragment 路径 | fragment adopt 重置后 LR 恢复累计进度 |

progress.md 每条记录必须列出覆盖的 SCH ID（P8）。

## 8. 验证阶梯

1. **登录节点**：SCH-05 grep、lint。
2. **1 节点 compute**：单元（golden/不变性）→ 全量 pytest → 三类 tiny run 的 LR 序列断言。
3. 9 节点：**不作为本计划门禁**。但本计划是 reset/preserve 消融（≥3 seeds + validation eval，P6/P7）的启动门禁——该实验属于后续实验计划，其第一批 run 同时充当新基线。

## 9. 报告、证据与 Checker

报告目录 `reports/imp_plans/bug_fix-B/B2/`。核心证据：修复前锯齿 LR 轨迹 vs 修复后单调轨迹的对照图/序列（从事件日志提取）、golden 表、SCH-02 输出。无 Checker 结构变更；若 Checker 校验 resolved config 字段清单，同步新字段。

## 10. 停止与升级规则

按 AGENTS.md 三连败升级。golden 表本身如与实现争执（期望值算错），修订 golden 表属于 SPECIFY 变更，需在 failures.md 记录推导过程，不得改测试凑实现。

## 11. 文档同步

- `plans/todo/cosine_scheduler_decoupling.md` 内容替换为指向本计划的链接（todo 已被本计划吸收并扩展）；
- docs 的训练配置章节：新字段语义、累计步进度定义、"warmup 只发生一次"；WSD 只标为未实现的后续方向，不列作可用选项；
- review 报告 B2/Q1 条目标注完成 commit；run_analysis 追加对照污染声明（§5）。
