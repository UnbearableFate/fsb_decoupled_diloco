# External remediation rereview dispositions

- Reviewed target: `3b99d1a995245639f236fe73efd013e4f12c910a`
- PBS job: `2521428.opbs`
- Compute node: `mg0011`
- Target snapshot digest before/after:
  `3ceae9fcad7b8cd0fa6610c1cdfe672fa816c3b27a72ca96d3f313bc11f61a8a`
- Scheduler result: exit 0 after terminal orchestration

## Reviewer validity

- Claude Opus 5 returned HTTP 429 session capacity before inference. Its raw
  `modelUsage` is empty, so it is `skipped-capacity`, not approval.
- GLM-5.2, DeepSeek V4 Flash and MiniMax M3 each reached the registered
  1200-second timeout without a complete Markdown report or final verdict.
  Their OpenCode banners match the requested lanes, but banner identity and
  partial tool traces are not valid reports and are not approvals.
- Every read-only snapshot digest was unchanged. The invocation summary,
  prompt, raw output and stderr are retained even though no lane completed.

## Partial-observation disposition

No partial transcript contained a complete finding suitable for adoption as an
external verdict. MiniMax's terminal tool trace did expose one concrete oracle
problem before timeout: `test_runtime_composition_has_one_current_actor_boundary`
checked whether `"__main__"` was in a set containing only function/class names,
so its two new assertions could never detect a module main guard.

Codex independently reproduced the AST-set behavior and accepted this as
FSD-R10 (Low). Target `47662f8d872f4a5e451908796a6b677105a28c52`
replaces those assertions with an AST string-literal inventory, which detects
the deleted main guards regardless of quote style. The focused architecture
test passed 7/7 and the exact target subsequently passed the complete
registered one-node validation producer.

The remaining partial searches did not establish another defect. In
particular, `tests/module_coverage.json` owns modules rather than every test
function, and `artifact-schema.md` plus the registered takeover workload own
the durable boundary field while `docs/testing.md` already documents the
committed-version-2 behavior. Neither observation requires a duplicate
inventory or interface.

No valid external finding remains open. External unavailability is recorded,
not represented as approval; the final decision is the independently saved
Codex rereview of the remediated exact target.

Verdict: external-unavailable; FSD-R10 accepted and fixed
