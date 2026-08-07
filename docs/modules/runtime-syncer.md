# 模块参考:`fs_diloco/runtime/syncer.py`

syncer 进程实现。整体流程见 [03-runtime-flow.md](../03-runtime-flow.md) 第 4、5 节;合并算法见 [02-architecture.md](../02-architecture.md) 第 4 节;术语约定见 [00-glossary.md](../00-glossary.md)。

HA full 仍只有一个 active 合并/发布者,但允许多个独立 candidate process。入口先用 run descriptor/bootstrap 门禁选择 legacy 或 HA 路径;HA candidate 只有取得单调 `LeaderToken` 后才构造 leader-bound store、加载模型/DB 状态并初始化 W&B。后继者从 DB current committed row 恢复;每次发布使用 epoch 唯一 checkpoint 路径,隔离提交后发布权威控制。正常或异常收尾都不会让 stale token 写 terminal/maintenance 状态;正常收尾先发布 early stop 世代,完成 summary/maintenance 后再发布更高的完整世代。若两阶段之间崩溃,后继者取得新 epoch 重建 latest/heartbeat/summary 并重跑幂等 maintenance 后完成终态;异常生成的 `error` terminal 可诊断但不阻止 successor acquire/resume,也不让 learner 提前停止。

dynamic HA 复用同一 leader/token/发布链,并增加 schema v3 成员控制器。第一任 leader 初始化固定数据流池和 bootstrap 请求;每轮批量摄取 current heartbeat、registration 和 proposal,记录幂等容量观测并驱动 scale 启动发件箱。选择仍用既有陈旧度/token 数学,但最终提交在同一隔离事务重验 instance/placement/stream/admission generation,并原子提交对应 `merge:<version>` observation;饥饿世代也只与其 observation 一起分配。global/token/manual/deadline/budget/no-progress close 会冻结 admission generation、close generation 与 max terminal version,发布排空并等待 ack/revoke/可见性闭合。

## runtime/syncer_ha.py

- `acquire_candidate()` 以 source/config identity 创建 owner 专属候选日志,轮询 `LeaderLeaseStore` 直到取得 token、terminal 已完整发布或等待预算耗尽;只有 DB terminal 对应 generation 的权威 stop/summary 发布路径、owner、epoch 与 SHA 均有效才视为完成,缺一时允许 successor 取得 token 执行 terminal repair;loser 没有业务 mutator。
- `open_leader_store()` 用已验证 bootstrap identity 打开 `FencedSQLiteStore` 并返回绑定 current token 的 runtime store。
- `LeaseRenewalThread` 周期续约并用 `EpochControlPublisher` 写 syncer 心跳;短暂 `SQLITE_BUSY/locked` 在本地单调租约安全预算内带抖动重试,其他异常或预算耗尽 fail closed 并由主线程检查。线程累计真实 renew transaction、busy retry 及 heartbeat wall/CPU 遥测。`run_syncer()` 从 acquire 成功开始把 store/logger/renew 线程的部分初始化纳入同一 cleanup ownership guard;任一步失败都会停止已启动线程、关闭 store、尝试 release 精确 token 并关闭 lease connection。renew 线程启动超时先设置 stop,不能稍后继续续租。

`runtime/pbs_scheduler.py` 规范化 PBS job ID 和 qstat 字段,把 queued/prologue/running/suspended/finished/unknown 分类;candidate qsub 带唯一 request fingerprint、shared root、descriptor SHA 和配置中显式估算的短 walltime。该 walltime 必须在尽量短的同时为启动、运行和收尾保留足够余量。qstat/qsub 命令缺失或 timeout 被转成 `query_failed`/失败回执,不向上抛出并终止 learner。`runtime/launch_outbox.py` 用确定性 observation key 和原子 mkdir 选出单个 attempt winner;此外用文件系统全局 reservation 把跨 observation 的归档/对账、全局 outstanding 预算检查、当前 observation 尝试预算与 durable claim 创建串行化,避免多个 learner 同时越过全局 candidate 上限。reservation 在 claim timeout、uncertainty timeout 与 scheduler timeout 导出的保守期限后才允许接管。随后按当前/历史 qstat 结果、uncertainty 窗口、指数退避和最大尝试对账;mkdir 后 claim.json 尚未可见的窗口用 attempt 目录 mtime 保守计作 live claim,提交回执丢失时会先按 fingerprint 查 scheduler,而不是立即重复 qsub。当前 stale observation 的 attempts 不会因 claim retention 被归档并重置预算,只有看到新 observation 后才允许把旧终态 claim 写入 history。

