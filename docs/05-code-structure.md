# 05 代码结构

## 1. 目录树与职责

```
fsb_decoupled_diloco/
├── fs_diloco/                     # Python 包
│   ├── core/                      # 最底层:配置与常量,不依赖其他子包
│   │   ├── config.py              #   YAML → dataclass 配置,resolve/校验/序列化
│   │   ├── constants.py           #   格式版本、状态常量、文件名模板、learner id 编解码
│   │   └── run_descriptor.py      #   immutable run/source/config 身份的构建与启动前校验
│   ├── storage/                   # 文件系统与元数据持久化
│   │   ├── atomic_io.py           #   原子写(bytes/text/json/writer)、safe_read_json、sha256
│   │   ├── paths.py               #   RunPaths:共享目录布局的唯一定义;prepare_run_dirs
│   │   ├── tensor_codec.py        #   safetensors 存取:全局权重/外层状态/update 向量
│   │   ├── sqlite_store.py        #   持久 SQLite:update/learner/version/事务状态机
│   │   ├── schema.sql             #   数据库 schema(随包分发)
│   │   ├── schema_bootstrap.py    #   HA 唯一 initializer 与 bootstrap-complete 发布
│   │   ├── leader_lease.py        #   单调 epoch 租约、renew/release 与本地安全预算
│   │   ├── fenced_store.py        #   token 绑定、事务内隔离校验和业务事务遥测
│   │   └── maintenance.py         #   crash-safe JSONL 归档、DB 剪枝、引用驱动 GC
│   ├── protocol/                  # 协议逻辑(纯函数为主,不做 I/O 或仅薄封装)
│   │   ├── merge.py               #   陈旧度、加权、每 learner 选一、加权平均
│   │   ├── liveness.py            #   心跳校验/摄取、active/stale/dead 分类、无进展超时
│   │   ├── control_epoch.py       #   epoch 权威控制发布、读取、修复与 history
│   │   ├── membership.py          #   dynamic UUID、注册/准入、bootstrap manifest
│   │   ├── dynamic_terminal.py    #   identity-bound manual close 与权威排空读写
│   │   ├── fragment_index.py      #   分片索引构建(full/balanced_tensor)与严格校验
│   │   ├── fragment_codec.py      #   分片抽取/scatter/物化、分片 safetensors 存取
│   │   └── fragment_scheduler.py  #   轮转调度与期望版本推算
│   ├── modeling/                  # 模型与训练组件
│   │   ├── hf_model.py            #   HF 模型/tokenizer 加载、TinyCausalLM 冒烟模型、选设备
│   │   ├── hf_data.py             #   WikiText 分片/切块/无限批迭代器、合成数据
│   │   ├── param_index.py         #   参数索引:模型 ↔ 扁平向量互转与兼容性校验
│   │   └── outer_optim.py         #   显式扁平向量外层优化器(sgd/momentum/nesterov/adamw)
│   ├── observability/             # 观测
│   │   ├── logging_utils.py       #   JsonlLogger、未捕获异常钩子
│   │   ├── metrics.py             #   CSV 追加与三张表的字段清单
│   │   ├── phase1_performance.py  #   matched p99 门禁的冻结采样数/公式
│   │   ├── resource_monitor.py    #   /proc CPU、CUDA utilization、step/cycle 资源统计
│   │   └── wandb_logging.py       #   W&B 命名/标签/config/选中更新统计
│   ├── runtime/                   # 进程实现(组装以上全部)
│   │   ├── adoption.py            #   full learner 的 replace/rebase/predict 策略状态机
│   │   ├── learner.py             #   learner 主循环(全量 + 分片两套)
│   │   ├── syncer.py              #   syncer 主循环(全量 + 分片两套)、初始化/恢复/发布
│   │   ├── syncer_ha.py           #   leader-bound store、租约续约线程、epoch bootstrap/repair
│   │   ├── pbs_scheduler.py       #   qsub/qstat 适配、job 分类、candidate/dynamic 提交
│   │   ├── launch_outbox.py       #   恢复 claim 及 dynamic 扩容发件箱/对账
│   │   └── failure_sim.py         #   故障注入:随机睡眠/跳过上传/崩溃
│   ├── tools/                     # 离线工具
│   │   ├── init_run.py            #   HA run 唯一 initializer CLI
│   │   ├── launch_independent_run.py # initializer + 独立 syncer/learner qsub
│   │   ├── launch_phase1_acceptance.py # crash/successor/learner 验收提交与持久回执
│   │   ├── phase1_matched_performance.py # completed Checker 所需 matched 性能 artifact
│   │   ├── launch_phase2_acceptance.py # G8/G9 独立 dynamic job 提交与 bootstrap manifest
│   │   ├── launch_phase2_matched.py # static/dynamic 同配置顺序隔离 matched 提交
│   │   ├── phase2_{test,chaos,matched}_evidence.py # Phase 2 结构化证据生成器
│   │   ├── request_dynamic_close.py # identity-bound operator close CLI
│   │   ├── clean_run.py           # completion-evidence 门禁、dry-run 清单与可审计清理
│   │   ├── analysis.py            #   run 摘要与断言(读共享目录 + 持久 DB/archive,不依赖 torch)
│   │   ├── compare_event_traces.py #  profile 驱动的 actor 事件轨迹比较
│   │   ├── eval_lm_harness.py     #   checkpoint 解析/导出为 HF 目录/lm-eval 结果转 CSV
│   │   ├── run_metrics_csv.py     #   多 run 系统/质量指标矩阵导出
│   │   ├── validation_eval.py     #   resolved-config validation loss/ppl 与身份校验
│   │   └── publish_quality_gate.py #  FP32/BF16 paired quality/trend 三态门禁
│   ├── cli.py                     # python -m fs_diloco.cli {syncer|learner|inspect}
│   ├── __init__.py                # 包版本 __version__ = 0.1.0
│   └── {learner,syncer,analysis,eval_lm_harness}.py   # 兼容入口(转发到 runtime/tools)
├── main.py                        # 等价于调用 fs_diloco.cli.main()
├── configs/                       # YAML 配置(gpt2+wikitext2 全量/分片、tiny 冒烟)
│   └── 5000/                     #   保留的 200×25 historical experiment config
├── scripts/
│   ├── miyabi/                    # PBS 批作业(1/2/9 节点)与检查脚本
│   └── local/                     # 本地 CPU 合成冒烟
├── tests/                         # pytest 单元/集成测试
├── docs/                          # 本系统文档
├── plans/                         # 实施计划与已完成任务记录,不进入 runtime
├── reports/                       # 已保留的实验/审计证据与 run metrics
├── pyproject.toml                 # 包元数据、依赖/extras、console scripts
├── uv.lock                        # 锁定依赖
└── .python-version                # Python 3.13
```

