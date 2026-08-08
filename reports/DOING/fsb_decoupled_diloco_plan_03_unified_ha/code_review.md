# Plan 03 连续失败代码审查记录

初始创建时尚未触发同一实验连续三次失败升级。

## 2026-08-09 00:34 JST — 第二轮增量review finding disposition

- Codex Medium（checker differences未落盘）已修：PBS传入`--inventory-output`，artifact同时保存frozen/current两组source ref与differences。Codex Low（oracle mutation自比较）已修：主oracle和negative test共用`_assert_fixture_matches`，negative从真实store/filesystem projection篡改theta。
- Claude H-1已修并补反例：preexisting final连续两次均fail closed且不留reservation；foreign entry不被认领/可见；different staging不能抢reservation；same staging peer mkdir race以reservation/final identity同inode证明；reservation缺失时普通reader不可见，只有completed全量自检后的显式repair恢复。
- Claude M-1..M-5全部fixed。旧`20260808-225600`只保留为历史记录，不再是matrix gate evidence；authoritative FS artifact为`20260809-003337`。阶段结束前必须在commit后运行`check_plan03.py --expect ... --verify-boundaries --require-tracked-evidence`。
- L-1/2/3/4/5/7/8/9/10/12/13 fixed；L-6以正式G10固定20 pairs处置；L-11拒绝在本phase全局unignore所有历史reports artifacts，因为静态检查证明会暴露大量其他计划产物，Plan03自身已正确放行。所有High/Medium均无defer。

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
## 2026-08-09T04:19:38+09:00 — Comprehensive review after three consecutive P3 validation failures

### Scope and evidence

This Codex/GPT review covers the complete first P3 slice from learner accounting inputs through typed protocol objects, v4 SQLite transactions, scheduler reconciliation, staged filesystem publication, audit/GC references and observable outputs. Evidence is the three consecutive `p3-compute-validation` runs: job `2508845.opbs` stopped at an over-broad format scope, job `2508848.opbs` stopped at an over-frozen Checker boundary, and job `2508854.opbs` passed all static/Checker gates then exposed three focused behavioral/test-contract failures (`3 failed, 232 passed`). Attempts 1 and 2 were validation-harness faults; attempt 3 proves the harness now reaches the intended state machines.

### End-to-end data and control flow reviewed

1. Learner-side steps enter `TrainingSegmentAccumulator`; destructive pre-publication replace transfers current effective tokens to local-discarded and resets effective loss/example/gradient state, while retained rebase preserves it. The cycle closes into a typed `CycleReceiptV1`; a positive effective segment may additionally produce `FullUpdateProposalV2`.
2. `LeaderSession.ingest_cycle_receipt` runs under `BEGIN IMMEDIATE` and a current static/dynamic fence, verifies contiguous sequence/hash/cursor, inserts receipt and token fate, advances contributor/stream progress and incrementally updates `token_rollups`. Proposal ingest verifies immutable filesystem payload before entering the fenced transaction, binds it to the exact receipt, and leaves at most one pending proposal per contributor.
3. Selection first chooses one proposal per contributor, then orders contributors from persistent `selection_state`; selection only marks rows. `commit_merge` revalidates all fences, commits version/publication/update/token transitions and service credit in one SQLite transaction. Failed selection/publication/commit does not update service credit.
4. Dynamic replacement retires the exact old fence and its active or receipt-only outstanding work in the same transaction, then returns cursor, receipt hash and next sequence from base contributor progress. Terminal close snapshots current fences/progress, blocks admission, accepts at most the frozen current cycle, records ack/hard-crash bound and only finalizes after active work/intents/tokens drain.
5. Scheduler no-record observations persist uncertainty/deadline/evidence without releasing the anti-duplicate reservation. Operator tooling writes only create-no-replace immutable requests; an active leader applies expected-state CAS and audits either applied or stale-rejected outcome. It never admits or calls qdel.
6. Initializer prepares config/source/descriptor/DB/policy/identity in same-parent staging, owns a same-inode sibling reservation, exclusively creates final, hard-links manifest objects and publishes `.complete` last. Reader validates identities and immutable hashes before returning. Audit history is written as an immutable batch before its exact dependency-closed SQLite rows are pruned; cumulative token rollup remains authority.

### Invariants, transaction boundaries and failure semantics

