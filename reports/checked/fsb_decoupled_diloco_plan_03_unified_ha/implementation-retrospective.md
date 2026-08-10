# Plan 03 实施复盘：遇到的问题与得到的经验

## 1. 范围与结论

本文复盘 `fsb_decoupled_diloco_plan_03_unified_ha` 的实际实施过程，重点总结问题模式、根因、有效做法以及后续计划可直接复用的工作方法。详细的逐次事实仍以以下记录为准：

- [实施进度](progress.md)
- [失败记录](failures.md)
- [连续失败代码审查](code_review.md)
- [plan-complete 修缮记录](plan-complete-remediation.md)
- [最终 requirement Checker 结果](artifacts/20260810-082200_p6-requirements-plan-final-completed-pass.json)
- [最终计划](../../../plans/DONE/plan03/fsb_decoupled_diloco_plan_03_unified_ha.md)
- [最终 requirement matrix](../../../plans/DONE/plan03/fsb_decoupled_diloco_plan_03_unified_ha-requirement-matrix.csv)

最终正式源码目标为 `9b7e1dacdecbea8951121b3f70a6caece481a380`，plan-final 提交为 `5352dad092b42b7c1bf1196f3fbd4f3b352ad8ee`。G0–G10、静态/动态 9 节点、10,000-cycle boundedness、2-node SQLite/共享文件系统、paired performance、文档和 tracked-evidence Checker 全部通过；最终完整测试为 `805 passed, 2 skipped`。

这次实施最重要的总体认识是：困难不主要来自单个算法，而是来自同一个逻辑身份需要同时穿过配置、descriptor、文件系统、SQLite、scheduler、进程、模型加载器和证据系统。多数高价值缺陷都出现在两个边界对“同一个对象”使用了不同身份或不同权威来源的地方；多数低价值失败则来自测试 harness、命令、格式化范围或 evidence 生命周期没有精确复现 production 边界。

## 2. 最终完成了什么

本计划最终完成了以下核心收敛：

1. static 与 dynamic full-update runtime 统一到 Full Protocol v4、单 candidate lease、单调 epoch 和 fenced SQLite authority。
2. classic full writer 与 fragment writer、正式配置、PBS 入口及生产 fallback 被删除；旧完成 run 只保留明确的 query-only inspect/export/eval 边界。
3. proposal、cycle receipt、membership、token fate、selection credit、terminal fence、publication intent、audit/GC 等状态进入 typed、可审计、事务化的 authority 模型。
4. learner publication 与 authority ingestion 之间增加精确 receipt acknowledgement，使 terminal hard-crash gap 保持在声明上界内。
5. dynamic admission/replacement 在 Torch/CUDA 加载前完成，并同时区分 request、command、physical actor、stable stream、fence、PBS allocation 等身份。
6. Full-v4 Hub 输入使用 immutable commit pin，实际 loader 传递同一 revision；没有 descriptor-bound content manifest 时，本地 mutable model/data 在 actor-time fail closed。
7. recovery hot set 与线性 audit history 分离，10,000-cycle 门禁证明 hot SQLite pages/files 有界，而不把合法的 immutable audit 增长误判为泄漏。
8. 正式性能比较固定 20 个 AB/BA pairs、共同 timer anchor、完整 workload identity 和 one-sided 95% upper bound，未通过 clipping 或修改门槛获得结果。

最终关键结果包括：

- G8 static：8 learners + 1 candidate，FP32，60 local steps/cycle，最终 version 21，8 个 contributor 均获得 credit，ledger balance 为 0。
- G9 dynamic：8 bootstrap learners + 1 candidate allocation，BF16 update/publish、CPU FP32 merge，包含 loss/replacement/duplicate/takeover，最终 version 121，ledger balance 为 0。
- G6：10,000 cycles；live-page slope 的 one-sided 95% upper 为 `0.00559848 < 0.01`，active/recovery file slope upper 为 `0.00452273 < 0.01`。
- G10 classic 对比：signed median `-10.3342%`，one-sided 95% upper `-9.1358%`；dynamic 对比：median `0.1026%`，upper `2.6745%`，均满足预注册 `<=10%` 门槛。

## 3. 主要问题及根因

### 3.1 身份在不同层之间不一致

