# 03 系统运行流程

本页按时间顺序描述一次 run 中每个进程做的事。函数名均可在 [modules/](modules/) 参考页中查到细节。

## 1. 启动与初始化

### 1.1 配置解析(两类进程相同)

`core/config.py: resolve_config()`:

1. 读 YAML(所有节都有默认值,未知键报错);
2. CLI 参数覆盖:`--run-id`、`--shared-root`、`--num-learners`;
3. `run_id` 缺省时取 `$RUN_ID` 环境变量或 `时间戳_run.name`;
4. `shared_root` 缺省为 `<cwd>/runs/fs_diloco/<run_id>`;
5. `num_learners` 覆盖时同步收紧 `quorum_min/max`;fragment 配置合法性校验。

### 1.2 syncer 初始化(全量模式,`initialize_run`)

1. `prepare_run_dirs()` 建齐共享目录树;直接打开 `<shared_root>/control/syncer_metadata.sqlite3`(建表幂等,rollback journal + FULL sync);
2. 若 DB 已有 committed global row → 报错退出(防误覆盖已有 run;`latest.json` 不参与这个判定);
3. 加载模型 → `build_param_index()` → flatten 出初始 θ(float32)→ `init_outer_state()`;
4. 原子发布 `control/param_index.json`,并把完整配置快照同时写到 run 根和 `control/run_config.resolved.yaml`;
5. `publish_global(version=0)`:保存 `global_v000000.safetensors` + `outer_v000000.safetensors`,在一个 DB 事务中写 committed v0、run identity 与配置快照,**最后**原子写 `latest.json`(v0 就绪,learner 可以开工);
6. 执行首次 archive/GC;初始化 W&B(失败降级为不上报)。

fragment 模式(`initialize_fragment_run`)额外:构建并发布 `fragment_index.json`;对每个 fragment 抽取 θ_f、建外层状态、保存 v0 权重与优化器状态、写 `fragments`/`fragment_versions` 表;materialize 事件 0 的完整权重;发布 fragment 布局的 `latest.json`。

### 1.3 syncer 恢复(`resume_run`,仅全量模式)

`init.resume: true` 时代替初始化:

1. 在创建任何新库之前确认持久 DB 文件存在;缺失即 fail closed;
2. 运行 SQLite `integrity_check`,校验 DB 中的 run identity、format/protocol version、模式和模型配置;
3. 取 DB 中最大 committed `global_versions` 行作为唯一恢复版本;校验 param index 与当前模型兼容,并要求该行引用的权重/outer 文件都存在;
4. 分别加载权重 θ 与 outer checkpoint 中的 θ,要求形状和值完全一致;不从 `latest.json` 猜测或回退;
5. 将崩溃遗留的全部 `selected` 行重置为 `pending`,原子重建 `latest.json`,执行 archive/GC 后继续主循环。重复 resume 不增加 global row,也不会重复应用 update。

### 1.4 learner 启动(`run_learner` / `run_fragment_learner`)

1. 建目录、开 JSONL 日志、按 `seed + learner_index` 设随机种子、选 GPU;
2. 加载模型与 tokenizer(HF `gpt2` 等,或 `synthetic-tiny` 冒烟模型);
3. **阻塞等待** `param_index.json`(fragment 模式还有 `fragment_index.json`),用本地模型重建 index 严格比对;
4. **阻塞等待** `latest.json`,整体加载全局权重(fragment 模式:从各片权重 materialize 后加载),建内层 AdamW + warmup/cosine 调度器;
5. 写第一份心跳(phase=`loaded_global` / `loaded_fragment_latest`);
6. 构建数据迭代器:WikiText-2 按 learner 连续分片(`dataset.shard(num_shards=N, index=i, contiguous=True)`)后切块、无限循环;或合成随机 token 流。

## 2. learner 主循环(全量模式)

每一轮(一个"更新区间"):

