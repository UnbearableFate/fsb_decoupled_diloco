# E5：syncer 节点成本入账与 CPU 化试验

## 1. 元信息

- 来源：review E5（低）。9 节点部署中 syncer 独占一个 GPU 节点，duty cycle 低（merge 计算 1–2s / ~20s interval）。124M 模型的 8 向量加权平均 + Nesterov 是内存带宽型计算，CPU 化或与 learner 同节点在当前规模下可行。00 计划 §4.7 讨论过多 syncer 容量，但"第 9 节点利用率"作为成本项从未进 run_analysis 时间账本。
- 性质：**计量 + 部署实验**。协议与代码预期零改动或极小改动（`syncer.device` 已可配，config.py:81）。
- 影响文件：run_analysis 模板、PBS 脚本（新增 8 节点变体）、可能的配置样例。
- 前置：无。

## 2. 目标与完成谓词

1. 成本入账：run_analysis 的时间/资源账本增加"syncer 节点 GPU 空置成本"条目（节点小时 × duty cycle 实测），基于既有 run 数据先补一行（SNC-01）；
2. CPU syncer 实测：1 节点上 `syncer.device=cpu` 的 merge+publish 耗时与 GPU 基线对比（tiny 与 124M 两个规模），数据入报告（SNC-02）；
3. 部署决策产出：判据冻结为 124M、8-vector 的 `read+aggregation+outer_step` CPU p95 < 既有 20 秒 interval 的 20%（4 秒），且共置后 3-seed 完整训练时间中位数不比同 fingerprint 9 节点基线劣化 >10%。满足则产出 8 节点共置 PBS 变体并冒烟/对照（SNC-03）；否则记录负结论，计划完成；
4. 决策与数据写入 run_analysis 与 00 §4.7。

## 3. 范围与非目标

- **范围内**：计量、CPU/共置试验、PBS 变体。
- **非目标**：多 syncer 容量设计（00 §4.7 的研究方向）；更大模型下的结论外推（判据按当前 124M，报告注明规模适用性）；syncer 计算本身的优化。

## 4. Loop Engineering 实施循环

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | HARDEN、CHECK、PERSIST |
| --- | --- | --- | --- |
| L0 账本 | 口径冻结：duty cycle 定义（merge+publish 活跃时间/墙钟） | 无代码 | 用既有 run 日志补一行账（SNC-01） |
| L1 CPU 实测 | 上述 4 秒判据冻结；发布 I/O 与计算分开，不把相同共享 FS 写入误算为 CPU merge | benchmark/现有 device=cpu 路径 | 1 节点 tiny + GPT-2 8-vector CPU/GPU p50/p95（SNC-02） |
| L2 部署变体 | 8 节点：rank0 同时运行 CPU syncer + GPU learner_000，其余七 rank 各一 learner；退出码/信号/日志双进程闭合 | PBS 变体脚本（`bash -n` + literal group） | 先短冒烟，再与 9 节点新基线做同 fingerprint/3-seed 对照（SNC-03） |

## 5. 测试矩阵与通过条件

| ID | 测试 | 通过条件 |
| --- | --- | --- |
| SNC-01 | 账本 | 账本含空置成本行，口径可复算 |
| SNC-02 | CPU 对比 | 两规模 merge+publish 耗时数据齐备；判据结论明确 |
| SNC-03 | 变体 | 短冒烟正确性全过；3-seed 3 条均成功，完整训练时间中位数劣化 ≤10%，否则明确否决默认部署 |

## 6. 验证阶梯

登录节点静态（PBS `bash -n`、group ID 字面值）→ 1 节点（SNC-02）→ 8 节点变体冒烟（SNC-03，属交付物）。

## 7. 报告与升级规则

报告目录 `reports/imp_plans/perf_fix-E/E5/`。负结论（CPU 化不达判据）同样是合格交付；不得为凑结论放宽判据（判据修改需记 failures.md）。

## 8. 文档同步

docs 部署章节补 8 节点变体与适用条件；00 §4.7 登记数据。
