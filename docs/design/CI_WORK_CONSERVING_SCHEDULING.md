# Design: work-conserving scheduling while external CI is pending (#65)

## Status: DRAFT — minimal slice, not yet implemented

This document scopes issue #65 to a minimal, correct, independently
reviewable slice before implementation, per the explicit instruction not to
rush a scheduler/lifecycle change.

## Problem, restated precisely

StageMesh's own `INTEGRATING -> DONE` transition (`build_coordinator/runner/
upstream.py`, `build_coordinator/runner/orchestrator.py`) currently pushes
the integrated branch and immediately marks the task `DONE`. It never
observes whether GitHub Actions (or any other externally hosted required
check) on that push actually passed. This session's own PR #63 experience
showed real hosted-CI runs can take 15–40+ minutes on Windows — time during
which:

1. A task that has been pushed and is "done" from StageMesh's point of view
   might still fail CI and need remediation, but nothing in StageMesh's
   state machine represents "pushed, CI pending" as distinct from "pushed,
   verified done."
2. If a future change makes `DONE` conditional on CI (which is what #65
   ultimately wants), a worker slot must not sit idle polling CI when other
   independent READY tasks exist.

## Non-goals for this slice

- Not building a general "any external CI provider" abstraction. This slice
  targets GitHub Actions via `gh`, consistent with the existing `gh`
  dependency in `task_source/github.py`.
- Not changing how StageMesh's own internal deterministic validation gate
  (`VALIDATING` state, `run_validation()`) works — that is unaffected.
- Not implementing retry/backoff tuning, metrics, or observability beyond
  what's needed to prove the mechanism works.
- Not hardcoding StageMesh's own repo or PR numbers into scheduler logic.

## State machine change

Add one new terminal-adjacent state: `AWAITING_EXTERNAL_CI`.

```
INTEGRATING -> AWAITING_EXTERNAL_CI   (new: push succeeded, CI not yet observed)
AWAITING_EXTERNAL_CI -> DONE          (new: CI reported success for the exact pushed SHA)
AWAITING_EXTERNAL_CI -> REWORK_REQUIRED (new: CI reported failure; route to remediation)
AWAITING_EXTERNAL_CI -> BLOCKED       (new: CI status could not be determined after retries)
```

`policy.py` changes:

```python
VALID_TRANSITIONS["INTEGRATING"] = frozenset(
    {"DONE", "BLOCKED", "FAILED", "REVIEW_READY", "REVIEWING", "AWAITING_EXTERNAL_CI"}
)
VALID_TRANSITIONS["AWAITING_EXTERNAL_CI"] = frozenset({"DONE", "REWORK_REQUIRED", "BLOCKED"})
```

`AWAITING_EXTERNAL_CI` is deliberately **not** added to `CLAIMABLE_STATES` —
a task waiting on CI is not available for a worker to claim; it is waiting
on an external signal, not on agent work. This is the mechanism that frees
worker capacity: the task simply does not compete for a builder/reviewer
slot while in this state, the same way `DONE` or `BLOCKED` don't today.

## Where CI gets observed

New module: `build_coordinator/runner/ci_reconciliation.py`.

```python
def poll_external_ci(session, *, repo: str, sha: str) -> CIObservation:
    """Query `gh pr checks <sha> --repo <repo> --json ...` (or
    `gh api repos/{repo}/commits/{sha}/check-runs` if no PR exists yet for
    the SHA) and return a small typed result: PENDING / SUCCESS / FAILURE,
    with the raw check list for evidence."""
