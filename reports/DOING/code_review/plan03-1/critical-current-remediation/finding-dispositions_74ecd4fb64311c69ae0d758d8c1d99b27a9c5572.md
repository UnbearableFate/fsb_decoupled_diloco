# Current-review remediation finding dispositions

- Base: `5b474d5c1735beb8cca922fd6cc7b6304926df2c`
- Reviewed target: `74ecd4fb64311c69ae0d758d8c1d99b27a9c5572`
- PBS job: `2520922.opbs`
- Compute node: `mg0011`
- Scheduler result: exit 0 after 20:14

## Reviewer validity

- Claude Opus 5: `completed`, unchanged snapshot, valid
  `CHANGES_REQUIRED` report. Raw `modelUsage` verifies canonical/actual model
  `claude-opus-5`, session `944698db-e744-486d-8b3f-44c045ae3582`.
- MiniMax M3: `completed`, unchanged snapshot, valid `APPROVE` report. The
  OpenCode banner and report identity match requested `opencode-go/minimax-m3`.
- GLM-5.2 and DeepSeek V4 Flash: `invalid-output`; neither produced a complete
  report with the required final verdict. Their partial progress text is not
  treated as approval or as a valid finding source.

## Dispositions

| ID | Severity | Decision | Current-only remediation |
|---|---|---|---|
| FSD-R1 | High | accepted | Register the takeover boundary in the completion contract, emit it only for the takeover workload, bind it in runtime artifacts and add a discriminating contract test. |
| FSD-R2 | Medium | partially accepted | The retryability risk is real, but restoring `except Exception` would reintroduce the disposed integrity defect. Retry only SQLite `BUSY`/`LOCKED`; propagate I/O, schema, command and stale-token failures. |
| FSD-R3 | Medium | accepted | Exercise the positive exact authorization path and a mismatched old-fence mutation through real `LeaderAuthority` composition. |
| FSD-R4 | Low | accepted | Parametrize receipt and proposal propagation over schema, command-conflict and stale-token failures. |
| FSD-R5 | Low | accepted | Make `clean_run` consume `RunPaths.epoch_stop_path`; remove the duplicate test fixture derivation. |
| FSD-R6 | Low | accepted | Require the rank environment, publish the boundary in the gate artifact and prove boundary 3 passes while a 3-vs-2 mismatch fails. |
| FSD-R7 | Low | accepted | Mark the candidate failed before error fencing so failed fencing cannot trigger release or replace the primary exception; test the double-failure path. |
| FSD-R8 | Low | accepted | Delete the runtime entrypoint `__main__` guards; keep only `python -m fs_diloco.learner` and `python -m fs_diloco.syncer`. |
| FSD-R9 | Low | accepted | Add a non-empty storage-module inventory guard to the dependency test. |

The MiniMax residual observation about richer release-failure distinction is
subsumed by FSD-R7. A successful candidate still surfaces its own release
failure; a candidate already in its error boundary never attempts a misleading
release.

All accepted source/test changes require compute-node focused and full-suite
validation, followed by a Codex rereview. They invalidate target `74ecd4f` for
promotion; no formal ladder gate may reuse its source identity.

Verdict: CHANGES_REQUIRED
