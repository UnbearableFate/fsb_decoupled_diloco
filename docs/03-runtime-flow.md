# 03 系统运行流程

本页按时间顺序描述一次 run 中每个进程做的事。函数名均可在 [modules/](modules/) 参考页中查到细节;术语约定见 [00-glossary.md](00-glossary.md)。

## 1. 启动与初始化

### 1.1 配置解析(两类进程相同)

`core/config.py: resolve_config()`:

1. 读 YAML(所有节都有默认值,未知键报错);
2. 命令行参数覆盖:`--run-id`、`--shared-root`、`--num-learners`;
3. `run_id` 缺省时取 `$RUN_ID` 环境变量或 `时间戳_run.name`;
4. `shared_root` 为 null 时回退到 `<project_root 或 cwd>/runs/fs_diloco/<run_id>`;非空字符串中的 `{run_id}` 用最终 ID 替换。仓库正式配置显式使用主工作树绝对模板;
5. `num_learners` 覆盖时同步收紧 `quorum_min/max`;通用配置校验后按 `global_adoption_strategy` 调用对应策略类的 `validate`。旧/未知键失败即关闭(fail-closed)。

### 1.2 syncer 初始化(全量模式,`initialize_run`)

1. 公共启动先解析 syncer 设备、`prepare_run_dirs()` 建齐共享目录树、直接打开 `<shared_root>/control/syncer_metadata.sqlite3`(建表幂等,回滚日志 + FULL sync),建立日志并初始化 W&B;W&B import/init 失败降级为不上报,然后才分派 full/fragment 初始化;
2. 若 DB 已有已提交 global 行 → 报错退出(防误覆盖已有 run;`latest.json` 不参与这个判定);
3. 按 `syncer.device` 选择 CPU/GPU,加载模型 → `build_param_index()` → 按 `syncer.compute_dtype` 扁平化初始 θ → `init_outer_state()`;
4. 原子发布 `control/param_index.json`,并把完整配置快照同时写到 run 根和 `control/run_config.resolved.yaml`;
5. `publish_global(version=0)`:保存 `global_v000000.safetensors` + `outer_v000000.safetensors`,在一个 DB 事务中写已提交 v0、run identity 与配置快照,**最后**原子写 `latest.json`(v0 就绪,learner 可以开工);
6. 执行首次归档/GC。

分片模式(`initialize_fragment_run`)额外:构建并发布 `fragment_index.json`;对每个分片抽取 θ_f、建外层状态、保存 v0 权重与优化器状态、写 `fragments`/`fragment_versions` 表;物化事件 0 的完整权重;发布分片布局的 `latest.json`。

### 1.3 syncer 恢复(`resume_run`,仅全量模式)

`init.resume: true` 时代替初始化:

1. 在创建任何新库之前确认持久 DB 文件存在;缺失即 fail closed;
2. 运行 SQLite `integrity_check`,校验 DB 中的 run identity、format/protocol 版本、模式和模型配置;
3. 取 DB 中最大已提交 `global_versions` 行作为唯一恢复版本;校验参数索引与当前模型兼容,并要求该行引用的权重/外层文件都存在;
4. 分别加载权重 θ 与外层 checkpoint 中的 θ,要求形状和值完全一致;不从 `latest.json` 猜测或回退;
5. 读取并校验当前每个固定心跳指针,保存其完整内容 SHA256 隔离栅栏;在一个 SQLite 事务中将崩溃遗留的全部 `selected` 行重置为 `pending`,把预期 learner 行重置为 `unknown/resumed` 并清空本代 `hostname/pid/last_seen/last_heartbeat_path`,同时写入 `run_state.resume_generation`;
6. 切代事务成功后,把仍存在的 stop/summary 标记(不可读,或可读但相应 reason 为空/`error`)用 `os.replace` 归档到 `logs/resume_*`;原子重建 `latest.json`,执行归档/GC 后继续主循环。摄取层忽略与隔离栅栏完全相同的旧心跳,learner 原子替换为新 active 后才重新打开本代输入。重复 resume 不增加 global 行,也不会重复应用更新;任一可读 stop reason 或 summary stop_reason 表示非 error 终态时拒绝 resume,不要求两文件成对存在或彼此一致。