```
while not stop_requested():                     # 默认:本地上限或 stop;global_only:只看 stop
  记录区间起点(步数、base_global_version)
  for _ in range(training.inner_steps):         # 内层训练
      train_one_step():
          gradient_accumulation_steps 个 micro-batch 前向/反向(可选 bf16 autocast)
          可选梯度裁剪 → optimizer.step() → scheduler.step()
      按 log_every_steps 记 JSONL;按 heartbeat_interval 写心跳
      可选:poll_latest_during_inner_steps=true 时每个 optimizer.step() 后无阻塞轮询新版
           replace:整体替换本地权重
           rebase_post_publish_delta:仅当 CPU reference 尚在等待新版时轮询;
                                     发现后 local ← global_new + (local - reference),随即释放 reference
  # —— 上传阶段 ——
  failure_sim:可选随机睡眠;可选跳过上传;可选崩溃(exit 97)
  按 io.tensor_dtype flatten 模型参数(50x10 配置为 bfloat16),用 float32 累积计算 param_norm
  write_update():先原子写 payloads/learner_XXX/<update_id>.params.safetensors,
                 再原子替换 latest/learner_XXX.json(固定 proposal 提交面)
  记 learner_metrics.csv、update_manifest.csv,写心跳(phase=update_written)
  # —— 采纳阶段 ——
  if learner.adopt_global_after_upload:
      读 latest.json
      若已有新版:直接采纳,不构造 CPU FP32 reference
      若无新版且使用 local-delta rebase:才从当前模型构造并保留 CPU FP32 reference
      采纳后重建内层优化器/调度器
finally:
  写 status=stopped 的最终心跳,记 process_exit
```

要点:

- 上传的是**参数本身**而非差值;伪梯度由 syncer 计算。
- `base_global_version` 初始取区间开始时已加载的版本;显式开启区间中途采纳后,会更新为本次上传实际基于的最新 global。
- local-delta rebase 仅在发布后的第一次 latest 检查未发现新版时保留 `x_local,t`;发现首个新版并完成 `global_new+(local-x_local,t)` 后立即释放,直到下一次发布才可能重新建立。
- learner 不自行删除 proposal;payload 生命周期由 syncer 根据 DB/指针引用统一回收。
- 心跳、日志、指标全部只追加/原子覆盖,不依赖任何锁。
- `completion_mode=global_only` 到达 `max_local_steps` 时记录 `local_step_horizon_reached`,但继续执行完整训练/上传循环,直到 syncer 达到全局目标并发布 `stop.json`。

## 3. learner 主循环(fragment 模式差异)

- 每轮上传前用 `select_fragment(local_update_index, K)` 决定本轮上传哪一片,再用 `extract_fragment_from_model()` 按 fragment slices 直接读取目标命名参数并转换为 `io.tensor_dtype`,不构造完整 flatten 向量;随后写 `update_*_fragment_XXX.params.safetensors` + meta(带 `update_kind: "fragment"`、`fragment_id`、`base_fragment_version`、`base_global_merge_event`、`tensor_dtype`)。
- `param_norm` 通过逐参数 FP32 L2 norm 再汇总计算,同样不需要完整扁平副本;`fragment_norm` 对实际上传片以 FP32 累积计算。
- 采纳是**增量**的:`adopt_fragment_updates()` 只加载 `latest.json` 中版本比本地新的片,scatter 进本地向量后一次性写回模型;有变化时按配置重置内层优化器。
- fragment proposal 仍在 `updates/payloads/learner_XXX/` 中保留独立 meta 文件供扫描;不同片消费后由 syncer 的 reference-driven maintenance 统一回收。
- 收尾:到达 `max_local_steps` 后不立即退出,在 finally 中轮询等待 `global_merge_event` 达到 `stop_after_outer_steps`(带超时),期间持续采纳,最后再整体采纳一次最终版本——保证退出时本地模型是最终模型。

## 4. syncer 主循环(全量模式)

每次迭代尝试完成一次 `v → v+1`:

```
while True:
  停机检查:stop_after_outer_steps / stop_after_global_tokens
  sync_liveness_and_metadata():摄取心跳 → 重分类 liveness → 读取恰好 N 个固定 proposal pointers
  eligible = 库中 pending 且 staleness ≤ max_staleness_versions 的更新
  丢弃张量文件已消失者(dropped: missing_file)
  one_per_learner = select_one_per_learner(eligible, quorum_max 截断)

  if 不足 quorum_min:
      terminal drain 可行?(全部预期 learner 最终心跳均为 stopped)
        → 是:以 oldest_pending 选剩余更新,继续走合并
        → 否:记 quorum_wait;无进展超时则停机;否则 sleep(scan_interval) 重来
  else:
      collect_with_grace_window():在宽限窗口内反复重扫,凑 quorum_max 或超时
      (窗口结束仍不足 quorum_min → 再试 terminal drain,否则重来)

  mark_updates_selected(CAS:仅 pending → selected)
  读取阶段:再查文件存在性;加载所有向量、转为 float32 后送到 GPU
      (发现丢失 → 丢该份、其余回滚 pending、放弃本次合并)
  加权:w_i ∝ tokens_i / (1 + λ·staleness_i),归一化
  聚合:p̄ = Σ wᵢ pᵢ;g = θ − p̄
  外层步进:θ, state = outer_optimizer_step(θ, g, state)
  发布:publish_global(v+1)
      # 权重 → outer → 一个 SQLite 事务(版本+applied+drop) → latest.json
  archive_and_prune():终态 update/旧 version 先 append+fsync JSONL,再删活跃 DB 行
  collect_runtime_artifacts():按 DB/latest/pointer 引用只保留当前 checkpoint 与 live payload
  记 syncer_metrics.csv 与 W&B;v = v+1;刷新进展时间戳
finally:
  发布 stop.json(带 reason)→ 等待 stopped 心跳/末次摄取 → 终态化剩余 proposal
  → summary → archive/GC → W&B finish → 关库
```

## 5. syncer 主循环(fragment 模式差异)

- 每轮先算目标片 `k = global_merge_event mod K`,资格查询、quorum、宽限窗口都**只针对该片**的更新、以该片的 `fragment_version` 计 staleness;
- 合并后:该片 θ_f/外层状态/版本推进,`global_merge_event +1`;保存该片新权重与优化器状态、写 `fragment_versions` 行;
- `publish_fragment_latest()`:按 `should_materialize_fragment_full()` 决定是否重拼完整权重(事件 0、达到目标步数、或每 `materialize_full_every_events` 次),然后原子写 fragment 布局的 `latest.json`;
- 丢弃逻辑同样按片(superseded 按 learner+片;obsolete 按该片 staleness 窗口);
- 正常停机时 finally 中额外做一次**最终 materialize**,发布 stop 后做末次摄取、终态化、archive/GC。
- 不支持 resume;没有 terminal merge(quorum 不足只能等待或超时停机)。

## 6. 一次完整 run 的时间线(全量模式示例)

```
t0   syncer: 初始化,发布 v0 + latest.json
t0+  learners: 等到 latest.json,加载 v0,开始 inner steps
t1   各 learner 陆续提交第 1 份 update(base=0)
t1+  syncer: 凑齐 quorum → 宽限窗口 → 合并 → 发布 v1
t1++ learners: 上传后轮询发现 v1 → 整体采纳、重置内层优化器 → 继续训练(base=1)
...  循环往复;慢 learner 的更新带着更高 staleness 参与或被弃
tN   达到 stop_after_outer_steps → syncer 发布 stop.json、收尾 archive/GC、退出
tN+  learners: 看到 stop.json(或先到 max_local_steps),写 stopped 心跳退出
```

## 7. 运行期观测点

| 想看什么 | 看哪里 |
|---|---|
| syncer 在等 quorum 还是在合并 | `logs/syncer.jsonl`(`quorum_wait` / `updates_selected` / `outer_step_applied` 事件) |
| 每次合并的耗时分解 | `metrics/syncer_metrics.csv`(read/aggregation/outer_step/publish 秒数) |
| 各 learner 的 loss / 吞吐 | `metrics/learner_metrics.csv`,或 W&B(syncer 侧汇总 selected 更新的 loss 统计) |
| 某 learner 是否活着 | `heartbeats/learner_XXX.json` 的 timestamp/phase |
| 每份 update 的下场 | 活跃 SQLite + `metrics/update_history.jsonl` 的合并视图(`python -m fs_diloco.analysis summary <run_root>`) |
