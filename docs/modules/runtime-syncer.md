# 模块参考:fs_diloco/runtime/syncer.py

syncer 进程实现。整体流程见 [03-runtime-flow.md](../03-runtime-flow.md) 第 4、5 节;合并算法见 [02-architecture.md](../02-architecture.md) 第 4 节。

HA full仍只有一个 active合并/发布者，但允许多个独立 candidate process。入口先用 run descriptor/bootstrap gate选择 legacy或 HA路径；HA candidate只有 acquire单调 `LeaderToken`后才构造 leader-bound store、加载模型/DB state并初始化 W&B。successor从 DB current committed row恢复；每次 publish使用 epoch唯一 checkpoint path，fenced commit后发布 canonical control。正常或异常收尾都不会让 stale token写 terminal/maintenance状态；正常收尾先发布 early stop generation，完成 summary/maintenance 后再发布更高的完整 generation。若两阶段之间崩溃，successor取得新 epoch重建 latest/heartbeat/summary并重跑幂等 maintenance 后完成终态；异常生成的 `error` terminal可诊断但不阻止 successor acquire/resume，也不让 learner提前停止。

dynamic HA复用同一leader/token/publication链，并增加schema v3 membership controller。first leader初始化固定stream pool和bootstrap requests；每轮批量摄取current heartbeat、registration和proposal，记录幂等capacity observation并驱动scale launch outbox。selection仍用既有staleness/token数学，但final commit在同一fenced transaction重验instance/placement/stream/admission generation，并原子提交对应`merge:<version>` observation；starvation generation也只与其observation一起分配。global/token/manual/deadline/budget/no-progress close会冻结admission generation、close generation与max terminal version，发布drain并等待ack/revoke/visibility闭合。

## runtime/syncer_ha.py

- `acquire_candidate()` 以 source/config identity创建 owner-scoped candidate日志，轮询 `LeaderLeaseStore`直到取得 token、terminal已完整发布或等待预算耗尽；只有 DB terminal 对应 generation 的 canonical stop/summary publication路径、owner、epoch与 SHA 均有效才视为完成，缺一时允许 successor取得 token执行 terminal repair；loser没有业务 mutator。
- `open_leader_store()` 用已验证 bootstrap identity打开 `FencedSQLiteStore`并返回绑定 current token的 runtime store。
- `LeaseRenewalThread` 周期 renew并用 `EpochControlPublisher`写 syncer heartbeat；短暂 `SQLITE_BUSY/locked` 在本地 monotonic lease安全预算内带抖动重试，其他异常或预算耗尽fail closed并由主线程检查。线程累计真实renew transaction、busy retry及heartbeat wall/CPU遥测。`run_syncer()`从acquire成功开始把store/logger/renew线程的部分初始化纳入同一cleanup ownership guard；任一步失败都会停止已启动线程、关闭store、尝试release精确token并关闭lease connection。renew线程启动超时先设置stop，不能稍后继续续租。

`runtime/pbs_scheduler.py` 规范化 PBS job ID和 qstat字段，把 queued/prologue/running/suspended/finished/unknown分类；candidate qsub带唯一 request fingerprint、shared root、descriptor SHA和配置中显式估算的短 walltime。该walltime必须在尽量短的同时为启动、运行和收尾保留足够余量。qstat/qsub命令缺失或 timeout被转成 `query_failed`/失败 receipt，不向上抛出并终止 learner。`runtime/launch_outbox.py` 用 deterministic observation key和 atomic mkdir选出单个 attempt winner；此外用文件系统全局 reservation把跨observation的archive/reconcile、全局outstanding预算检查、当前observation尝试预算与durable claim创建串行化，避免多个learner同时越过全局candidate上限。reservation在claim timeout、uncertainty timeout与scheduler timeout导出的保守期限后才允许接管。随后按当前/历史 qstat结果、uncertainty窗口、指数 backoff和最大尝试 reconcile；mkdir后 claim.json尚未可见的窗口用 attempt目录mtime保守计作 live claim，submission receipt丢失时会先按 fingerprint查 scheduler，而不是立即重复 qsub。当前 stale observation的 attempts不会因 claim retention被归档并重置预算，只有看到新 observation后才允许把旧终态 claim写入 history。

