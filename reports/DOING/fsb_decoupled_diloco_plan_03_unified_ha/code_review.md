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

## 2026-08-09 07:15 JST — P3 incremental remediation三连失败全面审查

### 范围与共同模式

- 审查覆盖jobs `2509023`、`2509032`、`2509033`的同一incremental-remediation objective。attempt1/2均为新RED fixture没有准确编码既有state/lease/manifest边界；attempt3已证明production fixes和全部focused `296`项通过，唯一full failure位于evidence-checker测试生命周期。
- production数据面（terminal ack、initializer identity、cleanup、scheduler reservation/deadline、v4 DDL）在attempt3没有失败；不得为解决artifact bootstrap放宽这些协议。

### 输入、状态、持久化、恢复与输出流

1. Matrix给每个P3 invariant绑定runtime test artifact和checker result artifact。runtime artifact包含PASS、完整/聚焦计数、raw-log hash、`requirements_covered`和被测source commit；checker result只汇总matrix/owner/evidence状态，不能作为自己的独立证明。
2. `verify_phase_requirements`读取每个evidence JSON，排除当前`--inventory-output`，只接受source commit等于显式verification target的独立runtime coverage或既有checker result。正式CLI默认verification target为HEAD，因此phase-final不会接受旧target evidence。
3. Remediation compute必须先在无新evidence的target上运行；PBS用`--verify-p3-operational-contracts`完成pre-test source/static contract检查，随后focused/full pytest产生事实。PASS后才可持久化target-bound runtime summary、更新matrix并运行phase-final requirement checker。
4. attempt3中的repository unit test绕过上述分层，直接用当前HEAD检查matrix里上一target的retained evidence。于是新target必须先有PASS evidence才能运行产生该evidence的full suite，形成不可满足的循环；这不是stale evidence被phase-final误接受。
5. 恢复边界：失败job保留完整raw log和结构化FAIL summary；下一target可以沿用上一target evidence做repository历史一致性测试，但正式phase-final必须在新PASS artifact生成后重新绑定HEAD。任何checker self-only路径仍BLOCKED。

### 替代方案与处置

- 在pytest前伪造新target PASS artifact会让未发生的runtime结果通过，拒绝。
- 关闭source binding或重新允许checker self-proof会重引入Codex M2/Claude L4，拒绝。
- 在unit test中验证matrix当前声明的独立runtime artifact、自带source commit并排除checker artifact；由已存在的synthetic test独立验证wrong-source和self-only均BLOCKED。采用该最小修订。
- 正式顺序保持：pre-test operational checker → compute tests → target-bound runtime summary → matrix更新 → `--verify-phase-requirements ... --verification-target-ref HEAD` → tracked evidence gate。第四次只有这五层全部通过才归零。
# P4 three-failure comprehensive review — 2026-08-09 08:22 JST

Review trigger: `p4-accounting-terminal-runtime-validation` attempts 1–3 failed consecutively (`2509093`, `2509095`, `2509096`). The first two failures were test-loader fixture mistakes. Attempt 3 passed all focused tests and exposed a production terminal race, so no fourth compute attempt is allowed before this review and remediation.

## Inputs and trust boundaries

- Learners publish immutable cycle receipts and proposals under their contributor fence. These objects are untrusted until a current leader validates and commits them through `LeaderAuthority`.
- Learners consume only current-epoch controls authenticated by a live, checksum-valid leader heartbeat. They must not read SQLite or treat fixed `latest.json`/`stop.json` as authority.
- A terminal acknowledgement can name only the authority-frozen last cycle or one cycle that was already in flight when close began. This is what keeps the hard-crash uncertainty bounded by one configured cycle.

## Control flow finding

The v4 learner starts its next cycle immediately after publishing the preceding receipt/proposal. `on_after_publish` may observe a newer global version, but global publication is neither an acknowledgement of this contributor's receipt nor guaranteed to select it. Consequently there is no backpressure between learner publication and leader ingestion. In run `plan03_p4_static_2509096`, both learners had published through cycle 27 when close began, the leader's scan snapshot had ingested only through cycle 9, and the learners finished cycle 28 before observing drain. Their exact terminal acknowledgements were therefore correctly rejected.

The terminal authority must not be weakened to accept arbitrary post-freeze backlog: doing so would allow an unbounded number of uncommitted cycles beyond the frozen accounting boundary and invalidate the one-cycle hard-crash upper bound.

## Persistence and idempotency review

The missing primitive is an immutable receipt-ingestion acknowledgement scoped to the current leader epoch/owner. Its identity must include run ID, epoch, owner, stable contributor key, cycle sequence, receipt ID, receipt digest, and contributor fence. It is published only after `ingest_cycle_receipt` succeeds (including exact replay), at a deterministic epoch path with byte-identical replay. The learner accepts it only through a live current-epoch heartbeat and only when every identity/digest field matches its exact receipt and fence.

One receipt may be in flight when drain is published. The learner wait therefore multiplexes three outcomes: exact receipt acknowledgement permits the next cycle; drain publishes the exact terminal acknowledgement and stops training; finalized terminal exits. Stale-epoch acknowledgements, fixed caches, malformed JSON, or another contributor's acknowledgement are ignored.

## Recovery and takeover review

A successor re-ingests immutable receipts idempotently and republishes acknowledgements in its own epoch. A learner waiting on an old leader will follow the live highest epoch and can complete without SQLite access. If no leader is live, it waits without training additional cycles. If drain is already authoritative, it stops and acknowledges instead of waiting for a receipt acknowledgement. Candidate death after authority ingestion but before acknowledgement publication is safe because successor replay reconstructs the acknowledgement.

## Output and accounting review

At most one cycle per contributor can remain outside authority progress. `begin_terminal_close` can therefore continue freezing `close_last_cycle_seq` and accepting exactly that sequence or `+1`. Normal shutdown must leave every frozen row in `acked`, no pending/selected updates, and zero hard-crash gap. Malformed or premature terminal acknowledgements remain reject-and-telemetry events rather than candidate-fatal errors.

