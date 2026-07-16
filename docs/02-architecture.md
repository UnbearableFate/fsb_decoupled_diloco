# 02 详细系统设计

## 1. 进程角色与部署形态

一次 run 由 `N+1` 个进程组成,典型部署(Miyabi PBS,9 节点 = 8 learner + 1 syncer):

| 角色 | 数量 | GPU | 入口 | 关键本地状态 |
|---|---|---|---|---|
| learner | N(`sync.num_learners`) | 每进程 1 张 | `python -m fs_diloco.learner --config ... --learner-id learner_000` | 模型副本、内层 AdamW、数据分片迭代器 |
| syncer | 1 | 1 张(合并与外层步进在 GPU 上做) | `python -m fs_diloco.syncer --config ...` | 全局参数扁平向量 θ、外层优化器状态、**节点本地** SQLite |

进程间没有任何直接连接。所有协调通过共享目录 `run.shared_root`(默认 `runs/fs_diloco/<RUN_ID>`)完成。

## 2. 通信契约(Runtime Contract)

这是整个系统最重要的不变式集合:

1. **大张量一律 safetensors**(权重、外层优化器状态、update 向量、fragment)。
2. **原子发布**:所有共享文件通过 `storage/atomic_io.py` 的"写临时文件 → fsync → `os.replace`"发布。读者要么看到旧文件、要么看到完整的新文件,永远不会读到半截。
3. **update 的 `.meta.json` 是提交标记**:learner 先写张量文件、再写元数据 JSON。syncer 只扫描 `*.meta.json`;元数据存在即视为该 update 已提交,其中 `file_path` 指向张量文件。
4. **`control/latest.json` 是 learner 轮询的唯一全局指针**。learner 不扫描权重目录、不读数据库。
5. **心跳 JSON 只是存活提示**,不参与正确性(丢心跳最多导致 liveness 误判,不会丢更新)。
6. **SQLite 只属于 syncer、放节点本地盘**(`TMPDIR`,可用 `io.sqlite_local_dir` 覆盖),绝不放共享文件系统(规避 Lustre/NFS 上 SQLite 锁不可靠的问题);通过 `db_dumps/` 周期性快照到共享盘,供 resume 和离线分析。
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
- 达到 `quorum_min` → 进入宽限窗口(`grace_window.fixed_seconds`,上限 `max_seconds`):循环重扫元数据,凑满 `quorum_max` 份或超时为止。目的是让慢一拍的 learner 也进入本次合并,减少更新浪费。

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

随后 syncer 依次:保存 `global_v{v+1}.safetensors` 与 `outer_v{v+1}.safetensors` → 写 SQLite `global_versions` 行 → **原子覆盖 `latest.json`**(这一步之后新版本才对 learner 可见)→ 把选中更新标记 `applied`(记录实际权重、staleness)→ 丢弃被取代/过期的 pending 更新 → 按需 dump 数据库、清理旧版本文件。

### 4.6 更新丢弃规则

| drop_reason | 触发条件 |
|---|---|
| `missing_file` | 元数据在库但张量文件已不存在(如被 learner 侧保留策略删除) |
| `stale` | staleness 超过 `max_staleness_versions`(terminal drain 阶段不执行,以免排空前误删) |
| `superseded` | 同一 learner 有更新的一份已被选中,较旧的 pending 份被取代 |

## 5. fragment 分片模式

### 5.1 分片定义

`protocol/fragment_index.py` 在扁平向量上定义 K 个**互不重叠、完全覆盖**的分片:

- `full`:K=1,退化为全量(但走 fragment 协议路径);
- `balanced_tensor`:以**整个张量**为最小单位,按 numel 从大到小贪心装入当前最小的桶(不切开单个张量),`num_fragments ≤ 可训练张量数`。

fragment index 发布为 `fragments/fragment_index.json`,构建时经过严格校验(覆盖性、连续性、id 连续、与 param index 一致)。

### 5.2 双侧 round-robin 调度

