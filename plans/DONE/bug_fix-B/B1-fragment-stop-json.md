# B1：fragment learner 忽略 stop.json（委托 S4 执行）

## 1. 元信息

- 来源：review B1（高，确认 bug）。`fragment_stop_requested`（learner.py:337-342）在 `local_or_global` + `max_local_steps` 已配置时从不检查 stop.json；syncer 达到 outer 目标发布 stop 后，fragment learner 继续空烧 GPU 直到本地步数上限。`configs/fs_diloco_gpt2_wikitext2_8l_fragment_5000steps.yaml` 命中。
- **执行载体**：本问题与 review S4（两个停止谓词应合并）根因相同。为避免同一改动存在两份权威规格（经验文档 §1.1 反对多事实源），实施规格、真值表、loop 循环与测试矩阵**全部以 [bug_fixing_plans/S4-stop-predicate-unification.md](../bug_fixing_plans/S4-stop-predicate-unification.md) 为唯一权威**。本文件只承载 B1 视角的验收追踪，不得在此重复或修改 S4 的规格。

## 2. B1 视角的完成谓词

S4 计划完成，且下列 B1 专属证据齐备：

1. S4 测试 **STP-03**（B1 回归：`local_or_global` + `max=5000` + `step=100` + stop.json 存在 → 停止）在修复前 RED、修复后 GREEN 的两份输出均已归档；
2. S4 测试 **STP-07**（tiny fragment 管线：stop 发布后 learner 于本地上限之前退出）通过，且证据中含 learner 退出时的 `local_step < max_local_steps` 数值；
3. `reports/imp_plans/bug_fix-B/B1/progress.md` 写入一条指向 S4 报告目录相应证据的记录（含修复 commit）。

## 3. 对照污染声明（P6）

修复改变 fragment 5000-step 类 run 的结束时间与完整训练时间统计。修复合入后，任何与历史 fragment run 的耗时/利用率对比必须注明本修复 commit；这一条同样写在 S4 计划 §3，此处重申以便从 B 编号侧检索。

## 4. 若 S4 计划被放弃

仅在 S4 被明确放弃（决定不合并两个谓词）时，本文件才升级为独立实施计划；届时最小修复为 review B1 建议的单行语义（`stop_json.exists() or (completion_mode != "global_only" and 已达 max_local_steps)`），并沿用 S4 的 STP-03/STP-07 作为测试。该分支发生时需先在本文件补全 loop 表与完成谓词，再动代码。