## Revised implementation and falsification gates

1. Add deterministic current-epoch receipt acknowledgements to the v4 control publisher/reader, with exact replay and full field validation.
2. Publish the acknowledgement immediately after each successful receipt ingest, before proposal processing; publish it again safely during successor replay.
3. After every receipt/proposal publication, block the learner from starting another cycle until exact receipt acknowledgement, drain, or terminal. Do not use global-version change as a substitute.
4. Add focused tests for byte-idempotent acknowledgement, stale/wrong-identity rejection, and the one-cycle wait contract.
5. Compute attempt 4 must pass both tiny pipelines and assert all terminal fences are `acked`, the total hard-crash gap is zero, and no contributor publishes more than one cycle beyond authority progress at close.

# P4 dynamic-replacement three-failure comprehensive review — 2026-08-09 08:49 JST

Review trigger: dynamic replacement attempts `2509115`, `2509141`, and `2509143` failed consecutively. The first exposed missing child-log evidence, the second exposed an actual pre-admission torch import through a type-only dependency, and the third completed the replacement but revealed a wrong wrapper exit expectation.

## Inputs and admission boundary

The bootstrap and replacement learners have distinct random instance/admission-token identities but intentionally share stable stream 0 and the same placement. A replacement request must name the exact current instance and a non-empty explicit launch request ID. Admission responses are accepted only from the highest live current epoch; stale epoch response files cannot open the torch/GPU gate. Type-only control imports must remain under `TYPE_CHECKING` so this validation path is torch-free.

## Control flow and fencing

The first process is paused immediately after authority admission. The replacement command atomically retires its current fence, terminalizes any active work, advances stream/placement epochs, and admits the new instance. Resuming the old process is an adversarial action. It may exit nonzero: no contract promises graceful behavior for a process whose authority fence was revoked while stopped. The contract is that no post-boundary receipt/proposal can be ingested or committed.

## Persistence and collision semantics

Cycle receipt identity is stable stream key plus cycle sequence. When the old process had not yet durably advanced stream progress, both processes can attempt cycle 1. Create-if-absent publication makes this a deterministic collision; the old process cannot overwrite the replacement's receipt. If the stale process wins filesystem publication first, authority fence validation rejects it and the replacement can retry only through its authoritative resume sequence. The gate must therefore accept either an immutable collision or a membership-fence rejection as the stale-process outcome, never silent overwrite or applied work.

## Recovery and terminal behavior

The replacement inherits the authority cursor/receipt chain, not process-local state. Terminal close freezes only the current epoch-2 stream fence; the revoked epoch-1 instance is not a terminal contributor. The replacement must acknowledge gracefully with zero hard-crash gap. The resumed stale process may observe terminal and exit zero or fail earlier; both are safe if its applied-version maximum is at or before the recorded replacement boundary.

## Outputs and revised falsification gates

1. Capture the stale process exit under `set +e`; require either zero after terminal observation or a retained collision/fence diagnostic.
2. Do not let that expected stale exit skip final authority assertions.
3. Require old status `revoked`, new status `stopped`, strictly advanced stream epoch, exactly one graceful terminal fence, zero hard-crash gap, and balanced terminal summary.
4. Query old-instance updates and require `MAX(applied_version) <= version_at_replacement`; this is the authoritative successful-commit=0 assertion.
5. Attempt 4 must close all assertions and retain the process logs as evidence.

# P4 Plan01-v4 regression three-failure comprehensive review — 2026-08-09 09:27 JST

Review trigger: `p4-plan01-v4-regression` jobs `2509229`, `2509238`, and `2509243` failed consecutively. The first two runs exposed shared migration/lifecycle omissions and reduced the suite from 199 failures to one. Attempt 3 passed 862 tests and stopped only on a stale singular launcher-result assertion, before the strict-v4 smoke. No production behavior is changed by the remaining remediation.

## Config input and initialization boundary

- The independent launcher validates both requested PBS walltimes before creating an immutable run, resolves only through `resolve_config_v4`, then passes the complete `ConfigV4` to `initialize_run`. Missing or sub-ten-minute walltime therefore cannot leave a bootstrapped but unsubmitted run. The migrated test now patches that actual strict-v4 loader and supplies the v4 wrapper; restoring a module-level legacy `resolve_config` dependency would reopen a removed production route.
- Formal full configs are validated as v4 before any compatibility projection. The compatibility projection in classic `load_config` exists only so retained P1–P3 oracle tests can run until P5 deletion; Plan01 production entrypoints do not consume it. HA initializer tests now exercise mandatory v4 leader configuration rather than setting removed `coordination.syncer_ha`.

## Launcher control flow and scheduler side effects

- Submission order is syncer first, followed by one static learner-array command or independently submitted dynamic bootstrap learners. A syncer rejection returns `failed` immediately. Once the syncer has an accepted job ID, a learner rejection returns `partial`; it intentionally does not issue `qdel`, because implicit cancellation would destroy the operator's accepted-job receipt and create an unobservable scheduler race.
- The attempt-3 fixture is static, so exactly one learner receipt is appended. Its mocked qsub results prove the accepted syncer ID survives and the learner failure is returned. Dynamic mode uses the same ordered list for multiple bootstrap slots; a singular learner field cannot represent that contract.

## Receipt persistence and recovery contract

- `syncer_submission` is a single receipt because there is exactly one initial candidate submission. `learner_submissions` is always an ordered list once learner submission begins, with `bootstrap_slot=None` for the static array and the exact slot for dynamic jobs. `accepted_learner_job_ids` records only successful learner submissions on a partial dynamic topology. This output is the recovery/operator boundary; no accepted ID may be discarded or synthesized.
- The stale test's `learner_submission` key predates independent dynamic learners. Adding it back as an alias would create two authorities for the same receipt, force callers to guess which shape applies, and preserve code P5 is required to delete. Updating the assertion to list index 0 is the only contract-preserving remediation.

## Terminal and strict-v4 smoke output