HA 正常收尾在写最终 `process_exit` 事件之前同步停止 `LeaseRenewalThread`,再从已经停止的线程取得冻结的 lease/heartbeat 遥测;stop 或线程内失败会记录 `lease_renewer_stop_failed` 并向上抛出,而不会伪造成功的最终指标。最后关闭 leader-bound store,并只用当前精确 token 释放 lease 后关闭 lease connection。启动期任一步失败也由同一 ownership guard 按 stop thread → close store → exact-token release → close lease 的顺序清理。

## CLI 与入口

- **`parse_args(argv)`** — `--config`(必填)、`--run-id`、`--shared-root`、`--num-learners`,以及实验覆盖参数:`--training-seed`、`--scan-interval-seconds`、`--syncer-device`、`--syncer-publish-dtype`、`--staleness-lambda`、`--max-staleness-versions`、`--global-adoption-strategy`、`--completion-mode`、`--parallel-checkpoint-writes`、`--materialize-full-every-events`、`--ingest-during-publish`、`--capture-terminal-predecessor-for-eval`(与 learner 的 parse_args 对称)。
- **`sqlite_path(config) -> Path`** — 唯一持久库路径:`<shared_root>/control/syncer_metadata.sqlite3`。
- **`run_identity(config)`** — 精确包含 run_id、format/protocol、mode、model name、num_fragments 与 git commit/dirty/source fingerprint;full resume 要求整个 dict 相等。
- **`resolve_syncer_device(config) -> torch.device`** — 解析 `syncer.device=auto/cpu/cuda`;显式 CUDA 不可用时快速失败。
- **`syncer_compute_dtype(config)`** / **`syncer_publish_dtype(config)`** — 把 syncer 的计算/发布 dtype 配置解析为 torch dtype。
- **`maybe_capture_terminal_predecessor_for_eval(...)`** — 默认关闭的 full terminal-partial 研究捕获;从当前权威 weight 建硬链接(失败则原子复制),写入校验和/版本/selected/quorum 清单。清单前残留 checkpoint 若同源则复用、不同则由 source 原子覆盖;已有清单的 identity/checkpoint/source 任一冲突 fail closed。返回值只供事件日志,路径不进入 DB/latest/resume。
- **`align_state_to_publication_dtype(config, theta, state, *, roundtrip_metrics_out=None)`** — 每次发布前按发布 dtype 量化浮点权重/状态并转回计算 dtype,让持续运行、learner 可见 checkpoint 与 resume 共享同一数值边界;可在覆盖原值前记录分块 L2/L∞/relative-L2 量化误差。
- **`_publication_error_metrics()` / `publication_roundtrip_metrics()`** — 每个 chunk 先以 FP32 tensor 求误差平方和/参考平方和,再把 `.item()` 结果累加到 Python float;给出 L2、L∞ 与相对 L2。reference 范数为 0 时相对值固定为 0。`publication_roundtrip_metrics` 对非浮点或已是 publish dtype 的 tensor 直接返回三个 0。
- **`publication_failpoint(name)`** — `$FS_DILOCO_PUBLICATION_FAILPOINT` 命中 `weight_temp/after_weight/after_outer/sqlite_transaction/after_db_commit/after_latest` 时触发;`$FS_DILOCO_FAILPOINT_ACTION=kill` 发 SIGKILL,缺省 `raise` 抛异常,其他 action 报配置错误。
- **`main(argv)`** — `resolve_config` 后调 `run_syncer`。
- **`run_syncer(config)`** — 公共启动(目录、SQLite、日志、设备、W&B)后分派:fragment 模式 → `run_fragment_syncer`;否则按 `init.resume` 走 `resume_run`/`initialize_run`,再执行全量模式主循环(函数体内,详见下文)。W&B 初始化发生在 initialize/resume 之前;初始化/恢复自身失败尚未进入主循环 `try/finally`,不会走正常 stop/summary/W&B-finish/close 序列。

