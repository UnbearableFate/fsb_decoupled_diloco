# S4：统一学习者停止谓词（含 B1 修复）

## 1. 元信息

- 来源：review S4（低）+ B1（高，确认 bug）。两者根因相同：`stop_requested`（learner.py:324-334）与 `fragment_stop_requested`（learner.py:337-342）分开维护，导致 fragment 路径在 `local_or_global` + `max_local_steps` 组合下从不检查 `stop.json`。
- 性质：**语义变更**（fragment 路径的 B1 行为被修正），其余路径行为保持。
- 影响文件：`fs_diloco/runtime/learner.py`、`tests/test_learner_completion.py`。
- 前置依赖：无。规模：最小（预计单文件 <40 行净改动）。

## 2. 目标与完成谓词

全部满足才可声明完成：

1. `fs_diloco/runtime/learner.py` 中停止判定只存在**一个**函数；`fragment_stop_requested` 标识符被删除（`grep -rn fragment_stop_requested fs_diloco/` 返回 0 行；tests 中仅允许出现在删除该别名的历史记录里）。
2. 测试矩阵 STP-01–STP-07 全部通过，其中 STP-03 为 B1 回归测试，必须先在未修改代码上 RED。
3. tiny fragment pipeline 冒烟：syncer 发布 stop 后，learner 在 `local_step < max_local_steps` 时退出，stop 原因事件可在日志中定位（STP-07）。
4. 全量 pytest 通过；full 模式 tiny run 事件轨迹与基线等价（该路径无语义变更）。

## 3. 范围与非目标

- **范围内**：统一谓词；B1 语义修正；补齐真值表全组合测试。
- **非目标**：
  - B7（`global_only` 下 syncer 死亡后 learner 无退出路径）——谓词真值表维持现语义（`global_only` 且无 stop.json → 继续），修复另行计划；
  - B4（fragment syncer terminal drain）——见 S3 计划的非目标声明；
  - `completion_mode` 语义本身的重新设计（review P1 的完成谓词配置化属于 imp_plans 层，不在 bug 修复范围）。
- **对照污染警告**（P6）：本修复改变 fragment 5000-step 类 run 的结束行为（`fs_diloco_gpt2_wikitext2_8l_fragment_5000steps.yaml` 正好命中 B1）。修复前后的 fragment run 不构成受控对照，run_analysis 引用时必须注明本修复的 commit。

## 4. 规格：统一停止真值表

唯一权威判定函数（签名沿用 `stop_requested(paths, local_step, config)`），full 与 fragment 两个主循环调用同一函数：

| completion_mode | stop.json 存在 | max_local_steps 已配置且 local_step ≥ 上限 | 判定 |
| --- | --- | --- | --- |
| local_or_global | 是 | 任意 | 停止 |
| local_or_global | 否 | 是 | 停止 |
| local_or_global | 否 | 否（或未配置） | 继续 |
| global_only | 是 | 任意 | 停止 |
| global_only | 否 | 任意 | 继续 |

现 `stop_requested` 已实现该表；现 `fragment_stop_requested` 违反第 1 行（`local_or_global` + `max_local_steps` 已配置时跳过 stop.json 检查）。即本计划的实现方向是**保留 full 语义、删除 fragment 变体**，而不是折中两者。

## 5. Loop Engineering 实施循环

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | HARDEN、CHECK、PERSIST |
| --- | --- | --- | --- |
| L1 真值表测试先行 | 按 §4 真值表为两个现存函数写全组合参数化测试；STP-03（B1 场景）对 `fragment_stop_requested` 断言"应停止"，在当前代码上确认 RED | 不改实现，只提交测试与 RED 证据 | RED 输出存入 artifacts；failures.md 不记（预期失败）；progress.md 记录 STP-01–06 中现 PASS/RED 分布 |
| L2 谓词合并 | — | 删除 `fragment_stop_requested`，fragment 主循环全部调用点改为 `stop_requested` | STP-01–STP-06 全 GREEN；`grep` 静态检查通过；全量 pytest |
| L3 管线级验证 | 设计 STP-07 场景：tiny fragment 配置 + 较大 `max_local_steps`，令 syncer 的 global 目标先达成 | 如现有 tiny fragment 配置不满足场景，新增一份最小配置变体 | 在 compute 节点跑通 STP-07；核对 learner 日志 stop 事件、`control/summary.json` 与最终心跳；证据入 artifacts |

## 6. 测试矩阵与通过条件

| ID | 测试 | 通过条件 |
| --- | --- | --- |
| STP-01 | local_or_global，未配置 max_local_steps，stop.json 存在 | 停止 |
| STP-02 | local_or_global，max=8：step 7 / step 8 | 7 继续，8 停止 |
| STP-03 | **B1 回归**：local_or_global，max=5000，step=100，stop.json 存在 | 停止（修复前 fragment 路径 RED） |
| STP-04 | global_only，step ≥ max，无 stop.json | 继续 |
| STP-05 | global_only，stop.json 存在 | 停止 |
| STP-06 | 静态检查 | `fragment_stop_requested` 在 `fs_diloco/` 中出现 0 次；fragment 主循环调用统一函数 |
| STP-07 | tiny fragment 管线 | syncer 发布 stop 后 learner 于 local 上限之前退出；stop 原因、summary、最终心跳一致 |

progress.md 每条记录必须列出本轮覆盖的 STP ID（P8）。

## 7. 验证阶梯

1. **登录节点**：`git diff --check`、lint、STP-06 的 grep 静态检查。不运行 pytest。
2. **1 节点 compute**：`pytest tests/test_learner_completion.py` → 全量 pytest → STP-07 管线冒烟（`scripts/local/run_tiny_2proc_smoke.sh` 的 fragment 配置变体）。
3. 不需要 2 节点与 9 节点。下一次 9 节点 fragment 实验运行在修复后 commit 上时，在 run_analysis 中注明（见 §3 对照污染警告）。

## 8. 报告、证据与 Checker

- 报告目录：`reports/imp_plans/bug_fixing/S4/`，文件与追加规则按 [plans/AGENTS.md](../AGENTS.md)。
- STP-07 的证据必须同时包含：learner/syncer 日志路径、`stop.json` 内容、summary 摘要、退出时 local_step 值——不得只看进程退出码（经验文档 §5）。
- 无需独立 Checker；以 STP 矩阵 + 全量 pytest 为验收。

## 9. 停止与升级规则

同一测试（以 STP ID 计）连续失败三次后停止局部试错，按 AGENTS.md 升级 code_review.md。修改超时、日志级别不算新实验。

## 10. 文档同步

- 若 `docs/` 中存在描述 completion/stop 语义的段落，更新为统一真值表；
- run_analysis 的 B1 相关"仍需进一步研究"条目在完成后标注修复 commit（写入 reports，不改写历史结论）。