- The PBS gate runs all pytest tests before creating the smoke run, so failed regression attempts leave no run root to clean. On success, `run_tiny_2proc_smoke.sh` initializes a strict-v4 local run and launches one candidate plus two learners. The post-run gate reads immutable summary/SQLite authority state and requires finalized terminal state, two graceful `acked` fences, zero hard-crash gap, three attestations, three JSONL telemetry streams, and no legacy CSV output.
- Attempt 2's terminal CHECK failure confirmed the schema rule that `final_update_id` is transient state owned only by `draining`; retirement must clear it after final-update adjudication. Attempt 3 passed that authority test and all other terminal tests, providing a regression counterexample against weakening the CHECK or terminal fence logic.

## Alternatives, remaining edit, and fourth-attempt gate

- Reintroducing `learner_submission`, weakening the ten-minute scheduler minimum, accepting a legacy `Config` at `initialize_run`, or skipping pytest to reach the smoke are rejected because each hides rather than migrates a removed boundary.
- A repository-wide search found the singular key only in the two adjacent stale assertions; the production launcher and P4 mandatory launcher tests consistently use `learner_submissions`. Replace both assertions with `result['learner_submissions'][0]`, preserving the failed status and exact stderr checks.
- Before attempt 4, rerun Ruff/format/compile, `bash -n` over every Miyabi PBS/shell script, and `git diff --check`. Attempt 4 passes only if all repository tests pass, the strict-v4 smoke completes, SQLite integrity/terminal/accounting assertions pass, and the completion marker is emitted. If the fourth attempt fails, record the new evidence and re-audit the entire failing boundary rather than restoring compatibility aliases.
# 2026-08-09 comprehensive escalation review — P4 incremental admission remediation

Trigger: three consecutive terminal failures of `run_plan03_phase4_tests.pbs` (`2509653`, `2509656`, `2509663`). Per `plans/AGENTS.md`, no fourth run is allowed until the failure chain and revised logic are reviewed end to end.

## Failure chain and actual boundaries

1. `2509653`: 76/77 focused tests passed. The remaining cross-epoch replacement replay used a stable command ID but reconstructed a different command payload after the binding became current, so the command journal correctly returned `CommandConflictError` and the runtime incorrectly persisted rejection.
2. `2509656`: 77/77 focused passed; full pytest had one unrelated default-run-ID second-rollover flake (`13:08:34`→`13:08:35`). No P4 assertion failed.
3. `2509663`: 77/77 focused, 894/894 full, and static runtime passed. Dynamic authority admitted the instance, but the learner timed out because the revised reader looked up `current/<actor_id>.json`; dynamic current pointers are intentionally keyed by stable stream ID (`current/0.json`).

The failures are not repeated local patches to one unknown symptom. They expose two distinct identity layers that the first rewrite had not made explicit enough: request identity versus committed command identity, and physical actor identity versus stable contributor identity.

## End-to-end producer/consumer review

### Hot request discovery and disposal

- Producer publishes an immutable regular JSON request at a bounded discovery path.
- Discovery now returns an explicit observation containing exact bytes, `(device,inode)`, decoded payload, or an unreadable state. A read/open/stat failure is deferred and never converted into malformed content.
- Successfully read invalid UTF-8/JSON is content-addressed by raw bytes and removed only with the observed inode identity. Valid JSON is content-addressed and archived using the same canonical bytes as its digest.
- Each observation has its own runtime exception boundary. A corrupt/unreadable request can no longer terminate or starve the remaining admission scan.

### Authority mutation and cross-epoch replay

- New mutations use `admit-<canonical-request-sha>` so command identity is epoch-independent.
- Static replay first checks whether the exact `(learner, logical launch, attempt)` is already the active binding. That is a read-only idempotent completion path; it does not bypass authorization for a different attempt or logical launch.
- A non-current attempt still requires the exact immutable operator authorization before the authority mutation. The authorization-derived command payload is used only for the original mutation, not reconstructed after that mutation has already committed.
- After any partial publication failure, a successor repairs controls for the exact current authority fence and completes disposition/history as `admitted`; it does not rerun a stale generation CAS and manufacture a rejection.

### Filesystem control identities

- Admission responses are immutable under a canonical contributor-fence namespace, so reusing an old attempt ID with a new binding generation cannot collide with the old response.
- Rejections are immutable under canonical request SHA, so repeated attempt IDs with different request/rejection reasons cannot collide.
- Current pointers remain one mutable cache per **stable contributor key**. This means static uses learner ID, while dynamic uses stream ID; actor/instance ID remains inside the exact pointer/response payload.
- The learner now passes both identities explicitly: `actor_id` locates and validates the physical response/rejection identity; `stable_contributor_key` locates and validates the current-fence pointer. Dynamic obtains the stable key from its descriptor-validated stream ID before torch import.
- The public reader validates request-specific rejection first, then exact current pointer, derived fence-namespaced response path, response digest, exact fields, typed fence, and typed resume state.
- Disposition replay reuses those consumer-equivalent validators and additionally requires the exact current pointer before a valid hot request can be removed.

## DB/filesystem ordering and crash windows

The retained ordering is:

`observe request → authority command commit/replay → immutable response or rejection → disposition → canonical history → inode-checked hot removal`.

- Failure before authority commit leaves the request hot.
- Failure after authority commit but before response leaves the request hot; retry/successor reconstructs response from current authority state.
- Failure after response but before disposition reuses exact immutable response/resume bytes.
- Failure after disposition but before history/removal validates response/rejection plus current pointer before retrying canonical history and inode-checked removal.
- A changed hot inode is never removed on behalf of the old observation.

SQLite remains the only writer authority. Filesystem controls are immutable facts or explicitly repairable current caches; no fixed cache can authorize a contributor. The changed path layout stays epoch/owner scoped, and no admission path is used as a DB mutation authority.

## Scheduler, process, and timing interaction

