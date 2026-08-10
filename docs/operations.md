# Miyabi 运维

## 执行位置

Miyabi login node 只用于编辑、静态检查、`qsub`、`qstat` 和 `qdel`。Python import、pytest、Torch/CUDA、SQLite runtime 和训练只能在确认的 PBS compute allocation 中运行。所有 PBS 脚本使用 literal group `xg24i002`；提交前运行：

```bash
bash -n scripts/miyabi/*.pbs
bash -n scripts/miyabi/*.sh
```

walltime 必须根据当前 workload 和近期证据估算，至少十分钟。`launch_independent_run` 要求 syncer 和 learner walltime 都显式传入。

## 初始化与独立 actor

先预览，不提交：

```bash
python -m fs_diloco.tools.launch_independent_run \
  --config configs/full_protocol_static.yaml \
  --run-id RUN_ID \
  --shared-root /absolute/run/root \
  --project-root "$PWD"
```

确认输出后，加上：

```text
--submit --syncer-walltime HH:MM:SS --learner-walltime HH:MM:SS --log-root /absolute/log/root
```

该工具先创建 immutable run，然后提交一条 `run_syncer.pbs` 和每个 learner 各一条 scalar `run_learner.pbs`；static 模式不使用 PBS job array。部分 qsub 成功时不会隐式 qdel；返回的 accepted job IDs 是 operator receipt，必须先核对 scheduler 再决定后续操作。

Miyabi login node 不运行 Python。正式提交使用 `run_independent_launcher.pbs` 在短 compute allocation 中完成初始化和 actor qsub；actor 终态后再提交 `check_independent_run.pbs`。后者从九个 immutable actor attestations 验证九个不同的 scheduler job ID 和九个 host，不把 checker 自己的一节点 nodefile 误当成 actor topology。

## Static learner replacement

活跃 static attempt 不能被相同 learner ID 的新逻辑 launch 静默替换。操作者先从 authority 读取精确旧 fence，再执行：

```bash
python -m fs_diloco.tools.request_static_replacement \
  --shared-root RUN_ROOT \
  --run-id RUN_ID \
  --descriptor-sha256 SHA \
  --learner-id learner_000 \
  --old-logical-launch-id OLD_LOGICAL \
  --old-attempt-id OLD_ATTEMPT \
  --old-binding-generation GENERATION \
  --new-logical-launch-id NEW_LOGICAL \
  --new-attempt-id NEW_ATTEMPT \
  --reason REASON
```

随后用完全相同的 new identity 启动 learner。authorization 是一次性 immutable 文件；identity 不匹配会在 Torch import 前拒绝。

## Syncer takeover

提交同一 descriptor/root 的新 `run_syncer.pbs`。candidate 只在获得 leader lease 后运行；failed 或 expired predecessor 会被更高 epoch fence。不要删除 SQLite、WAL/SHM、epoch control 或 publication objects 来帮助恢复。

## 检查、手工关闭和 scheduler uncertainty

```bash
python -m fs_diloco.tools.analysis RUN_ROOT
python -m fs_diloco.tools.request_terminal_close --shared-root RUN_ROOT --reason REASON
python -m fs_diloco.tools.resolve_scheduler_uncertainty --help
```

analysis 汇总 immutable identity、integrity、controller、latest version、contributor fence/progress、token ledger、terminal state、syncer epochs、dynamic capacity 和 audit 状态。

## 安全清理

完成 checker 生成 PASS evidence 后先 dry-run：

```bash
python -m fs_diloco.tools.clean_run \
  --project-root "$PWD" \
  --run-root RUN_ROOT \
  --evidence EVIDENCE_JSON
```

确认 stdout 中的精确路径后，另加 `--execute --manifest REPORT_JSON`。工具拒绝 symlink run root、缺失 artifact policy、非 PASS evidence、authority live reference 和执行前发生变化的 inode/size/mtime。

正式 gate 的完整 run、raw log 和 Checker 所列 evidence paths 必须保留到 completed completion Checker 通过且 plan 已归档；staged PASS 或 final-evidence review 本身不授权提前清理。归档后也只能使用对应 gate 的精确 PASS artifact 执行上述清理流程。
