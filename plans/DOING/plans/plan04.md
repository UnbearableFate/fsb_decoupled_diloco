# full_protocol 实验

## 参照

1. 测试和实验阶段使用 `$miyabi-development` skill。
2. 主要原则遵循 `AGENTS.md`。
3. 实施流程遵循 `plans/workflow.md`。

## 目的

设计并实施 full_protocol 实验，验证此前设计在正常训练、错峰启动和任务故障场景下的行为。

## 要求

- 自行添加/修改/拷贝实验专用代码、启动脚本、配置文件
- 将本plan中实施实验所需要的实验专用代码、启动脚本,配置文件等放在 `do_experiments/full_protocol/experiment{id}/` 下,将其作为独立的实验启动包.
- 启动脚本应支持通过一行 `bash ***.sh` 命令直接提交 job，便于人工复核和重复实验。
- 每个 full_protocol 实验提交 8 个相互独立的 learner job，以及 1 个 syncer job；需要验证双 syncer 的实验可提交 2 个 syncer job。训练规模统一设为 `local steps 100 × global steps 10`，并将 syncer 的 merge 阈值设为 4 个 learner parameter。
- 使用tools/summarize_runs.py统计实验结果,追加到`runs/summary.csv`,并与baseline 结果比较。
- 在遇到运行错误,以及需要改进的地方, 自行对代码进行修改.
- pbs jobs使用 -q regular-g -l walltime=00:30:00

## 实验

0. **Baseline**：已完成,位于`runs/full_protocol`, 统计数据位于`runs/summary.csv`

1. **正常执行**：同时提交 8 个独立的 learner job 和 1 个 syncer job，按 `local steps 100 × global steps 10` 正常训练至结束。与Baseline 比较, 如果关键指标,如最终平均loss,train time等相差超过20%,则需要寻找原因并解决.

2. **Learner 分批启动**：先提交 4 个 learner job，等待 30 秒后再提交其余 4 个 learner job。

3. **Learner 不均衡分批启动**：先提交 3 个 learner job，等待 30 秒后再提交 3 个 learner job；再等待 30 秒，提交最后 2 个 learner job。

4. **同时启动时 1 个 learner 掉线**：同时提交 8 个独立的 learner job 和 1 个 syncer job。启动约 60 秒后，随机 `qdel` 1 个 learner job，观察 syncer 能否检测到故障。随后提交新的 learner job，并确认训练能够正常结束。

5. **分批启动时 1 个 learner 掉线**：先提交 4 个 learner job，等待 30 秒后再提交其余 4 个 learner job。启动约 60 秒后，随机 `qdel` 1 个 learner job，观察 syncer 能否检测到故障。随后提交新的 learner job，并确认训练能够正常结束。

6. **Syncer 掉线**：同时提交 8 个独立的 learner job 和 1 个 syncer job。启动约 60 秒后 `qdel` syncer job，等待 20 秒后重新 `qsub` 1 个 syncer job。观察新 syncer 能否接替原 syncer，并确认训练能够正常结束。

7. **双 Syncer 冲突**：同时提交 8 个独立的 learner job 和 1 个 syncer job。启动约 60 秒后，再 `qsub` 1 个 syncer job，观察系统能否正确处理双 syncer 冲突。随后等待 60 秒，`qdel` 第一个 syncer job，观察剩余 syncer 能否接替其职责。