- The dynamic learner already knows the descriptor-bounded stream ID from bootstrap slot or replacement request; passing it as the stable key introduces no scheduler lookup or new trust source.
- Rejected/waiting actors still complete all admission reads before importing torch or allocating CUDA.
- Transient hot-request read failures now defer one poll and do not consume authority state. Unreadable poison entries emit telemetry but do not block healthy requests.
- The full P4 PBS remains bounded by its existing 180-second process timeouts. The dynamic timeout in `2509663` is fully explained by the wrong pointer key and is locked by a new direct reader regression in addition to the real pipeline.

## Test coverage required before the next run

- Focused: unreadable entry alongside healthy request, invalid UTF-8, one-shot read error, canonical valid duplicate, request-specific rejection collision, old attempt-ID reuse, malformed resume/rejection/current pointer, cross-epoch partial-publication replay, and dynamic stream-key pointer lookup.
- Checker: MODE-02 is owned by P4 and evidence-only descendant commits are accepted only when the tracked source/test/script/config tree is identical; real source drift remains blocked.
- Complete: ruff/format, current-boundary Checker, full pytest, static pipeline, and dynamic pipeline in the same PBS job.

## Review conclusion

No unexplained failure remains. The revised API makes both identity distinctions explicit and preserves fail-closed authority semantics without allowing one filesystem entry to kill a candidate. A fourth validation run is authorized only after formatting, static compilation/lint, repository-wide `bash -n`, and confirmation that all reader call sites supply the stable contributor key.

# 2026-08-09 P5 deletion/migration gate three-failure comprehensive review

Review trigger: three consecutive attempts of the P5 classic/fragment deletion validation failed (`2510689`, `2510803`, `2510805`). No fourth compute attempt is permitted until this review is recorded and its High/Medium findings are covered by tests and remediation.

## Failure chain and common pattern

1. `2510689` was the intentional RED run taken before the legacy query-only reader existed. It failed on the expected missing import and established that the deleted fragment runtime was not being silently retained.
2. `2510803` passed the deletion/architecture Checker but failed Ruff formatting for two newly created, untracked files. The login-node format command had derived its scope from tracked files, while the PBS script's explicit file list correctly included the new files.
3. `2510805` passed lint, format, architecture/deletion Checker, and 383 focused tests. Its sole failure was the test expression `set & dict` in `test_config.py`; production config parsing had already completed successfully.

The shared pattern is gate lifecycle and test-harness construction, not three manifestations of one production defect. The differences matter: attempt 1 was a deliberate behavioral RED, attempt 2 was an incomplete local validation scope, and attempt 3 was an invalid assertion operand. Nevertheless, the consecutive-failure rule applies to the complete P5 objective, so this review rechecks the whole data path rather than authorizing only the one-line test correction.

## End-to-end data, control, and persistence flow

1. A new run enters through strict `ConfigV4` parsing and migration, descriptor construction, immutable initializer publication, and v4 SQLite schema/bootstrap. Leader acquisition and all subsequent mutations remain fenced by epoch/owner identity and SQLite transactions.
2. Runtime admission/control now lives under `storage`, because it reads and publishes filesystem state. Typed protocol modules contain no `Path`, filesystem I/O, SQLite access, or process launch. Learner receipts/proposals flow through immutable filesystem publication into fenced authority transactions, global publication, acknowledgements, and terminal accounting.
3. Classic full-update/fragment configuration keys and runtime writers are absent. New configs cannot express those modes, fragment DDL cannot be created, and no runtime module imports `legacy`.
4. An old full-update or fragment run is a query-only input: the legacy SQLite reader opens the database read-only/query-only, and the fragment decoder is a pure function over already-read rows/bytes. Analysis, export, and evaluation may consume that projection, but cannot initialize, resume, repair, compact, or mutate the old run.
5. Fresh-v4 identity and byte-stability guarantees apply only to fresh attempts. An old in-progress root is never resumed or upgraded in place; an authorization collision requires a fresh attempt ID rather than mutable overwrite.
6. Test/PBS lifecycle is `lint -> explicit format scope -> architecture/deletion/config Checker -> focused pytest -> full pytest -> evidence summary`. A failed precondition emits a retained raw log and structured failure before any source change. Successful phase evidence is generated only after the complete compute gate passes.

## Invariants and transaction/recovery audit

- Immutable run publication, leader fencing, authority transactions, token accounting, terminal drain, audit dependency closure, and GC reference rules remain v4 responsibilities. P5 deletion must not weaken them merely because classic tests are removed.
- The four legacy fragment tables are recognizable only by the legacy reader. No current schema, migration, bootstrap, writer, recovery path, or garbage collector may create or mutate them.
- Query-only compatibility must be an explicit dependency of tools, never an implicit fallback in production config/runtime loading. A strict loader must continue rejecting removed keys.
- Shared SQLite/GC/terminal tests deleted with classic files must be mapped to retained v4 tests or recreated. Deleting a test because its fixture used fragments is not evidence that the underlying transaction, reference, or cleanup invariant became obsolete.
- Filesystem publication remains create-no-replace with immutable identities. Deletion of classic fragment directories must not broaden cleanup to unknown files or historical roots.

## Findings

### High — legacy export/evaluation config loading is currently broken

`eval_lm_harness.py` loads a manifest config with strict `load_config`, while `validation_eval.py` and `publish_quality_gate.py` use the now-strict resolved-snapshot loader. Old v1-v3 snapshots contain removed `init`, `fragments`, `failure_sim`, or coordination keys, so those query-only tools reject exactly the historical full/fragment roots that P5 promises to retain for analysis/export/evaluation. The analysis reader alone is insufficient.

Required remediation: add an explicitly named legacy query-config projection under `fs_diloco.legacy`. It may discard known runtime-only legacy sections and construct only the current model/data/run fields needed for read-only analysis/evaluation. It must not add removed fields back to `Config`, perform migration writes, or be imported from runtime. Tool call sites must opt into this loader, and a test must prove the strict loader rejects the same old snapshot that the legacy tool projection can read.

### Medium — shared `resolve_config` owns a v4-only stop-target rule it cannot evaluate

After removal of `stop_after_global_tokens`, shared `Config` no longer contains `stop_after_direct_weight_tokens_applied`. Its `global_only` check therefore rejects a valid v4 envelope that specifies only the direct-token target. Stop-target completeness belongs in `ConfigV4`, where both current targets are visible. Keeping the check in the lossy shared projection creates inconsistent acceptance between strict parsing and later resolution.

