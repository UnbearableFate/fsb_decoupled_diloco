# 运维

## 初始化与启动

优先使用 `python -m fs_diloco.tools.launch_independent_run`，它在任何 qsub 前验证两个 walltime，再创建 immutable run。也可以分步执行 `tools.init_run`，然后提交 `run_syncer_candidate.pbs` 与 static/dynamic learner 脚本。

正式环境必须提供与 descriptor 一致的 git commit、source fingerprint 和 resolved config。不要手工修改已发布的 `.identity`、`.complete`、descriptor、config、source manifest 或 bootstrap marker。

## Candidate 接管

candidate 异常后提交新的 `run_syncer_candidate.pbs`，使用同一 immutable descriptor config/root。不要删除 DB、WAL/SHM、epoch control 或 fixed cache来“帮助”恢复。successor 从 DB reconcile，并在新 epoch 重发 controls。系统不会自动 `qdel` 旧 job；若旧进程持续占 SQLite lock，由 operator 先核实 scheduler/host 状态。

## Learner 替换

static active attempt 只能通过 `tools.authorize_static_replacement` 发布精确 create-no-replace authorization。若 authorization 路径发生不同内容 collision，使用 fresh attempt ID；不得覆盖文件。

dynamic replacement 必须带 current instance ID、stream ID 和明确 launch request ID。旧进程恢复后可以非零退出，但它的 stale fence 不能提交新 receipt/proposal/version。

启用 scaling 时不要手工 qsub dynamic learner。leader 为连续低容量或有 terminal scheduler evidence 的 exact lost instance 创建 reservation，并把 stream/replacement identity 注入 PBS。只有同一 launch request 和 PBS job identity 的 registration 会被 admission；bootstrap array slot 不能复用。

## Scheduler 不确定

no-record/unknown 观察不会释放 anti-duplicate reservation。使用 `tools.resolve_scheduler_uncertainty` 生成 expected-state CAS operator request；leader 审计 applied 或 stale-rejected 结果。不要通过直接改 DB 或删除 launch record 解除状态。

## Terminal close

global-target/deadline/launch-budget policy 由 leader 自动评估。manual policy 使用：

```bash
python -m fs_diloco.cli close \
  --shared-root /path/to/run \
  --expected-descriptor-sha256 <sha256> \
  --reason "operator maintenance"
```

request 是 descriptor-bound immutable object；不同的第二次请求会 identity collision。close 后不再接收新 admission。只有显式开启 `allow_preclose_admission_during_drain` 时，leader 才在冻结前等待 registration visibility grace，并且只处理 close intent 前创建的 request。

## 状态检查

- `python -m fs_diloco.analysis <run-root>`：只读 summary。
- `scripts/miyabi/inspect_run.sh <run-root>`：文件/日志检查入口。
- `check_plan03.py`：阶段 contract/evidence gate。
- `PRAGMA integrity_check` 只能通过 read-only/checker 路径执行；learner 不应打开 DB。

fixed `latest.json`、`stop.json`、`summary.json` 可能落后或被修复。判断 leader、publication、membership、token 或 terminal 状态时以 SQLite authority 和 current epoch controls 为准。

## 清理

`python -m fs_diloco.tools.clean_run` 默认 dry-run，并要求 exact completed PASS evidence、descriptor/source/final version identity 和 artifact policy。`--delete` 还要求 report manifest。它会重新锚定 parent/inode 并拒绝 symlink、changed candidate、authority sidecar、live DB reference 或 unknown artifact class。

pre-policy legacy root 只有显式 `--allow-legacy-run-without-policy` 才使用保守 allowlist；这不会使旧 run 可恢复。不要用宽泛 shell glob 删除 run 内容。

## 分析、导出和评估旧 run

completed v1-v3/full 和 Fragment V0 可以用 current analysis/export/eval 工具读取。工具显式调用 legacy config projection 和 query-only SQLite reader；Fragment V0 评估需要已物化的完整 checkpoint。输出写到源 run root 之外或正式的 evaluation output 位置。

旧 in-progress root 不可 resume。需要继续实验时，从保留的模型 checkpoint 和明确的新语义配置创建 fresh v4 run/attempt。

## Miyabi/PBS

提交任何 PBS 脚本前：

1. 在安全静态环境运行 `bash -n scripts/miyabi/*.pbs`。
2. 确认每个 `#PBS -W group_list=` 是账户可用的字面 group ID。
3. 根据 workload/既有证据估算最短实用 walltime，至少 10 分钟并保留启动、运行和 orderly teardown 裕量；默认明显过长时用 `qsub -l walltime=...` 覆盖。
4. login node 只做静态检查；pytest、torch、CUDA、NCCL 和真实进程实验放到 compute allocation。

当前主要脚本包括 1/2/9-node full v4、独立 syncer/static/dynamic learner、torch baselines、validation/eval，以及 Plan03 phase gates。文件名含 fragment/no-fragment 的历史 PBS 已从主线删除。

## 常见失败

- admission 超时且无 torch import：检查 descriptor/config identity、current epoch heartbeat、request digest 和 stable contributor key。
- candidate 无法取得 lease：检查 current leader 是否仍 live、SQLite lock 和 wall-clock/monotonic evidence；不要绕过 fence。
- publication commit 失败：核对 intent、weight/optim size/SHA/theta identity 和 contributor selection fence。
- terminal 无法完成：检查冻结 contributor 的 receipt ack/final cycle；超时只能按配置记 hard-crash gap，不能伪造 graceful ack。
- legacy config 被 strict loader 拒绝：这是预期；仅在 query-only tool 中使用 legacy projection。
