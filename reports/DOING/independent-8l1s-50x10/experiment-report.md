# 8 learner + 1 syncer 独立 PBS job 实验报告

日期：2026-08-11（JST）
结论：**PASS**

## 目标与验收范围

在当前 clean source 上，以 1 个独立 syncer PBS job 和 8 个独立 scalar learner PBS jobs 执行正式 static Full Protocol 配置。每个 learner 每轮执行 50 个 local optimizer steps，syncer 提交 10 个 global versions。验收要求九个 actor job 使用九个不同节点、全部正常退出，并由 strict Checker 验证完整 authority、token ledger、publication、terminal、scheduler identity 和 source identity。

本实验直接证明当前实现中的 learner/syncer **可以处于不同的 scheduler jobs**。本次配置是 static membership，因此它不作为当前 commit 上 dynamic replacement/scale-out 的运行时证明；dynamic 能力边界见文末。

## 冻结身份

- Source commit：`7c9c273e575ec5f002387e744a944767cdad5fbe`
- Source fingerprint：`sha256:c418eee35551589af5988b686c32fbe1cc8017f9ab97ab707e944555a2c76512`
- Git dirty：`false`
- Run ID：`independent_8l1s_50x10_20260811_034000`
- Descriptor SHA-256：`7580f8c4e0608e35b7dfb578c6ca1e789badb5069563a62c72e0b83ec9801ecd`
- Resolved config SHA-256：`703e057807b8986c0940abb54fda88e50f278c0adf77e2346161e7ba79b6cee5`
- Source manifest SHA-256：`5e04bd569c51142c048410b5262691d869a50066058876516e7bdc2ccf040cb2`
- Protocol / authority schema：`4 / 11`
- Membership mode：`static`
- Workload：8 contributors × 50 local steps × 10 global steps；800 tokens/update；预期 direct tokens 64,000

## 源码验证

Compute validation job `2523223.opbs` 在 `mg0007` 上对上述 clean commit 返回 PASS：

- Ruff format：PASS
- Ruff lint：PASS
- Focused pytest：195 passed，0 failed/error/skipped
- Full pytest：553 passed，0 failed/error/skipped
- Validation artifact：`artifacts/20260811_independent_validation_7c9c273.json`

## 调度过程

第一次提交 `independent_8l1s_50x10_20260811_032500` 产生 jobs `2523101.opbs`–`2523109.opbs`。当时 regular/small queue 资源已经形成错开的预计启动时间，九个 actor 无法在 initial membership deadline 内共同启动。九个 jobs 均尚未运行即被显式 `qdel`；PBS history 记录为 `Not Running: Insufficient amount of resource`，没有 actor runtime 或实验结果。该次提交被保留为 scheduler diagnostic，不计为运行失败。

最终提交前的资源快照显示 debug 队列仅使用 3/48 nodes，而 regular 队列使用 937/1022 nodes。为保证 startup overlap，launcher control job `2523231.opbs` 和九个 actors 显式使用 `debug-g`；actor walltime 均为最短允许值 `00:10:00`。九个 actor 在 2026-08-11 03:42:17–18 JST 的一秒窗口内全部启动。

## 最终 actor topology

| Role | PBS job | Host | PBS state | Exit | Used walltime |
| --- | --- | --- | --- | --- | --- |
| syncer | `2523233.opbs` | `mg0006` | `F` | 0 | 00:00:21 |
| learner_000 | `2523234.opbs` | `mg0007` | `F` | 0 | 00:00:21 |
| learner_001 | `2523235.opbs` | `mg0008` | `F` | 0 | 00:00:21 |
| learner_002 | `2523236.opbs` | `mg0009` | `F` | 0 | 00:00:21 |
| learner_003 | `2523237.opbs` | `mg0010` | `F` | 0 | 00:00:20 |
| learner_004 | `2523238.opbs` | `mg0011` | `F` | 0 | 00:00:21 |
| learner_005 | `2523239.opbs` | `mg0012` | `F` | 0 | 00:00:21 |
| learner_006 | `2523240.opbs` | `mg0013` | `F` | 0 | 00:00:21 |
| learner_007 | `2523241.opbs` | `mg0014` | `F` | 0 | 00:00:20 |