## 发布

- **`latest_payload(...)`** — 全量 `latest.json` 同时写 `total_update_tokens` 与 `total_seen_tokens`,并携带按 `RunPaths` 生成的 weight/optim/param-index 路径、created_at、format/run/version;函数不额外调用 `resolve()`,路径是否绝对取决于 shared root。
- **`publish_global(...)`** — 默认两个 worker 并发原子写 weight/outer,主线程确认双成功后才调用 v0 初始化事务或 `commit_full_merge`,最后原子写 latest。dynamic merge 必须同时传入 capacity observation,由同一事务提交 global row 和 `merge:<version>` observation。`num_updates` 形参在 merge commit 中不作权威计数,DB 取 selected 长度。串行模式保持 weight→outer 次序。只有并行 futures 尚未全部完成时才可能调用 during-wait ingestion;callback poll 上限为 `min(0.2, scan_interval)`,只做心跳/metadata,不做 selection/maintenance/version commit。返回 worker/墙钟/bytes/摄取/roundtrip 计时。DB commit 是版本边界,latest 是缓存。
- **`checkpoint_wait_ingestion_callback(config, callback)`** — `sync.ingest_during_publish=false` 时返回 None,true 时原样返回 callback,不再包装。pass/update/heartbeat/seconds 是 `publish_global` 调用它时累加的。串行 checkpoint 写入没有 futures wait,因此即便开关 true 也不会摄取。
- **`fragment_latest_payload(*, ..., global_merge_event, fragment_versions, fragment_updated_events, materialized_weight_path)`** — 分片布局的 latest 内容(`latest_kind: "fragment"`,每片版本/路径/最后更新事件)。
- **`should_materialize_fragment_full(config, global_merge_event) -> bool`** — 是否本事件重拼完整权重:事件 0、达到 `stop_after_outer_steps`、或按显式正整数 `materialize_full_every_events` 取模;fragment 配置缺失/null/≤0 会在启动时 fail closed。
- **`FragmentLatestPublication` / `publish_fragment_latest(...)`** — 按需物化完整权重再原子写 fragment latest;返回路径、耗时、是否发生和 bytes。不物化时沿用旧路径。注意片 weight、outer、`fragment_versions`、latest、update applied/drop 是独立顺序步骤,不构成 full publication 那样的单一事务;fragment 又不支持 resume。
- **`publish_stop(paths, *, config, reason, version, total_seen_tokens)`** — 原子写 `control/stop.json`。

## 初始化与恢复

- **`initialize_run(config, paths, store, logger, *, device) -> (version=0, theta, outer_state, param_index, total_seen_tokens=0)`** — 全新 run:DB committed 防覆盖 → 模型/index 及 compute-dtype θ/outer → 先把浮点 state 对齐 publication roundtrip → 原子写 index 与两份 config → `publish_global(0)` 按 publish dtype 写 checkpoint 并在一个事务写 v0+identity+config → maintenance。
- **`initialize_fragment_run(config, paths, store, logger, *, device) -> (event=0, fragment_thetas, outer_states, param_index, fragment_index, fragment_versions, fragment_updated_events, total_seen_tokens=0, materialized_weight_path)`** — fragment 版:另建并发布 fragment index;逐片抽取 θ_f、建状态、存 v0、写 `fragments`/`fragment_versions` 表;物化事件 0 并发布 fragment latest。
- **`resume_run(config, paths, store, logger, *, device)`** — DB-first 恢复(仅 full):integrity → identity 精确匹配 → 最大 committed row → model/index → 文件存在 → 按 compute dtype 加载 → weight theta 与 outer theta `torch.equal` → heartbeat fences → 单事务切代 → 归档剩余 marker(不可读,或 reason 空/`error`)→ 重建 latest → maintenance。只要 stop reason 或 summary stop_reason 任一为非空且非 error 就拒绝,不要求成对一致。DB 是否存在由 `run_syncer` 在构造 store 前检查;不读 latest 决定版本,也无 DB dump fallback。