HA正常收尾在写最终`process_exit`事件之前同步停止`LeaseRenewalThread`，再从已经停止的线程取得冻结的lease/heartbeat遥测；stop或线程内失败会记录`lease_renewer_stop_failed`并向上抛出，而不会伪造成功的最终指标。最后关闭leader-bound store，并只用当前精确token释放lease后关闭lease connection。启动期任一步失败也由同一ownership guard按stop thread → close store → exact-token release → close lease的顺序清理。

## CLI 与入口

- **`parse_args(argv)`** — `--config`(必填)、`--run-id`、`--shared-root`、`--num-learners`,以及实验覆盖参数:`--training-seed`、`--scan-interval-seconds`、`--syncer-device`、`--syncer-publish-dtype`、`--staleness-lambda`、`--max-staleness-versions`、`--global-adoption-strategy`、`--completion-mode`、`--parallel-checkpoint-writes`、`--materialize-full-every-events`、`--ingest-during-publish`、`--capture-terminal-predecessor-for-eval`(与 learner 的 parse_args 对称)。
- **`sqlite_path(config) -> Path`** — 唯一持久库路径:`<shared_root>/control/syncer_metadata.sqlite3`。
- **`run_identity(config)`** — 精确包含 run_id、format/protocol、mode、model name、num_fragments 与 git commit/dirty/source fingerprint；full resume 要求整个 dict 相等。
- **`resolve_syncer_device(config) -> torch.device`** — 解析 `syncer.device=auto/cpu/cuda`;显式 CUDA 不可用时 fail fast。
- **`syncer_compute_dtype(config)`** / **`syncer_publish_dtype(config)`** — 把 syncer 的计算/发布 dtype 配置解析为 torch dtype。
- **`maybe_capture_terminal_predecessor_for_eval(...)`** — 默认关闭的 full terminal-partial 研究捕获；从当前权威 weight 建 hardlink（失败则原子 copy），写入 checksum/版本/selected/quorum manifest。manifest 前残留 checkpoint 若同源则复用、不同则由 source 原子覆盖；已有 manifest 的 identity/checkpoint/source 任一冲突 fail closed。返回值只供事件日志，路径不进入 DB/latest/resume。
- **`align_state_to_publication_dtype(config, theta, state, *, roundtrip_metrics_out=None)`** — 每次发布前按发布 dtype 量化浮点权重/状态并转回计算 dtype,让持续运行、learner 可见 checkpoint 与 resume 共享同一数值边界；可在覆盖原值前记录 chunked L2/L∞/relative-L2 量化误差。
- **`_publication_error_metrics()` / `publication_roundtrip_metrics()`** — 每个 chunk 先以 FP32 tensor 求误差平方和/参考平方和，再把 `.item()` 结果累加到 Python float；给出 L2、L∞ 与相对 L2。reference 范数为 0 时相对值固定为 0。`publication_roundtrip_metrics` 对非浮点或已是 publish dtype 的 tensor 直接返回三个 0。
- **`publication_failpoint(name)`** — `$FS_DILOCO_PUBLICATION_FAILPOINT` 命中 `weight_temp/after_weight/after_outer/sqlite_transaction/after_db_commit/after_latest` 时触发；`$FS_DILOCO_FAILPOINT_ACTION=kill` 发 SIGKILL，缺省 `raise` 抛异常，其他 action 报配置错误。
- **`main(argv)`** — `resolve_config` 后调 `run_syncer`。
- **`run_syncer(config)`** — 公共启动(目录、SQLite、日志、设备、W&B)后分派:fragment 模式 → `run_fragment_syncer`;否则按 `init.resume` 走 `resume_run`/`initialize_run`,再执行全量模式主循环(函数体内,详见下文)。W&B 初始化发生在 initialize/resume 之前；初始化/恢复自身失败尚未进入主循环 `try/finally`，不会走正常 stop/summary/W&B-finish/close 序列。