这是整个计划中重复最多、影响最大的问题族。

具体表现包括：

- qsub receipt 使用规范化 job ID，而 actor registration 保存完整的 `*.opbs` ID，harness 用字符串相等连接两者。
- annotated Git tag 的 tag-object SHA 被拿来与 peeled commit SHA 比较。
- classic venv 的包来源检查在当前仓库 cwd 下执行，空路径优先级遮蔽了 detached worktree 中的安装。
- admission request identity、command journal identity、actor instance identity、stable contributor/stream identity 和 current pointer key 一度被混用。
- replacement harness 通过再次扫描 PBS ID 推断新 instance，而 authority 已经持久化了更准确的 `launch_requests.admitted_instance_id`。
- config 中看似合法的 Hub 字符串可能在 actor cwd 下解析为本地目录、symlink，或被环境变量替换成 mutable local dataset。

根因不是“少做一次字符串标准化”，而是没有在边界处明确记录身份的类型、产生方式和权威 owner。字符串相同不等于语义相同，字符串不同也不一定代表不同对象。

最终采用的原则是：

- authority foreign key 优先于日志或字符串扫描；
- typed fence/stream/request identity 优先于物理进程 ID；
- Git ref、tag object、peeled commit 分开记录；
- PBS receipt 与完整 scheduler ID 各自保留，只在明确的 canonicalizer 后比较；
- Hub ID 与 local path 的判定必须在实际 producer 调用前再次执行；
- source/evidence identity 使用共同的内容指纹，而不是只依赖 HEAD 名称。

### 3.2 测试 oracle 验证了进程表象，而不是持久化安全性质

多个正式场景最初使用了错误的通过条件：

- 旧 learner 恢复后必须非零退出；实际上它也可能先看到 terminal 并干净退出。真正的不变量是 replacement boundary 之后没有旧 fence 的成功 authority effect。
- kill 一个与 candidate 共享 PBS allocation 的本地 subprocess，被当成 scheduler 已确认的永久 learner loss；scheduler 看到的 allocation 仍是 RUNNING，production 正确地拒绝 replacement。
- scheduler-confirmed expiry 被 harness 断言为 `revoked`，但 authority 明确区分 `expired` 与 operator revocation。
- terminal 判断曾使用 wrapper exit、错误状态字段或 hot-row 数量，而没有联合检查 terminal fence、token ledger、current authority、hot+archive history 和 SQLite integrity。

经验是：分布式故障测试的 oracle 应尽量来自 durable authority，而不是 Unix exit code、日志末行、PID、临时文件数量或 wall-clock 猜测。进程状态只能作为诊断信息，不能代替“是否成功提交”“哪个 fence 当前有效”“哪个 token fate 已裁决”等持久化事实。

### 3.3 learner 与 authority 之间缺少真正的流控

P4 暴露了一个真实协议缺陷：learner 发布 receipt/proposal 后会立即开始下一 cycle，而 global version 的变化不等于该 contributor 的 receipt 已被 authority ingest。terminal close 时，learner 已发布到 cycle 27/28，authority 只 ingest 到 cycle 9，精确 terminal acknowledgement 因而被正确拒绝。

错误修法是放宽 terminal authority，让它接受任意积压；这会破坏“每个突然丢失 incarnation 最多一个 cycle gap”的声明。

最终方案是新增 epoch/fence-bound、byte-idempotent 的 receipt-ingestion acknowledgement：

1. authority 成功 ingest receipt 后发布精确 ack；
2. learner 在 ack、drain 或 terminal 三者之一出现前不开始下一 cycle；
3. successor 可通过幂等 replay 重建 ack；
4. stale epoch、错误 contributor、错误 digest 的 ack 全部忽略。

这说明背压必须绑定实际需要保护的权威事件，不能用“看到了新 global version”之类相关但不等价的信号替代。

### 3.4 fairness 与 accounting 的定义需要直接编码数量语义

P3 中原先的选择顺序 `(last_selected_committed_version, stable_key)` 在 batch service 下产生确定性的 `500/333` 不均衡：同一 batch 内成员拥有相同 version，部分耗尽的 age cohort 会反复借用下一 cohort 的低 stable key。

最终顺序改为：

```text
(committed_service_count, last_selected_committed_version_or_minus_one, stable_key)
```

