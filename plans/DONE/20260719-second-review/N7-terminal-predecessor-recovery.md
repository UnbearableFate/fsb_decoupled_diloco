# N7：terminal predecessor 证据包的崩溃恢复与一致性

## 1. 目标与权威边界

`maybe_capture_terminal_predecessor_for_eval()` 当前在 hardlink 目标已存在但与 source
不同、manifest 尚未写入时，会把既有错误 checkpoint 直接登记为当前版本证据。

本计划不把 `eval_checkpoints/` 提升为训练权威。训练 source 仍是当前 SQLite/latest
指向的 committed global weight；evidence manifest 是证据包的唯一提交点：

```text
committed source weight（训练权威，只读）
    ↓ hardlink 或原子 copy
uncommitted evidence checkpoint（可校验/覆盖）
    ↓ checksum + identity 校验后 atomic manifest
committed evidence package（研究证据，不参与 resume/GC authority）
```

完成后，“checkpoint 写完、manifest 未写”窗口可安全重试：内容相同则复用，内容不同
则由 source 原子覆盖后再提交 manifest；已有 manifest 的冲突一律 fail closed，不能静默
改写已经提交的研究证据。

## 2. 恢复状态机

| manifest | checkpoint | source/checkpoint 关系 | 行为 |
| --- | --- | --- | --- |
| 无 | 无 | N/A | 尝试 hardlink；失败则原子 copy；校验后写 manifest |
| 无 | 有 | samefile 或 checksum 相同 | 复用 checkpoint；计算/校验 checksum；写 manifest |
| 无 | 有 | checksum 不同 | 原子 copy source 覆盖 checkpoint；重新校验；写 manifest |
| 有 | 有 | manifest identity、manifest checksum、checkpoint checksum、source checksum 全一致 | 幂等返回已有 manifest |
| 有 | 有 | 任一冲突 | fail closed，不覆盖 checkpoint/manifest |
| 有 | 无 | N/A | fail closed；已提交证据不完整，不能静默重建后假装未发生损坏 |

manifest identity 至少校验 schema version、source global version、source path、checkpoint
path 与 checksum。source checksum 在 capture 期间若发生变化，说明训练权威被非法原地
修改；必须失败，不能循环重试到某个随机版本。

为控制大模型额外 I/O，可先用 `samefile` 快路；需要比较内容时使用流式 SHA256，不把
checkpoint 整体读入内存。manifest 最终记录的 checksum 必须来自覆盖/复用完成后的
checkpoint，并与 source 相同。

## 3. 范围与非目标

范围内：`fs_diloco/runtime/syncer.py` capture helper、必要的 atomic I/O helper、
`tests/test_terminal_predecessor_capture.py` crash/retry 矩阵和 capture docs。

非目标：把 evidence 加入 DB/latest、让 runtime GC 管理该目录、增加自动保留策略、
回填既有 run、改变何时触发 terminal partial capture 或开展质量实验。

## 4. Requirement 与测试矩阵

| ID | 场景 | 通过条件 |
| --- | --- | --- |
| TCP-01 | 默认开关关闭或非 input-closed/non-partial | 不创建目录、checkpoint、manifest；现有行为保持 |
| TCP-02 | clean capture，hardlink 成功 | checkpoint 与 source samefile；manifest checksum/identity 正确；DB/latest 字节不变 |
| TCP-03 | hardlink 普通 OSError | 原子 copy；checksum 与 source 一致；manifest 最后发布 |
| TCP-04 | FileExistsError，无 manifest，既有 checkpoint 与 source 相同但非 samefile | 复用或安全重拷均可；manifest 正确；不得读取错误内容登记 |
| TCP-05 | FileExistsError，无 manifest，既有 checkpoint 与 source 不同 | 原子覆盖后 checksum 相同；manifest 指向新正确内容 |
| TCP-06 | 有 manifest + 正确 checkpoint，重复调用 | 幂等返回；文件内容和 manifest bytes 不变 |
| TCP-07 | 有 manifest，但 checkpoint 缺失/损坏，或 identity/source checksum 冲突 | fail closed；不覆盖任何已提交证据 |
| TCP-08 | 在 checkpoint 完成后、manifest 前注入崩溃，再次调用 | 按 TCP-04/05 恢复；最终只有一个正确 evidence package |
| TCP-09 | capture 全过程 | SQLite/latest/summary/训练 checkpoint 集合不变；evidence 不进入 resume/GC live set |

## 5. Loop Engineering 实施循环

| Loop | SPECIFY / RED | IMPLEMENT / GREEN | HARDEN / CHECK / PERSIST |
| --- | --- | --- | --- |
| L0 复现错误登记 | TCP-05：预置不同内容 checkpoint、无 manifest，调用当前 helper | 无实现 | 保存错误 manifest/checksum RED；冻结 manifest commit 语义 |
| L1 未提交 checkpoint 恢复 | TCP-03/04/05 | 提取原子 copy/校验路径；FileExistsError 后比较 samefile/checksum；冲突原子覆盖 | source/checkpoint digest 对账；DB/latest 不变 |
| L2 已提交证据 fail closed | TCP-06/07 | 对已有 manifest 做完整 identity/checksum 校验；冲突报错 | 幂等 bytes、损坏/缺失/错版本反例共同通过 |
| L3 crash matrix | TCP-08/09，failpoint 位于 link/copy 后和 manifest atomic replace 前 | 只修复确定性窗口，不增加重试循环 | 每个 failpoint 重复多次；无 tmp、无错误 manifest、训练权威不变 |
| L4 集成与文档 | TCP-01/02 + 全量 pytest | 无额外功能 | terminal partial tiny（开关开/关各一）或等价聚焦集成；记录 artifact |

## 6. 验证与性能边界

登录节点执行 INDEX G1。compute 节点运行 terminal capture 聚焦组和全量 pytest；该 helper
本身不要求 GPU。一次小型 terminal-partial pipeline 用于确认真实 source/latest/DB 未被
修改，不需要 2/9 节点。

测试必须校验临时文件集合为空，不能只校验最终 manifest。checksum 成本属于研究开关
开启后的低频 terminal 路径，不为此增加常规 syncer telemetry；若真实 GPT-2 capture
显示不可接受开销，只记录观察并另立优化计划，不弱化一致性检查。

## 7. 报告与文档

报告目录：
`reports/imp_plans/20260719-second-review/N7-terminal-predecessor-recovery/`。
TCP-08 artifact 记录 failpoint、source/checkpoint/manifest digest、重试次数、最终目录与
DB/latest 前后摘要。

同步 `docs/06-configuration.md` 与 `docs/modules/runtime-syncer.md`：说明 manifest 是研究
证据提交点、manifest 前 checkpoint 可恢复、manifest 后冲突 fail closed。具体 run 与
checksum 数字只写 reports。

## 8. 失败升级

同一 TCP failpoint 三连败后停止添加 catch-all exception 或直接 unlink。全面审查
source 生命周期、hardlink inode 语义、atomic copy/replace、manifest publication 和
重试幂等性；至少比较“原子覆盖现目标”和“版本化临时 checkpoint 后 rename”两种实现，
并以不改变训练权威链为首要约束。

