# 02 详细系统设计

> 术语约定见 [00-glossary.md](00-glossary.md)。反引号内为代码标识符/文件/字段原文,不翻译。

## 1. 进程角色与部署形态

一次 run 由 `N+1` 个进程组成。典型部署(Miyabi PBS,9 节点 = 8 learner + 1 syncer)的进程表:

| 角色 | 数量 | GPU | 入口 | 关键本地状态 |
|---|---|---|---|---|
| learner | static 为 N(`sync.num_learners`);dynamic 由准入/发件箱维护目标容量 | `choose_device()` 有 CUDA 时选首张可见卡,否则 CPU;PBS 通常每进程隔离 1 张 | static 传 `--learner-id learner_000`;dynamic 传 `--bootstrap-slot` 或 `--launch-request-id` 并自行生成 UUID | 模型副本、内层 AdamW、固定数据流数据迭代器 |
| syncer | 1 个活跃;HA 模式可同时存在多个候选但只有一个领导者(leader) | `syncer.device=cuda` 时 1 张;也可用 CPU | `python -m fs_diloco.syncer --config ...` | 全局参数扁平向量 θ、外层优化器状态、共享 run 内的持久 SQLite、HA 模式的 `LeaderToken` |

进程之间没有任何直接连接,所有协调通过共享目录 `run.shared_root` 完成。dataclass 默认值是 `null`,解析时回退到 `<project_root 或 cwd>/runs/fs_diloco/<run_id>`;仓库随附的正式配置显式使用主工作树的绝对 `runs/fs_diloco/{run_id}` 模板,避免从其他 worktree 启动时把产物写散。

## 2. 通信契约(Runtime Contract)

这是整个系统最重要的不变式集合:

1. **大张量一律 safetensors**(权重、外层优化器状态、更新向量、分片)。
2. **按介质选择一致性原语**:JSON 指针/控制面/心跳与 safetensors 通过 `storage/atomic_io.py` 的「同目录临时文件 → 文件 fsync → chmod → `os.replace`」发布,读者只会看到旧文件或完整新文件;SQLite 用回滚日志(rollback-journal)事务;每 actor JSONL、syncer CSV 和历史 JSONL 为单写者追加。`learner_metrics.csv` 与 `update_manifest.csv` 由多个 learner 无锁共享追加,只是尽力而为遥测,不是提交权威。原子 helper 不 fsync 父目录,保证的是运行期原子可见性,而不是断电后目录项必然持久。
3. **提议指针是提交标记**:learner 先写不可变张量载荷,再原子替换固定指针。全量模式为每 learner 一个 `updates/latest/learner_XXX.json`;分片模式为每 `(learner, fragment)` 一个 `updates/latest/learner_XXX_fNNN.json`。syncer 每轮只枚举 `N` 或 `N×K` 个固定路径,持久化摄取水位在两种模式都防重放;只有分片路径额外用进程内 stat 签名跳过未变化指针的 JSON 解析。runtime 不扫描历史载荷元数据。
4. **全局指针按模式选择**。legacy full/fragment 的 learner 轮询 `control/latest.json`;HA full(static 或 dynamic)的 learner 不打开 SQLite,而是从有界 `control/syncer_epochs/` 选择最高合法 epoch,再校验该 epoch 的权威头部、指针路径和 SHA。两者都不扫描权重目录;HA fixed cache 只是便利面,不是权威。dynamic 还读取自校验的准入与排空产物。
5. **心跳 JSON 只是带代际边界的存活提示**,不参与训练版本权威链。static 的 `stopped` 只证明当前 syncer 代际的输入闭合;full resume 会隔离旧指针内容,直到 learner 原子发布不同内容的新心跳。dynamic 心跳还必须匹配 current instance/部署位置/数据流/准入令牌,但已摄取提议能否提交仍由最终数据库事务重验,不能由心跳单独决定。
6. **SQLite 是共享目录中的持久提交记录**:`control/syncer_metadata.sqlite3`,使用回滚日志(`journal_mode=DELETE`)和 `synchronous=FULL`。legacy 只有单 syncer 写者;HA 候选先取得单调 epoch,领导者的每个业务事务在 `BEGIN IMMEDIATE` 后校验 epoch/持有者,旧令牌随后不能续约或提交。不同计算节点可以重开并恢复同一 run;不使用 WAL、节点本地副本或 DB dump。
7. **learner 的全局采纳由单一策略状态机决定**:`replace`(替换)直接整体覆盖并重置内层优化器矩;调度器对象可重建,但始终恢复到累计本地步相位。rebase(变基)/prediction(预测)对齐在合成尚未发布的本地差值后保留完整内层训练状态。不存在与此并行的配置布尔开关。
8. **外层优化器是显式扁平向量实现**(`modeling/outer_optim.py`),不复用 `torch.optim`,以便把优化器状态精确序列化成 safetensors 并跨 resume 保持一致。

