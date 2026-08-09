# 模块参考：protocol

该包只包含纯对象和算法，不执行 Path/filesystem、SQLite、runtime 或 PBS I/O。

- `proposal.py`：`FullUpdateProposalV2` strict JSON round-trip、path/size/digest/cursor/fence validation。
- `cycle_receipt.py`：`CycleReceiptV1`、canonical receipt ID/path、hash chain 和 token/cursor invariants。
- `contributor.py`：static/dynamic fence 与 membership scope；stable contributor key 在两种 topology 下统一选择语义。
- `data_cursor.py`：`ContributorResumeState` 和 deterministic indexed block cursor。
- `token_accounting.py`：`TrainingSegmentAccumulator`、segment snapshot 和 cycle accounting。
- `selection.py`：persistent fair selector；committed service count、last served version、stable key 构成 deterministic order。
- `merge.py`：typed staleness/token weight 与 stable reduction order。
- `authority.py`：command/read result value objects，如 publication intent、selection batch、terminal/token summary、typed conflict。
- `scheduler.py`：operator action/request 与 expected-state digest。

filesystem admission/control adapters 位于 `storage/`；这不是 protocol 层的例外。
