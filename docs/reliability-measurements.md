# Reliability measurements: before and after

Reproduce with `scripts/measure_reliability.py` (same scenarios, two source trees). Baseline: commit `1fb0ad4` (PR #396 merge), the
state before follow-through was wired. Raw results: `.stagemesh/reports/reliability-20261018/`.

## What this is and is not

* Deterministic replays: fake providers/reviewers, temporary repositories, a controlled clock, a fake PR opener and merge guard, an
  in-memory OS scheduler. No live provider, network, evaluation or client repository was touched.
* **Acceptance expectations were fixed in the script (AC1-AC8) before any comparison ran.** Two measurement *definitions* were
  corrected after the first run (an "idle window" is one that attempted no publication, landing or implementation; "waiting for
  checks" notices are counted as progress, not completion). Expectations were not changed.
* **Baseline automation is modelled.** At `1fb0ad4` the only way to get unattended follow-through is to re-run the same `run-window`
  on a timer, and nothing can end that timer. It is modelled as firing every 10 simulated minutes for 24 simulated hours. Baseline
  window counts therefore scale with that assumed horizon; the measured, horizon-independent quantity is the cost of one idle
  wakeup (a full preflight plus a provider-health check). Current-tree schedule behaviour is measured.
* Time to the terminal outcome is identical in both trees (600 s to published, 3000 s to merged): follow-through is not faster, it
  stops sooner and repeats less.
* **No model-token savings are claimed.** No scenario reduces real provider (model) calls; repeated provider failure was already
  bounded at 3 calls by PR #396 and is unchanged. Provider usage was never recorded before this change, so no historical token
  comparison exists. Any token figure for these scenarios would be synthetic; none is reported as observed.
* `duplicate_wakeups` baseline publication/implementation counts (3 vs 1) are **overstated by the fakes**: the fake task runner does
  not model per-task claims and the fake opener is not idempotent like the real one, so production would absorb part of that. The
  valid finding is concurrent windows executed (5 vs 1) and the preflight cost.

## Results

| Scenario | Metric | Baseline | Current |
| --- | --- | --- | --- |
| Deadline before publication, restart, wakeups | windows run / idle / provider-health preflights | 145 / 143 / 145 | 2 / 0 / 2 |
| | PRs published; time to published | 1; 600 s | 1; 600 s |
| | schedule left running at end | yes (modelled) | no (removed, verified) |
| | implementations after restart | 0 | 0 |
| Pending CI x5 then green (merge authorized) | landing attempts | 6 | 6 |
| | windows run | 145 | 6 |
| | identical "waiting" notices / completion notices | 5 / 1 | 1 / 1 |
| | time to merged | 3000 s | 3000 s |
| Publication denied, found by a wakeup | notices / wakeups | 36 / 36 | 1 / 1 |
| | attempts at the denied action | 1 | 1 |
| Same, task id with non-ASCII characters | attempts at the denied action | **36** | **1** |
| | final reported state | IN_PROGRESS (blocker lost) | EXTERNALLY_BLOCKED |
| 5 simultaneous wakeups | windows executing concurrently | 5 | 1 (4 `duplicate_worker`) |
| | provider-health preflights | 6 | 2 |
| Implementer disposes its own finding | accepted | **yes** | no |
| Control: setup timeout x4 windows | implementations | 1 | 1 |
| Control: crashing provider x8 windows | provider calls | 3 | 3 |

Acceptance (AC1-AC8): baseline fails AC2, AC3, AC4, AC6, AC8; current passes all eight. AC7 (nothing weakened) is shown by equal
implementation counts and by the unchanged validation/review/integration logic (the diff only adds recording around them); the
existing end-to-end and gate tests pass unchanged.

## Test evidence

Regression tests drive the real `run_window`, the follow-up worker, the real Windows adapter (recording runner, plus a one-off
real Task Scheduler round trip: create, idempotent re-create, remove, verified gone), the real provider adapters, `drive_task` and the
CLI: `tests/test_delivery_identity.py`, `test_delivery_followup.py`, `test_efficiency.py`, `test_review_disposition.py`. A CI-configured
selection of 1,299 tests had 5 failures, all identical on the baseline commit (environment-related: venv `python` resolution and
Windows path/timing in queue-reconcile and execution-timing tests).