Required remediation: remove the incomplete target check from the shared helper, retain/strengthen it in `ConfigV4`, and test both a direct-token-only valid envelope and an envelope with no stop target.

### Medium — deletion classification must prove shared invariant migration

Several deleted fragment-oriented tests also exercised generic storage, retention, or long-cycle behavior. Before phase acceptance, the classification artifact must map every deleted test to `migrate-to-unified`, `retain-legacy-reader`, or `delete-obsolete`, name the retained replacement assertion, and explain parameterized count deltas. Any unmapped shared GC/SQLite/terminal invariant requires a new v4 test rather than a prose-only disposition.

### Low — format discovery differed between local and compute gates

Tracked-only discovery omitted new files. Static validation must use the same explicit P5 file set as PBS, or include cached plus untracked nonignored paths. This is a gate reproducibility issue; the compute gate already caught it fail-closed.

### Low — the Checker contains some lexical SQL/entrypoint checks

The lexical checks are useful deletion tripwires but are not semantic proof. They remain supplemental to import tests, strict config tests, legacy query-only tests, and the real v4 pipelines required in P6.

## Alternatives considered

- Keeping classic writers/schema behind feature flags would reduce deletion work but violates the explicit source/config/DDL absence gates and leaves two authorities. Rejected.
- Teaching the production loader to silently accept and discard old keys would make tools work, but would also make old modes appear resumable and weaken typo detection. Rejected.
- A separate strict v4 path plus an explicitly imported legacy query-only projection is chosen. It preserves a narrow compatibility surface and makes runtime-import checks enforceable.
- Recreating a mutable legacy ORM/store would simplify some tests but would reintroduce repair/write capability. Rejected; the reader and fragment decoder remain read-only/pure.
- Requiring every projection of `Config` to validate envelope-only stop semantics duplicates incomplete state. The v4 envelope is the single correct validation boundary; shared projection helpers remain structural.

## Revised implementation and falsification sequence

1. Correct the invalid set/dict test expression only after this review is saved.
2. Add a legacy query-config projection and route analysis/export/evaluation/quality-gate consumers through it. Add a counterexample containing removed old keys: strict current load must fail, query-only projection must preserve the evaluation-relevant fields.
3. Move the global-only stop-target completeness assertion to the `ConfigV4` boundary and add direct-token-only/no-target cases.
4. Audit every deleted test function against retained v4 storage/GC/terminal coverage and publish the exact classification/count artifact; add any missing shared-invariant tests.
5. Rerun Ruff, explicit format checks including untracked files, compileall, deletion/architecture Checker, `git diff --check`, and repository-wide `bash -n` before submission.
6. Attempt 4 passes only if all static/Checker gates pass, focused pytest has zero failures and no unexpected xfail, full pytest has zero failures, and the emitted evidence binds the exact source tree and test counts. A further failure must be recorded and re-audited before another change.

# 2026-08-10 P6 G5 three-failure Codex+GPT comprehensive review

Review trigger: formal G5 jobs `2512722.opbs`, `2512732.opbs`, and `2512750.opbs` failed consecutively. The third job passed four scenarios before the local dynamic-loss topology reached a proven no-progress state and was explicitly deleted. No fourth G5 submission is authorized until the topology and durable assertions below replace the original design.

## Independent review lenses and the three-failure pattern

Codex implementation audit and GPT protocol-model audit agree on the following facts:

1. Attempt 1 exposed a real recovery-hot-set defect, not a scenario timing issue. A lagging contributor's last receipt was held by a hot foreign key, retaining its applied update, selection/publication dependency closure and predecessor global version. The repair made progress's ID/hash/cursor the continuity authority and allowed already-adjudicated noncurrent history to archive.
2. Attempt 2 exposed a wrong test oracle. A resumed replaced static actor may cleanly observe terminal state before attempting a write; nonzero Unix exit is not the safety property. The revised oracle checks durable replacement history, exact current generation and the union of hot+archived updates for zero old-generation authority updates.
3. Attempt 3 exposed a topology contradiction. Both manually spawned dynamic learners inherited the parent candidate's `PBS_JOBID`. Killing one subprocess cannot make that shared PBS allocation terminal, while production replacement deliberately requires live/historical scheduler proof before revocation. No timeout, extra capacity window, log verbosity or heartbeat threshold can turn a RUNNING shared job into evidence that one contained actor is permanently lost.

The common pattern is that the first harness treated process-local observations as if they were authority facts: a row count was assumed to mean current-only recovery, an exit status was assumed to mean stale fencing, and a killed PID was assumed to mean scheduler-confirmed loss. The corrected gate must validate the same durable identities production uses.

## End-to-end data and control flow

The dynamic scenario starts from a clean descriptor-bound run root and fresh dynamic authority. Initial admission consumes one bootstrap slot per stream and records the exact physical instance, stream epoch, placement identity and PBS job ID in one fenced SQLite transaction. Learners publish immutable receipt/proposal/payload objects; the syncer observes them, verifies the current dynamic fence, records receipt and proposal commands, and selection requires both current streams at quorum 2.

Permanent-loss handling begins only after the dead timeout and lack of current progress. `DynamicCapacityService._confirmed_lost_instance()` then queries both live and historical scheduler views for that instance's exact PBS job. Only FINISH evidence permits a replacement plan. The plan reserves the stream, persists an exact launch request, transitions through submission state around qsub, and records the returned child job ID. A replacement learner must present that request/stream/replaced-instance identity before admission advances the stream epoch and revokes the old instance. Only then may quorum resume.

The original local topology bypassed the scheduler for both bootstrap actors. Consequently the killed instance and survivor shared the candidate job ID, the scheduler truth remained RUNNING, SQLite correctly retained both as admitted, and all survivor proposals were superseded/dropped while v0 remained current. This was fail-closed production behavior.

## Transactions, publication, archive/GC and process lifecycle

