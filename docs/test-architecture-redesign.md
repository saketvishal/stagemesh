# Test architecture and regression evidence

StageMesh has one authoritative complete-regression entry point:

```bash
python scripts/run_regression.py --report .stagemesh/regression-report.json
```

This command is distinct from component validation and the synthetic stabilization gate. A component run cannot claim complete-regression coverage.

## Coverage contract

Before executing tests, the runner performs canonical pytest collection and independently collects every audited tier. It refuses to run when the tier union has a missing, extra, duplicate, malformed, or misclassified node ID. A successful verdict additionally requires the aggregate passed, failed, skipped, and subtest counts to equal the canonical collected count.

`src/stagemesh/test_taxonomy.py` is the reviewed file manifest. New or renamed test files fail closed until classified. The tier names describe execution and isolation contracts; they do not claim that every file is a pure unit test. After the file-level audit, tests are packed into deterministic runtime-balanced batches, with at most four batches per worker and parallel tier. Only measured files above the ten-minute batch target are split, into stable contiguous node-ID ranges; this replaces the former blanket twelve-test sharding while retaining enough work stealing for slow files. The exact node-ID union is checked and native-platform tests remain in one explicitly serial batch.

When `--jobs` is omitted locally, the runner uses at most eight worker slots subject to the host CPU count. Isolation contracts also impose resource-group budgets so explicit higher job counts cannot run more than eight real Git/subprocess integration and soak lanes together. Host-sensitive native work and wall-clock acceptance tests each have explicit serial tiers. Planning uses the same limits and four batches per worker, keeping each process-heavy lane bounded without returning to hundreds of tiny shards. Measured 24-worker, 16-worker, and mixed 16-slot/eight-process-heavy runs all saturated Windows process creation and produced clustered 900-second lane timeouts; using every logical CPU or applying one global high-concurrency number is explicitly rejected. Hosted jobs pass an explicit reviewed worker count for their runner size, but cannot exceed the audited resource-group limits. Historical Windows phase durations are scheduling hints only: they are provenance-labelled in the taxonomy, are included in the catalog digest, and never substitute for current collection or result evidence. Unknown files fall back to live node counts.

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

An exact clean run of candidate `3e4ec444` rejected the initial 24-worker policy. It collected 4,334 node IDs, then produced one contention-sensitive soak failure and thirteen 900-second integration/component timeouts in its first 36 of 164 lanes. The run was deliberately interrupted rather than spending another hour after the verdict was already irrecoverably failed. Its append-only journal was preserved; Windows console interruption exposed and led to a separate process-group and responsive-parent fix. This is negative evidence, not an accepted performance result.

On the same machine, the isolated Git/process hotspot `test_provider_timeout_falls_through_and_is_scoped_to_the_task` took 49.82 seconds on clean `3e4ec444`. Combining workspace status/HEAD observation, resolving worktree Git directories without a subprocess, collecting identity config once, caching repository detection against the local-config digest, and removing redundant object probes reduced the same test to 31.96 seconds (35.9%). The optimized candidate also passed 117 focused identity/ownership tests, 35 contract tests, the exact failed soak file's 57 tests, and a real Windows Ctrl-Break partial-report test. These focused results justify the implementation direction but do not replace a complete exact-head run.

The first clean 16-worker run of `909c8f4` collected 4,338 node IDs and rejected one remaining taxonomy assumption: a real Windows process-identity recovery test in `test_continuous_driver.py` failed only while its 57-test file ran concurrently with other process-heavy lanes, then passed alone in 0.82 seconds. The file is therefore classified with the bounded serial native-platform tests; its process-liveness and child-reaping contract is not weakened or mocked. The failed journal also showed that retaining only the tail of pytest output can discard the traceback when full duration reporting follows it, so failed records now preserve bounded prefix and suffix evidence.

The next clean 16-worker run of `4d0ba67` proved that the problem was broader than that file: three integration timing assertions inflated from a required sub-15-second bound to 25–65 seconds, while five other integration lanes timed out together at 900 seconds. Those assertions pass outside the saturated run. This rejects a single global 16-worker budget and motivates the shared eight-worker external-process resource group; deadlines and functional assertions remain unchanged.

