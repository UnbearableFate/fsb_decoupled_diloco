# 02 详细系统设计

## 1. 进程角色与部署形态

一次 run 由 `N+1` 个进程组成,典型部署(Miyabi PBS,9 节点 = 8 learner + 1 syncer):

| 角色 | 数量 | GPU | 入口 | 关键本地状态 |
|---|---|---|---|---|
| learner | N(`sync.num_learners`) | `choose_device()` 有 CUDA 时选首张可见卡，否则 CPU；PBS 通常每进程隔离 1 张 | `python -m fs_diloco.learner --config ... --learner-id learner_000` | 模型副本、内层 AdamW、数据分片迭代器 |
| syncer | 1 | `syncer.device=cuda` 时 1 张；也可用 CPU | `python -m fs_diloco.syncer --config ...` | 全局参数扁平向量 θ、外层优化器状态、共享 run 内的持久 SQLite |

进程间没有任何直接连接。所有协调通过共享目录 `run.shared_root` 完成。dataclass 默认值是 `null`，解析时回退到 `<project_root 或 cwd>/runs/fs_diloco/<run_id>`；仓库随附的正式配置显式使用主工作树的绝对 `runs/fs_diloco/{run_id}` 模板，避免从其他 worktree 启动时把产物写散。

## 2. 通信契约(Runtime Contract)

这是整个系统最重要的不变式集合:

1. **大张量一律 safetensors**(权重、外层优化器状态、update 向量、fragment)。
2. **按介质选择一致性原语**:JSON pointer/control/heartbeat 与 safetensors 通过 `storage/atomic_io.py` 的“同目录临时文件 → 文件 fsync → chmod → `os.replace`”发布，读者只会看到旧文件或完整新文件；SQLite 用 rollback-journal 事务；每 actor JSONL、syncer CSV 和历史 JSONL 为单写者追加。`learner_metrics.csv` 与 `update_manifest.csv` 则由多个 learner 无锁共享追加，只是 best-effort 遥测，不是提交权威。atomic helper 不 fsync 父目录，保证的是运行期原子可见性，而不是断电后目录项必然持久。
3. **proposal pointer 是提交标记**:learner 先写不可变张量 payload，再原子替换固定 pointer。全量模式为每 learner 一个 `updates/latest/learner_XXX.json`；fragment 模式为每 `(learner, fragment)` 一个 `updates/latest/learner_XXX_fNNN.json`。syncer 每轮只枚举 `N` 或 `N×K` 个固定路径，持久 frontier 在两种模式都防重放；只有 fragment 路径额外用进程内 stat signature 跳过未变化 pointer 的 JSON 解析。runtime 不扫描历史 payload metadata。
4. **`control/latest.json` 是 learner 轮询的唯一全局指针**。learner 不扫描权重目录、不读数据库。
5. **心跳 JSON 只是带代际边界的存活提示**,不参与训练版本权威链。`stopped` 只证明当前 syncer 代际的输入闭合；full resume 会 fence 旧 pointer 内容，直到 learner 原子发布不同内容的新心跳。
6. **SQLite 是共享目录中的持久提交记录**:`control/syncer_metadata.sqlite3`,使用 rollback journal(`journal_mode=DELETE`)、`synchronous=FULL`、60 秒 busy timeout。只有 syncer 改业务表,但不同计算节点可以重开并恢复同一 run;不使用 WAL、节点本地副本或 DB dump。
7. **learner 的 global adoption 由单一策略状态机决定**:`replace`/直接 adoption 整体覆盖并重置内层 optimizer moments；scheduler 对象可重建，但始终恢复到累计 local-step 相位。rebase/prediction reconcile 在合成尚未发布的本地差值后保留完整内层训练状态。不存在与此并行的配置布尔开关。
8. **外层优化器是显式扁平向量实现**(`modeling/outer_optim.py`),不复用 `torch.optim`,以便把优化器状态精确序列化成 safetensors 并跨 resume 保持一致。

## 3. 参数的扁平向量表示

