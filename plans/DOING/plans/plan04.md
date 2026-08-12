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
- 每个 full_protocol 实验提交 8 个相互独立的 learner job，以及 1 个 syncer job；需要验证双 syncer 的实验可提交 2 个 syncer job。模型与数据统一使用固定版本的 GPT-2 和 WikiText-2。训练规模统一设为 `local steps 200 × global steps 25`，并将 syncer 的 merge 阈值设为 4 个 learner parameter。
- 使用tools/summarize_runs.py统计实验结果,追加到`runs/summary.csv`,并与baseline 结果比较。
- 在遇到运行错误,以及需要改进的地方, 自行对代码进行修改.
- PBS jobs 使用 `-q debug-g -l walltime=00:30:00`；提交前用 `qstat --rscuse` 核对 queue 容量。
- 每项实验只需提交 1 seed即可,无需重复实验
- 需要确保实验完成后,存活的 learner和 syncer 的作用域下,只保留最新的model weight, 因为死亡而无法清理model weight是在预料内的.
- syncer 达到预定的 25 steps 正常结束，并且最终 loss 较 baseline 没有出现异常升高则视为通过。

## 实验

0. **Baseline**：使用 `torch_ddp_baselines` 中准备的两种 baseline，提交 `5000 steps DDP` 训练和 `local steps 200 × global steps 25 periodic_average` 训练。两项 baseline 的 wall-time 均为 30 分钟，后续可根据实际运行证据调整。

1. **正常执行**：同时提交 8 个独立的 learner job 和 1 个 syncer job，按 `local steps 200 × global steps 25` 正常训练至结束。Wall-time 为 30 分钟，后续可根据实际运行证据调整。与 Baseline 比较，如果最终平均 loss、训练时间等关键指标相对 baseline 升高超过 30%，则需要寻找原因并解决。

2. **Learner 分批启动**：先提交 4 个 learner job，等待 30 秒后再提交其余 4 个 learner job。

3. **Learner 不均衡分批启动**：先提交 3 个 learner job，等待 30 秒后再提交 3 个 learner job；再等待 30 秒，提交最后 2 个 learner job。这个实验应该观察到,只有3 个 learner job时无法推进global steps.

4. **同时启动时 1 个 learner 掉线**：同时提交 8 个独立的 learner job 和 1 个 syncer job。启动 60 秒后，随机 `qdel` 1 个 learner job，观察 syncer 能否检测到故障。随后提交新的 learner job，并确认新追加的learner可以从它启动时最新的版本开始训练而不掉队。

5. **分批启动时 1 个 learner 掉线**：先提交 4 个 learner job，等待 30 秒后再提交其余 4 个 learner job。启动约 60 秒后，随机 `qdel` 1 个 learner job，观察 syncer 能否检测到故障。随后提交新的 learner job，并确认新追加的learner可以从它启动时最新的版本开始训练而不掉队。

6. **Syncer 掉线**：同时提交 8 个独立的 learner job 和 1 个 syncer job。启动约 60 秒后 `qdel` syncer job，等待 30 秒后重新 `qsub` 1 个 syncer job。观察新 syncer 能否接替原 syncer，并确认训练能够正常结束。

7. **双 Syncer 冲突**：同时提交 8 个独立的 learner job 和 1 个 syncer job。启动约 30 秒后，再 `qsub` 1 个 syncer job，观察系统能否正确处理双 syncer 冲突。在双 Syncer的状态产生后,等待 30 秒，`qdel` 第一个 syncer job，观察剩余 syncer 能否接替其职责。