## 3. 参数的扁平向量表示

- syncer 初始化时构建 **param index**(参数索引,`modeling/param_index.py`):按 `named_parameters()` 声明顺序记录每个可训练参数的 `name/shape/dtype/numel/offset`,发布为 `control/param_index.json`。
- 双方启动时都会用本地模型重建一份索引并 `validate_compatible_index()` 严格比对顶层四字段和完整 `params` Python 列表,保证两边「模型 → 扁平向量」的语义映射一致;它不比较 JSON 原始字节或排版。
- 全局权重文件按**参数名 → 张量**存储(便于单独加载/导出为 HF 模型);更新向量按单键 `local_params` 存储扁平向量;分片按单键 `fragment_params` 存储。

## 4. 合并协议(核心算法)

### 4.1 资格筛选

一份待处理(pending)更新有资格参与第 `v → v+1` 次合并,当且仅当:

- 状态为 `pending`;
- `base_global_version ≤ v`,且 `staleness = v − base_global_version ≤ sync.max_staleness_versions`;
- 其张量文件仍然存在(丢失则标记 `dropped(missing_file)`)。

### 4.2 每 learner 选一

`protocol/merge.py: select_one_per_learner()`——同一 learner 若有多份合格更新,按策略选一份:

- `most_recent_per_learner`(默认):取 `(local_step_end, committed_at)` 最大者;
- `oldest_pending`:取 `committed_at` 最小者。

同一 `sync.selection_policy` 同时用于常规合并与末端排空,不会在末端切换策略。结果截断到 `quorum_max` 份。

### 4.3 法定人数与宽限窗口

- 合格更新(每 learner 一份)不足 `quorum_min` → 不合并,睡 `scan_interval_seconds` 后重扫;持续无进展超过 `liveness.no_progress_timeout_seconds` 则以 `no_progress_timeout` 停机。
- 达到 `quorum_min` → 进入宽限窗口:固定模式等待 `fixed_seconds`;自适应模式从 `initial_seconds` 开始,根据 syncer 进程首次摄取更新的单调时钟时刻加 `local_cycle_step_time_seconds_mean × inner_steps` 估计最快下一次上传,动态把截止时刻向前收紧但绝不延长。首见时刻通常比 learner 提交晚,常规轮询下约为一个扫描周期,但 checkpoint I/O、调度或启动时序可使延迟更长;晚记录会让预估截止时刻更晚,方向保守。resume 后没有进程内首见记录的旧更新不收紧窗口。循环重扫元数据,凑满 `quorum_max` 份或截止时刻到达为止。

### 4.4 token × 陈旧度加权平均

对选中的更新集合 S(`protocol/merge.py`):

```
raw_i  = tokens_i / (1 + λ · staleness_i)        λ = sync.staleness_lambda
w_i    = raw_i / Σ raw_j                          (归一化)
p̄      = Σ w_i · p_i                              (参数加权平均)
```

- `tokens_i` 是该更新区间实际消费的 token 数——吞吐高的 learner 权重更大;
- 陈旧度越大权重越低,λ 控制惩罚力度。

### 4.5 外层步进与发布

```
g      = θ − p̄                                    (外层伪梯度)
θ', st' = outer_optimizer_step(θ, g, st)          (SGD / momentum / Nesterov / AdamW,见 modules/modeling.md)
```