## 摄取(共享盘 → SQLite)

- **`validate_update_metadata(payload, *, config, paths) -> bool`** — 校验 format/run/learner/mode/path ownership;dynamic 按 UUID validator 并要求 instance/placement/stream/admission/token 字段,static 仍按固定 ID 集合;fragment 另校验 kind/fragment/base。
- **`ingest_update_metadata(...)`** — static full 读取恰好 N 个固定指针,fragment 读取 N×K;dynamic 通过 `RunPaths.iter_instance_update_pointers()` 发现 UUID instance 并结合 DB admission 验证,绝不调用 static 白名单。各模式都由持久摄取水位防重放且不扫描历史载荷 metadata。
- **`sync_liveness_and_metadata(...)`** — static 摄取本代 heartbeat、重分类并摄取 proposal;dynamic 把同一扫描中验证过的 current heartbeat 合入一个隔离事务,再摄取 registration 与 membership-fenced proposal,降低控制路径事务数量而不放宽逐 instance fence。
- update 元数据中的 learner 资源字段随 `updates`/`fragment_updates` 行持久化;已有 SQLite 文件在连接时用兼容迁移补齐新列。

## 选择

- **`UpdateProposalSource` / `full_update_proposal_source(...)` / `fragment_update_proposal_source(...)`** — 参数化 full/fragment 的候选枚举、陈旧度键、缺文件降级动作、事件名与上下文字段;两种磁盘协议保持各自原有事件载荷。
- **`drop_missing_update_files(store, updates, logger, *, source) -> list`** — 共享过滤骨架;张量文件消失时经 source 在对应表中标 `dropped(missing_file)`,发 full 或 fragment 原事件并返回存活子集。
- **`configured_grace_seconds(config)`** — fixed 模式返回 `max(0,min(fixed_seconds,max_seconds))`,adaptive 模式对 `initial_seconds` 做同样 clamp。
- **`UpdateFirstSeen` / `UpdateFirstSeenRegistry`** — entry 保存 learner/可选 fragment、monotonic/wall 首见。`observe` 只插新 ID,容量满按 FIFO 淘汰;`get/discard_many` 管理 ID;full 新 proposal 还用 `discard_full_learner` 删除同 learner 旧首见。容量为 `max(64, 4 × learners × max(1, fragments))`。
- `UpdateFirstSeenRegistry.__init__()` 建有序 dict,`__len__()` 返回当前项数,`get()` 查询,`discard_many()` 批删;`update_first_seen_capacity()` 计算上述容量。`_pointer_signature()` 从 stat 返回 inode/size/mtime_ns/ctime_ns,文件消失返回 None。
- `UpdateProposalSource.eligible_updates()` / `drop_updates()` 把同一 grace/terminal 骨架参数化到 full 或指定 fragment 的 SQLite 查询与 drop 方法。
- **`interval_breakdown(...)`** — 用单调时钟校验一次 merge interval 的 discovery、idle polling、grace、read、merge、publish、maintenance 分量互不重叠并显式给出 residual;full/fragment 主循环跨多次 quorum wait 累计这些计数,只在成功 merge 后重置。
- **`fastest_next_upload_eta_seconds(updates, *, first_seen, inner_steps, now_monotonic)`** — 用各已选 update 的 `first_seen_monotonic + local_cycle_step_time_seconds_mean × inner_steps` 估计下一上传时间,返回最快剩余秒数;不读取 learner `committed_at`,因此不受跨节点墙钟偏差影响。
- **`maybe_shorten_grace_deadline()` / `collect_with_grace_window(...)`** — adaptive 只把截止时刻向前移动。入口先用当前 DB 做一次【source 查合格 → missing 过滤 → 每 learner 选一】;若继续等待,sleep 后同步心跳/metadata,再进入下轮查询。满 quorum_max 或截止时刻到即返回;函数可能返回少于 quorum_min,调用方必须再次检查。resume 中没有首见记录的旧 update 不缩短窗口。
- **`all_expected_learners_stopped(store, config) -> bool`** — 实现取 DB 中所有 `status=stopped` 的 ID 集,要求它与预期 ID 集**完全相等**;所以全部预期 learner 都必须存在并明确 stopped,dead/步数达标不够,异常遗留的额外 stopped ID 也会阻止闭合(正常摄取校验不会创建这种额外 ID)。
- **`TerminalDrainDecision(state, selected)`** — terminal selector 的显式返回契约:`open`、`closed_empty`、`closed_selected`;空 selected 不再同时表示重新打开和耗尽。
- **`select_terminal_drain_updates(...)`** — 输入闭合后的全量末端排空:再次确认闭合,仍执行严格 future/staleness 准入与 missing-file 检查,按配置策略每 learner 选一,允许低于 quorum;只有 `closed_empty` 由主循环转为 `input_exhausted`。
- **`select_terminal_drain_fragment_updates(...)`** — fragment 对应入口:对当前调度目标片执行相同三态契约和严格 future/staleness/missing-file 准入;目标片 `closed_empty` 才结束,不跨过轮转顺序消费其他片。
- **`_select_terminal_drain_from_source()`** — 两个 terminal 入口在已确认闭合、已 drop `future_base/too_stale` 后调用的共同实现;helper 自身只查 eligible、drop missing、按正常 selection policy 取最多 quorum_max 并记事件,不再次查闭合。wrapper 随后用 `TerminalDrainDecision.__post_init__()` 校验 state 与 selected 是否一致。
- **`merge_staleness_evidence(...)`** — 按实际 normalized merge weights 计算 effective staleness 均值、fresh 权重质量和 staleness count JSON;full 使用 global base,fragment 使用目标片 base。