`committed_service_count` 表达真正的累计服务数量；last version 保留同服务量下的 oldest-service 偏好；stable key 只做确定性最终 tie-break。与此同时，只有成功 global commit 才消耗 service credit，selection 或 abandoned publication 不得改变它。

另一个 accounting 问题是空 receipt ledger 时 read model 没有汇总独立 terminal fence 中的 hard-crash gap。修复没有伪造 receipt 或零值，而是承认 terminal fence 与 token rollup 是两个独立 authority domain，并在 read model 中同时聚合。

经验是：不要让一个方便的代理字段承载它并不完整表达的语义；公平性要记录服务数量，terminal gap 要读取拥有它的 authority 表。

### 3.5 dynamic replacement 必须穿过真实 scheduler 边界

G5 前三次动态 failure 场景证明，process-local fault injection 不能替代 scheduler-backed loss：两个本地 learner 继承同一 `PBS_JOBID`，杀掉一个 PID 并不会产生该 instance 对应 allocation 的 historical FINISH 证据。production 的 fail-closed 行为是正确的。

正式场景随后改为：

- losable learner 使用独立 PBS child allocation；
- survivor 可留在 parent allocation；
- 只在 exact child job 具有 historical FINISH 后允许 production capacity service 计划 replacement；
- replacement 仍由真实 outbox/qsub/registration/admission 流程完成；
- harness 不直接修改 membership SQLite 行，也不伪造 FINISH。

后续又暴露 bootstrap grace、scale-out 与 replacement startup race，以及 normalized/full PBS ID、`expired`/`revoked` 等 oracle 问题。这些问题最终通过 immutable authority creation time、request-keyed admission foreign key、精确 retirement reason 和统一 job-ID canonicalizer 解决。

经验是：如果生产安全条件依赖 scheduler 的第二因子，测试必须真正跨过 scheduler 边界；降低 timeout、修改 heartbeat 或直接写 DB 只会绕过要验证的协议。

### 3.6 删除旧代码比新增功能更需要边界证明

P5 删除 classic/fragment writer 时，主要风险不是 import 报错，而是把仍有价值的兼容能力和共享不变量一起删除。

实际发现的问题包括：

- old v1–v3 config 含已删除字段，严格 current loader 正确拒绝，但 eval/export/quality 工具也因此失去 query-only 能力；
- shared `Config` 投影看不到 v4-only stop target，却仍承担完整性校验，导致合法配置被拒绝；
- 一些 fragment-oriented tests 同时覆盖通用 SQLite、GC、terminal 不变量，不能因为旧 fixture 被删除就视为测试可整体删除；
- 恢复旧 alias 或让 production loader 静默丢弃 removed keys 虽然短期方便，却会重新制造“旧 run 可 resume”的错觉。

最终保留了显式、窄范围的 legacy query-config projection；current loader 继续严格拒绝 removed keys；旧 reader 只读、query-only，不能初始化、恢复、repair 或 compact。每个删除测试都映射到 `migrate-to-unified`、`retain-legacy-reader` 或 `delete-obsolete`，共享 invariant 必须有 v4 replacement test。

经验是：删码验收应同时证明“旧 writer 不存在”和“需要保留的读取语义仍存在”，并为每个删除测试说明其业务不变量去了哪里。

### 3.7 performance 比较先后暴露环境、workload 和生产路径问题

G10 的失败分为三层。

第一层是环境身份：

- 导出依赖时丢失 locked PyTorch index；
- cwd 遮蔽 classic editable install；
- annotated tag 与 commit 比较层级错误。

第二层是 workload oracle：早期只比较 selected/applied update 投影，忽略 terminal authority 中实际 adjudicated processed tokens 和 cursor。某些 trial 对外宣称 256 tokens、cursor `[4,4]`，但实际已处理 320/384 tokens、cursor 6，因此并不可比。

第三层是真实 production 问题：

- learner 在达到 global target 但 drain 尚未发布的窗口继续启动新 cycle；
- syncer admission-bearing module 在 module import 时加载 Torch，导致 candidate model init 与 learner admission/model init 串行；
- 成功 trial 清理前没有保留足够的 lifecycle event tape。

