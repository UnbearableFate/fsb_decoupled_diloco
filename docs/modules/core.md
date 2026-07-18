# 模块参考:fs_diloco/core

配置与共享常量。常量是包内最底层；配置解析在完成通用校验后延迟调用 torch-free adoption 策略校验器。

## core/constants.py — 共享常量

| 常量 | 值 | 用途 |
|---|---|---|
| `FORMAT_VERSION` | `1` | 所有共享 JSON(latest/心跳/update 元数据/param index/fragment index)的协议版本;不匹配即拒收 |
| `PROTOCOL_VERSION` | `3` | 持久 DB identity 中的协议版本;v3 引入 fragment per-pair pointer/frontier，resume 必须精确匹配 |
| `DEFAULT_RUNS_DIR` | `runs/fs_diloco` | 缺省 shared_root 的父目录 |
| `LEARNER_ID_PREFIX` | `learner_` | learner id 前缀 |
| `UPDATE_STATUS_*` | `pending/selected/applied/dropped/failed` | update 状态机(`failed` 当前未使用) |
| `LEARNER_STATUS_*` | `unknown/active/stale/dead/stopped` | learner liveness 状态 |
| `GLOBAL_STATUS_*` | `writing/committed/abandoned` | 全局版本状态(当前只写入 `committed`) |
| `GLOBAL_WEIGHT_TEMPLATE` | `global_v{version:06d}.safetensors` | 全局权重文件名模板 |
| `OUTER_OPTIM_TEMPLATE` | `outer_v{version:06d}.safetensors` | 外层优化器状态文件名模板 |

函数:

- **`learner_id_from_index(index) -> str`** — `3 → "learner_003"`。
- **`learner_index_from_id(learner_id) -> int`** — 逆运算;前缀不符抛 `ValueError`。

## core/config.py — YAML 配置加载与解析

### 配置 dataclass

`Config` 由 15 个顶层节组成:`run / init / model / data / sync / syncer / liveness / training / inner_optimizer / outer_optimizer / io / learner / fragments / failure_sim / wandb`;`learner` 内含 `prediction` 子节(每节字段与语义见 [06-configuration.md](../06-configuration.md))。

### 函数

- **`_coerce_scalar(value, target_type)`**(私有)— 按类型注解做轻量转换:list→tuple(如 betas)、Optional 解包;其余原样返回。
- **`_from_dict(cls, data, path=())`**(私有)— 递归把 dict 构造成 dataclass;**遇到未知键抛 `ValueError`**(拼写保护)。已移除键给出完整 dotted path；旧 prediction timeout 额外指出新路径。
- **`config_to_dict(config) -> dict`** — `dataclasses.asdict` 全量导出(用于写快照、W&B config、SQLite run_state)。
- **`load_config(path=None) -> Config`** — 读 YAML(可为 None/空文件 → 全默认值);顶层必须是 mapping。
- **`_default_run_id(name)`**(私有)— `strftime("%Y%m%d_%H%M%S") + "_" + name`。
- **`resolve_config(path, *, run_id, shared_root, num_learners, project_root) -> Config`** — 加载 + 运行时补全:
  1. CLI 覆盖 run_id/shared_root;
  2. run_id 缺省:`$RUN_ID` 或时间戳；非空 shared_root 中的 `{run_id}` 替换为最终 run ID；仅当 shared_root 为空时才回退 `<project_root|cwd>/runs/fs_diloco/<run_id>`；
  3. `num_learners` 覆盖时把 `quorum_min/max` 收紧到不超过 learner 数;
  4. 通用 fragment/completion/grace/wait 合法性校验;
  5. 延迟调用 `validate_global_adoption_strategy`:replace no-op，rebase/predict 各自校验所需组合，prediction timeout 只对 predict 生效;
  6. `training.block_size = data.block_size`(数据侧为准)。
- **`write_resolved_config(config, path)`** — 原子写解析后配置的 YAML 快照(`control/run_config.resolved.yaml`)。
