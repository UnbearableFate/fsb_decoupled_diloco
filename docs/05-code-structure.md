# 05 代码结构

## 1. 目录树与职责

```
fsb_decoupled_diloco/
├── fs_diloco/                     # Python 包(约 6300 行)
│   ├── core/                      # 最底层:配置与常量,不依赖其他子包
│   │   ├── config.py              #   YAML → dataclass 配置,resolve/校验/序列化
│   │   └── constants.py           #   格式版本、状态常量、文件名模板、learner id 编解码
│   ├── storage/                   # 文件系统与元数据持久化
│   │   ├── atomic_io.py           #   原子写(bytes/text/json/writer)、safe_read_json、sha256
│   │   ├── paths.py               #   RunPaths:共享目录布局的唯一定义;prepare_run_dirs
│   │   ├── tensor_codec.py        #   safetensors 存取:全局权重/外层状态/update 向量
│   │   ├── sqlite_store.py        #   持久 SQLite:update/learner/version/事务状态机
│   │   ├── schema.sql             #   数据库 schema(随包分发)
│   │   └── maintenance.py         #   crash-safe JSONL 归档、DB 剪枝、引用驱动 GC
│   ├── protocol/                  # 协议逻辑(纯函数为主,不做 I/O 或仅薄封装)
│   │   ├── merge.py               #   staleness、加权、每 learner 选一、加权平均
│   │   ├── liveness.py            #   心跳校验/摄取、active/stale/dead 分类、无进展超时
│   │   ├── fragment_index.py      #   分片索引构建(full/balanced_tensor)与严格校验
│   │   ├── fragment_codec.py      #   分片抽取/scatter/materialize、分片 safetensors 存取
│   │   └── fragment_scheduler.py  #   round-robin 调度与期望版本推算
│   ├── modeling/                  # 模型与训练组件
│   │   ├── hf_model.py            #   HF 模型/tokenizer 加载、TinyCausalLM 冒烟模型、选设备
│   │   ├── hf_data.py             #   WikiText 分片/切块/无限批迭代器、合成数据
│   │   ├── param_index.py         #   参数索引:模型 ↔ 扁平向量互转与兼容性校验
│   │   └── outer_optim.py         #   显式扁平向量外层优化器(sgd/momentum/nesterov/adamw)
│   ├── observability/             # 观测
│   │   ├── logging_utils.py       #   JsonlLogger、未捕获异常钩子
│   │   ├── metrics.py             #   CSV 追加与三张表的字段清单
│   │   └── wandb_logging.py       #   W&B 命名/标签/config/选中更新统计
│   ├── runtime/                   # 进程实现(组装以上全部)
│   │   ├── learner.py             #   learner 主循环(全量 + fragment 两套)
│   │   ├── syncer.py              #   syncer 主循环(全量 + fragment 两套)、初始化/恢复/发布
│   │   └── failure_sim.py         #   故障注入:随机睡眠/跳过上传/崩溃
│   ├── tools/                     # 离线工具
│   │   ├── analysis.py            #   run 摘要与断言(读共享目录 + 持久 DB/archive,不依赖 torch)
│   │   └── eval_lm_harness.py     #   checkpoint 解析/导出为 HF 目录/lm-eval 结果转 CSV
│   ├── cli.py                     # python -m fs_diloco.cli {syncer|learner|inspect}
│   └── {learner,syncer,analysis,eval_lm_harness}.py   # 兼容入口(转发到 runtime/tools)
├── configs/                       # YAML 配置(gpt2+wikitext2 全量/分片、tiny 冒烟)
├── scripts/
│   ├── miyabi/                    # PBS 批作业(1/2/9 节点)与检查脚本
│   └── local/                     # 本地 CPU 合成冒烟
├── tests/                         # pytest 单元/集成测试
└── docs/                          # 本文档
```

## 2. 分层与依赖方向

依赖严格单向,自下而上:

```
core  ←  storage  ←  protocol / modeling / observability  ←  runtime  ←  (入口 shim)
                                                          ←  tools
```

