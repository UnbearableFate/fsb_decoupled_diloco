# 代码结构

## 主包

- `core/config.py`：model/data/training 等共享 dataclass；不包含 classic/fragment/旧 HA switch。
- `core/config_v4.py`：完整 v4 envelope、leader/maintenance、strict loader、resolver 和有限 v3→v4 config migration。
- `core/run_descriptor.py`：加载/验证 immutable descriptor、config/source identity 和 actor attestation。
- `protocol/`：typed value objects 与纯算法，不执行文件、SQLite、scheduler 或 process I/O。
- `storage/authority.py`：有限的 `LeaderAuthority`/`LeaderSession` commands 和 read model。
- `storage/admission.py`、`storage/control.py`：filesystem adapter；它们从 protocol 移入 storage 是因为需要 `Path`/I/O。
- `storage/run_initializer.py`：same-parent staging、identity reservation 和 `.complete` publication。
- `runtime/learner_entrypoint.py`：torch-free descriptor/admission gate。
- `runtime/learner_v4.py`：admitted learner 的训练、receipt/proposal、ack/adoption/terminal loop。
- `runtime/syncer_entrypoint.py`：candidate/lease/renewer composition。
- `runtime/syncer_v4.py`：CLI-independent composition/main loop；不内嵌 PBS、SQL 或第二套 merge/terminal 实现。
- `runtime/services/merge.py`：normal/terminal 共用的唯一 selection、tensor merge 和 publication path。
- `runtime/services/dynamic_capacity.py`：capacity window、durable launch reservation、qsub/qstat reconcile 和 exact replacement composition。
- `runtime/services/terminal.py`：close-policy evaluation、pre-close visibility、drain ack、bounded terminal merge 和 finalization。
- `legacy/`：query-only old-run reader/config projection/pure Fragment V0 decoder；runtime 禁止 import。
- `baselines/`：独立 torch protocol/health/artifact consumer。
- `tools/`：operator 与离线工作流；工具可显式使用 legacy reader。

## 公共入口

以下 shim 保持 `python -m` 兼容，但不保留旧 writer：

- `python -m fs_diloco.syncer`
- `python -m fs_diloco.learner`
- `python -m fs_diloco.analysis`
- `python -m fs_diloco.eval_lm_harness`
- `python -m fs_diloco.cli close --shared-root <run> --reason <text>`（仅 manual terminal policy）

`syncer`/`learner` shim 直接指向 mandatory v4 entrypoint，不按配置分派 classic 或 fragment。

## 依赖规则

- protocol → 仅标准纯数据/validation dependencies；无 `Path`、storage/runtime/PBS。
- storage → core/protocol + filesystem/SQLite adapter。
- runtime → core/protocol/storage/modeling/observability；无 legacy。
- baselines → core/modeling/baseline helpers；无 runtime learner。
- tools → 可组合 current storage 或显式 legacy query-only API。

`tests/architecture/` 和 `check_plan03.py --verify-p5-contracts` 对 removed surface、import boundary、DDL 和 entrypoint 做 fail-closed 扫描。

## 测试结构

- `tests/protocol/`：wire validation、accounting、selection 和 golden projection。
- `tests/storage/`：SQLite transaction、fence、publication、visibility、terminal、audit/GC、initializer。
- `tests/runtime/`：mandatory v4 admission/control/cutover regression。
- `tests/legacy/`：old full/Fragment V0 query-only fixtures。
- `tests/architecture/`：依赖和删除边界。
- 顶层 tests：modeling、tools、baseline 与 checker。

删除 classic/fragment 测试的逐函数 disposition 和 collection delta 位于 Plan03 report artifacts，并由 `scripts/miyabi/build_plan03_p5_test_accounting.py` 重建。
