# 02 详细系统设计

## 1. 进程角色与部署形态

一次 run 由 `N+1` 个进程组成,典型部署(Miyabi PBS,9 节点 = 8 learner + 1 syncer):

| 角色 | 数量 | GPU | 入口 | 关键本地状态 |
|---|---|---|---|---|
| learner | N(`sync.num_learners`) | 每进程 1 张 | `python -m fs_diloco.learner --config ... --learner-id learner_000` | 模型副本、内层 AdamW、数据分片迭代器 |
| syncer | 1 | 1 张(合并与外层步进在 GPU 上做) | `python -m fs_diloco.syncer --config ...` | 全局参数扁平向量 θ、外层优化器状态、共享 run 内的持久 SQLite |

进程间没有任何直接连接。所有协调通过共享目录 `run.shared_root`(默认 `runs/fs_diloco/<RUN_ID>`)完成。

## 2. 通信契约(Runtime Contract)

这是整个系统最重要的不变式集合:

1. **大张量一律 safetensors**(权重、外层优化器状态、update 向量、fragment)。
2. **原子发布**:所有共享文件通过 `storage/atomic_io.py` 的"写临时文件 → fsync → `os.replace`"发布。读者要么看到旧文件、要么看到完整的新文件,永远不会读到半截。
3. **proposal pointer 是提交标记**:全量 learner 先写不可变张量 payload、再原子替换 `updates/latest/learner_XXX.json`;syncer 每轮只读取 `N` 个固定路径。fragment 模式暂保留 payload 目录扫描,但消费后同样由统一 maintenance 回收。
4. **`control/latest.json` 是 learner 轮询的唯一全局指针**。learner 不扫描权重目录、不读数据库。
5. **心跳 JSON 只是存活提示**,不参与正确性(丢心跳最多导致 liveness 误判,不会丢更新)。
6. **SQLite 是共享目录中的持久提交记录**:`control/syncer_metadata.sqlite3`,使用 rollback journal(`journal_mode=DELETE`)、`synchronous=FULL`、60 秒 busy timeout。只有 syncer 改业务表,但不同计算节点可以重开并恢复同一 run;不使用 WAL、节点本地副本或 DB dump。
7. **learner 采用"整体覆盖"语义**:采纳新全局版本时,把整个模型参数替换为全局权重,并**重置内层优化器与调度器**(`inner_optimizer.reset_on_global_update`,当前实现固定重置)。
8. **外层优化器是显式扁平向量实现**(`modeling/outer_optim.py`),不复用 `torch.optim`,以便把优化器状态精确序列化成 safetensors 并跨 resume 保持一致。

## 3. 参数的扁平向量表示

- syncer 初始化时构建 **param index**(`modeling/param_index.py`):按 `named_parameters()` 声明顺序记录每个可训练参数的 `name/shape/dtype/numel/offset`,发布为 `control/param_index.json`。
- 双方启动时都会用本地模型重建一份 index 并 `validate_compatible_index()` 严格比对,保证两边"模型 → 扁平向量"的映射逐字节一致。
- 全局权重文件按**参数名 → 张量**存储(便于单独加载/导出为 HF 模型);update 向量按单键 `local_params` 存储扁平向量;fragment 按单键 `fragment_params` 存储。

## 4. 合并协议(核心算法)

### 4.1 资格筛选

一个 pending update 有资格参与第 `v → v+1` 次合并,当且仅当:

- 状态为 `pending`;
- `staleness = v − base_global_version ≤ sync.max_staleness_versions`;
- 其张量文件仍然存在(丢失则标记 `dropped(missing_file)`)。

### 4.2 每 learner 选一

`protocol/merge.py: select_one_per_learner()`——同一 learner 若有多份合格更新,按策略选一份:

- `most_recent_per_learner`(默认):取 `(local_step_end, committed_at)` 最大者;
- `oldest_pending`:取 `committed_at` 最小者(仅 terminal drain 使用)。

结果截断到 `quorum_max` 份。

### 4.3 quorum 与宽限窗口

- 合格 update(每 learner 一份)不足 `quorum_min` → 不合并,睡 `scan_interval_seconds` 后重扫;持续无进展超过 `liveness.no_progress_timeout_seconds` 则以 `no_progress_timeout` 停机。
- 达到 `quorum_min` → 进入宽限窗口:固定模式等待 `fixed_seconds`;自适应模式从 `initial_seconds` 开始,根据已选 learner 的 `committed_at + local_cycle_step_time_seconds_mean × inner_steps` 估计最快下一次上传,动态把 deadline 向前收紧但绝不延长。循环重扫元数据,凑满 `quorum_max` 份或 deadline 到达为止。

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