- SQLite remains the sole membership and writer authority. The harness must not directly edit `learner_instances`, `streams`, launch requests or candidate leases, and must not synthesize FINISH state in the database.
- The initial losable learner must therefore run in its own real PBS child allocation. `run_dynamic_learner.pbs` supplies the descriptor/source checks and bootstrap-slot admission boundary. Its existing post-admission test hook may TERM only that learner; normal PBS job completion then supplies real live/historical terminal evidence.
- The survivor may remain a subprocess in the parent candidate allocation because it is not the object whose scheduler death is being proved. The production scheduler submits the replacement using the configured literal script, queue and at-least-10-minute walltime. The parent plus one learner child are the maximum live allocations; the replacement is submitted only after the first child is terminal.
- Receipt/proposal/checkpoint publication ordering is unchanged. A failed child can leave immutable receipts/proposals, but current-fence ingest and terminal accounting decide their fate. Maintenance archives dependency-closed adjudicated rows, writes command receipts before pruning, and GC deletes only identity-checked claimed objects; validation must read hot and immutable audit history where an assertion spans compaction.
- Parent teardown must wait for or explicitly terminate only job IDs created by this scenario. It must never qdel an unknown/successor allocation merely because the parent exits. The final artifact records both child IDs and scheduler terminal observations.

## Test-oracle audit and omitted counterexamples

The six scenario names remain correct, but three original assertions were not: exact one-row hot version state was tested before the archive closure could remove last-progress dependencies; stale safety was mapped to process exit; permanent loss was injected below the scheduler identity boundary. The revised oracles are:

- terminal/current-only hot DB plus balanced rollup and no active/prepared rows;
- old static generation has no hot or archived update, independent of its exit code;
- lost dynamic instance has a distinct terminal child job, status `revoked`, replacement has a different child job, same stream with greater epoch, and no old-fence application after the replacement boundary;
- all six scenarios still validate summary, terminal fences, checkpoint controls, integrity, temp files and candidate epoch history.

Missing counterexamples to add or preserve are: a killed subprocess whose shared PBS job remains RUNNING must not be labeled permanent loss; a qstat live `no_record` without historical FINISH must remain uncertain; a replacement registration with the wrong request/stream/replaced-instance must be rejected before model load; and an old dynamic instance resuming after replacement must have zero successful commits after the boundary.

## Alternative explanations and designs

Increasing timeouts or lowering `heartbeat_dead_after_seconds` is rejected because scheduler FINISH is a mandatory second factor. Directly calling `retire_dynamic_incarnation` from the harness is rejected because it bypasses the capacity/scheduler contract G5 is meant to exercise. A fake scheduler injected only into the candidate would make the test deterministic but would no longer be a real tiny pipeline. Running both bootstrap learners as child jobs is valid but unnecessarily raises the live allocation and submission cost; one real losable child plus one parent-local survivor exercises the exact boundary with the frozen maximum of two live allocations.

## Revised implementation and RED-to-GREEN sequence

1. Add bounded qsub/query helpers to the G5 runner. Submit bootstrap slot 0 through `run_dynamic_learner.pbs` with descriptor/source variables, `debug-g`, explicit `00:10:00`, and the existing terminate-after-admission hook. Launch only slot 1 locally.
2. Record the initial child job ID. Wait for its exact admission row, then for live/historical scheduler FINISH; do not infer death from elapsed time alone.
3. Let the production capacity service plan and submit the replacement. Read the durable replacement launch row to obtain the second child job ID and require it differs from the lost job. Do not manually admit or mutate the stream.
4. Wait for survivor, replacement and candidate to finish. Query hot plus archived updates and record the version at replacement. Require old-instance `MAX(applied_version)` absent or at most that boundary, replacement same stream/higher epoch, single current admission, terminal fences acknowledged, balanced token ledger and current-only hot authority.
5. Add a focused scheduler-identity regression showing two instances with the same RUNNING job cannot produce `_confirmed_lost_instance`, while exact historical FINISH for a distinct job can. This is the RED test for the prior topology assumption.
6. Run Ruff/format/compile, full PBS shell syntax and literal-group checks, then the affected complete G2 suite before G5 attempt 4. Attempt 4 passes only if all six partial artifacts and the final artifact exist, all process/scheduler/DB/artifact assertions pass, maximum live allocations is at most two, and no scenario-created child job remains queued/running. This data flow avoids all three prior failure classes because each gate now uses the durable authority identity at its actual trust boundary.

# 2026-08-10 P6 G5 attempts 4–6 second Codex+GPT comprehensive review

Review trigger: after the first three-failure topology review, formal G5 attempts `2512873.opbs`, `2513024.opbs`, and `2513076.opbs` formed a second consecutive three-failure window. No attempt 7 is authorized until this review and the revised launch/admission oracle are implemented and the complete G2 group passes.

## Evidence pattern and independent review lenses

The Codex implementation audit followed each persisted transition and filesystem control. The GPT protocol-model audit independently classified which identity owns each assertion. Both reach the same result:

1. Attempt 4 was the only run in this window with production behavior defects. Before both declared bootstrap slots appeared, capacity tick treated the temporary gap as ordinary low capacity and submitted an extra scale-out. A new replacement polling the same stream then interpreted the old incarnation's legitimate tombstone as a rejection of its not-yet-processed request. Launch churn exhausted the terminal launch budget. The fixes now anchor initial bootstrap grace in immutable authority creation time, distinguish pre-capture polling from post-capture fence revalidation, start loss age at `max(admitted_at, progress.updated_at)`, and avoid telemetry identity collisions. Clean G2 target `7aff255` passed all four RED regressions and the full suite.
2. Attempt 5 proved those fixes in the real scheduler: one initial child, one replacement, no scale-out, v11 terminal. The failure was entirely in the harness joining a normalized launch receipt (`2513032`) to the full registration identity (`2513032.opbs`) by literal SQL equality.
3. Attempt 6 normalized the join and again proved the real pipeline, then failed only because the harness mapped scheduler-confirmed expiry to `revoked`. Authority and its existing focused test deliberately use `expired` with reason `confirmed_scheduler_terminal_after_progress_stall`; changing production would erase the distinction between scheduler expiry and operator revocation.