## 观测与辅助

- **`init_wandb_run(*, config, paths, logger, device, hostname) -> run | None`** — W&B 初始化(项目/名称/标签/config 由 `observability/wandb_logging.py` 生成;`syncer/version` 定义为 step 轴);禁用、import 失败、init 失败都返回 None 并记日志,不影响训练。
- **`_fragment_staleness_stats(selected, current_fragment_version)`**(私有)— 选中集合的陈旧度 min/mean/max。
- **`_selected_resource_csv_fields(metrics)`** — 把 selected resource summary 的 W&B 风格键映射成 syncer CSV 列名,缺失项保持空。
- **`learner_shutdown_timeout_seconds(config)`** — 显式 `liveness.learner_shutdown_timeout_seconds` 优先;null 时返回 `max(120, 2 × heartbeat_interval_seconds)`,不再用旧的 120 秒上限。
- **`wait_for_learner_shutdown(...)`** — reason=`error` 时立即返回 false;否则在 timeout 内继续摄取,sleep 单次最多 1 秒。全部 expected learner stopped 才返回 true;超时逐 learner 记录 status/reason/last_seen,并跳过强制终态化。
- **`learner_resource_summary(...)`** — 合并**当前 live update 表**聚合与最终 heartbeat,不回读已归档 JSONL;后者通常是完整训练峰值的主要终态证据。
- **`write_training_summary(...)`** — 原子写 summary;进入主循环后,无论其以 error/non-error 离开都会在收尾序列中尝试执行,并尝试更新 W&B summary。初始化/恢复阶段异常不在这个保证内。

## 主循环

### 全量模式(`run_syncer` 内)

