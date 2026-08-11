# plan04 执行包

## 当前状态与边界

- Plan：`plans/DOING/plans/plan04.md`
- Branch：`plan04`
- Branch point：`2433f59a5109675d79423e6c2ddb71b72bf5be74`
- Workflow pin：`7288dc8086a95d2294a92fb999e8539991d86ec1`
- 已有实现包含 dynamic admission、固定 stream pool、quorum merge、PBS 终态确认后的 learner replacement、leader lease 与 syncer takeover。
- 本 plan 新增 Dynamic Full 唯一正式配置、场景调度器、结果汇总与证据检查；若实验暴露产品缺陷，则直接修正当前实现。
- 不保留只服务旧实验的接口、配置或兼容路径。已有 run 仅作回归参考，不作为本 plan 的正式验收证据。

## Formal source scopes

`fs_diloco` 运行身份将覆盖：

```text
fs_diloco
configs
do_experiments
scripts/miyabi
tests
tools
torch_ddp_baselines
pyproject.toml
README.md
docs
```

Baseline 依据同一 clean commit，并使用 `torch_ddp_baselines` 自身的不可变 run manifest。所有最终场景必须绑定同一 `FINAL_COMMON_TARGET`。

## 测试阶梯与资源预算

1. Login node：CodeGraph 检查、`bash -n`、PBS group literal 检查、Git/source-scope 检查。
2. 单节点 `debug-g`：focused tests、summary/harness tests、配置解析和 tiny dry-run，最短 walltime `00:10:00`。
3. 单节点 `debug-g`：完整相关测试与 Full Protocol 短路径回归，walltime 依实际耗时留出收尾余量。
4. PREFORMAL：创建 clean candidate commit，完成 current-state 完整审查，修正 blocking finding 后冻结 `FINAL_COMMON_TARGET`。
5. FORMAL baseline：2 个 8-node `debug-g` job，每个 rank 执行 2,000 optimizer steps；Periodic Average 每 200 steps 同步一次，共 10 次。单 job 预算 `00:15:00`。
6. FORMAL Dynamic Full：normal 单独执行，避免其他 workload 污染正式时延；normal 通过后，六个只验证正确性的场景按两场景一组并行执行。每个场景使用独立 run root、8 个初始 learner、1–2 个 syncer 和 1 个 supervisor；掉线场景允许 production capacity service 另提交 1 个 replacement learner。基于 normal 实测吞吐，actor 预算 `00:25:00`，supervisor 预算 `00:30:00`。

## 高风险边界与 oracle

- 统一 workload：GPT-2/WikiText-2 固定 Hub revision，8 streams，`inner_steps=200`，`stop_after_outer_steps=10`，每次 merge 精确选择 4 个 contributor。
- Learner fault：只对 authority 中已 admitted 的 bootstrap job 执行 `qdel`；必须观察到 scheduler FINISH、精确 replacement launch request、同 stream 更高 epoch 以及旧 fence 无越界 effect。
- Syncer fault/conflict：以 `syncer_epochs`、lease 与 terminal authority 为 oracle；不以单一进程退出码代替 takeover 证据。双 syncer 候选等待上界必须覆盖 60 秒并行观察窗口和旧 lease 失效时间。
- 性能比较同时保留 protocol/workload 差异；超过 20% 时必须归因，不得仅通过放宽门槛处理。
- 每个场景的 supervisor 拥有其提交的 job ID 集合，失败时执行 best-effort exact-ID cleanup；最终完成前确认无 active/queued job。
- 按用户本次指示跳过 multi-agent review；PREFORMAL 与 FINAL 均由当前 Codex coordinator 独立完成并保存结论。