最终修复包括 target-aware await-close、Torch-heavy import 延迟到 fenced runtime、leadership 后先进行 Torch-free admission、以 terminal processed/cursor 作为 workload authority，并在 cleanup 前提取事件证据。值得注意的是，没有通过缩短 timer、删除 fsync、减少 pair、调整 margin 或 clipping 来“修好”性能。曾考虑的 initializer fsync batching 因风险高且在移除意外 Torch 依赖后已无必要而被拒绝。

经验是：性能回归可能揭示 correctness 问题；只有实际工作量、终态、环境和 timer anchor 都相同，数字才有解释价值。

### 3.8 external input identity 必须贯穿 validator、descriptor 和 loader

plan-complete current-state review 在所有 P6 gate 已完成后仍发现：

- model/tokenizer/dataset revision 没有完整传到实际 Hugging Face producer；
- Full-v4 config 允许缺失或可移动 revision；
- 11 个 repository-owned GPT-2/WikiText config 没有 immutable pin；
- path-prefix 规则会豁免 mutable local input；
- relative path、symlink 和 `FS_DILOCO_HF_WIKITEXT_REPO` override 可在 actor cwd 下绕过 config-time 检查。

修复最终形成三层闭环：

1. Full-v4 non-synthetic Hub input 在 config 中必须是精确 40 位 commit SHA；
2. descriptor 保存相同 identity，loader 把 revision 传给 model、tokenizer、primary dataset 和 fallback；
3. producer 调用前检查解析后的实际引用，任何现存 local path/symlink/env override 在没有 descriptor-bound content manifest 时 fail closed。

Torch baseline 与 classic/fragment query-only 使用独立 validation profile，未被 Full-v4 限制误伤。

经验是：config-time validation 只能验证字符串，不能证明 actor 实际读取的 bytes。所有影响可复现性的外部输入都必须追到最终 I/O 调用点。

## 4. 实施过程中的低效与可避免失败

### 4.1 harness 和命令问题占用了过多 compute 轮次

失败记录中混合了预期 RED、真实 production defect、fixture 错误、CLI 组合错误、format/lint、导入路径、错误 JSON 字段假设、unsupported qstat option、patch hunk 错误和 dirty-source BLOCKED。很多问题本可在 qsub 前由本地静态或纯单元测试发现。

典型例子包括：

- Ruff 只扫描 tracked 文件，遗漏新建未跟踪文件；
- 对全仓运行 formatter，触碰被冻结的 baseline bytes；
- 使用 system Python 做项目 import probe；
- 同时传入 Checker 的 mutually exclusive phase 参数；
- 读取 artifact 时假设不存在的 nested `source` 结构；
- 测试使用 `set & dict`、遗漏 import、过窄错误文案断言；
- formal source 仍 dirty 时重复执行行为已经通过的 compute gate。

改进方向是把“实验 harness 自身的 schema、命令和状态机”视为需要单独测试的代码，而不是正式作业开始前的临时脚本。

### 4.2 过晚的 plan-complete current-state review 导致整套正式证据重跑

第一次 P6 G0–G10 和 tracked-evidence Checker 已经完成，但 plan-complete current-state review 随后发现 H1/M1/M2/L1，并在增量 review 中继续发现 local input identity 绕过。因为这些修复改变 formal source fingerprint，G0–G10、quality/docs、matrix 和 Checker 必须在 `9b7e1da...` 上全部重建。

正式门禁本身没有错，但执行顺序可以更经济：在昂贵的 9-node、10,000-cycle 和 20-pair performance 之前增加一次“预 plan-complete current-state audit”，先处理架构、dead code、external identity 和文档漂移；正式 plan-complete review 仍保留，但若其前后源代码不再变化，就不必重复全部昂贵证据。

### 4.3 将预期 RED 与基础设施失败计入同一连续失败窗口，增加了审查成本

三连失败升级非常有效，但同一 experiment 的记录有时依次包含预期 RED、配置抄写错误和 formatter 越界。它们都应记录，也确实应该停止盲目重试；不过后续可在 experiment schema 中显式分类：

```text
EXPECTED_RED | PRODUCT_FAIL | HARNESS_FAIL | ENV_FAIL | SOURCE_BLOCKED
```

连续失败门禁仍按 terminal non-PASS 触发，但审查可以更快判断是否需要重推协议，还是需要重建 harness/preflight。