## 发布

- **`latest_payload(...)`** — 全量 `latest.json` 同时写 `total_update_tokens` 与 `total_seen_tokens`，并携带按 `RunPaths` 生成的 weight/optim/param-index 路径、created_at、format/run/version；函数不额外调用 `resolve()`，路径是否绝对取决于 shared root。
- **`publish_global(...)`** — 默认两个 worker 并发原子写 weight/outer，主线程确认双成功后才调用 v0 初始化事务或 `commit_full_merge`，最后原子写 latest。dynamic merge必须同时传入capacity observation，由同一事务提交global row和`merge:<version>` observation。`num_updates`形参在merge commit中不作权威计数，DB取selected长度。串行模式保持weight→outer次序。只有并行futures尚未全部完成时才可能调用during-wait ingestion；callback poll上限为`min(0.2, scan_interval)`，只做心跳/metadata，不做selection/maintenance/version commit。返回worker/墙钟/bytes/摄取/roundtrip计时。DB commit是版本边界，latest是缓存。
- **`checkpoint_wait_ingestion_callback(config, callback)`** — `sync.ingest_during_publish=false` 时返回 None，true 时原样返回 callback，不再包装。pass/update/heartbeat/seconds 是 `publish_global` 调用它时累加的。串行 checkpoint 写入没有 futures wait，因此即便开关 true 也不会摄取。
- **`fragment_latest_payload(*, ..., global_merge_event, fragment_versions, fragment_updated_events, materialized_weight_path)`** — fragment 布局的 latest 内容(`latest_kind: "fragment"`,每片版本/路径/最后更新事件)。
- **`should_materialize_fragment_full(config, global_merge_event) -> bool`** — 是否本事件重拼完整权重:事件 0、达到 `stop_after_outer_steps`、或按显式正整数 `materialize_full_every_events` 取模；fragment 配置缺失/null/≤0 会在启动时 fail closed。
- **`FragmentLatestPublication` / `publish_fragment_latest(...)`** — 按需 materialize 完整权重再原子写 fragment latest；返回路径、耗时、是否发生和 bytes。不 materialize 时沿用旧路径。注意片 weight、outer、`fragment_versions`、latest、update applied/drop 是独立顺序步骤，不构成 full publication 那样的单一事务；fragment 又不支持 resume。
- **`publish_stop(paths, *, config, reason, version, total_seen_tokens)`** — 原子写 `control/stop.json`。

## 初始化与恢复

- **`initialize_run(config, paths, store, logger, *, device) -> (version=0, theta, outer_state, param_index, total_seen_tokens=0)`** — 全新 run:DB committed 防覆盖 → 模型/index及 compute-dtype θ/outer → 先把浮点 state 对齐 publication roundtrip → 原子写 index 与两份 config → `publish_global(0)` 按 publish dtype写 checkpoint并在一个事务写 v0+identity+config → maintenance。
- **`initialize_fragment_run(config, paths, store, logger, *, device) -> (event=0, fragment_thetas, outer_states, param_index, fragment_index, fragment_versions, fragment_updated_events, total_seen_tokens=0, materialized_weight_path)`** — fragment 版:另建并发布 fragment index;逐片抽取 θ_f、建状态、存 v0、写 `fragments`/`fragment_versions` 表;materialize 事件 0 并发布 fragment latest。
- **`resume_run(config, paths, store, logger, *, device)`** — DB-first 恢复(仅 full):integrity → identity exact match → 最大 committed row → model/index → 文件存在 → 按 compute dtype加载 → weight theta 与 outer theta `torch.equal` → heartbeat fences → 单事务切代 → 归档剩余 marker（不可读，或 reason 空/`error`）→ 重建 latest → maintenance。只要 stop reason 或 summary stop_reason 任一为非空且非 error 就拒绝，不要求成对一致。DB 是否存在由 `run_syncer` 在构造 store 前检查；不读 latest 决定版本，也无 DB dump fallback。

