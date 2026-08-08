# Plan 03 `P0-freeze-oracles` 第二轮修复增量代码审查（gpt-5.6-sol）

- Base commit：`0993737978da3c52990734cb6eef1aee84172d1f`
- Target commit：`1024cf53df603c0468b36e05a44f007eec0865a6`
- 审查范围：完整 `git diff 0993737978da3c52990734cb6eef1aee84172d1f 1024cf53df603c0468b36e05a44f007eec0865a6`
- Target ancestry：base是target直接祖先
- Reviewer约束：只读审查target；本报告是审查后唯一新增文件。在调用或读取fresh Claude reviewer前已保存。

## 结论先行

未发现阻止P0收口的Critical、High或Medium finding。上一轮Codex的1项Medium/1项Low和Claude的1项High/5项Medium均有实质修复、测试与compute证据；关键filesystem publication契约的修订没有再依赖瞬态进程布尔作持久所有权证明。

**Verdict：APPROVE_WITH_FOLLOWUPS**

## 审查覆盖

- 逐行审查checker、filesystem capability/fallback、paired performance runner、两个PBS脚本和7个对应测试文件。
- 核对计划INIT-01/G10、97行requirement matrix、scripts文档以及progress/failures/code-review disposition。
- 核对第二轮Codex/Claude报告与invocation metadata；核对attempt 1/2失败日志、attempt 3成功日志、current-boundary/FS/performance/RED JSON/log及其matrix绑定。
- 复核target内phase-final静态门禁：`--expect ... --verify-boundaries --require-tracked-evidence`为PASS，frozen/current/tracked-evidence三组differences均为空。
- 复核compute证据：job `2508527.opbs` focused `30 passed, 5 xfailed`、RED `5 failed`、full `556 passed, 5 xfailed`；job `2508517.opbs` paired feasibility完成且artifact标明非正式G10。

## 上轮finding处置

### Codex

1. checker diagnostics：**fixed**。PBS传`--inventory-output`；payload区分frozen snapshot、current migration boundary和phase-final tracked evidence，并保存具体differences。
2. oracle negative test：**fixed**。主oracle与negative test共用`_assert_fixture_matches`；negative从真实SQLite/filesystem projection篡改theta，不再是fixture字典自比较。

### Claude High

H-1：**fixed**。

- 新建reservation后遇既有final时，在失败前验证并删除本次创建的same-inode reservation并fsync parent；第二次retry仍走同一fail-closed路径，不能利用残留reservation接管目录。
- marker前恢复权由staging identity与sibling reservation的`(st_dev, st_ino)`一致性持久证明；内容相同但inode不同的staging在final identity出现前不能mkdir。
- same-staging peer先mkdir时，调用方只在final identity与reservation为同一inode且digest匹配时收敛；否则超时清理自己新建的reservation并失败。
- reader要求final精确包含协议条目；foreign entry使其不可见。probe覆盖preexisting final连续retry、reservation无泄漏、different-staging抢占、same-staging mkdir交错与foreign entry。

### Claude Medium

1. 当前树门禁：**fixed**。`verify_boundaries`对当前tracked worktree比较fragment/baseline/historical/mutator列表、计数和边界文件hash；真实临时clone新增tracked fragment config会BLOCKED。
2. reservation生命周期：**fixed**。计划定义reservation与run同生命周期、generic cleanup不可删；completed run只有在不依赖sibling完成精确条目/identity/manifest/object hash全量校验后，才可显式从`final/.identity` same-inode repair。
3. evidence目录/命名：**fixed**。probe scratch使用实际`runs/` parent；输出目录独立；文件名在payload状态已知后决定`pass|fail`；PBS检查证据目录无probe scratch残骸。
4. 旧FS artifact冲突：**fixed**。被推翻的`20260808-225600`不再由P0-FS-CAP matrix绑定；authoritative evidence只有`20260809-003337`。
5. source binding：**fixed**。FS artifact包含timezone-aware `recorded_at`以及commit/dirty/fingerprint。

## 其余正确性观察

- RED summary的正则对数字边界做精确限制，不会把15/25 failed误判为5。
- performance clock通过参数注入，未修改stdlib全局module object；主elapsed在全部process wait完成后、读取log前截断。classic/HA共用fresh-root timer起点，timeout明确是共同end-to-end预算。
- 正式G10固定20 pairs并平衡AB/BA首臂，不再允许以不平衡的5-pair中间结果作正式判定；历史P0 5-pair artifact明确不是formal gate。
- tracked-evidence检查区分单文件与目录prefix，并用隔离Git fixture覆盖tracked/untracked行为；precommit可发现性与post-commit tracked gate不再循环依赖。

## Low follow-ups（不阻塞P0）

1. `plan03_fs_capability.main()`只有probe正常返回`status=FAIL`时才写`_fail.json`；多数原语失败会直接抛异常，只留下PBS raw log。当前证据规则由失败日志满足且不会产生伪`_pass`文件，但P3把该逻辑生产化前宜捕获异常并输出带source identity的结构化fail artifact。
2. same-staging并发probe用`before_final_mkdir`精确重放关键持久状态交错，不是两个OS进程的压力测试。该状态机证明足以冻结P0契约；P3 INIT-01仍应增加真实多进程/跨节点重复并发测试。
3. current-boundary review artifact把完整frozen inventory作为顶层payload，current侧仅保存commit和differences；PASS可判定但不直接携带current boundary snapshot。P5删除迁移面前可把current boundary摘要也写入artifact，提升离线可审计性。

这些follow-up均未削弱当前P0 gate：失败不会被命名为pass，关键并发交错已枚举，当前边界由commit和零differences可重建。