Operator receipt、九个 immutable actor attestations、PBS running/final history 与 Checker 看到的 job/host mapping 完全一致。没有使用 PBS job array。

## Strict Checker 结果

Checker job `2523248.opbs` 在 `mg0006` 上退出 0，artifact `artifacts/20260811_034000_independent_8l1s_50x10_result.json` 返回 `status=PASS`、`errors=[]`。

- Global versions：连续 `0..10`，terminal final version 10，stop reason `configured_target`
- Applied proposals：80；每个 learner 精确 10 个
- Terminal overshoot：22 个 dropped proposals，17,600 direct tokens，全部有 durable adjudication
- Receipt/proposal：各 102 个；processed receipt tokens 81,600
- Direct applied tokens：64,000；direct outstanding/quarantined/unpublished：全部 0
- Token balance：0
- Publication objects：22 个，size 和 SHA-256 全部正确
- SQLite integrity：`ok`
- Syncer epochs：1，最终 `released`
- Static bindings：8 个 generation-1 terminal bindings
- Terminal fences：8 个，全部 `acked`，hard-crash gap upper bound 0
- Attestations：1 syncer + 8 learners
- Scheduler topology：精确 9 个 job IDs、9 个 hosts、queue `debug-g`；receipt mapping 全部一致
- Source identity：commit、fingerprint、clean state 全部精确匹配

## 证据位置与哈希

- Run root：`/work/xg24i002/x10041/fsb_decoupled_diloco/runs/full_protocol/independent_8l1s_50x10_20260811_034000`
- Log / receipt root：`/work/xg24i002/x10041/fsb_decoupled_diloco/logs/qsub_independent_8l1s_50x10_20260811_034000`
- Strict PASS artifact SHA-256：`4c84c53fb63f1536c0ae02bd91bb0490b5c24aeb345f9225832e70ba3eacdee4`
- Validation PASS artifact SHA-256：`acfc18a0765e0e3e4c6efca6e6849c5ff6657624a41df78d92a06c02fd5a97c9`
- Final qstat history SHA-256：`9d58c50988e1c3b2113f4bd462e1139ec3de22c093bc9f652a9338e073bd3285`
- Running qstat snapshot SHA-256：`1ade7e377e953cefe10b1c989d14fa832786f128e5536729f61a3319044d407a`
- Final submission receipt SHA-256：`a91d749a0f6e01da0d0e5167423f04e0ea5d2d05ec7cc31d1705827592b87c13`
- Run descriptor file SHA-256：`0ecbbe58a0d3a69c75d1a86589bcb2d437e0826a2a1562159b7ea7b41ec2eea1`

## 当前 dynamic 设计边界

当前代码同时保留唯一的 `dynamic` membership 设计：固定 virtual stream pool，物理 learner job 通过 bootstrap 或 leader-fenced launch request 动态 admission；syncer 的 capacity service 可在 contributor 低于目标或 actor 丢失时创建 replacement/scale-out PBS learner job，并持久化 launch reservation 与 scheduler reconciliation。syncer 候选自身也通过 SQLite lease 在独立 job 间 fail over。

“动态”指 **物理 learner instances/jobs 可加入、退出和替换**，其数量可在配置的 quorum/desired/pool/budget 边界内变化；它不表示运行中任意修改 immutable stream pool、配置或把 learner/syncer 互换角色。此前 plan-final G9 在 source commit `9b7e1dacdecbea8951121b3f70a6caece481a380` 上完成过 1 candidate + 8 learners + 1 bounded replacement 的 scheduler-backed dynamic PASS（artifact `reports/checked/fsb_decoupled_diloco_plan_03_unified_ha/artifacts/20260810-075600_p6-g9-plan-final-pass.json`）。当前 commit 的 553-test full suite 覆盖 dynamic admission/capacity/scheduler/replacement，但本报告的最终九节点运行只验收 static independent topology。