## 2. 分层与依赖方向

主要运行时依赖自下而上:

```
core  ←  storage  ←  protocol / modeling / observability  ←  runtime  ←  (入口 shim)
                                                          ←  tools
```

- `core/config.py` 在 resolve 尾部延迟 import 无 torch 依赖的 `runtime/adoption.py`,只为经唯一策略类型表调用当前策略的 class-level `validate`;写快照时另延迟 import `storage.atomic_io`。这是配置扩展点的显式反向依赖,adoption 模块不得因此 import config 或 learner runner;
- `protocol/merge.py`、`fragment_scheduler.py`、`outer_optim.py` 是**纯函数**模块,不碰文件系统,单测友好;
- 只有 `runtime/` 同时了解「共享文件系统协议」和「SQLite 状态机」;
- `tools/analysis.py` 故意只用标准库 + 少量 protocol 纯函数,可在无 GPU/torch 环境运行(`eval_lm_harness.py` 的 torch 依赖也是函数内延迟 import)。

## 3. 两个运行时进程的内部结构

### `runtime/learner.py` 与 `runtime/adoption.py`

| 区块 | 函数 |
|---|---|
| CLI 与配置 | `parse_args`, `main` |
| 共享文件读写 | `write_heartbeat`, `wait_for_json`, `read_latest_if_newer`, `read_fragment_latest_if_newer`, `wait_for_fragment_latest_if_newer` |
| 停止判定 | `stop_requested`(full/fragment 共用) |
| 训练组件 | `build_inner_optimizer_and_scheduler`, `maybe_autocast`, `train_one_step` |
| 全局权重采纳 | `adopt_global`(全量)/ `load_fragment_latest_into_model`, `adopt_fragment_updates`, `apply_fragment_adoption`(分片) |
| full 采纳策略 | `GlobalAdoptionStrategy` 及 replace/rebase/predict 实现、`make_global_adoption_strategy` |
| update 提交 | `write_update` / `write_fragment_update` |
| 主循环 | `run_learner`(全量)/ `run_fragment_learner`(分片);`run_learner` 按配置分派 |