随后 syncer 依次:原子保存 `global_v{v+1}.safetensors` 与 `outer_v{v+1}.safetensors` → 在**一个 SQLite 事务**中校验前驱/目标版本与 selected 集合,插入 committed `global_versions` 行,把 selected 更新标记 `applied` 并把被取代、过期或未来版本 proposal 终态化 → 原子覆盖 `latest.json` → archive/GC。事务提交是全局版本的正确性边界;`latest.json` 只在提交后更新。

### 4.6 更新丢弃规则

| drop_reason | 触发条件 |
|---|---|
| `missing_file` | proposal 已摄取但不可变 payload 不存在或读取前消失 |
| `stale` | staleness 超过 `max_staleness_versions`;terminal drain 也不放宽这个正确性边界 |
| `superseded` | 同一 learner 有更新的一份已被选中,较旧的 pending 份被取代 |
| `future_base_version` | proposal 的 base 高于当前 committed version,不能用于当前分支 |

## 5. fragment 分片模式

### 5.1 分片定义

`protocol/fragment_index.py` 在扁平向量上定义 K 个**互不重叠、完全覆盖**的分片:

- `full`:K=1,退化为全量(但走 fragment 协议路径);
- `balanced_tensor`:以**整个张量**为最小单位,按 numel 从大到小贪心装入当前最小的桶(不切开单个张量),`num_fragments ≤ 可训练张量数`。

fragment index 发布为 `fragments/fragment_index.json`,构建时经过严格校验(覆盖性、连续性、id 连续、与 param index 一致)。

### 5.2 双侧 round-robin 调度

- **syncer 侧**:第 `e` 次全局合并事件的目标 fragment 为 `e mod K`(`protocol/fragment_scheduler.py: select_fragment`)。每次合并只处理目标片的更新,该片 `fragment_version +1`,同时 `global_merge_event +1`。
- **learner 侧**:第 `u` 次上传(`local_update_index`)上传 `u mod K` 号片。learner 按 fragment index 直接从对应的命名参数切片构造连续 fragment,不会先物化完整扁平向量;因此 CPU 暂存和 GPU→CPU 搬运量只与目标片大小相关。

因此每个 fragment 的更新供给和消费频率天然对齐,`expected_fragment_versions_after_events()` 可静态推算 E 次事件后各片应有的版本(分析工具用它做断言)。

### 5.3 merge 与发布的差异

- 合并数学与全量模式相同,只是 θ、p、优化器状态都是**每片一份**(`fragment_thetas[k]`、`outer_states[k]`);staleness 以 `base_fragment_version` 对比该片当前版本计。
- `latest.json` 采用 `latest_kind: "fragment"` 布局:携带 `global_merge_event`、每片 `{version, weight_path, optim_path, updated_at_global_merge_event}`,以及最近一次 materialize 的完整权重路径。
- **materialize**:按 `fragments.materialize_full_every_events` 周期(以及事件 0 和到达目标步数时)把所有片拼回完整向量,存成 `weights/global_v{event:06d}.safetensors`,供评测/导出使用。
- **learner 采纳是增量的**:对比 `latest.json` 中每片版本与本地已加载版本,只加载变化的片、scatter 进本地扁平向量再写回模型(`adopt_fragment_updates`),并按 `fragments.reset_inner_optimizer_on_fragment_adopt` 决定是否重置内层优化器。

learner 上传文件的 dtype 由 `io.tensor_dtype` 决定。使用 BF16 时,全量和 fragment 的参数 payload 都约为 FP32 的一半;syncer 的 `load_update_vector()` / `load_fragment_update()` 在合并前统一提升为 FP32,外层参数、聚合与优化器状态仍保持 FP32。BF16 只压缩 learner→共享文件系统这段传输与落盘,不会把 syncer 的数值主路径降为 BF16。

### 5.4 限制

- `fragments_per_update` 固定为 1;调度只支持 `round_robin_global`;resume 未实现。

## 6. liveness(存活管理)