- syncer 初始化时构建 **param index**(`modeling/param_index.py`):按 `named_parameters()` 声明顺序记录每个可训练参数的 `name/shape/dtype/numel/offset`,发布为 `control/param_index.json`。
- 双方启动时都会用本地模型重建一份 index 并 `validate_compatible_index()` 严格比对顶层四字段和完整 `params` Python 列表,保证两边"模型 → 扁平向量"的语义映射一致；它不比较 JSON 原始字节或排版。
- 全局权重文件按**参数名 → 张量**存储(便于单独加载/导出为 HF 模型);update 向量按单键 `local_params` 存储扁平向量;fragment 按单键 `fragment_params` 存储。

## 4. 合并协议(核心算法)

### 4.1 资格筛选

一个 pending update 有资格参与第 `v → v+1` 次合并,当且仅当:

- 状态为 `pending`;
- `base_global_version ≤ v`，且 `staleness = v − base_global_version ≤ sync.max_staleness_versions`;
- 其张量文件仍然存在(丢失则标记 `dropped(missing_file)`)。

### 4.2 每 learner 选一

`protocol/merge.py: select_one_per_learner()`——同一 learner 若有多份合格更新,按策略选一份:

- `most_recent_per_learner`(默认):取 `(local_step_end, committed_at)` 最大者;
- `oldest_pending`:取 `committed_at` 最小者。

同一 `sync.selection_policy` 同时用于常规合并与 terminal drain,不会在末端切换策略。

结果截断到 `quorum_max` 份。

### 4.3 quorum 与宽限窗口

- 合格 update(每 learner 一份)不足 `quorum_min` → 不合并,睡 `scan_interval_seconds` 后重扫;持续无进展超过 `liveness.no_progress_timeout_seconds` 则以 `no_progress_timeout` 停机。
- 达到 `quorum_min` → 进入宽限窗口:固定模式等待 `fixed_seconds`;自适应模式从 `initial_seconds` 开始,根据 syncer 进程首次摄取 update 的 monotonic 时刻加 `local_cycle_step_time_seconds_mean × inner_steps` 估计最快下一次上传,动态把 deadline 向前收紧但绝不延长。首见时刻通常比 learner commit 晚，常规轮询下约为一个 scan interval，但 checkpoint I/O、调度或启动时序可使延迟更长；晚记录会让预估 deadline 更晚，方向保守。resume 后没有进程内首见记录的旧 update 不收紧窗口。循环重扫元数据,凑满 `quorum_max` 份或 deadline 到达为止。

### 4.4 token × staleness 加权平均

对选中的更新集合 S(`protocol/merge.py`):

```
raw_i  = tokens_i / (1 + λ · staleness_i)        λ = sync.staleness_lambda
w_i    = raw_i / Σ raw_j                          (归一化)
p̄      = Σ w_i · p_i                              (参数加权平均)
```

- `tokens_i` 是该更新区间实际消费的 token 数——吞吐高的 learner 权重更大;
- staleness 越大权重越低,λ 控制惩罚力度。

### 4.5 外层步进与发布

```
g      = θ − p̄                                    (外层伪梯度)
θ', st' = outer_optimizer_step(θ, g, st)          (SGD / momentum / Nesterov / AdamW,见 modules/modeling.md)
```

随后 syncer 默认用两个有界 I/O worker 并发原子保存 `global_v{v+1}.safetensors` 与 `outer_v{v+1}.safetensors`；`parallel_checkpoint_writes=false` 时改为 weight→outer 串行。主线程确认**两份均成功**后，才在一个 SQLite 事务中校验前驱/目标版本与 selected 集合,插入 committed `global_versions` 行,把 selected 更新标记 `applied` 并把被取代、过期或未来版本 proposal 终态化 → 原子覆盖 `latest.json` → archive/GC。worker 不写 DB/latest；任一写失败都不会提交版本。事务提交是全局版本的正确性边界;`latest.json` 只在提交后更新。

### 4.6 更新丢弃规则

| drop_reason | 触发条件 |
|---|---|
| `missing_file` | proposal 已摄取但不可变 payload 不存在或读取前消失 |
| `too_stale` | staleness 超过 `max_staleness_versions`;terminal drain 也不放宽这个正确性边界 |
| `superseded` | 同一 learner 有更新的一份已被选中,较旧的 pending 份被取代 |
| `future_base` | proposal 的 base 高于目标提交允许的版本,不能用于当前分支 |