## 摄取(共享盘 → SQLite)

- **`validate_update_metadata(payload, *, config, paths) -> bool`** — 校验format/run/learner/mode/path ownership；dynamic按UUID validator并要求instance/placement/stream/admission/token字段，static仍按固定ID集合；fragment另校验kind/fragment/base。
- **`ingest_update_metadata(...)`** — static full读取恰好N个固定pointer，fragment读取N×K；dynamic通过`RunPaths.iter_instance_update_pointers()`发现UUID instance并结合DB admission验证，绝不调用static白名单。各模式都由持久frontier防重放且不扫描历史payload metadata。
- **`sync_liveness_and_metadata(...)`** — static摄取本代heartbeat、重分类并摄取proposal；dynamic把同一扫描中验证过的current heartbeat合入一个fenced transaction，再摄取registration与membership-fenced proposal，降低控制路径transaction数量而不放宽逐instance fence。
- update 元数据中的 learner 资源字段随 `updates`/`fragment_updates` 行持久化;已有 SQLite 文件在连接时用兼容迁移补齐新列。

## 选择

- **`UpdateProposalSource` / `full_update_proposal_source(...)` / `fragment_update_proposal_source(...)`** — 参数化 full/fragment 的候选枚举、staleness 键、缺文件降级动作、事件名与上下文字段；两种磁盘协议保持各自原有事件 payload。
- **`drop_missing_update_files(store, updates, logger, *, source) -> list`** — 共享过滤骨架；张量文件消失时经 source 在对应表中标 `dropped(missing_file)`，发 full 或 fragment 原事件并返回存活子集。
- **`configured_grace_seconds(config)`** — fixed 模式返回 `max(0,min(fixed_seconds,max_seconds))`,adaptive 模式对 `initial_seconds` 做同样 clamp。
- **`UpdateFirstSeen` / `UpdateFirstSeenRegistry`** — entry 保存 learner/可选 fragment、monotonic/wall 首见。`observe` 只插新 ID，容量满按 FIFO 淘汰；`get/discard_many` 管理 ID；full 新 proposal 还用 `discard_full_learner` 删除同 learner 旧首见。容量为 `max(64, 4 × learners × max(1, fragments))`。
- `UpdateFirstSeenRegistry.__init__()` 建有序 dict，`__len__()` 返回当前项数，`get()` 查询，`discard_many()` 批删；`update_first_seen_capacity()` 计算上述容量。`_pointer_signature()` 从 stat 返回 inode/size/mtime_ns/ctime_ns，文件消失返回 None。
- `UpdateProposalSource.eligible_updates()` / `drop_updates()` 把同一 grace/terminal 骨架参数化到 full 或指定 fragment 的 SQLite 查询与 drop 方法。
- **`interval_breakdown(...)`** — 用单调时钟校验一次 merge interval 的 discovery、idle polling、grace、read、merge、publish、maintenance 分量互不重叠并显式给出 residual；full/fragment 主循环跨多次 quorum wait 累计这些计数，只在成功 merge 后重置。
- **`fastest_next_upload_eta_seconds(updates, *, first_seen, inner_steps, now_monotonic)`** — 用各已选 update 的 `first_seen_monotonic + local_cycle_step_time_seconds_mean × inner_steps` 估计下一上传时间,返回最快剩余秒数；不读取 learner `committed_at`，因此不受跨节点 wall-clock 偏差影响。
- **`maybe_shorten_grace_deadline()` / `collect_with_grace_window(...)`** — adaptive 只把 deadline 向前移动。入口先用当前 DB 做一次【source 查合格 → missing 过滤 → 每 learner 选一】；若继续等待，sleep 后同步心跳/metadata，再进入下轮查询。满 quorum_max 或 deadline 到即返回；函数可能返回少于 quorum_min，调用方必须再次检查。resume 中没有首见记录的旧 update 不缩短窗口。
- **`all_expected_learners_stopped(store, config) -> bool`** — 实现取 DB 中所有 `status=stopped` 的 ID 集，要求它与预期 ID 集**完全相等**；所以全部预期 learner 都必须存在并明确 stopped，dead/step 达标不够，异常遗留的额外 stopped ID 也会阻止闭合（正常摄取校验不会创建这种额外 ID）。
- **`TerminalDrainDecision(state, selected)`** — terminal selector 的显式返回契约：`open`、`closed_empty`、`closed_selected`；空 selected 不再同时表示重新打开和耗尽。
- **`select_terminal_drain_updates(...)`** — 输入闭合后的全量末端排空:再次确认 closure，仍执行严格 future/staleness 准入与 missing-file 检查,按配置策略每 learner 选一,允许低于 quorum；只有 `closed_empty` 由主循环转为 `input_exhausted`。
- **`select_terminal_drain_fragment_updates(...)`** — fragment 对应入口：对当前调度目标片执行相同三态契约和严格 future/staleness/missing-file 准入；目标片 `closed_empty` 才结束，不跨过 round-robin 顺序消费其他片。
- **`_select_terminal_drain_from_source()`** — 两个 terminal 入口在已确认 closure、已 drop `future_base/too_stale` 后调用的共同实现；helper 自身只查 eligible、drop missing、按正常 selection policy 取最多 quorum_max 并记事件，不再次查 closure。wrapper 随后用 `TerminalDrainDecision.__post_init__()` 校验 state 与 selected 是否一致。
- **`merge_staleness_evidence(...)`** — 按实际 normalized merge weights 计算 effective staleness 均值、fresh 权重质量和 staleness count JSON；full 使用 global base，fragment 使用目标片 base。