The common failure mode shifted from production topology to verifier identity ownership. The next revision must stop inferring replacement admission from a second PBS-ID scan when the authority already persists an exact `launch_requests.admitted_instance_id` foreign key, and must assert the status/reason pair defined by the retirement cause.

## End-to-end data/control flow and invariants

The dynamic scenario initializes one authority and candidate, starts survivor slot 1 under the parent job, and submits losable slot 0 as a real child. Registration carries the full PBS job ID and is admitted as bootstrap stream 0/epoch 1. The harness qdels only that exact child and waits for live/historical FINISH. Capacity service observes stale progress plus terminal scheduler evidence, and one fenced `plan_dynamic_launch_request` transaction expires the old instance, releases its stream/placement, abandons its active proposal dependencies, and reserves an exact replacement request. The scheduler outbox stores the normalized qsub receipt; the replacement process publishes a request containing the full PBS ID plus launch request, stream, and replaced-instance identities. `admit_dynamic_incarnation` atomically binds `launch_requests.admitted_instance_id` to the new instance and advances stream epoch. The learner may load Torch only after the matching admission response; its receipt/proposal then participates in a normal merge. Terminal close freezes only current survivor/replacement fences, adjudicates any hard crash, finalizes token fate, archives noncurrent history, and leaves one hot current version.

Required invariants are: parent plus at most one child live; initial scheduler FINISH precedes replacement submission; exactly one nonbootstrap launch and it is a replacement of the exact old instance; launch receipt and registered PBS ID are equal after canonical normalization; launch `admitted_instance_id` is the sole replacement-instance join; old row is `expired` with exact scheduler-terminal reason; replacement is same stream/higher epoch and finishes `stopped`; old-fence applied version never exceeds the measured replacement boundary; replacement has at least one applied update; no pending/selected/prepared state, token imbalance, temp object, child job, or active noncurrent member remains. SQLite transactions and publication/archive/GC ordering remain production-owned and must not be synthesized by the harness.

The sixth scenario adds a candidate lifecycle: epoch 1 commits v2 then crashes outside the transaction, epoch 2 takes over from authority, and the scheduler loss/replacement flow occurs under the successor. It must prove first candidate nonzero, successor/survivor clean exit, at least two candidate epoch records, one exact replacement and the same membership/publication/terminal invariants. Candidate takeover does not authorize an extra learner launch or reset launch identity.

## Test-oracle audit and alternative design

The four static scenarios have passed attempts 3–6 and retain valid oracles. The fifth scenario's Unix/PBS/process assertions are secondary to durable authority relationships. The sixth has not executed in this second window, so all variables used after `try/finally` were audited for assignment, all scenario-created child IDs are bounded and cleanup catches both query and terminal-wait errors, and replacement contribution is checked across hot plus content-addressed audit history.

The rejected design is to keep normalizing arbitrary `learner_instances.pbs_job_id` rows until one matches: it is susceptible to accidental reuse and duplicates a relationship already committed transactionally. The selected alternative adds a request-keyed wait that joins `launch_requests.request_id → admitted_instance_id → learner_instances.instance_id`, then separately verifies normalized PBS equality. This makes the authority foreign key the join and PBS normalization only a consistency assertion. Another rejected alternative is accepting either `expired` or `revoked`; that would hide cause drift. The oracle must require the exact `expired`/scheduler-terminal pair for this injection.

## Revised implementation and attempt-7 gate

1. Keep the normalized job helper only for the initial bootstrap child, where no production launch request exists. Add `_wait_replacement_admission(request_id)` for production replacements and require launch state `admitted`, non-null exact `admitted_instance_id`, joined instance in an admitted/draining/stopped state, and normalized launch/instance PBS IDs equal.
2. Require lost final status `expired` and exact reason `confirmed_scheduler_terminal_after_progress_stall` in both dynamic scenarios. Keep `replacement_final='stopped'`, one exact replacement, contribution and old-fence boundary assertions.
3. Run Ruff/format/compile/PBS syntax/literal-group checks, freeze a clean commit, then run complete G2. Attempt 7 may be submitted only after that group passes.
4. Attempt 7 passes only if six partial artifacts plus final structured artifact exist; every scenario validates integrity, terminal/current-only authority, token balance and artifacts; both dynamic scenarios record exact request/instance/scheduler bindings; qstat shows no scenario child queued/running. Any further failure is recorded before modification and re-audited against this request-keyed control flow rather than patched by timeout, budget, or permissive status sets.

# 2026-08-10 P6 G10 attempts 1–3 Codex+GPT comprehensive review

Review trigger: formal G10 jobs `2513331.opbs`, `2513381.opbs`, and `2513422.opbs` failed consecutively before producing any timed arm. No attempt 4 is authorized until this environment/source/config/timer review is saved, the latent config counterexample has a RED test, and a clean compute regression passes.

## Three-failure pattern and independent review lenses

The Codex implementation audit traced the PBS shell, Git objects, uv environments, subprocess cwd, run lifecycle and evidence writer. The GPT protocol/statistics audit independently checked comparability identities, timer anchors, pair order, bootstrap method and acceptance thresholds. The failures share one pattern: setup code compared string-shaped identities without preserving the semantic layer that produced them.

1. Attempt 1 exported a locked `torch==2.13.0+cu132` requirement but discarded the explicit PyTorch index that makes that version resolvable. No environment or arm existed yet. The repair now builds both independent venvs directly from the same current frozen project/lock and replaces only the classic project source with the detached worktree.
2. Attempt 2 proved both installs, but `python -c` ran from the current project root, so the interpreter's empty path entry shadowed the classic editable install. The neutral-`TMPDIR` probe fixes observation without `PYTHONPATH` or weakened assertions.
3. Attempt 3 passed environment and package-origin preflight, then compared an annotated tag object's SHA (`53e05fa...`) with the detached checkout's peeled commit SHA (`a00a3d6...`). Git correctly checked out the tag's commit; the validator compared different object kinds.

