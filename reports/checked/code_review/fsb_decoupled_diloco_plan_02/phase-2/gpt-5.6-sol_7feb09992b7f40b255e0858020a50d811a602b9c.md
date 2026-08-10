# Independent Codex review — Plan 02 Phase 2 final evidence target

## Review identity

- Decision: **CHANGES_REQUIRED**
- Reviewer source: Codex
- Actual model: `gpt-5.6-sol`
- Review target: `7feb09992b7f40b255e0858020a50d811a602b9c`
- Comparison base: `fefef86b68aa346afee93680ad9c494657412074`
- Reviewed diff: `fefef86b68aa346afee93680ad9c494657412074..7feb09992b7f40b255e0858020a50d811a602b9c`
- Executable source identity: clean commit `85febbaee653dcff04897eea35a15dd8f31172c2`, source fingerprint `sha256:b8684b5d90d22a341da3e30dfca375b6de4026103ee64769f2b71aae500fba69`; the later target commit adds synchronized documentation and retained evidence without changing executable Phase 2 source.

## Scope and method

I independently reviewed the cumulative Phase 2 diff before invoking or reading the external reviewer. The scope included dynamic configuration and schema v3; immutable run/bootstrap identity; registration, admission, replacement and stream fencing; proposal ingestion and commit-time membership validation; capacity hysteresis and the PBS launch outbox; dynamic close, drain acknowledgement and terminal repair; bounded archival/discovery; the Phase 2 Checker and launchers; focused tests; the retained G7/G8/G9/compatibility/matched/completed evidence; requirement records; and synchronized documentation.

The retained formal workload is strong positive-path evidence: the final G9 run reaches version 120 with eight bootstrap slots, replacement after a permanent learner loss, a rejected physical duplicate, a short pause, terminal controller state and 1,521,024 tokens; the focused and full PBS test jobs report 21 and 473 passes; the completed Checker returns `PASS`; and matched dynamic control overhead is below the frozen threshold. Those results do not exercise the crash windows and timeout counterexamples below.

## Findings

### High — dynamic no-progress timeout publishes a normal terminal without closing admission and suppresses recovery

When quorum is unavailable while the controller is still `open`, the no-progress branch sets `stop_reason="no_progress_timeout"` and breaks directly (`fs_diloco/runtime/syncer.py:3407-3413`). It does not start the persisted dynamic drain, cancel launch capacity, wait for drain acknowledgements, or close the controller. The unconditional finalizer nevertheless writes a non-error `terminal_state` and canonical stop before asking whether dynamic input is closed (`fs_diloco/runtime/syncer.py:3766-3799`). For dynamic mode, `wait_for_learner_shutdown()` immediately returns `store.dynamic_input_closed()`, which is false for the open controller. Consequently the later closed-controller assertion is skipped because it is guarded by `all_learners_stopped` (`fs_diloco/runtime/syncer.py:3841-3863`), and the process can exit with a normal terminal row while admission remains open.

That state is not self-healing. A successor treats every non-error terminal row as completed and enters terminal repair, but `repair_completed_ha_terminal()` rejects an `open` controller. Learner-assisted recovery also treats the normal terminal as authoritative, so candidates can repeatedly fail repair instead of resuming or completing a drain. This violates the `open -> draining -> closed -> terminal` state machine, MEM-17/MEM-19, and the rule that terminal state is committed only after input closure.

Route dynamic no-progress through an explicit fenced transition. Either classify it as a recoverable error without publishing a completed terminal, or begin a generation-bound drain and finish the complete closure predicate before normal terminal publication. Make the finalizer reject every non-error dynamic terminal unless the controller is already `closed`, regardless of `all_learners_stopped`. Add a runtime regression covering an open/manual-policy run with no quorum through successor behavior and Checker result.

### High — capacity observations have unrecoverable post-commit and post-allocation crash gaps

The merge is durably committed by `publish_global()` before `record_dynamic_capacity("merge:<new_version>")` runs in a separate fenced transaction (`fs_diloco/runtime/syncer.py:3553-3630`). A crash, lease loss, or SQLite error after the global commit but before the second transaction leaves a committed version with no merge observation. Resume starts from the committed head and never backfills that key; only a later merge or starvation event can create a new observation. Missing a high-capacity merge can leave `consecutive_low_count` stale and cause a false scale-out, while missing the second low merge can suppress or delay the required request.