### 1.4 HA full 初始化与接管

HA full 不复用上面的 legacy `initialize_run/resume_run` 入口:

1. operator/launcher 先运行 `python -m fs_diloco.tools.init_run`。它要求新的 run root,冻结 source manifest、解析后配置和 run descriptor,由 `initialize_new_run()` 创建完整 schema(static HA 为 v2,dynamic 为 v3),最后写 `bootstrap_complete.json`;
2. `run_syncer_candidate.pbs` 和 `run_static_learner.pbs` 分别在独立 PBS job 中读取 descriptor,并在 import runtime 前比对 git commit、dirty 状态和 source fingerprint;
3. candidate 用 `open_existing()` 验证 bootstrap 后轮询 `LeaderLeaseStore.acquire()`。只有成功者获得 `LeaderToken` 并打开领导者绑定的隔离 store,loser 不初始化 W&B、不写业务表;
4. 第一 epoch 从模型创建 v0。后继者从 SQLite current committed row 恢复权重/外层/selected 状态,校验文件 size 及所选摘要模式,修复 DB 已提交但权威控制面尚未发布的窗口;
5. 每次 checkpoint 先写到 `weights/epochs/eNNNNNN/` 和 `optim/epochs/eNNNNNN/` 的唯一发布路径,然后隔离 DB 事务提交 version/updates/control manifest,最后发布同 epoch 权威头部并尽力刷新 fixed cache;
6. leader 周期续约租约并发布独立 epoch 心跳;续约成功同时推进供业务事务共用的本地单调安全水位。正常结束先提交并发布只含 stop 的 early terminal 世代让 learner 停止,完成末次摄取、summary 与 maintenance 后再提交更高世代并成对发布权威 stop/summary。candidate 只有在 DB terminal 与这对最终控制面发布的路径、owner、epoch 和 SHA 全部一致时才拒绝重新取得运行权;中途崩溃留下的不完整 normal terminal 可由后继者取得新 epoch 并修复。`error` 世代只保留诊断/恢复依据,不是 learner 停止权威,learner 继续恢复对齐,后继者仍可取得新 epoch 并覆盖为更高世代正常终态。

learner 启动时只创建自身 instance 目录且不打开 SQLite。`EpochControlReader` 从有界 epoch 目录选择含合法自校验心跳或权威头部的最高的 epoch;头部再绑定不可变指针路径和 SHA,终态必须位于同一 epoch/owner 目录并通过自身 `payload_sha256` 复算。最高 epoch 暂缺头部时返回未就绪而不回退。fixed cache 被旧进程覆盖、低于 current epoch 或 identity 不符时直接忽略。看门狗在 leader 心跳陈旧但恢复 claim 仍在排队/运行或权威修复窗口内时继续等待。learner-assisted qsub 默认关闭;启用时也只有 claim/对齐能力,不会直接授予领导权。同一 stale observation 的 attempt 目录在看到更新 observation 前不会被归档,因此 retention 不会重置每 observation 预算;跨 observation 的 outstanding 上限按全部 active claim 计算。

### 1.5 dynamic 成员引导与准入

dynamic 只走 HA full 路径,并在加载模型前完成成员准入:

1. initializer 以 `stream_pool_size` 预建固定数据流,并为 `bootstrap_instances` 建立确定性引导启动请求;operator 把唯一 `BOOTSTRAP_SLOT` 传给各独立 `run_dynamic_learner.pbs`,scale replacement 则传 `FS_DILOCO_LAUNCH_REQUEST_ID`;
2. learner 每次启动生成新的 `learner_li_<uuid4>`,从 hostname 与 CUDA identity 构造部署位置,写身份/source/config/PBS-job 绑定的注册请求;它拒绝 `--learner-id` 和 `--num-learners`,不能伪装成 static 成员或在线改写数据流池;
3. leader 扫描固定请求目录,在隔离事务中验证请求 TTL/重放、logical 请求、调度器 job identity、健康部署位置替换策略和容量预算。带调度器的请求若尚无 launch 行精确 job 回执绑定则持久化为 pending 并保留文件,后续扫描在绑定到达后重试;错绑拒绝。验证完成才分配 `placement_epoch/stream_id/stream_epoch/admission_generation/token` 并发布 instance 级准入产物;
4. learner 只接受与自身请求、descriptor、instance 和令牌一致的准入。随后数据迭代器使用 `stream_id` 作为分片/RNG 身份、`stream_pool_size` 作为固定分母;replacement 复用数据流时以新 `stream_epoch` 记录重启,但数据映射不随活跃数改变;
5. leader 把已准入心跳批量摄取到单个隔离事务,周期记录幂等容量观测。merge 观测与对应 global version 同事务提交;饥饿世代与对应观测也同事务提交。只有不同观测形成的连续 low 窗口才创建 scale 启动请求;发件箱在 qsub 后持久化回执并用 qstat 对账保持 queued/running 预留,直到准入或已确认的调度器终态。

### 1.6 learner 启动(`run_learner` / `run_fragment_learner`)

1. 建目录、开 JSONL 日志、按 `seed + learner_index` 设随机种子、选 GPU;
2. 加载模型与分词器(HF `gpt2` 等,或 `synthetic-tiny` 冒烟模型);
3. **阻塞等待** `param_index.json`(分片模式还有 `fragment_index.json`),用本地模型重建索引严格比对;
4. **阻塞等待** `latest.json`,整体加载全局权重(分片模式:从各片权重物化后加载),按累计本地步 0 建内层 AdamW + 可选 warmup/cosine 调度器。若指针引用的 current-only 文件已被下一轮 GC 回收,learner 在有界预算内等待严格更新的 latest 并从整份新快照重试;
5. 写第一份心跳(phase=`loaded_global` / `loaded_fragment_latest`);
6. 构建数据迭代器:static WikiText-2 按 learner index/N 连续分片;dynamic 按已准入 stream ID/固定数据流池连续分片。随后逐文本分词、样本后加 EOS、拼接后切成不重叠整块并丢尾;`data.shuffle_blocks=true` 时以同一固定身份生成每 epoch 稳定重排,false 时按原序无限循环。`streaming` 只控制 datasets 加载方式,随后仍会物化块列表。合成模式也以 static learner index 或 dynamic stream ID 隔离随机流。

## 2. learner 主循环(全量模式)

启动步骤全部成功并进入训练 `try` 后,每一轮(一个「更新区间」)如下;更早的模型/index/latest/data 初始化异常不会发布最终 stopped 心跳:

```
while not stop_requested():                     # 默认:本地上限或 stop;global_only:只看 stop
  记录区间起点(步数、base_global_version)
  for _ in range(training.inner_steps):         # 内层训练
      train_one_step():
          gradient_accumulation_steps 个微批前向/反向(可选 bf16 autocast)
          可选梯度裁剪 → optimizer.step() → scheduler.step()
      按 log_every_steps 记 JSONL;按 heartbeat_interval 写心跳
      可选:poll_latest_during_inner_steps=true 时每个 optimizer.step() 后无阻塞轮询新版
           由 GlobalAdoptionStrategy 决定是否轮询与如何采纳:
             replace:整体替换本地权重
             rebase/predict:仅当私有 reference 尚在等待新版时轮询并对齐
  # —— 上传阶段 ——
  failure_sim:可选随机睡眠;可选跳过上传;可选崩溃(exit 97)
  按 io.tensor_dtype 扁平化模型参数(50×10 配置即 50 内层步 × 10 外层步,此处为 bfloat16),用 float32 累积计算 param_norm
  write_update():先原子写 payloads/learner_XXX/<update_id>.params.safetensors,
                 再原子替换 latest/learner_XXX.json(固定提议提交面)
  记 learner_metrics.csv、update_manifest.csv,写心跳(phase=update_written)
  # —— 采纳阶段 ——
  if learner.adopt_global_after_upload:
      strategy.on_after_publish():读 latest;若已有新版则直接采纳
      若已有 stop 仍先采纳已可见新版;无新版则不再构造变基锚点/预测参照
      无 stop 且无新版时,rebase 才构造锚点、predict 才构造预测参照
      runner 根据 StrategyAction 统一 reset 或保留优化器;重建调度器时恢复累计 local-step 相位并发公共事件
finally:
  写 status=stopped 的最终心跳,记 process_exit
```