随后 syncer 默认用两个有界 I/O worker 并发原子保存 `global_v{v+1}.safetensors` 与 `outer_v{v+1}.safetensors`;`parallel_checkpoint_writes=false` 时改为 weight→outer 串行。主线程确认**两份均成功**后,才在一个 SQLite 事务中校验前驱/目标版本与选中集合,插入已提交的 `global_versions` 行,把选中更新标记 `applied`,并把被取代、过期或未来版本提议终态化 → 原子覆盖 `latest.json` → 归档/GC。worker 不写数据库/latest;任一写失败都不会提交版本。事务提交是全局版本的正确性边界;`latest.json` 只在提交后更新。

### 4.6 更新丢弃规则

| drop_reason(丢弃原因) | 触发条件 |
|---|---|
| `missing_file`(文件缺失) | 提议已摄取但不可变载荷不存在或读取前消失 |
| `too_stale`(过时) | 陈旧度超过 `max_staleness_versions`;末端排空也不放宽这个正确性边界 |
| `superseded`(被取代) | 同一 learner 有更新的一份已被选中,较旧的待处理份被取代 |
| `future_base`(基准超前) | 提议的基准高于目标提交允许的版本,不能用于当前分支 |

以上是运行时代码写入 SQLite/历史 JSONL 的**字面值**。`stale`、`future_base_version` 等旧称只可能出现在历史 run 或兼容分析逻辑中。

丢弃原因只适用于已经摄取入库的提议。learner 若在 syncer 第一次观察前连续替换同一固定指针,中间提议不会进入数据库,也不会得到 `dropped` 行;其不可变载荷作为未引用孤儿经过 maintenance 宽限期后删除,产生侧只能依赖 learner 日志/清单的尽力而为证据。

## 5. 分片(fragment)模式

### 5.1 分片定义

`protocol/fragment_index.py` 在扁平向量上定义 K 个**互不重叠、完全覆盖**的分片:

- `full`:K=1,退化为全量(但走分片协议路径);
- `balanced_tensor`:以**整个张量**为最小单位,按 numel 从大到小贪心装入当前最小的桶(不切开单个张量),`num_fragments ≤ 可训练张量数`。

分片索引发布为 `fragments/fragment_index.json`,构建时校验覆盖性、连续性、id 连续和每片 numel;传入参数索引时还确认每个切片(参数名)存在。校验器并不逐项重算切片内的 shape/dtype/参数偏移,这些由构建器从参数条目直接生成。

### 5.2 双侧轮转(round-robin)调度

- **syncer 侧**:第 `e` 次全局合并事件的目标分片为 `e mod K`(`protocol/fragment_scheduler.py: select_fragment`)。每次合并只处理目标片的更新,该片 `fragment_version +1`,同时 `global_merge_event +1`。
- **learner 侧**:第 `u` 次上传(`local_update_index`)上传 `u mod K` 号片。learner 按分片索引直接从对应的命名参数切片构造连续分片,不会先物化完整扁平向量;因此 CPU 暂存和 GPU→CPU 搬运量只与目标片大小相关。

因此每个分片的更新供给和消费频率天然对齐,`expected_fragment_versions_after_events()` 可静态推算 E 次事件后各片应有的版本(分析工具用它做断言)。

### 5.3 合并与发布的差异

- 合并数学与全量模式相同,只是 θ、p、优化器状态都是**每片一份**(`fragment_thetas[k]`、`outer_states[k]`);陈旧度以 `base_fragment_version` 对比该片当前版本计。
- `latest.json` 采用 `latest_kind: "fragment"` 布局:携带 `global_merge_event`、每片 `{version, weight_path, optim_path, updated_at_global_merge_event}`,以及最近一次物化的完整权重路径。
- **物化(materialize)**:按显式正整数 `fragments.materialize_full_every_events` 周期(以及事件 0、到达目标步数和任意正常终止时)把所有片拼回完整向量,存成 `weights/global_v{event:06d}.safetensors`,供评测/导出使用;开启 fragment 而缺失该字段会 fail closed。
- **learner 采纳是增量的**:对比 `latest.json` 中每片版本与本地已加载版本,只加载变化的片、散回(scatter)进本地扁平向量再写回模型(`adopt_fragment_updates`),并按 `fragments.reset_inner_optimizer_on_fragment_adopt` 决定是否重置内层优化器。

