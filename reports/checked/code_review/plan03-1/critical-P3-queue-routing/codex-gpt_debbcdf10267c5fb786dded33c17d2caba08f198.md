# Mandatory Codex P3 queue-routing critical-incremental review

- Plan: `plan03-1`
- Review kind: `critical-incremental`
- Continuous base: `4688bedebda2cee94137bf943425ca3d9c31ed17`
- Frozen target: `debbcdf10267c5fb786dded33c17d2caba08f198`
- Requirements: `FUNC-4L1S-01`, `FAULT-4L1S-01`
- External review: skipped under the user's Codex-only review directive

## Scope and evidence inspected

I inspected the complete continuous diff, the three scheduler histories, the
revised P3 manifest, the PBS wrapper and allocation launcher, the live
`qstat --rsc -x` queue contract, source-scope equality, walltime policy and the
previous approved P3 test-design report. The target is a report-only change:
there is no diff in `fs_diloco`, `configs`, `scripts/miyabi`, `tests`,
`pyproject.toml`, `README.md` or `docs` relative to the approved design target.

## Classification and routing

Jobs `2519520.opbs`, `2519545.opbs` and `2519576.opbs` never acquired execution
hosts and produced no run, log root, PBS stdout or Checker artifact. Treating
them as pre-execution infrastructure events, with the product failure counters
unchanged, is correct and prevents scheduler delay from being confused with a
protocol failure or promoted as evidence.

Live queue discovery proves `debug-g` is enabled and started, supports 1-16
nodes, permits 30 minutes and has a 100 GiB per-node ceiling. The registered
request needs five nodes, 16 GiB and ten minutes, so it fits every current queue
bound. `5:ncpus=8:mpiprocs=1:mem=16gb` makes the intended bounded resources
explicit and still yields exactly five PBS chunks and one MPI rank per node.
The site's implicit one-GPU-per-chunk allocation remains visible in scheduler
metadata and is independently checked by the existing topology/attestation
oracles.

## Test-design preservation

The queue change does not alter the synthetic model/data identity, seed,
20-local-step by 4-global-step workload, exact 5120-token formula, actor
mapping, fault layer, timeout, durable authority oracle, PASS/FAIL/BLOCKED
classification or cleanup boundary. Ten minutes remains both the workflow
minimum and the previously reviewed runtime budget; `debug-g` does not require
shortening it.

The next submission must use a new create-only run/log/PBS/evidence identity,
repeat shell/group/source/path preflight, and execute only the normal scenario.
Fault scenarios remain serially blocked until the normal job is terminal PASS.
All three scenarios must use the same post-review commit and formal-scope
fingerprint.

## Findings

No Critical, High, Medium or Low finding remains. The revised queue and exact
resource request are valid for this brief P3 candidate gate.

Verdict: APPROVE
