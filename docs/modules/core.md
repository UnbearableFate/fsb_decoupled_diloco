# 模块参考：`fs_diloco/core`

`core` 定义 wire-format 常量和强类型配置。字段默认值与完整语义见 [配置参考](../06-configuration.md)；本页说明解析路径和失败边界。

## `core/constants.py`

| 名称 | 当前值/行为 |
|---|---|
| `FORMAT_VERSION` | `1`；param/fragment index、latest、proposal、heartbeat、stop、summary 等主 runtime JSON 的格式版本；source identity、评估结果等工具产物有各自 schema。 |
| `PROTOCOL_VERSION` | `3`；写入 run identity，full resume 要求精确相等。 |
| `DEFAULT_RUNS_DIR` / `LEARNER_ID_PREFIX` | `runs/fs_diloco` / `learner_`。 |
| `UPDATE_STATUS_PENDING`, `UPDATE_STATUS_SELECTED`, `UPDATE_STATUS_APPLIED`, `UPDATE_STATUS_DROPPED`, `UPDATE_STATUS_FAILED` | `pending/selected/applied/dropped/failed`；`failed` 目前没有运行时转入路径。 |
| `LEARNER_STATUS_UNKNOWN`, `LEARNER_STATUS_ACTIVE`, `LEARNER_STATUS_STALE`, `LEARNER_STATUS_DEAD`, `LEARNER_STATUS_STOPPED` | `unknown/active/stale/dead/stopped`。 |
| `GLOBAL_STATUS_WRITING`, `GLOBAL_STATUS_COMMITTED`, `GLOBAL_STATUS_ABANDONED` | `writing/committed/abandoned`；当前正常版本行使用 `committed`。 |
| `GLOBAL_WEIGHT_TEMPLATE` / `OUTER_OPTIM_TEMPLATE` | 六位补零的 `global_v{version}.safetensors` / `outer_v{version}.safetensors`。 |

- `learner_id_from_index(index)` 用三位补零产生 ID；不拒绝负数或大于 999 的值。
- `learner_index_from_id(learner_id)` 只先校验 `learner_` 前缀，再把余串交给 `int()`；范围合法性由调用方根据 `num_learners` 判断。

## `core/config.py`

### 配置类型

`Config` 组合 `RunSection`、`InitSection`、`ModelSection`、`DataSection`、`SyncSection`（内嵌 `GraceWindowSection`）、`SyncerSection`、`LivenessSection`、`TrainingSection`、`InnerOptimizerSection`、`OuterOptimizerSection`、`IOSection`、`LearnerSection`（内嵌 `PredictionSection`）、`FragmentSection`、`FailureSimSection`、`WandbSection`。这些 dataclass 本身只提供默认值；跨字段约束在 `resolve_config()` 执行。

### 加载与转换

| 函数 | 精确行为 |
|---|---|
| `_coerce_scalar(value, target_type)` | YAML list 在目标为 tuple 时转 tuple；解开 `Optional[T]` 后递归处理；不会普遍执行 `int/float/bool` 强转。 |
| `_from_dict(cls, data, path=())` | 递归构造 dataclass。未知键立即报错；`REMOVED_CONFIG_KEYS` 中的键给出“已移除”及可用替代。缺失键保留 dataclass 默认。 |
| `config_to_dict(config)` | `dataclasses.asdict()` 深度导出，tuple 仍为 tuple，交给 YAML/JSON 调用方序列化。 |
| `load_config(path=None)` | 无路径或空 YAML 得到全默认 `Config`；非 mapping 顶层报错；普通用户配置不做旧键迁移。 |
| `load_resolved_config_snapshot(path)` | 专用于历史 resolved snapshot：删除三个无替代旧键，把旧平铺 prediction timeout 移到嵌套路径（新值已存在则保留新值），然后仍经 `_from_dict` 严格检查其余键。 |
| `_default_run_id(name)` | 本地时区 `YYYYmmdd_HHMMSS_<name>`，只有秒级粒度。 |
| `_environment_flag(name, default=False)` | 接受 `1/true/yes/on` 和 `0/false/no/off`（忽略大小写/两端空白）；其他非空值报错。 |
| `write_resolved_config(config, path)` | `yaml.safe_dump(sort_keys=False)` 后调用原子文本写；创建的是一个文件快照，run 初始化会分别写根目录和 control 副本。 |

### `resolve_config()` 顺序

1. `load_config()`；随后环境变量 `FS_DILOCO_GIT_COMMIT`、`FS_DILOCO_GIT_DIRTY`、`FS_DILOCO_SOURCE_FINGERPRINT` 覆盖 YAML identity。`FS_DILOCO_REQUIRE_SOURCE_IDENTITY=true` 时 commit 与 fingerprint 缺一即失败。
2. CLI/调用方覆盖 `run_id`；否则取 `$RUN_ID`，再否则生成时间戳 ID。覆盖 `shared_root`；null 时使用 `<project_root 或 cwd>/runs/fs_diloco/<run_id>`，非空路径只做字面 `{run_id}` 替换。
3. 依次应用 `num_learners`、training seed、scan/ingest、syncer device/publish dtype、staleness、adoption、terminal capture、completion、parallel write 和 materialize 覆盖。`num_learners` 只把两个 quorum 用 `min` 收紧，不建立下界或顺序。
4. 规范化 syncer device 与 compute/publish dtype；后者只接受 FP32/BF16 别名。执行 grace、completion、timeout、scheduler 和 fragment 组合校验。
5. 延迟导入 `runtime.adoption.validate_global_adoption_strategy()`，让选中的 replace/rebase/predict 类型校验自身前提；再校验 post-publish wait 非负、poll 正数。
6. 最后强制 `training.block_size = data.block_size` 并返回。

解析器没有统一校验所有数值范围。quorum 顺序、batch/step 正数、heartbeat 阈值顺序、failure probability、I/O dtype、selection policy 和 outer optimizer 等会在具体消费点才失败；不要把“未知键严格”误解为完整 schema validator。

### 已移除键

`sync.upload_mode`、`liveness.quorum_policy`、`inner_optimizer.reset_on_global_update` 无替代；`learner.prediction_reconcile_timeout_seconds` 的替代是 `learner.prediction.reconcile_timeout_seconds`。只有历史快照加载器迁移它们，普通配置始终拒绝。