## 观测与辅助

- **`init_wandb_run(*, config, paths, logger, device, hostname) -> run | None`** — W&B 初始化(项目/名称/标签/config 由 `observability/wandb_logging.py` 生成;`syncer/version` 定义为 step 轴);禁用、import 失败、init 失败都返回 None 并记日志,不影响训练。
- **`_fragment_staleness_stats(selected, current_fragment_version)`**(私有)— 选中集合的 staleness min/mean/max。
- **`_selected_resource_csv_fields(metrics)`** — 把 selected resource summary 的 W&B 风格键映射成 syncer CSV 列名，缺失项保持空。
- **`learner_shutdown_timeout_seconds(config)`** — 显式 `liveness.learner_shutdown_timeout_seconds` 优先；null 时返回 `max(120, 2 × heartbeat_interval_seconds)`，不再用旧的 120 秒上限。
- **`wait_for_learner_shutdown(...)`** — reason=`error` 时立即返回 false；否则在 timeout 内继续摄取，sleep 单次最多 1 秒。全部 expected learner stopped 才返回 true；超时逐 learner 记录 status/reason/last_seen，并跳过强制终态化。
- **`learner_resource_summary(...)`** — 合并**当前 live update 表**聚合与最终 heartbeat，不回读已归档 JSONL；后者通常是完整训练峰值的主要终态证据。
- **`write_training_summary(...)`** — 原子写 summary；进入主循环后，无论其以 error/non-error 离开都会在收尾序列中尝试执行，并尝试更新 W&B summary。初始化/恢复阶段异常不在这个保证内。

## 主循环

### 全量模式(`run_syncer` 内)

