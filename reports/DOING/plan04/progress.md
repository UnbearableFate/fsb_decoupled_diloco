# plan04 进度

## 2026-08-12 14:40 JST — INIT 盘点

- Source：branch `new_plan04`，branch point 与 workflow pin 均为 `f2ec3e886ce77b93497ab6cd3e306e5de13ef6a4`；启动时 source scopes 与 worktree clean。
- Host 为无 PBS allocation 的 `miyabi-g3` login node；`qstat` 确认项目无 active/queued job，`regular-g` 可提交，literal group 为 `xg24i002`。
- 当前产品已有独立 actor、replacement、leader takeover、terminal checker 和统一 summary；缺失当前 plan 指定的 100×10 七场景实验包与正式证据。
- `runs/full_protocol/` 当前为空，`runs/summary.csv` 仅保留两行 2,000-step torch baseline。已将该证据缺口写入执行边界，后续不把不存在的 run 推断为完成。
- 下一步：按当前 plan 重写 obsolete plan04 harness，建立唯一七场景 supervisor/配置/一行入口并完成静态及 compute-node 验证。

