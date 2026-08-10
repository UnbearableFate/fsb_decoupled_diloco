# 测试与证据

## 单节点测试

登录节点不运行项目测试。先申请一个 compute node，再依次运行 focused test、module suite、harness test 和 full suite。当前测试覆盖：

- strict config、Hub input identity、descriptor/source identity 和 staged initializer crash prefixes；
- proposal、receipt、cursor、selection、merge、token accounting 和 adoption；
- static/dynamic authority、leader fencing、publication reconcile、terminal、audit 和 cleanup；
- learner/syncer admission、dynamic capacity、PBS scheduler 和 operator tools；
- harness argument/PBS identity、structured failure、publication hashes 和 token-balance oracle。

单节点 evidence producer 按固定顺序运行 Ruff、focused pytest 和 full
pytest。两个 pytest gate 各自发布 create-only JUnit XML，并要求
`tests > 0`、`failures = errors = skipped = 0`；结构化 artifact 同时绑定
原始命令日志和两份 JUnit 文件，不能仅以 pytest 退出码作为 PASS。

## 4 learners + 1 syncer

`configs/full_protocol_functional.yaml` 固定 4 个 learner、full quorum、每轮 20 local optimizer steps 和 4 committed global steps。reviewed harness 使用 5 个独立节点验证三个场景：

1. normal：完整 initialization → admission → training → proposal → selection → merge → adoption → terminal。
2. learner replacement：`learner_000` 至少贡献一个 committed update 后终止进程；使用精确 old fence 发布 replacement authorization，并证明 binding generation 增长且 successor 继续贡献。
3. syncer takeover：primary 在 committed version 2 后、SQLite transaction 外且 lease renewer quiesced 的注册边界暂停，harness 强制终止该进程；successor 获得更高 epoch 并提交后续版本，旧 epoch 不得在 takeover 后提交。

`check_full_protocol_run.py` 不以 exit code 或 log 文本作为 PASS oracle。它读取 immutable descriptor/config、query-only SQLite、audit history、attestations 和 publication objects，验证 exact workload、5-host topology、contiguous versions、selection credit、token balance、terminal ack、object hash、replacement fence 或 successor epoch。

checker 只接受一个必填 `--fault-scenario`，取值为 `none`、
`learner_replacement` 或 `syncer_takeover`。binding generation、历史 attempt、
syncer epoch 和 fault evidence 全部由该场景唯一推导；normal 场景中出现任何
未注册 replacement/takeover 都会使 gate 失败。

## 8 learners + 1 syncer

正式实验使用 `configs/full_protocol_static.yaml` 和 `run_full_protocol.pbs`：9 个独立 `regular-g` nodes，8 learners，full quorum，每轮恰好 50 local optimizer steps，恰好 10 committed global steps。checker 还要求：

- 每个 learner 恰好获得 10 次 committed selection credit；
- direct applied tokens 等于 `10 × 8 × 50 × gradient_accumulation × micro_batch × block_size`；
- token ledger balance 为零且 outstanding 为零；
- versions `0..10` 连续、所有 checkpoint/outer-state size 和 SHA-256 匹配；
- terminal/controller finalized、四类 identity 一致、SQLite `integrity_check=ok`；
- source commit/fingerprint 与当前 clean target 完全一致。

正式验证结果在完成同一 target 的 9-node run 后写入本文件；若 README、docs 或任何 runtime source 随后改变，必须重新冻结 target 并重跑正式实验。