每次迭代尝试 `v → v+1`,详细伪代码见 [03-runtime-flow.md](../03-runtime-flow.md#4-syncer-主循环全量模式)。关键实现细节:

- 选中后、读取前**再次**检查文件存在性;`load_update_vector` 期间竞态出现 `FileNotFoundError` 时同样处理:丢失者 `dropped`,其余 `reset_selected_to_pending` 回滚,放弃本次合并;
- `run_selection_id = f"{run_id}_v{v+1:06d}"` 写入 selected_by_run,便于审计「哪次合并选了它」;
- 每次合并的 applied/`superseded`/`too_stale`/`future_base` 状态都由 `commit_full_merge` 与 global row 同事务提交;
- 全部 stopped 后先等待一个宽限/重新摄取周期并重新判断闭合;重新打开则复位宽限并回常规发现,仍闭合时只有 `closed_empty` 产生 `input_exhausted`;input-closed 分支不再额外执行一遍未使用的常规发现;
- fragment 主循环使用同一 input-closed 判定与宽限/重新摄取生命周期;每次 terminal merge 推进 global event 后重新计算目标片,最终 pending/selected fragment proposal 由统一停机终态化。
- 每次成功合并后刷新 `last_progress_time`;legacy/fragment 在 quorum 等待超过 `no_progress_timeout_seconds` 时按既有语义停机,dynamic 则以该原因启动持久排空,最多允许 `max_terminal_merges` 次额外 merge 且受 global outer target 约束,controller/input closure 前不发布普通 terminal;
- 每次成功提交后执行 archive/GC,因此 active DB/checkpoint/proposal 面有界;
- 一旦成功进入主循环,finally 序列为:HA 先提交并发布 early stop 世代(legacy 直接发布 stop)→ 非 error 时等待 learner/末次摄取 → 只有全 stopped 才终态化未消费提议 → summary → 非 error 的 archive/GC → HA 以更高世代成对发布最终 stop/summary;随后 W&B finish/关库。early generation、summary 或 maintenance 窗口崩溃都保持 terminal 不完整,使 successor 可执行幂等修复。

### dynamic full 控制器

- `FencedSQLiteStore.initialize_dynamic_membership()` 幂等建立 stream/bootstrap controller state 并发布 bootstrap-ready;successor 读取 schema v3 现状,不重新分配 stream 或 logical request。
- 注册摄取验证 TTL、内容 hash、source/config/path/PBS job 和 logical request;带调度器的请求在 launch 行尚无精确回执绑定时保存为 pending 并保留请求文件,绑定到达后重试,错绑拒绝。准入事务拒绝健康 placement 重复、超容量和已 fulfill 请求,分配 placement/stream/admission generation/token 后发布 instance 产物。
- `record_capacity_observation()` 以 observation key/sequence 去重,并在同一事务维护连续 low 计数、冷却期和请求预算;merge 路径改由 `commit_full_merge()` 原子写对应 observation,无 merge 路径由原子 starvation observation API 同时推进世代。`LearnerLaunchOutbox` 提交带 request ID、walltime/queue 的 learner job 并对账 queued/running/finished/unknown 状态,预留容量只在已证实终态时释放。
- `commit_full_merge` 的 dynamic 参数要求每个 selected row 在事务内仍是 current admitted incarnation,且同 stream/placement 不重复;selection 之后发生 replacement/revoke 时整批不会错误应用旧成员。
- close policy 在一个事务中关闭准入、取消 open request 并冻结 `close_generation/max_terminal_version`。token target 使用 current version 作为上限,no-progress 也进入该状态机。`DynamicTerminalPublisher` 发布排空;`dynamic_input_closed` 要求所有 logical request/registration 可见性收敛,current 成员均确认或超时撤销,最终指针均进入摄取水位。successor 沿用冻结原因和上限,terminal merge 绝不超过它;非 error terminal 要求 controller closed 且 input closed。
- dynamic maintenance 把过期 instance、registration、launch request 和超出 retention 窗口的 observation 先 append+fsync 归档,再 fenced 删除 active 行;物理 UUID 指针/载荷仍由引用驱动 GC 处理。

### fragment 模式(`run_fragment_syncer`)

结构同上,差异:

- 开头 `raise NotImplementedError` 拦截 resume;
- 每轮以 `select_fragment(global_merge_event, K)` 确定目标片;资格、法定人数、宽限窗口、陈旧度、superseded/obsolete 丢弃全部**按片**进行;
- 合并成功后:片版本 +1、事件 +1,存片权重/优化器状态、写 `fragment_versions` 行,`publish_fragment_latest`(内含按需物化);
- 输入闭合(全部预期 learner stopped)后与 full 一样走 terminal grace/drain:`select_terminal_drain_fragment_updates` 对当前目标片按严格准入放宽 quorum 合并,目标片无合法 pending 即 `input_exhausted`;输入未闭合时 quorum 不足只能等待或 `no_progress_timeout`;
- finally:非 error 时先做一次**最终物化 + 发布**,再走 stop/末次摄取/终态化/archive/GC/W&B/close 序列。