- `core` 不 import 包内任何东西(`config.py` 仅延迟 import `storage.atomic_io` 用于写快照);
- `protocol/merge.py`、`fragment_scheduler.py`、`outer_optim.py` 是**纯函数**模块,不碰文件系统,单测友好;
- 只有 `runtime/` 同时了解"共享文件系统协议"和"SQLite 状态机";
- `tools/analysis.py` 故意只用标准库 + 少量 protocol 纯函数,可在无 GPU/torch 环境运行(`eval_lm_harness.py` 的 torch 依赖也是函数内延迟 import)。

## 3. 两个运行时进程的内部结构

### `runtime/learner.py` 与 `runtime/adoption.py`

| 区块 | 函数 |
|---|---|
| CLI 与配置 | `parse_args`, `main` |
| 共享文件读写 | `write_heartbeat`, `wait_for_json`, `read_latest_if_newer`, `read_fragment_latest_if_newer`, `wait_for_fragment_latest_if_newer` |
| 停止判定 | `stop_requested`（full/fragment 共用） |
| 训练组件 | `build_inner_optimizer_and_scheduler`, `maybe_autocast`, `train_one_step` |
| 全局权重采纳 | `adopt_global`(全量)/ `load_fragment_latest_into_model`, `adopt_fragment_updates`(分片) |
| full 采纳策略 | `GlobalAdoptionStrategy` 及 replace/rebase/predict 实现、`make_global_adoption_strategy` |
| update 提交 | `write_update` / `write_fragment_update` |
| 主循环 | `run_learner`(全量)/ `run_fragment_learner`(分片);`run_learner` 按配置分派 |

### `runtime/syncer.py`(约 1340 行)

| 区块 | 函数 |
|---|---|
| CLI 与配置 | `parse_args`, `sqlite_path`, `main` |
| 发布 | `latest_payload`, `publish_global`(全量);`fragment_latest_payload`, `should_materialize_fragment_full`, `publish_fragment_latest`(分片);`publish_stop` |
| 初始化/恢复 | `initialize_run`, `initialize_fragment_run`, `resume_run`(DB-first) |
| 摄取 | `validate_update_metadata`, `ingest_update_metadata`, `sync_liveness_and_metadata` |
| 选择 | `collect_with_grace_window` / `collect_fragment_with_grace_window`, `drop_missing_update_files` / `drop_missing_fragment_update_files`, `finite_local_training_complete`, `select_terminal_drain_updates` |
| 观测 | `init_wandb_run`, `_fragment_staleness_stats`, `wait_for_learner_shutdown`, `write_training_summary` |
| 主循环 | `run_syncer`(全量,含分派)/ `run_fragment_syncer`(分片) |

## 4. 入口点一览

| 命令 | 实际实现 |
|---|---|
| `python -m fs_diloco.learner` | `runtime/learner.py: main` |
| `python -m fs_diloco.syncer` | `runtime/syncer.py: main` |
| `python -m fs_diloco.analysis` | `tools/analysis.py: main`(`summary` / `assert-fragment-smoke` / `assert-fragment-5000` 子命令) |
| `python -m fs_diloco.eval_lm_harness` | `tools/eval_lm_harness.py: main`(`resolve-checkpoint` / `export-checkpoint` / `results-to-csv`) |
| `python -m fs_diloco.cli {syncer,learner,inspect}` | 便捷分发器 |

## 5. 测试布局

`tests/` 顶层为聚焦单测:原子 I/O、配置、param index 往返、merge 选择/加权、外层优化器、liveness、持久 SQLite/事务/1000-cycle boundedness、maintenance、DB-first resume、fixed proposal surface、fragment 全家桶、syncer 选择逻辑、共享 SQLite probe 与 W&B 命名。子目录按协议/生命周期主题组织更多测试。

运行:`.venv/bin/python -m pytest tests/ -q`(需要 torch;部分测试用 `synthetic-tiny` 模型在 CPU 上跑)。