The starvation path has the same problem one step earlier: `allocate_starvation_observation_key()` durably advances both `starvation_generation` and `next_starvation_observation_at` (`fs_diloco/storage/fenced_store.py:2295-2322`), and only afterward does the syncer record the observation (`fs_diloco/runtime/syncer.py:3396-3405`). A crash between those transactions permanently consumes the generation and defers the next capacity decision for a full interval.

This violates the Phase 2 observation contract that each committed merge has the idempotent key `merge:<version>` and that unique starvation windows survive takeover. It also leaves MEM-14/MEM-15 unsubstantiated at the HA crash boundary. Fold observation state into the corresponding commit/allocation transaction, or persist a pending observation cursor that every successor must replay before processing later capacity input. The completed Checker should require exactly one merge observation for every committed dynamic version after v0 and contiguous, non-orphan starvation allocations. Add failpoints immediately after global commit and starvation allocation, followed by takeover, and assert identical low-count/request outcomes to an uninterrupted run.

### Medium — token-target drain can exceed the configured stop condition and its own terminal-merge budget

The target branch always passes `config.sync.stop_after_outer_steps` as `global_target`, even when the trigger is `stop_after_global_tokens` (`fs_diloco/runtime/syncer.py:3158-3177`). `begin_dynamic_drain()` then treats both target reasons alike: if that outer-step target is absent it permits `current_version + max_terminal_merges`, and if an outer-step target is also configured it permits all remaining versions up to that target (`fs_diloco/storage/fenced_store.py:1672-1685`). Thus a run that has already crossed its token stop can commit at least one more merge by default, or many more when the outer-step target is larger. Static mode stops immediately at the same token boundary, and the Phase 2 contract freezes a target-driven terminal maximum rather than allowing the non-target tail budget.

Define the version ceiling for a token-triggered close explicitly. Since the token threshold is already satisfied at the committed current version, the safe ceiling is the current version unless the plan is deliberately amended to specify a separate token-tail policy. Pass a reason-specific ceiling instead of the unrelated outer-step target, and add tests for token-only and combined token/outer targets proving the terminal version and total tokens cannot advance after the token trigger.

## Correctness, concurrency, persistence, and error handling

The central membership transaction design is otherwise sound: admission binds instance, placement, stream epochs and token hash; healthy placement replacement requires the configured policy or an audited authorization; selection and final merge revalidate current membership under the fenced writer transaction; one logical launch request cannot admit two instances; queued/running scheduler evidence remains reserved; and drain closure accounts for unsettled instances, launch capacity, pending registrations, final pointers and visibility grace. The reviewed positive-path tests cover those rules, including rollback when membership changes before commit.

The blocking defects are all persistence-order problems outside those successful atomic transactions. They matter specifically because Phase 2 is an HA protocol: a result that is correct only when the leader survives from one durable transaction to the next is not takeover-safe. The current completed Checker validates terminal/controller shape and observation uniqueness, but not merge-observation completeness, so the retained successful artifact cannot falsify the second finding.

## Test and acceptance evidence checked

- Focused Phase 2 PBS tests: job `2501472`, `21 passed`.
- Full regression PBS tests: job `2501475`, `473 passed`.
- G8 evidence: launcher `2501477`, `PASS`, terminal version 12.
- G9 evidence: launcher `2501510` with crash/successor/bootstrap/duplicate/checker children through `2501522`, `PASS`, terminal version 120 and 1,521,024 tokens.
- G7 and compatibility evidence: jobs `2501529` and `2501530`, both `PASS`.
- Matched performance: launcher `2501534`, checker `2501554`, `PASS`; dynamic control overhead ratio 0.
- Completed Checker: job `2501559`, `PASS`; schema v3, controller terminal generation 2, 64 active and 60 archived capacity observations, and zero reported failure events.
- Static repository checks recorded at the target: `git diff --check`, `bash -n scripts/miyabi/*.pbs`, and literal `xg24i002` group IDs pass.

## Final decision

**CHANGES_REQUIRED**

The positive G9 and regression evidence demonstrates the intended normal path, but Phase 2 cannot be closed while an ordinary dynamic no-progress timeout can create an irreparable normal terminal and while capacity hysteresis loses required inputs across two explicit HA crash windows. The token-target ceiling is also inconsistent with the configured stop semantics. These findings require code changes, negative regression tests, fresh compute-node verification, and an incremental review before the phase-final approval gate.