每次迭代尝试 `v → v+1`,详细伪代码见 [03-runtime-flow.md](../03-runtime-flow.md#4-syncer-主循环全量模式)。关键实现细节:

- 选中后、读取前**再次**检查文件存在性;`load_update_vector` 期间竞态出现 `FileNotFoundError` 时同样处理:丢失者 `dropped`,其余 `reset_selected_to_pending` 回滚,放弃本次合并;
- `run_selection_id = f"{run_id}_v{v+1:06d}"` 写入 selected_by_run,便于审计"哪次合并选了它";
- 每次合并的 applied/`superseded`/`too_stale`/`future_base` 状态都由 `commit_full_merge` 与 global row 同事务提交;
- 全部 stopped 后先等待一个 grace/reingest 周期并重新判断 closure；重新打开则复位 grace 并回常规 discovery，仍闭合时只有 `closed_empty` 产生 `input_exhausted`；input-closed 分支不再额外执行一遍未使用的常规 discovery;
- fragment 主循环使用同一 input-closed 判定与 grace/reingest 生命周期；每次 terminal merge 推进 global event 后重新计算目标片，最终 pending/selected fragment proposal 由统一 shutdown 终态化。
- 每次成功合并后刷新`last_progress_time`；legacy/fragment在quorum等待超过`no_progress_timeout_seconds`时按既有语义停机，dynamic则以该原因在current version启动持久drain，controller/input closure前不发布普通terminal；
- 每次成功提交后执行 archive/GC,因此 active DB/checkpoint/proposal 面有界;
- 一旦成功进入主循环，finally 序列为：HA先提交并发布 early stop generation（legacy直接 publish stop）→ 非 error 时等待 learner/末次摄取 → 只有全 stopped 才终态化未消费 proposal → summary → 非 error 的 archive/GC → HA以更高 generation成对发布最终 stop/summary；随后 W&B finish/关库。early generation、summary 或 maintenance 窗口崩溃都保持 terminal不完整，使 successor可执行幂等 repair。

### dynamic full控制器

- `FencedSQLiteStore.initialize_dynamic_membership()`幂等建立stream/bootstrap controller state并发布bootstrap-ready；successor读取schema v3现状，不重新分配stream或logical request。
- registration摄取验证TTL、内容hash、source/config/path/PBS job和logical request；scheduler-bearing request在launch row尚无精确receipt绑定时保存为pending并保留请求文件，绑定到达后重试，错绑拒绝。admission transaction拒绝健康placement duplicate、超容量和已fulfill request，分配placement/stream/admission generation/token后发布instance artifact。
- `record_capacity_observation()`以observation key/sequence去重，并在同一transaction维护连续low计数、cooldown和request预算；merge路径改由`commit_full_merge()`原子写对应observation，无merge路径由原子starvation observation API同时推进generation。`LearnerLaunchOutbox`提交带request ID、walltime/queue的learner job并reconcile queued/running/finished/unknown状态，reserved capacity只在已证实终态时释放。
- `commit_full_merge`的dynamic参数要求每个selected row在transaction内仍是current admitted incarnation，且同stream/placement不重复；selection之后发生replacement/revoke时整批不会错误应用旧成员。
- close policy在一个transaction中关闭admission、取消open request并冻结`close_generation/max_terminal_version`。token target使用current version作为上限，no-progress也进入该状态机。`DynamicTerminalPublisher`发布drain；`dynamic_input_closed`要求所有logical request/registration可见性收敛，current成员均ack或timeout revoke，final pointer均进入frontier。successor沿用冻结reason和上限，terminal merge绝不超过它；非error terminal要求controller closed且input closed。
- dynamic maintenance把expired instance、registration、launch request和超出retention窗口的observation先append+fsync归档，再fenced删除active行；物理UUID pointer/payload仍由引用驱动GC处理。

### fragment 模式(`run_fragment_syncer`)

结构同上,差异:

- 开头 `raise NotImplementedError` 拦截 resume;
- 每轮以 `select_fragment(global_merge_event, K)` 确定目标片;资格、quorum、宽限窗口、staleness、superseded/obsolete 丢弃全部**按片**进行;
- 合并成功后:片版本 +1、事件 +1,存片权重/优化器状态、写 `fragment_versions` 行,`publish_fragment_latest`(内含按需 materialize);
- 输入闭合(全部预期 learner stopped)后与 full 一样走 terminal grace/drain:`select_terminal_drain_fragment_updates` 对当前目标片按严格准入放宽 quorum 合并,目标片无合法 pending 即 `input_exhausted`;输入未闭合时 quorum 不足只能等待或 `no_progress_timeout`;
- finally:非 error 时先做一次**最终 materialize + 发布**,再走 stop/末次摄取/终态化/archive/GC/W&B/close 序列。
