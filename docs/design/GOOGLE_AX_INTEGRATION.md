# Google Ax integration (roadmap, not implemented)

## Status

Future research/integration objective. Google Ax is **not** a StageMesh
dependency today, and this document does not add one. It records the
integration boundary so that, once #49 evidence-driven routing has produced
enough trustworthy history, an optional experiment adapter can be built
without redesigning routing or policy.

## Goal

Allow an operator to run controlled optimization experiments (via
[Google Ax](https://ax.dev)) over StageMesh routing/policy parameters, using
evidence StageMesh already collects, while keeping Ax fully outside the core
coordinator path.

## Relationship to existing work

This builds on the evidence-driven routing work (#49) and the future
policy-learning track. It is not an independent routing engine: Ax only ever
proposes candidate parameter values for the *existing* routing/policy
mechanisms in `build_coordinator/runner/routing.py` and
`build_coordinator/policy.py`. It never bypasses them.

## Experiment inputs (read from existing evidence, no new collection path)

Candidate arms/observations draw only on data StageMesh already records:

- task/risk class (task metadata, review policy)
- provider / runtime / capability choice (`RoutingDecision.to_audit_dict`)
- latency (runner execution timestamps)
- success/failure/remediation outcomes (task state transitions, `policy.py`)
- review outcomes (`approving_reviewers`, review verdicts)
- cost telemetry, where trustworthy (provider/runtime metadata)

## Adapter boundary

```
build_coordinator/
  runner/routing.py        <- unchanged: deterministic routing, no Ax import
  policy.py                <- unchanged: enforced invariants, no Ax import
  experiments/              (new, optional)
    __init__.py             raises ImportError-friendly message if ax-platform
                             is not installed; nothing else in the coordinator
                             imports this package
    adapter.py               ExperimentAdapter protocol: to_observation(),
                              propose_arms(), record_result()
    ax_adapter.py             AxExperimentAdapter(ExperimentAdapter); only
                              module that imports `ax`
```

Rules for the boundary:

- `build_coordinator/experiments/` is imported only from an operator-invoked
  CLI/report command, never from the coordinator loop, runner, or policy
  modules.
- `ax-platform` is an optional extra (e.g. `pip install stagemesh[experiments]`),
  never a core dependency. Core coordinator code has no `import ax` anywhere.
- The adapter consumes `RoutingDecision`/task-state history already persisted
  by the coordinator; it does not add new required tables to the core schema.
  Any experiment-specific storage lives in its own optional table/namespace.

## Operator-defined search space and constraints

- The operator supplies the search space (which parameters vary, e.g.
  preference ordering, fallback thresholds, capability weighting) and hard
  constraints (e.g. "never route SECURITY_REVIEW off ADVANCED_REASONING
  workers") as adapter configuration. StageMesh ships no default search space.
- Constraints that touch security/review/permission invariants
  (`review_required`, `independent_review_required`, `SCM_WRITE` permission
  checks, capability requirements in `StageRequirement`) are not tunable
  parameters — the adapter's config schema excludes them, and any experiment
  arm that would alter them is rejected before being handed to Ax.

## Deterministic experiment/evidence identity

- Every experiment run is identified by a deterministic
  `(experiment_id, arm_id, evidence_window)` tuple, derived from the operator
  config hash plus the evidence snapshot range it was computed over — the same
  inputs always produce the same identity, so results are reproducible and
  auditable independent of Ax's internal trial bookkeeping.
- Evidence snapshots are immutable once used for a trial: re-running the same
  experiment id against the same window must reproduce the same recommendation.

## Recommendation vs. enforced policy

- An `AxExperimentAdapter` run produces a `PolicyRecommendation` (proposed
  parameter values + supporting evidence + confidence). This is a distinct
  data type from anything `policy.py` reads at runtime.
- Recommendations are never written to the live routing/policy config as a
  side effect of running an experiment. They are surfaced as a report for the
  operator to review.

## Explicit operator approval

- Applying a recommendation to live routing/policy config requires an
  explicit, separate operator action (e.g. a CLI command that diffs the
  recommendation against current config and asks for confirmation before
  writing). No automatic promotion path exists.

## Invariants Ax may never optimize through

The adapter's constraint layer hard-excludes, unconditionally:

- independent review separation (`reviewer_exclusions`, `approving_reviewers`)
- required capability/permission checks (`StageRequirement.capabilities`,
  `.permissions`, `DEFAULT_ROLE_PERMISSIONS`)
- state machine transitions in `VALID_TRANSITIONS`
- any SCM/security-sensitive worker selection

These are enforced by `policy.py` and `routing.py` regardless of what an
experiment recommends; the adapter has no mechanism to write to them directly.

## Disable / rollback

- The experiments package is optional at install time (uninstall the extra to
  remove Ax entirely) and disabled by default at runtime (no config flag
  enables it out of the box).
- An operator can disable experimentation without touching routing/policy
  code: removing or disabling the adapter configuration stops proposal
  generation; previously applied policy changes (each an explicit, approved
  config edit per above) are rolled back the same way any manual config change
  is rolled back — no Ax-specific rollback machinery is required because Ax
  never writes live config itself.

## Non-goals

- Ax is not a dependency of the core coordinator path: `pip install stagemesh`
  (no extras) must continue to work with zero Ax-related imports or files.
- This is not a new routing engine. It does not replace or duplicate
  `route_worker`; it only tunes the operator-supplied inputs to it.