### 4.4 evidence 生命周期曾形成自证循环

P3 一度要求新 target 的完整 pytest 在运行前就能从 matrix 找到绑定该 target 的 PASS evidence；但这个 evidence 恰恰要由该 pytest 产生，形成循环依赖。

最终顺序明确为：

```text
source/static contracts
→ compute behavior
→ target-bound runtime artifact
→ matrix binding
→ phase requirement Checker
→ tracked-evidence Checker
```

经验是：Checker 不能证明自身，也不能要求尚未产生的下游 evidence。每个 gate 都应只消费拓扑上更早的独立事实。

## 5. 最有效的工程方法

### 5.1 三连失败后停止局部修补

强制全面追踪输入、状态转换、事务、文件发布、恢复、进程和输出，几次真正改变了实现方向：

- fairness 从 version 代理改为 committed service count；
- terminal 从放宽 ack 改为 receipt-ingestion flow control；
- G5 从本地 kill 改为真实 scheduler child allocation；
- G10 从 selected-only workload 改为 terminal processed/cursor authority；
- incremental config patch 从增加删除 mini-language 改为删除重复抽象。

这个规则最大的价值是防止通过加 timeout、放宽状态集合、恢复 alias 或弱化 Checker 来让测试变绿。

### 5.2 RED 测试与反向兼容测试同时存在

对接受的行为缺陷先保存 pre-fix RED，再修改 production；同时为不应改变的边界保留反向测试。例如 Full-v4 local input fail closed 时，synthetic、Torch baseline 和 legacy query-only local input 仍必须通过。这样可以避免安全修复无意扩大作用域。

### 5.3 以 clean common target 生成正式 evidence

行为通过但 source dirty 的作业只记为开发证据，不能进入 formal matrix。最终所有 G0–G10 artifact 绑定同一 clean commit 和相同 source fingerprint，解决了“测试通过的是哪个版本”这一常见审计漏洞。

### 5.4 分层 gate 从廉价到昂贵

有效顺序是：diff/compile/lint/format/PBS syntax/group ID → focused/full compute tests → generated state machine/crash matrix → tiny real pipeline → boundedness/2-node → 9-node → performance → aggregate/matrix/completed Checker。后层失败不能用前层 PASS 代替，但前层能显著减少昂贵作业中的低级错误。

### 5.5 cleanup 也使用 evidence 和身份校验

G8/G9 cleanup 先 dry-run，绑定 exact terminal run、PASS evidence、artifact policy、inode/device/mtime/size，再删除 32 个终态 pointer/current payload 对象，共 110,447 bytes；authority、DB、audit、checkpoints 和 GC-owned 对象全部保留。另有六个已被最终 G5 aggregate 完整投影的中间 JSON，共 64,394 bytes，被作为重复报告删除。

这些删除不可直接恢复，但没有删除唯一证据。经验是：清理不是 `rm` 步骤，而是一个需要 owner、terminal state、引用闭包和 post-delete validation 的小型事务。

### 5.6 性能和质量门禁分域

性能门禁验证可比 workload 下的 end-to-end overhead；三种子训练质量研究仍明确为 nonblocking `NOT_RUN`，没有被小样本、单 seed 或性能结果替代。明确分域避免了把 correctness PASS 写成质量结论，也避免未运行的低功效研究阻塞删码完成定义。

### 5.7 审查连续性和 reviewer 不可用规则明确

每轮 review 都记录 exact base/target，Codex 在读取其他 reviewer 输出前先独立保存报告。Claude 只在可核验 HTTP 429 session-limit 时按规则 `skipped-session-limit`，没有伪造报告，也没有让 reviewer 服务状态阻塞 mandatory Codex gate。

## 6. 下一个类似计划的推荐实施顺序

### 6.1 开始实现前

1. 冻结 branch point、source scopes、环境 lock、config/schema identity 和旧行为 oracle。
2. requirement matrix 在 P0 就建立机器可解析 schema；每行明确 implementation owner、test owner、artifact producer 和 final Checker consumer。
3. 为所有跨层身份建立类型表：原始值、canonical value、authority owner、持久化位置、允许比较的另一类型。
4. 为实验 artifact 冻结 JSON schema，至少统一 `status`、`source_identity`、`environment`、`metrics`、`errors`、`evidence_paths`。
5. 将 runner/harness 的参数解析、job-ID normalization、Git ref peeling、package provenance 和 result projection 做成无 PBS 单元测试。