要点:

- 上传的是**参数本身**而非差值;伪梯度由 syncer 计算。
- LR 调度只由累计 `local_step`、`warmup_steps`、独立 `scheduler_total_steps` 与 `min_lr_ratio` 决定;采纳重建不会再次 warmup,超过上限后保持正下限。
- `base_global_version` 初始取区间开始时已加载的版本;显式开启 replace + 区间中途轮询后,每次中途采纳会更新为上传时的最新 global。此时区间前半段仍来自旧 base,提议以 `mid_cycle_adoption_count` 和最近一次 `base_switched_at_step` 标明这个近似;计数器在每个周期开始时清零,upload skip 不会把证据带入下一周期。
- local-delta rebase 仅在发布后的第一次 latest 检查未发现新版时保留 `x_local,t`;发现首个新版并完成 `global_new+(local-x_local,t)` 后立即释放,直到下一次发布才可能重新建立。prediction、reference 和对齐算术统一使用 `syncer.compute_dtype`,默认在 learner GPU 上执行;CUDA 安全余量不足或实际 OOM 时保持 dtype 回退 CPU,并把部署位置、估算字节数和回退原因写入 learner JSONL。
- predict 对齐的 wait 返回 `None` 后会重新检查 stop:stop 在场是正常放弃并清空预测状态;没有 stop 才是 `TimeoutError`。该区分不靠延长对齐超时。
- learner 不自行删除提议;载荷生命周期由 syncer 根据 DB/指针引用统一回收。
- dynamic 提议还携带 instance、部署位置纪元、数据流纪元、准入世代和令牌哈希。摄取时验证一次,选中提交前在同一 global commit 事务再次验证;被 replacement/revoke 越过的旧化身即使恢复写心跳或指针也不能提交。
- 心跳、日志、指标全部只追加/原子覆盖,不依赖任何锁。
- `completion_mode=global_only` 到达 `max_local_steps` 时记录 `local_step_horizon_reached`,但继续执行完整训练/上传循环,直到 syncer 达到全局目标并发布 `stop.json`。
- 首次加载 latest 后,full/fragment learner 都启动进展看门狗。每个 optimizer step 后检查截止时刻;触发前重读 latest/stop,确认 syncer 无进展后记录 `syncer_unresponsive`、写带同名 `status_reason` 的 stopped 心跳并以 0 退出。

## 3. learner 主循环(分片模式差异)

- 每轮上传前用 `select_fragment(local_update_index, K)` 决定本轮上传哪一片,再用 `extract_fragment_from_model()` 按分片切片直接读取目标命名参数并转换为 `io.tensor_dtype`,不构造完整扁平向量;随后写不可变 `update_*_fXXX.params.safetensors`,再把带 `update_kind: "fragment"`、`fragment_id`、`base_fragment_version`、`base_global_merge_event`、`tensor_dtype` 的元数据原子替换到 per-(learner,fragment) 固定指针。
- `param_norm` 通过逐参数 FP32 L2 范数再汇总计算,同样不需要完整扁平副本;`fragment_norm` 对实际上传片以 FP32 累积计算。
- 采纳是**增量**的:`adopt_fragment_updates()` 只加载 `latest.json` 中版本比本地新的片,散回本地向量后一次性写回模型;有变化时按配置重置内层优化器。若任一片在加载时已被 GC,丢弃本次私有 flat/version 草稿,等待更新的 global merge event 后从整份 fragment latest 重试,绝不混合两个快照。
- fragment 提议的载荷目录只保存不可变 tensor;syncer 只枚举 `updates/latest/learner_XXX_fNNN.json` 固定指针,SQLite 摄取水位与文件签名短路重放/重复解析,消费后的 tensor 由引用驱动 maintenance 统一回收。
- 收尾与 full 共用 `completion_mode`:`local_or_global` 到达 `max_local_steps` 后进入 final wait,`global_only` 则继续训练直到全局 stop。fragment final wait 在独立 no-progress 截止时刻内继续采纳 latest,并按 `heartbeat_interval_seconds` 写 `active, phase=final_fragment_wait`;版本采纳不延长截止时刻,退出 finally 最终再写一次 `stopped, phase=process_exit`。syncer 在全部 learner stopped 后负责末端宽限/排空和最终 stop。