```

This mirrors `task_source/github.py`'s existing pattern: a `gh` subprocess
call, a `client` injection point for tests, explicit `RuntimeError` on
`gh` absence/auth failure (never silently treated as PENDING forever).

A new orchestrator method, `_reconcile_awaiting_ci(session)`, runs once per
`continue` cycle (same cadence as the existing dispatch loop):

1. For every task in `AWAITING_EXTERNAL_CI`, look up the exact SHA that was
   pushed (recorded on the `INTEGRATION` execution's `result_data`, already
   captured today — see `upstream.py`'s `merge_commit_sha` field).
2. Call `poll_external_ci(session, repo=..., sha=that_sha)`.
3. On `SUCCESS`: `transition_task(..., "DONE")`, record a
   `runner.external_ci_reconciled` event with the check evidence.
4. On `FAILURE`: `transition_task(..., "REWORK_REQUIRED")`, record the
   failing check names/URLs so a human or a future remediation worker has
   the actual evidence, not just "CI failed."
5. On `PENDING`: no transition; task stays in `AWAITING_EXTERNAL_CI`. This
   is the work-conserving path — the cycle moves on to dispatch other
   READY tasks instead of blocking here.
6. If `gh` itself errors (not "checks pending" but genuinely unreachable)
   more than N consecutive cycles (config, default small), transition to
   `BLOCKED` with a clear `human_escalation_type` rather than polling
   forever silently.

**Same-SHA guarantee**: step 1 always re-reads the SHA from the durable
`INTEGRATION` execution record, never from a live branch tip, so a later
unrelated push to the same branch cannot cause `AWAITING_EXTERNAL_CI` to be
satisfied by the wrong commit's CI result. If the branch has moved (e.g. a
remediation cycle already re-pushed), the observation is compared against
the exact recorded SHA and treated as `PENDING`/stale otherwise.

## Where capacity actually gets released

Nothing new is needed here beyond removing `AWAITING_EXTERNAL_CI` from
`CLAIMABLE_STATES`. The existing dispatch loop (`orchestrator.py`'s
`_dispatch_builders`) already only ever launches workers for tasks in
`CLAIMABLE_STATES`; a task sitting in `AWAITING_EXTERNAL_CI` simply never
enters that set, so it holds zero worker/builder/reviewer capacity by
construction — the same mechanism that already lets `DONE` and `BLOCKED`
tasks not consume capacity today. Independent READY tasks are scheduled
exactly as they are now; this slice does not need new prioritization logic
beyond what `_dispatch_builders`'s existing P0-first sort already does.

## Config surface

```yaml
execution:
  external_ci:
    enabled: true            # default false: opt-in, no behavior change
                              # for projects that don't set this
    repo: "org/repo"         # falls back to task_sources.github.repo
    poll_interval_cycles: 1  # check every `continue` cycle by default
    max_consecutive_errors: 5
```

When `external_ci.enabled` is false (the default), `INTEGRATING -> DONE`
behaves exactly as it does today — this is a strictly additive, opt-in
change. This matters for StageMesh's own project (and any existing
project.yaml) not to silently change behavior on upgrade.

## Test plan for this slice

- `policy.py`: new transition table entries, existing
  `test_policy`-equivalent tests extended for the two new edges and that
  `AWAITING_EXTERNAL_CI` is absent from `CLAIMABLE_STATES`.
- `ci_reconciliation.py`: unit tests with an injected fake `gh` client
  (mirrors `GitHubTaskSource`'s `client=` injection) covering PENDING,
  SUCCESS, FAILURE, and unreachable-`gh` paths — no real network/CI calls
  in the test suite.
- Orchestrator integration test (in the style of
  `tests/test_project_backlog.py`'s `test_continue_recovers_*` tests):
  a task reaches `INTEGRATING`, is transitioned to `AWAITING_EXTERNAL_CI`
  with a fake CI client returning PENDING, a second independent READY task
  is dispatched and completes in the same `continue` invocation (proving
  capacity was not held), then the fake CI client is switched to SUCCESS
  and a subsequent cycle reconciles the first task to `DONE`.
- A same-SHA regression test: after `AWAITING_EXTERNAL_CI`, simulate a
  stale/mismatched SHA being reported and assert no transition occurs.

## Open questions before implementation

1. Should `AWAITING_EXTERNAL_CI` be entered only when `external_ci.enabled`
   AND an actual GitHub PR/push happened (i.e. `upstream.push: true` in
   project config), or is there a scenario where a project wants CI-gating
   without StageMesh's own push? (Current lean: require both — CI-gating
   only makes sense once StageMesh has actually pushed something for CI to
   run against.)
2. Confirm `gh pr checks <sha>` vs `gh api .../check-runs` is the right
   query — `pr checks` requires an open PR for the branch; `check-runs` by
   commit SHA works even before a PR exists. Current lean: use the
   check-runs API by SHA, since StageMesh's push may land before any PR is
   opened.