learner 上传文件的 dtype 由 `io.tensor_dtype` 决定。使用 BF16 时,全量和分片的参数载荷都约为 FP32 的一半。syncer 的 `load_update_vector()` / `load_fragment_update()` 在读取时转换为 `syncer.compute_dtype`;全局参数、聚合与外层优化器浮点状态也使用该 dtype。global/fragment 权重及外层状态按 `syncer.publish_dtype` 发布,因此可以分别控制 learner→syncer 更新、syncer 数值路径和 syncer→learner/checkpoint 三段各自的精度与 I/O 大小。

### 5.4 限制

- `fragments_per_update` 固定为 1;调度只支持 `round_robin_global`;resume 未实现。
- full 发布的「checkpoint 成功 → 单一 SQLite 提交 → latest」事务边界不适用于分片:分片权重、外层状态、`fragment_versions`、latest 和 update applied/drop 是顺序执行的独立持久化步骤。分片 syncer 崩溃可能留下跨文件的部分发布状态,而该模式又没有 resume;因此不能把它描述为 full 模式同等级的可恢复提交协议。

## 6. 存活管理(liveness)

- learner 每 `liveness.heartbeat_interval_seconds` 原子写 `heartbeats/<learner_id>.json`(含 phase、last_local_step、tokens/s、已加载版本等)。
- syncer 每轮扫描心跳入库,并按心跳年龄分类(`protocol/liveness.py: classify_liveness`):

| 状态 | 条件 |
|---|---|
| `active` | 心跳年龄 ≤ `stale_after_seconds` |
| `stale` | ≤ `dead_after_seconds` |
| `dead` | 超过 `dead_after_seconds` 或从未见过 |
| `stopped` | learner 退出时自报,粘性(不再被重分类) |

- 存活状态只影响观测与 `all_expected_learners_stopped()` 的输入闭合判断,**不直接**把 learner 的更新剔除——真正的准入由 future-base 检查和陈旧度窗口控制。
- full resume 的原子准备会把全部预期 learner 行重置为 `unknown/resumed`,并把当时有效心跳指针的内容 SHA256 写入 `run_state.resume_generation`。摄取层只忽略与隔离栅栏完全一致的旧内容;新 active/stopped 原子替换后正常进入本代粘性状态。
- syncer 侧的全局保护:`no_progress_timeout_seconds` 内没有任何合并发生 → 停机并发布 stop。
- learner 侧有对称看门狗(watchdog):首次加载 latest 后开始计时,严格更新的 full version/fragment global merge event 刷新计时;超过 `syncer_unresponsive_timeout_seconds`(null 时沿用 `no_progress_timeout_seconds`)且截止确认读仍看不到进展或 stop 时,learner 记录 `syncer_unresponsive` 并受控退出。

## 7. 停机协议

syncer 停止条件(任一):

- `sync.stop_after_outer_steps`:外层步数(或合并事件数)达标 → `stop_after_outer_steps`;
- `sync.stop_after_global_tokens`:累计合并 token 达标 → `stop_after_global_tokens`;
- 全部预期 learner 已 stopped 且末端排空无合法提议 → `input_exhausted`;
- 无进展超时 → `no_progress_timeout`;
- 异常 → `error`。