- Token conservation is `processed = local_discarded + applied + dropped + quarantined/conflicted + unpublished + outstanding`; carried ancestry and hard-crash gap upper bounds are separate. Every fate transition and its rollup bucket move must be one transaction. A read model must not condition terminal-gap visibility on whether any receipt exists.
- Fairness credit is consumed only by a successful global commit. Selection and prepare/abandon paths cannot mutate it. Tensor reduction order remains stable-key order even when admission order changes.
- The originally frozen two-field key `(last_selected_committed_version, stable_key)` is insufficient for the explicit count-difference gate with batched service: all members of a selected batch receive the same version, so a partially exhausted age cohort repeatedly borrows low stable keys from the next cohort. The observed `500/333` split is deterministic evidence, not noise. Keeping this key unchanged would force either a false gate or hidden mutable tie cursor.
- Revised ordering is `(committed_service_count, last_selected_committed_version_or_minus_one, stable_key)`. `committed_service_count` is already persisted and transactionally incremented; it supplies the missing service quantity. Last version preserves oldest-service preference within equal counts, stable key gives deterministic final order. The explicit Plan text's narrower key is rejected-with-evidence for this implementation; SEL-03/04/05/06 remain stronger and satisfied. No plan正文 is rewritten per `plans/AGENTS.md`; the disposition is recorded here/progress/matrix evidence.
- Terminal fence rows and token rollup are independent authority domains. The read path must aggregate terminal hard-crash bounds before/alongside the empty receipt ledger. This is a read-model bug only; the acknowledgement transaction and fence state were correct.
- Identity/config/source objects are intentionally 0444. A tamper test must first emulate an actor with replacement/permission authority, then assert loader fail-closed. Direct `write_text` is no longer a valid fixture setup. The immutable behavior must not be weakened to preserve an obsolete test assumption.
- Audit pruning deletion order must respect observation/frontier, batch/update/receipt/token and version/publication/artifact references. Immutable audit publication/hash validation precedes the single fenced prune transaction; generic cleanup has no audit deletion path.
- Initializer retry accepts only the staging identity inode that owns the reservation; same bytes on another inode fail. Marker-before-complete remains invisible. Process wait/timeout clocks remain monotonic; persistent wall samples occur after SQLite write-lock acquisition.

### Test review and missing counterexamples

The attempt-3 tests correctly detected two behavior/read-model defects and one obsolete setup. The fairness test needs a short deterministic prefix assertion in addition to aggregate metrics so future regressions identify cohort/tie behavior directly. Authority selection also needs a transaction-level test proving a selected-but-abandoned batch does not consume service count. Terminal tests need both empty-receipt hard crash and receipt-bearing final ack, ensuring gap aggregation does not alter token balance. Initializer tampering should cover descriptor, config and source using explicit chmod or atomic replacement while retaining the 0444 precondition assertion.

### Alternative explanations/implementations considered

- Adding a rotating in-memory stable-key cursor would balance counts but violates persistence, crash determinism and SEL-05/06; rejected.
- Assigning distinct fractional/ordinal pseudo-versions within one global commit could make the original key work, but falsifies the meaning/type of `last_committed_version` and complicates replay; rejected.
- Using only committed service count is count-fair but loses useful oldest-service information among equal counts. Count primary + last-version + stable key is the smallest durable deterministic correction and reuses existing schema.
- Returning a fabricated zero gap when `token_rollups` is absent could be patched in the test, but would hide real terminal authority; rejected. Querying the independently persisted gap for both empty/non-empty ledgers is required.
- Making initializer identity files writable would restore old tests but violate INIT-01 and allow post-complete silent mutation; rejected. Update the adversarial fixture instead.

### Revised implementation and RED tests before attempt 4

1. Change the pure selector and SQL selector ordering to committed service count first, then last committed version, then stable key; keep reduction order stable key. Add the first 16 selected sets, 1000-round count/wait/Jain metrics and failed-batch-no-credit authority RED tests.
2. Refactor `token_ledger_summary` so terminal gap is queried even when `token_rollups` has no singleton. Keep `TokenLedgerSummary.balance` independent of the gap and assert the empty-ledger hard-crash result is 64.
3. Assert initializer files are 0444, explicitly chmod only inside tamper setup (or atomically replace the name), and retain the existing loader checksum diagnostics plus zero-leadership-write check.
4. Run Ruff/format/compileall/Checker/bash/diff locally, then compute attempt 4. Exact pass condition: static and Checker PASS; focused group zero failures/xfails including the new prefix/no-credit cases; full suite zero failures/core xfails; terminal completion marker emitted. These changes avoid all three prior causes rather than relaxing the acceptance assertions.