以上是运行时代码写入 SQLite/历史 JSONL 的**字面值**。`stale`、`future_base_version` 等旧称只可能出现在历史 run 或兼容分析逻辑中。

drop reason 只适用于已经摄取入库的 proposal。learner 若在 syncer 第一次观察前连续替换同一 fixed pointer，中间 proposal 不会进入 DB，也不会得到 `dropped` 行；其不可变 payload 作为未引用 orphan 经过 maintenance grace 后删除，产生侧只能依赖 learner 日志/manifest 的 best-effort 证据。

## 5. fragment 分片模式

### 5.1 分片定义

`protocol/fragment_index.py` 在扁平向量上定义 K 个**互不重叠、完全覆盖**的分片:

- `full`:K=1,退化为全量(但走 fragment 协议路径);
- `balanced_tensor`:以**整个张量**为最小单位,按 numel 从大到小贪心装入当前最小的桶(不切开单个张量),`num_fragments ≤ 可训练张量数`。

fragment index 发布为 `fragments/fragment_index.json`,构建时校验覆盖性、连续性、id 连续和每片 numel；传入 param index 时还确认每个 slice 的参数名存在。校验器并不逐项重算 slice 内的 shape/dtype/param offset，这些由构建器从 param entry 直接生成。

### 5.2 双侧 round-robin 调度

- **syncer 侧**:第 `e` 次全局合并事件的目标 fragment 为 `e mod K`(`protocol/fragment_scheduler.py: select_fragment`)。每次合并只处理目标片的更新,该片 `fragment_version +1`,同时 `global_merge_event +1`。
- **learner 侧**:第 `u` 次上传(`local_update_index`)上传 `u mod K` 号片。learner 按 fragment index 直接从对应的命名参数切片构造连续 fragment,不会先物化完整扁平向量;因此 CPU 暂存和 GPU→CPU 搬运量只与目标片大小相关。

因此每个 fragment 的更新供给和消费频率天然对齐,`expected_fragment_versions_after_events()` 可静态推算 E 次事件后各片应有的版本(分析工具用它做断言)。

### 5.3 merge 与发布的差异

- 合并数学与全量模式相同,只是 θ、p、优化器状态都是**每片一份**(`fragment_thetas[k]`、`outer_states[k]`);staleness 以 `base_fragment_version` 对比该片当前版本计。
- `latest.json` 采用 `latest_kind: "fragment"` 布局:携带 `global_merge_event`、每片 `{version, weight_path, optim_path, updated_at_global_merge_event}`,以及最近一次 materialize 的完整权重路径。
- **materialize**:按显式正整数 `fragments.materialize_full_every_events` 周期(以及事件 0、到达目标步数和任意正常终止时)把所有片拼回完整向量,存成 `weights/global_v{event:06d}.safetensors`,供评测/导出使用；开启 fragment 而缺失该字段会 fail closed。
- **learner 采纳是增量的**:对比 `latest.json` 中每片版本与本地已加载版本,只加载变化的片、scatter 进本地扁平向量再写回模型(`adopt_fragment_updates`),并按 `fragments.reset_inner_optimizer_on_fragment_adopt` 决定是否重置内层优化器。

learner 上传文件的 dtype 由 `io.tensor_dtype` 决定。使用 BF16 时,全量和 fragment 的参数 payload 都约为 FP32 的一半。syncer 的 `load_update_vector()` / `load_fragment_update()` 在读取时转换为 `syncer.compute_dtype`；全局参数、聚合与外层优化器浮点状态也使用该 dtype。global/fragment 权重及外层状态按 `syncer.publish_dtype` 发布，因此可以分别控制 learner→syncer update、syncer 数值路径和 syncer→learner/checkpoint 三段的精度与 I/O 大小。

### 5.4 限制

- `fragments_per_update` 固定为 1;调度只支持 `round_robin_global`;resume 未实现。
- full publication 的“checkpoint 成功 → 单一 SQLite commit → latest”事务边界不适用于 fragment：fragment 权重、outer、`fragment_versions`、latest 和 update applied/drop 是顺序执行的独立持久化步骤。fragment syncer 崩溃可能留下跨文件的部分发布状态，而该模式又没有 resume；因此不能把它描述为 full 模式同等级的可恢复提交协议。