成功完成初始化/恢复并进入主循环后,syncer 才在 `finally` 中收尾;startup/init/resume 自身异常发生在这层 `try` 之前,不走该序列。legacy 路径按 stop、等待/末次摄取、summary、maintenance 和 W&B 顺序执行。HA 路径先提交并发布 early stop 世代,让 learner 停止;完成 summary 与 maintenance 后再以更高世代成对发布最终权威 stop/summary。候选进程只有验证最终世代对应的两条数据库发布记录及文件 SHA 后才将 run 判为不可重启;任一步崩溃留下的不完整终态由后继者以新 epoch 幂等修复。主循环收尾若前一步自身抛异常,后续步骤不会被独立保证执行,最内层 finally 仍会尝试结束 W&B 并关数据库。非 `error` 退出会在可配置停机窗口内等待 learner 收尾并继续摄取;只有全部 learner 确认 stopped 时才将未消费提议终态化。等待超时保持 active 引用不变并记录未确认状态。最终归档/GC 只在非 error 收尾路径执行;error 路径不把未消费输入伪装成正常终态。分片的强制最终物化也只在非 error 路径执行。

learner 的常规停止条件由 `training.completion_mode` 决定,full 与 fragment 共用同一判定。默认 `local_or_global` 在 `max_local_steps` 达标或看到 `stop.json` 时停止;`global_only` 则把 `max_local_steps` 视为名义训练/调度上限,达到后继续训练与上传,只在 syncer 达到全局目标并发布 `stop.json` 后退出。若 syncer 未发布 stop 就失去进展,看门狗提供独立的 `syncer_unresponsive` 自保退出。训练主循环的退出路径写 `status=stopped` 的最终心跳;看门狗路径同时写 `status_reason=syncer_unresponsive`。启动阶段在进入 runner `try` 前失败则没有这项保证。

有限步训练的收尾由 **terminal drain**(末端排空)衔接,full 与 fragment 均覆盖:只有全部预期 learner 的最终心跳都明确为 `stopped` 时输入才闭合。宽限/重新摄取后选择器返回显式三态:`open`(输入重新打开)、`closed_selected`(按严格 future/staleness/选择策略选中,允许低于法定人数)或 `closed_empty`(闭合且无候选)。只有 `closed_empty` 能产生 `input_exhausted`;`open` 会复位末端宽限并回到常规发现。fragment 保持全局事件的目标片调度,不跳过一个已耗尽的目标片去消费其他片;剩余项在终态化阶段处理。`dead` 但未自报 stopped 的 learner 不会触发末端排空。

full 模式可为研究评估显式开启 `sync.capture_terminal_predecessor_for_eval`。低于 `quorum_min` 的 terminal merge 在选中状态写入前,会把当前权威 weight 硬链接/复制到 `eval_checkpoints/` 并记录 source version、校验和、selected/quorum;清单是证据包提交点。清单前残留 checkpoint 会在校验 source 后复用或原子覆盖,清单后任一 identity/checksum/缺文件冲突都 fail closed。这些文件只用于离线 pre/post 评估,不是第二权威,也不参与恢复或 latest 发布。默认关闭时不会创建该目录。

## 8. 容错与崩溃恢复

| 故障 | 行为 |
|---|---|
| learner 崩溃 | 心跳变旧 → stale/dead;其已提交更新照常可被合并。只有剩余贡献者还能满足 `quorum_min` 时才能继续正常 merge;突然崩溃也不会写 current-generation `stopped`,因而不会自动形成末端输入闭合。 |
| learner 变慢 | 其更新陈旧度增大 → 权重被 λ 折减,过窗即弃。 |
| syncer 崩溃 | `init.resume: true` 必须原地打开持久 DB;先跑 `integrity_check`,校验 run/protocol identity,以最大已提交 DB 行确定版本,校验权重与外层文件及其中 theta 完全一致;单一事务回滚 selected、重置 learner 本代存活状态并持久化心跳隔离栅栏,再重建 `latest.json`、归档/GC。DB 缺失或不一致时 fail closed;只要可读 stop 的 reason 或 summary 的 stop_reason 表明非 error 终态,也拒绝恢复,并不要求两文件成对一致。 |
| 半截文件 | 原子发布的 JSON/safetensors 目标不会暴露半文件;张量写完前指针不存在。JSONL/CSV 是追加流,异常中止可能留下不完整尾行,离线读取器按各自容错规则处理。 |
| 更新文件丢失 | 选择前后各有一次存在性检查;选择后发现丢失 → 丢弃该份、其余回滚为待处理、放弃本次合并重来。 |
| 故障注入 | `failure_sim` 可随机睡眠、跳过上传或 `sys.exit(97)`。exit 仍运行 learner finally 并尝试 stopped 心跳,只模拟非零退出;真正的突然消失需外部 SIGKILL/调度器故障。 |

