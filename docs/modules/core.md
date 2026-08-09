# 模块参考：core

## `config.py`

定义 model、data、training、optimizer、sync、membership、scaling、terminal、liveness、I/O、learner、W&B 和 torch baseline 的共享 dataclass。`_from_dict` 拒绝 unknown/removed key，`ConfigSection.validate` 拒绝 bool 冒充整数和非有限数。

`load_config` 只返回共享 projection：完整 runtime config 会先经过 strict v4 envelope；partial mapping 只用于共享 modeling/baseline 测试。`resolve_config` 处理 run path/env override 和共享语义，不负责 envelope-only direct token target。

## `config_v4.py`

`ConfigV4` 包含 shared config、schema version、`LeaderSection`、`MaintenanceSection` 和 `stop_after_direct_weight_tokens_applied`。`load_config_v4` 拒绝 old init/fragment/failure/HA/recovery/token key，并要求 Hub-backed Full-v4 model、有效 tokenizer 与 dataset revision 是 immutable 40 位小写 commit SHA；synthetic/显式本地输入和 Torch baseline 有独立边界。`resolve_config_v4` 产生 fresh run identity。`migrate_v3_bytes_to_v4` 只做有限、可证明的 config 迁移，不替操作者猜 Hub revision。

## `run_descriptor.py`

descriptor 冻结 run ID/mode、source/config SHA、static learner set 或 dynamic stream pool。`load_run_descriptor` 验证 `.complete`、identity reservation、immutable object hashes 和 expected source identity，然后以 strict v4 loader读取 config。actor attestation 使用每 kind/actor/attempt 的不可变路径。

## `versions.py` / `constants.py`

每种 artifact 使用自己的 version 常量；proposal/receipt wire version 与 Full Protocol v4 名称分开。不要重新引入一个模糊的全局 `FORMAT_VERSION`。