### 6.2 每个工作单元

1. 写最小 RED，并声明它是预期行为失败还是 harness preflight。
2. 修复 production 后运行 focused static 和纯单元测试。
3. 检查 diff scope；formatter 只处理明确修改/新增文件，冻结文件做 byte comparison。
4. 创建 clean review target，再执行 compute；不要把 dirty-tree behavior PASS 反复当作 formal candidate。
5. 通过后立即生成 target-bound structured artifact、更新 matrix，再运行 phase Checker。
6. review finding 若改变 source fingerprint，只在最终 review target 上重建正式 evidence。

### 6.3 昂贵 acceptance 前

在 G8/G9/G10 前增加一次 current-state 预审，至少检查：

- external input 是否从 config 一直绑定到实际 loader；
- 是否仍有无 caller alias、fallback、旧 layout reader 或重复 version owner；
- actor admission 是否真的发生在 Torch/CUDA 之前；
- terminal/current/history/GC 边界是否使用 durable authority；
- benchmark 是否使用实际 processed work，而不是 selected projection；
- cleanup 是否已能从 final artifact 构造 exact plan。

只有这些检查稳定后，再提交 9-node、长周期和 20-pair 作业。

### 6.4 每个故障场景的最小审计表

每个 scenario 在编码前回答以下问题：

| 问题 | 必须明确的内容 |
|---|---|
| 故障注入在哪一层？ | process、allocation、scheduler、SQLite transaction 内/外或 filesystem visibility |
| 谁是安全性的 authority？ | SQLite row、immutable object、scheduler history、fence 或 descriptor |
| 身份是什么？ | request、command、actor、stream、generation、epoch、PBS job、Git commit |
| 什么才算成功？ | durable state transition，而不是单一 exit code 或日志字符串 |
| crash 后如何恢复？ | replay owner、幂等 key、publication order、stale rejection |
| cleanup 能删什么？ | exact owner、引用闭包、terminal proof、不可恢复影响 |

## 7. 可沉淀为仓库工具的改进

后续可以把本次经验进一步固化为工具，而不是只依赖执行者记忆：

1. 一个统一 identity 模块，集中处理 PBS job ID、Git ref peeling、source fingerprint 和 actor/stream display，避免 harness 各自字符串拼接。
2. 一个 artifact schema validator，所有 PBS producer 与 Checker 在写入/读取时使用同一 typed schema，杜绝对 `source`、`source_identity`、顶层 commit 字段的猜测。
3. 一个 experiment preflight 命令，统一检查 clean source、diff scope、untracked Python、formatter scope、PBS syntax/group、interpreter/package provenance、output path 和 walltime。
4. 一个 scenario authority assertion 库，复用 terminal fence、ledger balance、hot+archive update、current membership、SQLite integrity 和 child-job terminal 检查。
5. 一个 formal-ladder manifest，预先声明 G0–G10 的 producer、依赖、source commit 和 artifact path，aggregate 不再硬编码 requirement 数量或字段层级。
6. 在失败 artifact 中增加 failure class 与 stage，区分 expected RED、product、harness、environment 和 source-blocked，便于三连失败审查快速聚焦。

## 8. 最终经验总结

这次计划证明，可靠的 filesystem-based HA 不是依靠更多 fallback 或 timeout，而是依靠更少且更清楚的 authority：SQLite 负责 fenced mutation，immutable 文件负责可重放事实，mutable current pointer 只是可修复 cache，scheduler history 只证明 allocation 生命周期，descriptor/source manifest 负责输入身份。

同样，可靠的实施过程也不是依靠更多测试次数，而是让每个测试在正确的语义层验证正确的事实。后续最值得坚持的五条原则是：

1. 身份必须 typed、canonical，并由明确 authority 持久化。
2. fault test 验证 durable effect，不验证偶然的进程表象。
3. formal evidence 必须来自同一个 clean source target。
4. 性能比较必须先证明实际 workload 等价。
5. 删除、兼容、审查和 cleanup 都是协议工作，而不是收尾杂项。