The first resource-group run then exposed a catalog error rather than a scheduler failure: `test_regression_architecture_acceptance.py` was labelled deterministic even though it exercises real process groups, timeouts, repository locks, collection subprocesses, and cleanup. It is now native serial, and its nested real-runner test explicitly inherits the installed pytest import root while retaining the worker's isolated environment.

The exact native-tier run on `57cf720` accounted for all 219 tests but exposed the same import-boundary issue in four crash-fence children: those tests intentionally replace `PYTHONPATH`, while worker-isolated `APPDATA` also moved Python's user package base. Workers now pin `PYTHONUSERBASE` to the active interpreter's installed user base, independent of isolated HOME and AppData; a dedicated acceptance test replaces `PYTHONPATH` and proves a nested child can still import the runner dependency.

The corrected exact native-tier run on `faf3cb1` passed: 218 passed, 1 skipped, all 219 collected outcomes accounted, in 362.19 seconds. This is accepted evidence for the serial native lane only; it does not substitute for the complete exact-head regression.

The complete run on `a023201` then rejected the remaining mixed-concurrency and packing policy: all eight integration lanes timed out together at roughly 902 seconds, followed by a component timeout, even though the external-process group cap itself held. The default is therefore eight total workers, and planning uses four lanes per worker and tier so process-heavy batches retain bounded size and work stealing. No deadline was lengthened.

The eight-worker run on `33f5a12` passed every first-wave integration lane that had previously timed out, but rejected one final taxonomy assumption: two sub-15-second assertions in `test_provider_outage_recovery.py` took 32–38 seconds while sharing the host, then the complete file passed alone (13 tests in 33.37 seconds). That file is now in an explicit serial-timing tier; its real clock and timing bounds are unchanged.

The corrected exact serial-timing tier on `751c460` passed all 13 outcomes in one lane, with complete accounting, in 61.37 seconds. This is accepted evidence for that tier only; complete acceptance still requires the full exact-head report.

The first exact full run on `31d57e7` passed its first thirteen lanes, including five process-heavy integration lanes, then rejected two functional delivery tests in `integration-batch-29`. Both failures exhausted incidental 20/30-second wall-clock deadlines while their real Git operations were contending with seven other process-heavy workers; the same exact tests passed alone in 14.74 and 29.46 seconds. The runner was deliberately interrupted after the verdict, and process inspection confirmed no regression workers survived.

Those tests are not performance assertions: they prove exact-candidate landing, independent review, stacked ordering, red-CI refusal, and later retry. The delivery implementation now accepts an injected clock and sleeper while defaulting to real time in production. Functional success and simulated-wait tests use a local deterministic clock instead of patching Python's shared `time` module. Explicit deadline, missing-check, pause, and partial-landing tests remain unchanged. The complete controlled-merge and fast-forward files then passed all 92 tests in 268.20 seconds, down from 545.08 seconds in the immediately preceding run (50.8%), with measured per-file testcase totals of 85.502 and 181.668 seconds. These measurements replace the prior scheduling hints, which had been contaminated by global fake-time mutation. This is focused evidence only; it does not satisfy the complete-run target.

The redesigned candidate must not be described as meeting the performance target until an exact committed-head report shows all of the following on the current Windows development machine:

- canonical collection equals the duplicate-free audited union;
- every currently collected outcome is accounted for, including the existing skips;
- no lane fails or times out;
- wall time is at most 1,800 seconds;
- no competing StageMesh regression process ran during the measurement.

Until that report exists, the performance result is **OPEN**, not passed. Protected exact-head Windows and Linux CI are additional acceptance evidence and cannot be replaced by a local run.

## Independent acceptance

`tests/test_regression_architecture_acceptance.py` covers taxonomy rejection, collection mismatch and duplication, Windows path parsing, worker isolation, unsafe cleanup refusal, subprocess termination, timeout propagation, interruption, overlapping-run refusal, bounded report output, stale resume rejection, result-count reconstruction, and live canonical-versus-tier collection equality. `tests/test_ci_workflow_policy.py` independently checks that hosted full mode has one authoritative regression invocation per operating system and publishes its evidence.