## 4. syncer 主循环(全量模式)

每次迭代尝试完成一次 `v → v+1`:

```
while True:
  停机检查:stop_after_outer_steps / stop_after_global_tokens
  sync_liveness_and_metadata():摄取心跳 → 重分类存活状态 → 读取恰好 N 个固定提议指针
  input_closed = 全部预期 learner 最终心跳均为 stopped?
  if input_closed:
      (首次)末端宽限:睡一个宽限期后再摄取一轮
      decision = select_terminal_drain_updates():严格 future/staleness 准入,
                 按配置的选择策略每 learner 选一,允许低于 quorum_min
      decision=open → 复位宽限,回到常规发现
      decision=closed_selected → 合并 decision.selected
      decision=closed_empty → input_exhausted 停机
  else:
      eligible = 库中 pending 且 staleness ≤ max_staleness_versions 的更新
      丢弃张量文件已消失者(dropped: missing_file)
      one_per_learner = select_one_per_learner(eligible, quorum_max 截断)
      不足 quorum_min → 记 quorum_wait;无进展超时则停机;否则 sleep(scan_interval) 重来
      达到 quorum_min → collect_with_grace_window():宽限窗口内反复重扫,凑 quorum_max 或超时
                        (窗口结束仍不足 quorum_min → 重来)

  mark_updates_selected(CAS:仅 pending → selected)
  读取阶段:再查文件存在性;加载所有向量、转为 syncer.compute_dtype 后送到 syncer.device
      (发现丢失 → 丢该份、其余回滚 pending、放弃本次合并)
  加权:w_i ∝ tokens_i / (1 + λ·staleness_i),归一化
  聚合:p̄ = Σ wᵢ pᵢ;g = θ − p̄
  外层步进:θ, state = outer_optimizer_step(θ, g, state)
  发布:publish_global(v+1)
      # 默认并发写权重/外层(可配串行 weight→outer)
      # 两者完成 → 一个 SQLite 事务(版本+applied+drop) → latest.json
  archive_and_prune():终态 update/旧 version 先 append+fsync JSONL,
                      再以同一 SQLite 事务 stage gc_pending + 删除活跃 DB 行
  collect_runtime_artifacts():按 DB/latest/指针引用只保留当前 checkpoint 与 live payload
  记 syncer_metrics.csv 与 W&B;v = v+1;刷新进展时间戳
finally:
  HA:提交/发布 early stop 世代;legacy:发布 stop.json(带 reason)
  → 等待 stopped 心跳/末次摄取 → 终态化剩余提议 → summary
  → (非 error 才 archive/GC)
  → HA:提交并成对发布 post-maintenance stop/summary 世代
  → W&B finish → 关库
```

该 finally 只覆盖已经成功完成初始化/恢复并进入主循环的阶段;startup/init/resume 异常发生得更早,不走这套 stop/summary/finish/close。主循环 finally 中 stop、wait/ingest、summary 和 maintenance 是同一顺序 try;前一步如果自身抛异常,不保证后续步骤继续,但最内层 finally 仍尝试 W&B finish 和 DB close。

## 5. syncer 主循环(分片模式差异)