- **syncer 侧**:第 `e` 次全局合并事件的目标 fragment 为 `e mod K`(`protocol/fragment_scheduler.py: select_fragment`)。每次合并只处理目标片的更新,该片 `fragment_version +1`,同时 `global_merge_event +1`。
- **learner 侧**:第 `u` 次上传(`local_update_index`)上传 `u mod K` 号片。learner 从模型 flatten 出完整向量后用 `extract_fragment` 抽取该片上传。

因此每个 fragment 的更新供给和消费频率天然对齐,`expected_fragment_versions_after_events()` 可静态推算 E 次事件后各片应有的版本(分析工具用它做断言)。

### 5.3 merge 与发布的差异

- 合并数学与全量模式相同,只是 θ、p、优化器状态都是**每片一份**(`fragment_thetas[k]`、`outer_states[k]`);staleness 以 `base_fragment_version` 对比该片当前版本计。
- `latest.json` 采用 `latest_kind: "fragment"` 布局:携带 `global_merge_event`、每片 `{version, weight_path, optim_path, updated_at_global_merge_event}`,以及最近一次 materialize 的完整权重路径。
- **materialize**:按 `fragments.materialize_full_every_events` 周期(以及事件 0 和到达目标步数时)把所有片拼回完整向量,存成 `weights/global_v{event:06d}.safetensors`,供评测/导出使用。
- **learner 采纳是增量的**:对比 `latest.json` 中每片版本与本地已加载版本,只加载变化的片、scatter 进本地扁平向量再写回模型(`adopt_fragment_updates`),并按 `fragments.reset_inner_optimizer_on_fragment_adopt` 决定是否重置内层优化器。

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

无论哪种原因,syncer 退出前都会:发布 `control/stop.json`(含 reason/version)→ 最终 dump 数据库 → 关闭 W&B(fragment 模式还会先做一次最终 materialize)。

learner 停止条件:`training.max_local_steps` 达标,或看到 `stop.json`(全量模式;fragment 模式设置了 `max_local_steps` 时只看步数,收尾在 finally 中等待最终合并结果)。退出前写 `status=stopped` 的最终心跳。

有限步训练的收尾由 **terminal drain** 衔接:所有 learner 到达 `max_local_steps` 后,syncer 用 `oldest_pending` 策略、无视 `quorum_min` 地把剩余 pending 更新逐批合并,直到外层步数达标或无更新可用。

## 8. 容错与崩溃恢复

| 故障 | 行为 |
|---|---|
| learner 崩溃 | 心跳变旧 → stale/dead;其已提交更新照常可被合并;quorum 机制容忍缺席。 |
| learner 变慢 | 其更新 staleness 增大 → 权重被 λ 折减,过窗即弃。 |
| syncer 崩溃 | 重启时 `init.resume: true`:从 `latest.json`(或指定版本)恢复 θ 与外层状态,从 `db_dumps/` 恢复最近的元数据库(仅当本地库为空),重新扫描共享盘上的元数据(SQLite 唯一约束保证重复摄取幂等)。 |
| 半截文件 | 不可能出现(原子 rename);张量写完前元数据不存在,syncer 不会看见。 |
| 更新文件丢失 | 选择前后各有一次存在性检查;选择后发现丢失 → 丢弃该份、其余回滚为 pending、放弃本次合并重来。 |
| 故障注入 | `failure_sim` 配置节可让 learner 随机睡眠/跳过上传/以 exit 97 崩溃,用于韧性实验。 |

## 9. 为什么是 SQLite + JSON 的混合

- **跨进程共享的数据全是 JSON/safetensors 文件**(原子发布,天然适配共享文件系统);
- **syncer 单进程私有的状态机放 SQLite**(节点本地盘):update 生命周期的条件状态转移(`WHERE status=?` 的 CAS 式更新)、`UNIQUE(learner_id, local_step_end, base_*_version)` 幂等去重、staleness 窗口查询、可一致快照(`conn.backup()`)。
- 共享盘上的 `.meta.json` 才是事实源;SQLite 是可从 dump + 重扫描重建的索引。
