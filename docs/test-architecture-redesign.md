# Test architecture and regression evidence

StageMesh has one authoritative complete-regression entry point:

```bash
python scripts/run_regression.py --report .stagemesh/regression-report.json
```

This command is distinct from component validation and the synthetic stabilization gate. A component run cannot claim complete-regression coverage.

## Coverage contract

Before executing tests, the runner performs canonical pytest collection and independently collects every audited tier. It refuses to run when the tier union has a missing, extra, duplicate, malformed, or misclassified node ID. A successful verdict additionally requires the aggregate passed, failed, skipped, and subtest counts to equal the canonical collected count.

`src/stagemesh/test_taxonomy.py` is the reviewed file manifest. New or renamed test files fail closed until classified. The tier names describe execution and isolation contracts; they do not claim that every file is a pure unit test. After the file-level audit, tests are packed into deterministic runtime-balanced batches, with at most two batches per worker and parallel tier. Only measured files above the ten-minute batch target are split, into stable contiguous node-ID ranges; this replaces the former blanket twelve-test sharding while retaining enough work stealing for slow files. The exact node-ID union is checked and native-platform tests remain in one explicitly serial batch.

When `--jobs` is omitted locally, the runner uses up to 24 workers subject to the host CPU count. Hosted jobs pass an explicit reviewed worker count for their runner size. Historical Windows phase durations are scheduling hints only: they are provenance-labelled in the taxonomy, are included in the catalog digest, and never substitute for current collection or result evidence. Unknown files fall back to live node counts.

## Isolation and safety

Each worker receives separate temporary, home, user-profile, application-data, Git configuration, and port-base state. Ambient provider and GitHub credentials are removed. Commands are fixed argument lists with no shell interpolation. Cleanup validates its target before recursive removal and fails visibly if cleanup cannot complete.

A repository-scoped operating-system file lock rejects overlapping complete runs across worktrees that use this runner. This matters because simultaneous Git/process-heavy regressions distort deadlines and performance evidence. When the parent handles an interruption, it terminates registered subprocess trees, cancels queued lanes, cleans worker roots, and writes a partial report. Completed-lane records already appended to the journal remain durable even when the host terminates the parent before final report writing.

## Identity-bound resume and evidence

`--journal PATH` writes append-only lane completion records. `--resume` is accepted only when commit, dirty-tree digest, manifest digest, isolation-policy fingerprint, interpreter, operating system, and dependency hashes match. Passed lanes without complete result counts are rejected. An existing journal is never overwritten implicitly.

`--report PATH` writes bounded JSON containing:

- exact run identity and tree state;
- exact canonical node-ID inventory, count, and SHA-256 digest;
- per-tier and per-file collected counts;
- per-lane duration and outcome;
- the 50 slowest individual tests, aggregated across setup, call, and teardown phases;
- aggregate accounting and coverage equality;
- failure output capped at 20,000 characters per stream (passing output is omitted).

Hosted full mode runs this entry point once on Windows and once on Linux and uploads the report, journal, and audited validation plan. Main-branch push remains the previously established bounded smoke path; scheduled, manual, merge-group, and cross-cutting pull-request plans retain full mode. Operator and audited component modes remain separate.

## Measured evidence and claim boundary

The last accepted pre-redesign complete local Windows baseline is bound to commit `123aa659`: 4,276 collected outcomes (`4247 passed, 3 skipped, 26 subtests passed`) in 6,451.68 seconds on Windows. That record did not retain trustworthy per-file durations, so no baseline slow-file ranking is claimed.

The redesigned candidate must not be described as meeting the performance target until an exact committed-head report shows all of the following on the current Windows development machine:

- canonical collection equals the duplicate-free audited union;
- all 4,334 currently collected outcomes are accounted for, including the existing skips;
- no lane fails or times out;
- wall time is at most 1,800 seconds;
- no competing StageMesh regression process ran during the measurement.

Until that report exists, the performance result is **OPEN**, not passed. Protected exact-head Windows and Linux CI are additional acceptance evidence and cannot be replaced by a local run.

## Independent acceptance

`tests/test_regression_architecture_acceptance.py` covers taxonomy rejection, collection mismatch and duplication, Windows path parsing, worker isolation, unsafe cleanup refusal, subprocess termination, timeout propagation, interruption, overlapping-run refusal, bounded report output, stale resume rejection, result-count reconstruction, and live canonical-versus-tier collection equality. `tests/test_ci_workflow_policy.py` independently checks that hosted full mode has one authoritative regression invocation per operating system and publishes its evidence.