- learner 每 `liveness.heartbeat_interval_seconds` 原子写 `heartbeats/<learner_id>.json`(含 phase、last_local_step、tokens/s、已加载版本等)。
- syncer 每轮扫描心跳入库,并按心跳年龄分类(`protocol/liveness.py: classify_liveness`):

| 状态 | 条件 |
|---|---|
| `active` | 心跳年龄 ≤ `stale_after_seconds` |
| `stale` | ≤ `dead_after_seconds` |
| `dead` | 超过 `dead_after_seconds` 或从未见过 |
| `stopped` | learner 退出时自报,粘性(不再被重分类) |

- liveness 只影响观测与 `finite_local_training_complete()` 判断,**不直接**把 learner 的更新剔除——真正的准入由 staleness 窗口控制。
- syncer 侧的全局保护:`no_progress_timeout_seconds` 内没有任何合并发生 → 停机并发布 stop。

## 7. 停机协议

syncer 停止条件(任一):

- `sync.stop_after_outer_steps`:外层步数(或 merge event 数)达标 → `stop_after_outer_steps`;
- `sync.stop_after_global_tokens`:累计合并 token 达标 → `stop_after_global_tokens`;
- 无进展超时 → `no_progress_timeout`;
- 异常 → `error`。

无论哪种原因,syncer 退出前都会:发布 `control/stop.json`(含 reason/version)→ 等待 learner 收尾并摄取最后 proposal → 将未消费 proposal 终态化 → 写 summary → archive/GC → 关闭 W&B(fragment 模式还会先做一次最终 materialize)。

learner 停止条件由 `training.completion_mode` 决定。默认 `local_or_global` 保持原语义:`max_local_steps` 达标或看到 `stop.json`(fragment 设置本地上限时仍由本地上限收尾);`global_only` 则把 `max_local_steps` 视为名义训练/调度 horizon,达到后继续训练与上传,只在 syncer 达到全局目标并发布 `stop.json` 后退出。退出前写 `status=stopped` 的最终心跳。

有限步训练的收尾由 **terminal drain** 衔接:只有全部预期 learner 的最终心跳都明确为 `stopped` 时输入才闭合。syncer 再等待一个 grace/reingest 周期,随后用当前严格的 future/staleness 准入规则、`oldest_pending` 策略和放宽的 quorum 合并剩余 proposal;达到目标或耗尽合法输入后以 `input_exhausted` 停止。`dead` 但未自报 stopped 的 learner 不会触发末端排空。

## 8. 容错与崩溃恢复

| 故障 | 行为 |
|---|---|
| learner 崩溃 | 心跳变旧 → stale/dead;其已提交更新照常可被合并;quorum 机制容忍缺席。 |
| learner 变慢 | 其更新 staleness 增大 → 权重被 λ 折减,过窗即弃。 |
| syncer 崩溃 | `init.resume: true` 必须原地打开持久 DB;先跑 `integrity_check`,校验 run/protocol identity,以最大 committed DB 行确定版本,校验权重与 outer 文件及其中 theta 完全一致,重置遗留 selected,重建 `latest.json`,再做 archive/GC。DB 缺失或不一致时 fail closed。 |
| 半截文件 | 不可能出现(原子 rename);张量写完前元数据不存在,syncer 不会看见。 |
| 更新文件丢失 | 选择前后各有一次存在性检查;选择后发现丢失 → 丢弃该份、其余回滚为 pending、放弃本次合并重来。 |
| 故障注入 | `failure_sim` 配置节可让 learner 随机睡眠/跳过上传/以 exit 97 崩溃,用于韧性实验。 |

## 9. 为什么是 SQLite + JSON 的混合

- **大对象与轮询面使用 JSON/safetensors**:payload 不可变,proposal/latest/heartbeat 用原子替换,避免半文件可见;
- **权威提交与活跃状态放持久 SQLite**:版本提交、update 状态转移、identity、proposal frontier 和 active reference 处在同一共享 run 中;
- **历史与活跃状态分离**:applied/dropped update 和旧 global row 先追加并 fsync 到 `metrics/*_history.jsonl`,再从 DB 删除;分析器合并 live DB 与 archive 并按主键去重;
- **reference-driven GC**:只保留 DB 当前 global/fragment checkpoint、latest 引用的 materialized full、active update payload 和每 learner 固定 pointer。未发布孤儿经过至少两倍 heartbeat/scan 周期的 grace 后删除;已终态化引用可立即回收。