## 6. liveness(存活管理)

- learner 每 `liveness.heartbeat_interval_seconds` 原子写 `heartbeats/<learner_id>.json`(含 phase、last_local_step、tokens/s、已加载版本等)。
- syncer 每轮扫描心跳入库,并按心跳年龄分类(`protocol/liveness.py: classify_liveness`):

| 状态 | 条件 |
|---|---|
| `active` | 心跳年龄 ≤ `stale_after_seconds` |
| `stale` | ≤ `dead_after_seconds` |
| `dead` | 超过 `dead_after_seconds` 或从未见过 |
| `stopped` | learner 退出时自报,粘性(不再被重分类) |

- liveness 只影响观测与 `all_expected_learners_stopped()` 的输入闭合判断,**不直接**把 learner 的更新剔除——真正的准入由 future-base 检查和 staleness 窗口控制。
- full resume 的原子 preparation 会把全部预期 learner 行重置为 `unknown/resumed`，并把当时有效 heartbeat pointer 的内容 SHA256 写入 `run_state.resume_generation`。摄取层只忽略与 fence 完全一致的旧内容；新 active/stopped 原子替换后正常进入本代 sticky 状态。
- syncer 侧的全局保护:`no_progress_timeout_seconds` 内没有任何合并发生 → 停机并发布 stop。
- learner 侧有对称 watchdog：首次加载 latest 后开始计时，严格更新的 full version/fragment global merge event 刷新计时；超过 `syncer_unresponsive_timeout_seconds`（null 时沿用 `no_progress_timeout_seconds`）且 deadline 确认读仍看不到进展或 stop 时，learner 记录 `syncer_unresponsive` 并受控退出。

## 7. 停机协议

syncer 停止条件(任一):

- `sync.stop_after_outer_steps`:外层步数(或 merge event 数)达标 → `stop_after_outer_steps`;
- `sync.stop_after_global_tokens`:累计合并 token 达标 → `stop_after_global_tokens`;
- 全部 expected learner 已 stopped 且 terminal drain 无合法 proposal → `input_exhausted`;
- 无进展超时 → `no_progress_timeout`;
- 异常 → `error`。

成功完成初始化/恢复并进入主循环后，syncer 才在 `finally` 中按顺序尝试发布 `control/stop.json`、等待/末次摄取、写 summary、maintenance 和 W&B 收尾；startup/init/resume 自身异常发生在这层 `try` 之前，不走该序列。主循环收尾若前一步自身抛异常，后续步骤不会被独立保证执行，最内层 finally 仍会尝试 finish W&B 并关 DB。非 `error` 退出会在可配置 shutdown 窗口内等待 learner 收尾并继续摄取；只有全部 learner 确认 stopped 时才将未消费 proposal 终态化。等待超时保持 active 引用不变并记录未确认状态。最终 archive/GC 只在非 error 收尾路径执行；error 路径不把未消费输入伪装成正常终态。fragment 的强制最终 materialize 也只在非 error 路径执行。

learner 的常规停止条件由 `training.completion_mode` 决定，full 与 fragment 共用同一判定。默认 `local_or_global` 在 `max_local_steps` 达标或看到 `stop.json` 时停止；`global_only` 则把 `max_local_steps` 视为名义训练/调度 horizon,达到后继续训练与上传,只在 syncer 达到全局目标并发布 `stop.json` 后退出。若 syncer 未发布 stop 就失去进展，watchdog 提供独立的 `syncer_unresponsive` 自保退出。训练主循环的退出路径写 `status=stopped` 的最终心跳；watchdog 路径同时写 `status_reason=syncer_unresponsive`。启动阶段在进入 runner `try` 前失败则没有这项保证。