## 9. 为什么是 SQLite + JSON 的混合

- **大对象与轮询面使用 JSON/safetensors**:载荷不可变,提议/latest/心跳用原子替换,避免半文件可见;
- **权威提交与活跃状态放持久 SQLite**:版本提交、更新状态转移、identity、提议摄取水位和 active 引用处在同一共享 run 中;
- **历史与活跃状态分离**:applied/dropped 更新和旧 global 行先追加并 fsync 到 `metrics/*_history.jsonl`;终态载荷路径与活跃行删除在同一 SQLite 事务中写入 `gc_pending`/提交。分析器合并活跃 DB 与归档并按主键去重;运行时不回读归档;
- **引用驱动 GC**:只保留 DB 当前 global/fragment checkpoint、latest 引用的物化完整权重、active 更新载荷和每 learner 固定指针。未发布孤儿经过至少两倍心跳/扫描周期的宽限期后删除;已终态化引用由有界 `gc_pending` 立即回收,删除成功或文件已不存在后清行。
- **learner 读侧 GC 竞态防护**:读取 latest 与打开其 weight/outer 文件之间若 syncer 已发布并回收旧 current,full direct/rebase/prediction 与 fragment initial/incremental 都等待严格更新的指针并重跑整个加载回调;成功状态以实际加载版本为准,预算耗尽则保留 `FileNotFoundError` 链 fail closed。

## 10. Full-mode Syncer HA 与动态成员

`coordination.syncer_ha.enabled=true` 只支持 full,成员可为 `static` 或 `dynamic`。启动顺序是 initializer → 独立 syncer candidate job → 独立 learner jobs。initializer 是唯一 DDL 写者:它发布解析后配置、source manifest、run descriptor、数据库和最后的 bootstrap-complete 标记;static 使用 schema v2,dynamic 使用 schema v3 并预建固定数据流池和确定性引导启动请求。candidate 与 learner 在 import runtime 前校验 descriptor/source/config identity,既有或不完整 run 均 fail closed。

`LeaderLeaseStore` 在同一个 SQLite 中分配不复用的 epoch。candidate 获取成功后才得到 `FencedSQLiteStore` 的领导者绑定写面;所有 HA 业务修改器都必须携带 `LeaderToken(epoch, owner_id)`,并在持锁事务内重验。旧写者在事务外暂停时可在租约到期后被接管;若它暂停在 SQLite 写事务内,新候选必须等待调度器/操作员终止旧写者并释放锁。这是明确的可用性边界,不会以双写者换取接管速度。

每个 epoch 的 checkpoint 与权威 `head/stop/summary` 使用互不冲突的目录。SQLite 已提交行和 `control_publications` 清单是后继者/Checker 的恢复依据;后继者从 DB current version 恢复,修复同 epoch 缺失的控制面,再提交严格的 `N+1`。learner 侧 `EpochControlReader` 扫描有界 epoch 目录,以自校验心跳或权威头部识别最高合法 epoch,并用头部内的指针路径/SHA 校验不可变 latest;若最高 epoch 尚无头部则等待而不回退。fixed cache 允许被旧 epoch 覆盖。maintenance 先以隔离事务登记候选,删除前重查引用,并压缩旧 epoch 目录/历史;默认 `io.checkpoint_digest_mode=off`,以唯一路径、必填 size 和 safetensors 可加载性验证大文件。

