# Harness-remediation finding dispositions

- Plan: `plan03-1`
- Review target: `af54925a4d0487a37c20f298f2027003cb079d20`
- External reviewer job: `2518777.opbs`
- Review kind: `critical-incremental`

## External reviewer availability

No external invocation produced a valid reviewer report. Claude failed
authentication; GLM, DeepSeek and Kimi timed out without substantive output.
Their immutable target snapshots were unchanged. These outcomes are neither
approvals nor findings.

## Prior finding closure state

The mandatory Codex review found the implementations of C1-C4 and A1/A2/A4
suitable for runtime verification. Their final disposition remains `fixed
pending runtime verification`; the unavailable external invocations do not
alter that conclusion.

## Newly accepted coordinator findings

| ID | Severity | Decision | Evidence | Required closure evidence |
|---|---|---|---|---|
| C5 | High | `accepted` | `tests/harness/test_full_protocol_harness.py` manually publishes a three-field stop object and a three-field summary, while `ControlPublisher.publish_terminal` emits authoritative identity/schema fields. `check_full_protocol_run.py` only compares final versions and the fixed/immutable copies, so this impossible fixture currently passes. It also accepts a writable epoch terminal object. | Build the fixture through `ControlPublisher`; derive and require the exact stop and summary projections from the finalized authority row; require the epoch terminal publication to be immutable; mutation-test both control schemas. |
| C6 | Medium | `accepted` | The PBS wrapper test asserts trap-related source strings, but never runs `run_full_protocol.pbs`; an expansion, quoting, array, trap-order or exit-status bug could pass the current test. | Execute the real wrapper under `bash` with a failing allocation stub, Checker stub and module stub; assert preservation of the allocation exit status and publication of one structured blocked artifact. |

## Promotion decision

Runtime tests remain blocked. C5 changes the Checker acceptance boundary and C6
closes its PBS failure producer, so their continuous diff requires a new frozen
target, mandatory critical-incremental Codex report and best-effort external
review before the one-node `interact-g` test stage.

## 2026-08-10T17:43:57+09:00 — implementation freeze

- C5: `fixed pending review and runtime verification` at target
  `219abe663025adc7ff8f731f65d90fb27c42c0fe`. The fixture now uses the product
  publisher; exact stop/summary and immutable-mode mutations are registered.
- C6: `fixed pending review and runtime verification` at the same target. The
  real wrapper is executed with bounded allocation/Checker/module stubs and
  must preserve exit 23 while publishing the exact blocked reason.
- Mandatory critical-incremental Codex verdict: `APPROVE`. External review and
  all runtime tests remain pending.