有限步训练的收尾由 **terminal drain** 衔接，full 与 fragment 均覆盖:只有全部预期 learner 的最终心跳都明确为 `stopped` 时输入才闭合。grace/reingest 后 selector 返回显式三态：`open`（输入重新打开）、`closed_selected`（按严格 future/staleness/selection policy 选中，允许低于 quorum）或 `closed_empty`。只有 `closed_empty` 能产生 `input_exhausted`；`open` 会复位 terminal grace 并回到常规 discovery。fragment 保持 global event 的目标片调度，不跳过一个已耗尽的目标片去消费其他片；剩余项在终态化阶段处理。`dead` 但未自报 stopped 的 learner 不会触发末端排空。

full 模式可为研究评估显式开启 `sync.capture_terminal_predecessor_for_eval`。低于 `quorum_min` 的 terminal merge 在选中状态写入前，会把当前权威 weight hardlink/copy 到 `eval_checkpoints/` 并记录 source version、checksum、selected/quorum；manifest 是证据包提交点。manifest 前残留 checkpoint 会在校验 source 后复用或原子覆盖，manifest 后任一 identity/checksum/缺文件冲突都 fail closed。这些文件只用于离线 pre/post 评估，不是第二权威，也不参与恢复或 latest 发布。默认关闭时不会创建该目录。

## 8. 容错与崩溃恢复

| 故障 | 行为 |
|---|---|
| learner 崩溃 | 心跳变旧 → stale/dead;其已提交更新照常可被合并。只有剩余贡献者还能满足 `quorum_min` 时才能继续正常 merge；突然崩溃也不会写 current-generation `stopped`，因而不会自动形成 terminal input closure。 |
| learner 变慢 | 其更新 staleness 增大 → 权重被 λ 折减,过窗即弃。 |
| syncer 崩溃 | `init.resume: true` 必须原地打开持久 DB;先跑 `integrity_check`,校验 run/protocol identity,以最大 committed DB 行确定版本,校验权重与 outer 文件及其中 theta 完全一致；单一事务回滚 selected、重置 learner 本代 liveness 并持久化 heartbeat fence，再重建 `latest.json`、archive/GC。DB 缺失或不一致时 fail closed；只要可读 stop 的 reason 或 summary 的 stop_reason 表明非 error 终态，也拒绝恢复，并不要求两文件成对一致。 |
| 半截文件 | 原子发布的 JSON/safetensors 目标不会暴露半文件；张量写完前 pointer 不存在。JSONL/CSV 是追加流，异常中止可能留下不完整尾行，离线 reader 按各自容错规则处理。 |
| 更新文件丢失 | 选择前后各有一次存在性检查;选择后发现丢失 → 丢弃该份、其余回滚为 pending、放弃本次合并重来。 |
| 故障注入 | `failure_sim` 可随机睡眠、跳过上传或 `sys.exit(97)`。exit 仍运行 learner finally 并尝试 stopped 心跳，只模拟非零退出；真正的突然消失需外部 SIGKILL/调度器故障。 |

## 9. 为什么是 SQLite + JSON 的混合

- **大对象与轮询面使用 JSON/safetensors**:payload 不可变,proposal/latest/heartbeat 用原子替换,避免半文件可见;
- **权威提交与活跃状态放持久 SQLite**:版本提交、update 状态转移、identity、proposal frontier 和 active reference 处在同一共享 run 中;
- **历史与活跃状态分离**:applied/dropped update 和旧 global row 先追加并 fsync 到 `metrics/*_history.jsonl`;终态 payload 路径与 active-row 删除在同一 SQLite 事务中写入 `gc_pending`/提交。分析器合并 live DB 与 archive 并按主键去重;运行时不回读 archive;
- **reference-driven GC**:只保留 DB 当前 global/fragment checkpoint、latest 引用的 materialized full、active update payload 和每 learner 固定 pointer。未发布孤儿经过至少两倍 heartbeat/scan 周期的 grace 后删除;已终态化引用由有界 `gc_pending` 立即回收，删除成功或文件已不存在后清行。
- **learner 读侧 GC 竞态防护**：读取 latest 与打开其 weight/outer 文件之间若 syncer 已发布并回收旧 current，full direct/rebase/prediction 与 fragment initial/incremental 都等待严格更新的 pointer 并重跑整个加载回调；成功状态以实际加载版本为准，预算耗尽则保留 `FileNotFoundError` 链 fail closed。