这一链路已在最终 clean-source 的独立 1-syncer + 8-learner Miyabi run 中验证。验收绑定 source commit `36762854bfcbbc23b71ab838913023d64cf37b5e`:epoch 1 候选在 v0 DB 提交后、控制面发布前被 `SIGKILL`,独立后继者取得 epoch 2、修复权威控制面并连续提交 v1–v10;8 个 learner 都在独立 GPU 节点贡献更新并正常停止。最终为 5120 seen tokens,120 次租约续约与 457 次业务事务均无失败,stale epoch commit 与权威采纳错误均为 0;400+400 个业务样本和 100+100 个 checkpoint 样本也通过冻结的 matched p99 门禁,completed Checker 返回 `PASS`。精确 PBS 作业和产物见[运维文档](07-operations.md#4-miyabi-pbs-%E6%89%B9%E4%BD%9C%E4%B8%9A)。这是恢复、协调与控制面性能验证,不是训练质量结论。

dynamic 把成员权威放入同一个隔离 SQLite:`learner_instances`、`placements`、`streams`、`registration_requests`、`launch_requests` 和 `capacity_observations` 共同描述 current incarnation。每次 learner 启动创建新的 `learner_li_<uuid4>`,注册必须绑定 source/config、路径所有权、bootstrap slot 或扩容请求以及实际 PBS job identity。若注册携带调度器 job ID 而对应启动行尚未持久化精确回执绑定,leader 只保存 pending 请求并保留其文件;绑定出现后才重试准入,错绑则拒绝。准入事务分配部署位置纪元、数据流 ID/纪元、成员世代和一次性令牌;健康部署位置不能被普通重复驱逐,logical 启动请求最多满足一次。提议摄取携带整套成员隔离栅栏,最终全局提交在同一事务中重验 instance、部署位置、数据流和令牌,竞态失效的选中不会被提交。

数据流池在 initializer 后不可变。数据分片和随机种子使用 `stream_id/stream_pool_size`,不使用瞬时活跃数;同部署位置替换可按策略复用数据流,但必须提升 `stream_epoch` 并记录重启。leader 周期写唯一容量观测,只有不同观测组成的连续 low 窗口才能创建扩容请求。成功 merge 把 `global_versions(v)` 与 `merge:v` 观测放在同一事务;无 merge 路径也把新饥饿世代与其观测原子提交,崩溃不会留下序列缺口。pending/queued/running 作业始终占用预留容量,直到调度器确认终态并释放;冷却期、pending/total 预算以及 `admitted + reserved <= stream_pool_size` 共同限制扩容。发件箱把请求、qsub 回执、qstat 对账和准入映射持久化,qsub 本身不授予成员资格。

dynamic 收尾由持久控制器状态驱动。全局目标、token 目标、认证 manual close、截止时间、耗尽的启动预算或 no-progress 会在一个事务中关闭准入、取消尚未提交的 open 请求并冻结 `close_generation/max_terminal_version`;token 目标以检测时的 current version 作为冻结上限,no-progress 也进入排空而不直接发布终态。leader 发布自校验排空产物。健康实例在周期边界停止新提议、写最终指针并确认该世代;超时未响应者被隔离撤销。注册/提议可见性宽限期结束、所有 logical 请求已终态且每个 current instance 都确认或撤销后,`dynamic_input_closed` 才成立;后继者恢复同一关闭原因与冻结上限,非 error 终态只有在控制器已关闭且输入确实闭合后才能提交。

审查整改后的 Phase 2 正式 9 节点 dynamic 验收完成 v120:8 个 bootstrap 成员稳定后永久终止一个 learner,两个唯一 low 观测创建一个 replacement 请求并恢复 8 个 current contributor;重复物理 job 被拒绝,replacement 复用数据流并提升数据流纪元,最终排空/确认闭合。每周期为 51 local steps,超过 50×10 文档同步基线;completed Checker 和同 source/config/model/data/seed/v120 的 static/dynamic matched 门禁均为 `PASS`,冻结公式下额外 control-path 开销为 0。证据绑定 commit `61f571bbe4460b257abe8452c2ea63df79515b29` 和 fingerprint `sha256:cdf8f01bdb6f4bfd62dbe9a1103bca0a14f8b029ef3eaf12d8c77221aa94d0c0`;精确作业和产物见[运维文档](07-operations.md#4-miyabi-pbs-%E6%89%B9%E4%BD%9C%E4%B8%9A)。这些仍是恢复、成员与控制面结论,不是训练质量结论。