### `runtime/syncer.py`

| 区块 | 函数 |
|---|---|
| CLI 与配置 | `parse_args`, `sqlite_path`, `main` |
| 发布 | `latest_payload`, `publish_global`(全量);`fragment_latest_payload`, `should_materialize_fragment_full`, `publish_fragment_latest`(分片);`publish_stop` |
| 初始化/恢复 | `initialize_run`, `initialize_fragment_run`, `resume_run`(DB-first) |
| 摄取 | `validate_update_metadata`, `ingest_update_metadata`, `sync_liveness_and_metadata` |
| 选择 | `UpdateProposalSource`(full/fragment 参数对象),共享 `collect_with_grace_window`、`drop_missing_update_files`、`all_expected_learners_stopped` 与 terminal selector;full/fragment 分别由 `select_terminal_drain_updates` / `select_terminal_drain_fragment_updates` 接入严格准入 |
| 观测 | `init_wandb_run`, `_fragment_staleness_stats`, `wait_for_learner_shutdown`, `write_training_summary` |
| 主循环 | `run_syncer`(全量,含分派)/ `run_fragment_syncer`(分片) |

## 4. 入口点一览

| 命令 | 实际实现 |
|---|---|
| `python -m fs_diloco.learner` | `runtime/learner.py: main` |
| `python -m fs_diloco.syncer` | `runtime/syncer.py: main` |
| `python -m fs_diloco.analysis` | `tools/analysis.py: main`(`summary` / `assert-fragment-smoke` / `assert-fragment-5000` 子命令) |
| `python -m fs_diloco.eval_lm_harness` | `tools/eval_lm_harness.py: main`(`resolve-checkpoint` / `export-checkpoint` / `results-to-csv`) |
| `python -m fs_diloco.tools.init_run` | `tools/init_run.py: main`(创建 immutable HA run) |
| `python -m fs_diloco.tools.launch_independent_run` | `tools/launch_independent_run.py: main`(生成或提交独立 syncer/learner 作业) |
| `python -m fs_diloco.tools.launch_phase1_acceptance` | `tools/launch_phase1_acceptance.py: main`(提交 crash/successor/learner 验收作业并持久化回执) |
| `python -m fs_diloco.tools.phase1_matched_performance` | `tools/phase1_matched_performance.py: main`(生成 Phase 1 completed 门禁产物) |
| `python -m fs_diloco.tools.launch_phase2_acceptance` | `tools/launch_phase2_acceptance.py: main`(提交 G8/G9 dynamic 作业并持久化 receipt/manifest) |
| `python -m fs_diloco.tools.launch_phase2_matched` | `tools/launch_phase2_matched.py: main`(提交隔离的 static/dynamic matched run 与 checker) |
| `python -m fs_diloco.tools.request_dynamic_close` | `tools/request_dynamic_close.py: main`(发布认证 manual close request) |
| `fs-diloco-syncer` / `fs-diloco-learner` | 由 `pyproject.toml` 直接映射到两个 `runtime.*:main` |
| `fs-diloco-inspect` | `tools/analysis.py: main` |
| `fs-diloco-lm-eval` | `tools/eval_lm_harness.py: main` |
| `fs-diloco-export-run-metrics` | `tools/run_metrics_csv.py: main` |
| `fs-diloco-validation-eval` | `tools/validation_eval.py: main` |
| `fs-diloco-publish-quality-gate` | `tools/publish_quality_gate.py: main` |
| `python -m fs_diloco.cli {syncer,learner,inspect}` | 便捷分发器 |
| `python main.py {syncer,learner,inspect}` | 根目录薄入口,直接调用同一 `fs_diloco.cli.main` |

## 5. 测试布局

`tests/` 当前所有测试文件都直接位于顶层,没有按主题建立子目录。覆盖原子 I/O、配置、参数索引往返、merge 选择/加权、外层优化器、存活管理、持久 SQLite/事务/1000-cycle 有界性、maintenance、DB-first resume、fixed proposal 面、分片全链路、syncer 选择、共享 SQLite probe、观测与离线工具等。

运行:`.venv/bin/python -m pytest tests/ -q`(需要 torch;部分测试用 `synthetic-tiny` 模型在 CPU 上跑)。

`scripts/` 不在 Python 包内,但 launcher 和诊断工具的准确行为也是运行契约的一部分;逐函数参考见 [modules/scripts.md](modules/scripts.md),PBS 作业差异与提交前检查见 [07-operations.md](07-operations.md)。
