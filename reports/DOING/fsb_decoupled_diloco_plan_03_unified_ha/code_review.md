# Plan 03 连续失败代码审查记录

初始创建时尚未触发同一实验连续三次失败升级。

## 2026-08-09 00:06 JST — `p0-static-gate-wrapper` 三连失败审查

- 范围：三次一次性静态验证wrapper失败；均发生在验证器读取目标输入阶段，而不是production/test behavior断言阶段。
- 复核：真实matrix文件为 `fsb_decoupled_diloco_plan_03_unified_ha-requirement-matrix.csv`，主键为 `invariant_id`；`_BOUND_MUTATORS` 的唯一生产定义在 `fs_diloco/storage/fenced_store.py`；disposition CSV主键为 `old_name`。
- 根因：验证器连续猜测了三个不存在的文件/字段名，没有先读取repository事实。
- 处置：第四次前用 `rg --files`、`rg -n`和CSV header完成全面定位；第四次仅比较已观察到的源码集合与CSV集合，不再扩展职责。
- 结果：42个生产bound mutator与42行disposition精确一一对应，disposition值域严格为 `keep|merge|delete`。没有production defect或artifact drift；无需改变计划语义。

## 2026-08-08 23:37 JST — `p0-phase-review-remediation-validation` 三连失败全面审查

### 共同模式、差异与证据

- 三次都在同一10分钟P0 remediation batch、同一checker/FS/focused/full顺序中发生，且都在full suite前fail-fast；每次完整log和job ID已进入`failures.md`。
- attempt1/2是shared-FS probe的fault-state分类/fixture错误：第一次把marker linearization之后当成不可见prefix，第二次用identity B配identity A manifest而未到达final collision。attempt3证明修订后的正式shared-FS probe已PASS，说明publication protocol本身没有形成三连代码缺陷。
- attempt3的两个失败是新review约束的中间态建模错误：precommit evidence还未tracked，以及H-01a目标缺陷的异常类型本来就是RuntimeError。共同模式是测试先猜“失败表现/生命周期阶段”，没有把被测状态机的准确边界编码进fixture。

### 完整数据流与控制流

1. **Evidence Git生命周期**：plan/matrix给出artifact contract → 实施者在`reports/DOING/<plan>/artifacts/`生成新文件 → `.gitignore`必须允许普通`git add`发现 → precommit测试核对存在、命名和非ignored → review-fix commit冻结后，phase-final静态门禁再用`git ls-files`核对target内tracked。当前test把最后一步提前到生成和commit之间，造成循环依赖；artifact内容本身已可解析且matrix路径正确。
2. **H-01a proposal/membership路径**：两个admitted instance写proposal metadata → heartbeat只保持current instance存活 → fenced SQLite transaction撤销stale instance → legacy `eligible_updates`仍返回stale row → `mark_updates_selected`逐row在`BEGIN IMMEDIATE`事务内检查current placement/stream。第一行stale导致精确RuntimeError并rollback整个selection，current row也未选；因此accepted defect的直接输出是selection abort，测试末尾的`status=dropped/selected`只在未来修复后可达。
3. **SQLite/恢复不变量**：selection transaction rollback没有partial selected row；测试必须保留这一事实，不能catch任意RuntimeError后继续伪造state。未来P2修复要在同一fenced command中terminalize invalid rows并选择valid rows；P0仅字符化旧行为，不修改DB。
4. **Filesystem publication路径**：staging identity/objects/manifest均fsync → sibling reservation hard-link+parent fsync → final mkdir+parent fsync → identity/objects按hash hard-link且逐目录fsync → `.complete` hard-link成为visibility linearization point → final/parent durability fsync。marker前13个prefix不可见且可retry；marker后两个prefix已visible、same-identity retry补durability；different identity在parent reservation碰撞。attempt3 artifact已逐项证明。
5. **进程生命周期/输出**：PBS脚本checker和FS probe先执行，focused suite后才保存RED runxfail log，最后full suite。任一前置失败使后续证据不存在是预期fail-fast；本轮无live run、checkpoint或GC引用，FS temp由`finally`清理，performance scratch另一个job已清理。

### 测试假设审查与反例

- `git ls-files`只观察index/HEAD，不包含合法未tracked的新artifact；反例正是本轮remediation evidence。候选方案A是在pytest前`git add`全部artifact，但这让test偷偷依赖/修改index且会把未审查内容纳入scope，拒绝。方案B是precommit用`git ls-files --cached --others --exclude-standard`证明文件可被普通add发现，commit后独立静态门禁证明tracked；采用B。
- 给全部strict xfail统一`raises=AssertionError`能排除早期TypeError/AttributeError，却错误排除H-01a的目标RuntimeError。候选方案A改成`raises=RuntimeError`，但任何fixture/runtime RuntimeError仍可伪xfail。方案B只捕获精确selection-fence message，把该已确认目标行为转换成带finding ID的AssertionError；message不匹配则re-raise为normal failure；采用B。其他四项目标仍是AssertionError。
- FS post-marker可见不是错误；linearization与durability必须分组。正式probe现在对pre/post组分别断言且unit test绑定完整step列表，不再靠固定prefix数量猜测。

### 修订实施逻辑、影响范围与新RED

- 只修改`tests/test_plan03_checker.py`的evidence discoverability查询；phase commit后另跑一个不修改index的tracked-evidence静态命令。
- 只修改H-01a RED的异常适配：精确match `dynamic update is not pending/current at selection: stale-before-select` 后执行命名AssertionError；任何API签名、DB、其他RuntimeError漂移都会使focused suite真失败。
- 不修改production protocol/SQLite schema。FS修订保持parent reservation/hashed manifest契约，performance job `2508335.opbs`已独立terminal PASS。

### 第四次明确通过条件

- checker和formal shared-FS probe PASS；focused suite中review-support/oracle普通passing、5项RED全部且仅目标xfail；`--runxfail`精确5 failed并保存原始log；full suite零unexpected failure。
- 新artifact在precommit查询中可见且不命中ignore；review-fix commit创建后，matrix全部nonpending evidence由`git ls-files`证明tracked。
- 若第四次仍在相同目标失败，不再局部修改，重新检查batch顺序、pytest marker语义和target/index边界。