- 每轮先算目标片 `k = global_merge_event mod K`,资格查询、法定人数、宽限窗口都**只针对该片**的更新、以该片的 `fragment_version` 计陈旧度;
- 合并后:该片 θ_f/外层状态/版本推进,`global_merge_event +1`;依次保存该片新权重与优化器状态、提交 `fragment_versions` 行、发布 latest,再单独把选中 update 标为 applied/丢弃 obsolete;这些不是一个跨文件/跨表事务,且分片不支持崩溃恢复;
- `publish_fragment_latest()`:按 `should_materialize_fragment_full()` 决定是否重拼完整权重(事件 0、达到目标步数、或每个显式正整数 `materialize_full_every_events` 周期),然后原子写分片布局的 `latest.json`;
- 丢弃逻辑同样按片(superseded 按 learner+片;obsolete 按该片陈旧度窗口);
- 正常停机时 finally 中额外做一次**强制最终物化**,发布 stop 后做末次摄取、终态化、归档/GC。
- 不支持 resume;全部 learner 已 stopped 后会按片执行末端宽限/排空,允许低于正常 `quorum_min` 的最后合并;仍无合法 pending 时以 `input_exhausted` 正常结束。

## 6. 一次完整 run 的时间线(全量模式示例)

```
t0   syncer: 初始化,发布 v0 + latest.json
t0+  learners: 等到 latest.json,加载 v0,开始 inner steps
t1   各 learner 陆续提交第 1 份 update(base=0)
t1+  syncer: 凑齐 quorum → 宽限窗口 → 合并 → 发布 v1
t1++ learners: 上传后轮询发现 v1 → 按 replace/rebase/predict 策略采纳 → 继续训练
...  循环往复;慢 learner 的更新带着更高 staleness 参与或被弃
tN   达到 stop_after_outer_steps → syncer 发布 stop.json并等待 learners 收尾
tN+  learners: 看到 stop.json(或已在有限步 final wait),写 stopped 心跳退出
tN++ syncer:末次摄取;若全部 stopped 则终态化未消费提议,写 summary/archive/GC 后退出
```

HA full 的差异是 `t0` 之前由独立 initializer 完成 schema/descriptor;syncer/learner 分别由不同 PBS job 启动。任一时刻只有持 current token 的 leader 可提交。如果 leader 在 vN DB 提交后崩溃,后继者先取得 epoch `e+1`,从 vN 恢复并重发 current 权威头部,再把下一次训练提交写成 vN+1,而不是从 fixed latest 猜版本。

dynamic HA 在此基础上把 `t0+` 改为 bootstrap 注册/准入,并在运行中允许发件箱补充 replacement。global、token、manual、deadline、budget 或 no-progress 条件进入关闭事务后,准入关闭且 `max_terminal_version` 冻结;token 在 current version 冻结上限,no-progress 从 current version 启动持久排空,并与 manual/budget/deadline 一样至多允许 `max_terminal_merges` 次额外 merge(仍受 global outer target 约束),二者都不直接发布普通 terminal。leader 发布排空世代;健康 learner 在周期边界写最终指针和确认,超时实例被撤销。所有请求/注册可见性条件和确认/撤销条件都成立、`dynamic_input_closed` 为真后才执行最后的有界 merge 并发布 terminal control;后继者恢复冻结原因和上限。

## 7. 运行期观测点

| 想看什么 | 看哪里 |
|---|---|
| syncer 在等 quorum 还是在合并 | `logs/syncer.jsonl`(`quorum_wait` / `updates_selected` / `outer_step_applied` 事件) |
| 每次合并的耗时分解 | `metrics/syncer_metrics.csv`(read/aggregation/outer_step/publish 秒数) |
| 各 learner 的 loss / 吞吐 | `metrics/learner_metrics.csv`,或 W&B(syncer 侧汇总 selected 更新的 loss 统计) |
| 某 learner 是否活着 | `heartbeats/learner_XXX.json` 的 timestamp/phase |
| 每份 update 的下场 | 活跃 SQLite + `metrics/update_history.jsonl` 的合并视图(`python -m fs_diloco.analysis summary <run_root>`) |
