# Plan 03 连续失败代码审查记录

初始创建时尚未触发同一实验连续三次失败升级。

## 2026-08-09 00:06 JST — `p0-static-gate-wrapper` 三连失败审查

- 范围：三次一次性静态验证wrapper失败；均发生在验证器读取目标输入阶段，而不是production/test behavior断言阶段。
- 复核：真实matrix文件为 `fsb_decoupled_diloco_plan_03_unified_ha-requirement-matrix.csv`，主键为 `invariant_id`；`_BOUND_MUTATORS` 的唯一生产定义在 `fs_diloco/storage/fenced_store.py`；disposition CSV主键为 `old_name`。
- 根因：验证器连续猜测了三个不存在的文件/字段名，没有先读取repository事实。
- 处置：第四次前用 `rg --files`、`rg -n`和CSV header完成全面定位；第四次仅比较已观察到的源码集合与CSV集合，不再扩展职责。
- 结果：42个生产bound mutator与42行disposition精确一一对应，disposition值域严格为 `keep|merge|delete`。没有production defect或artifact drift；无需改变计划语义。