None is a performance result or production regression. The common correction is to capture a canonical identity once, retain its type/derivation, and compare like with like before entering the timer.

## End-to-end environment, run and evidence flow

The PBS job creates a detached classic worktree under job-owned `TMPDIR`, creates two physically distinct venv paths from the current `uv.lock`, and installs the archived source into only the classic venv with `--no-deps --reinstall -e`. Neutral-cwd probes must prove current import → current root, classic import → detached root, distinct Python paths and identical Torch versions. The frozen classic reference is an annotated tag, so preflight must record the ref object and explicitly resolve `^{commit}`; the worktree HEAD is compared only with that peeled commit.

For each comparison, the current source helper captures a clean commit/fingerprint. Configs are generated before timing from frozen templates. Every arm starts at a nonexistent fresh run root; the main timer begins before any arm-specific initializer and ends only after all three actor `wait()` calls return. Config preparation, result reading and cleanup remain outside the timer for both arms. Classic initializes inside its actor processes; unified static/dynamic includes the mandatory initializer inside its timed arm. All roots and logs are per-arm/per-pair and removed only after their terminal summaries have been projected into the retained trial record.

Classic-vs-unified runs one unmeasured warmup per arm followed by exactly twenty pairs, AB on even pair indices and BA on odd indices. Static-vs-dynamic repeats the same independent sequence. Each measured pair preserves baseline/candidate end-to-end seconds, signed overhead `(candidate-baseline)/baseline`, secondary active-protocol duration and actor output tails. The evidence writer runs only after all workload projections and statistics are available; a non-inferiority failure writes `BLOCKED` evidence before exiting nonzero.

## Workload, persistence and statistics invariants

- Both arms use two learners, quorum 2, two inner steps, two global versions, FP32 synthetic-tiny data/model, seed 1337, no failure injection and no mid-cycle replacement.
- Comparability is derived from terminal SQLite/audit facts, not declared config alone: contiguous final version, applied update count, processed/direct tokens, per-contributor cursor, source/config/model/data identity and equal resource allocation.
- Current summaries join hot plus content-addressed archived updates and reconcile `direct_applied` with the token rollup. Classic summaries reconcile applied updates with committed `total_seen_tokens`. Each arm requires SQLite integrity and terminal completion before any timing is accepted.
- Each arm has a distinct DB/run root and interpreter. No performance state is shared except normal read-only package/model caches; synthetic data avoids external cache effects.
- Formal inference remains the pre-registered median of twenty signed paired overheads plus a fixed-seed 10,000-sample one-sided paired-bootstrap 95% upper bound. Both must be `<=0.10`; absolute median above 20% is `INCOMPARABLE`, and no clipping is permitted.

## Findings discovered before attempt 4

### High — classic reference validation must peel the annotated tag

`git worktree add` dereferences the annotated tag to a commit, while plain `git rev-parse <tag>` returns the tag object. The expected identity must use `git rev-parse '<ref>^{commit}'`, and evidence should retain the frozen ref name, tag object SHA/type and peeled commit separately. Comparing worktree `HEAD` to the peeled commit proves the intended source without accepting another commit.

### High — static performance config assumes an optional YAML section exists

`_prepare_configs()` assigns `static["terminal"]["max_terminal_merges"]`, but the tracked `configs/fs_diloco_tiny_ha_static.yaml` relies on the dataclass default and has no `terminal` mapping. Once tag preflight passes, attempt 4 would fail with `KeyError` before warmup. Use `setdefault("terminal", {})` and add a direct RED regression that loads the actual tracked templates and generates the dynamic comparison configs; it must assert the resolved static baseline receives `max_terminal_merges=0` without mutating the source file.

### Rejected after exact-line verification — apparent duplicate unreachable raise

The initial review display concatenated two overlapping `sed` ranges and printed their shared boundary line twice. Exact source inspection and `git blame` confirm `_wait_processes()` contains one `raise RuntimeError`, followed by the intended `finally` teardown. No code change is warranted for this display artifact.

No production, persistent schema, timer formula, workload identity or statistical threshold change is justified by the three failures.

## Alternatives considered

Re-exporting requirements and manually adding one extra index is rejected because it recreates part of uv's source-resolution contract and can drift on future explicit sources. Sharing one interpreter with different `PYTHONPATH` values is rejected because it violates independent-env provenance and permits cwd shadowing. Checking only the tag name is rejected because annotated and lightweight refs have different object behavior. The selected approach uses two frozen-lock venvs, an exact detached worktree, neutral-cwd origin probes and an explicitly peeled commit.

An alternative performance design would run each arm in its own PBS job. It would add scheduler/node noise to pairs and weaken the common allocation anchor, so the single allocation with fresh sequential roots remains preferable. Reducing pairs, dropping warmups, changing the 10% margin or stopping after an interim result is rejected by the frozen method.

## Revised implementation, RED tests and attempt-4 gate

1. Add a frozen classic-ref resolver that records ref/object/type/peeled commit; compare classic worktree HEAD only with the peeled commit and include all identities in the artifact.
2. Change static config generation to tolerate an omitted optional `terminal` mapping. Add a full-suite RED test against the real templates, plus a ref-resolution test that proves the repository's annotated tag object differs from and peels to the expected commit.
3. Keep `_wait_processes()` unchanged because exact-line verification rejected the apparent duplicate. Keep the two independent frozen environments and neutral-cwd origin/version probes unchanged.
4. Run diff/compile/Ruff/format/PBS syntax/literal-group checks, freeze a clean target and pass the complete G2 compute suite before attempt 4.
5. Attempt 4 is successful only if both classic-vs-unified and static-vs-dynamic artifacts are written from the same clean target; each contains one warmup per arm, exactly twenty AB/BA pairs, invariant workload identity, unclipped raw signed deltas, complete timer/secondary metrics, and both median plus one-sided 95% upper bound `<=10%`. Any later failure is recorded and re-audited against this full flow before another run; timeouts, pair count, margin and CI method remain frozen.
