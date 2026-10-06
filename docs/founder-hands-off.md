# Founder Hands-Off

This is the single canonical document for StageMesh's Founder Hands-Off capability: what the gate is, what exists, what is missing,
which incident scenarios are enforced by tests, and how close StageMesh is. There is no other roadmap for this work; update this file.

## The gate

The founder provides only `Implement objective X.` StageMesh then handles, without further operational input:

`objective → scope/dependency analysis → implementation → validation → independent review → remediation → CI diagnosis →
PR/dependency management → integration → post-merge verification → next eligible task`

**The gate is met only when StageMesh has completed 10 real development tasks consecutively** without the founder giving operational
instructions about branches, commits, worktrees, rebasing/merging, candidate SHAs, CI failures, review cycles, PR dependencies, stale
branches, ordinary base advancement or recovery from agent failures. Escalation is allowed only for a genuine product, security or
destructive decision that cannot safely be inferred, and only as a typed `EscalationReason` carrying (1) what was tried, (2) why the
safe answer cannot be determined and (3) the smallest decision required. Generic questions ("should I rebase?", "CI failed, continue?")
are rejected at construction time.

## Status

| | |
|---|---|
| **Founder Hands-Off gate** | **NOT MET** |
| Ten-task autonomy streak | **0 / 10** (no real task has been run under the supervisor yet) |
| Capability readiness | **55%** (level model below; 3/3 requires proof on real tasks, which no capability has) |
| Incident scenarios with permanent regression tests | **12 of 12** (A to L), 34 mapped tests, all passing |
| First milestone (A, B, C, E/F) | implemented and passing end to end through the real coordinator |
| Human escalations observed on real tasks | **0** (no real tasks yet) |

Readiness is computed from `CAPABILITY_LEVELS` in `src/stagemesh/autonomy/corpus.py`
(`stagemesh autonomy readiness`). Levels: 0 absent, 1 deterministic policy with unit tests, 2 wired into the real lifecycle and
covered end to end locally, 3 additionally proven on real tasks against live systems. The percentage is `sum(levels) / (3 × 14)`.

## How it works

LLMs implement, review and may help diagnose. Every safety-critical workflow decision is made by deterministic policy in
`src/stagemesh/autonomy/` and recorded as a typed `AutonomyDecision` (condition, policy, action, SHAs, escalation) in the audit log:

```
BASE_HISTORY_REWRITTEN old_base=141d756 new_base=970efeb tree_equivalent=true original_candidate=4adfd88 replacement_candidate=<sha>
    policy=base-state/v1 action=CREATE_RETARGETED_CANDIDATE human_escalation=false
```

| Component | Module | Role |
|---|---|---|
| `AutonomyDecision`, `EscalationReason`, trace, ledger | `decisions.py` | typed decisions, escalation contract, durable trace, ten-task streak |
| `CandidateProvenance` | `provenance.py` | baseline / candidate / validation / review / integration SHAs and lineage; evidence authorizes only the exact candidate |
| `WorkspaceOwnership` | `provenance.py`, `supervisor.py` | detects HEAD, tracked-file, candidate-ref and remote-ref changes nobody registered; quarantines, never adopts |
| `BaseState` | `base_state.py` | classifies unchanged / advanced / history rewritten (tree-equivalent or not) / dependency landed; builds replacement candidates with equivalence proof |
| `CIDiagnosis` | `ci_diagnosis.py` | compares candidate CI with base CI per gate; separates test defects from production defects |
| `PRDependency` | `dependencies.py` | stacked PR blocking and automatic resumption |
| `ReviewState` | `review_policy.py`, `review_adapter.py` | blocking vs non-blocking vs unrelated; scope policy around the LLM reviewer |
| `TaskScope` | `scope.py` | allowed / forbidden files, objective, acceptance criteria, deferred work ledger |
| `IntegrationPolicy` | `merge_policy.py` | every merge condition, in a fixed precedence; post-merge verification before DONE |
| Recovery policy | `recovery_policy.py` | UNKNOWN process identity is never treated as dead; destructive git requests get a provenance-preserving replacement |
| Isolation guard | `isolation.py` | fails closed if any runtime path, git store, database or running code belongs to another StageMesh checkout |
| `Supervisor` | `supervisor.py` | the facade that observes, applies the policies, acts non-destructively and records |
| Wiring | `wiring.py`, `hooks.py`, `integration.py`, `cli.py` | opt-in integration with the existing coordinator, executors, integrator and CLI |
| GitHub boundary | `github_adapter.py` | pull requests, check runs, base retargeting, head-pinned merge on the existing `GitHubTransport` |

Reused unchanged: the state machine and evidence model (`coordinator.py`, `lifecycle.py`), exact-SHA evidence and change contracts,
`SerializedIntegrator` (subclassed), provider pools and independent-review verification, failure diagnosis, process-identity
classification, worktree management and the runtime-root safety checks.

Safety properties that hold by construction:

* The original candidate is never rewritten or force-pushed. Replacement candidates are new SHAs; the original is preserved under
  `refs/stagemesh/preserved/...`, and a replacement never inherits its original's validation or review.
* Refs are only ever created, never moved (`update-ref` against the null SHA); a second writer's work is preserved under
  `refs/stagemesh/quarantine/...` and is never merged into a candidate.
* A merge request names the exact validated head SHA; if the PR head moved, the host refuses and nothing is merged.
* DONE requires the expected content on the integration ref and the post-merge checks to pass.
* Every unknown (identity, base CI, mergeability, dependency state) fails closed.

## Enabling it

A project opts in with `.stagemesh/autonomy.json`; without it there is no behavior change.

```json
{ "enabled": true,
  "trusted_committer_emails": ["stagemesh@stagemesh.invalid"],
  "max_reconstructs": 1,
  "allow_baseline_ci_failures": true,
  "unknown_identity": "FENCE" }
```

When enabled, `continue` / `queue-run` verify isolation first and refuse to start if it fails, run the supervisor as the coordinator
guard, replace the integrator with `SupervisedIntegrator`, and scope the reviewer. An invalid file refuses to run (it never silently
runs unsupervised). Commands: `stagemesh autonomy isolation | trace | streak | readiness | record-outcome`. List other StageMesh
checkouts this one must never touch in `.stagemesh/isolation.json` (`{"forbidden_checkouts": [...]}`) or
`STAGEMESH_FORBIDDEN_CHECKOUTS`; sibling StageMesh checkouts are also detected heuristically.

## Capabilities

| # | Capability | Level | Where it acts today |
|---|---|---|---|
| 1 | Candidate integrity | 2 | coordinator guard: no integration without validation and review bound to the exact candidate |
| 2 | External workspace mutation | 2 | hooks in the executors; quarantine and restore; remote branch moves via fetch |
| 3 | Ordinary main advancement | 2 | `SupervisedIntegrator` refreshes the candidate and returns the task to VALIDATE |
| 4 | History rewrite detection | 2 | retargeted replacement with tree-equivalence proof and lineage |
| 5 | PR dependency / stacked PR handling | 1 | policy, supervisor entry points and GitHub adapter; nothing schedules them yet |
| 6 | CI diagnosis (candidate vs base) | 1 | gates integration when a `HostedCI` is supplied; the CLI does not configure one yet |
| 7 | Broken/fragile test detection | 1 | classification and remediation guard; no test harness emits observations yet |
| 8 | Independent review lifecycle | 2 | scope policy wraps the reviewer adapter in the CLI |
| 9 | Scope discipline | 2 | deferred-work ledger, scoped review (contract enforcement already existed) |
| 10 | Merge policy and post-merge verification | 1 | local-ref path verifies before DONE; the PR merge flow is library-only |
| – | Isolation guard | 2 | enforced by the CLI whenever the supervisor is enabled |
| – | Decision trace and escalation contract | 2 | every decision persisted; escalations typed and validated |
| – | Unknown process identity recovery | 2 | the coordinator fences an UNKNOWN execution onto a replacement worktree |
| – | Destructive git operation policy | 1 | policy and preservation; no git wrapper routes requests through it yet |

## Incident corpus

Each scenario is an executable regression test with a success path and a fail-closed path (`src/stagemesh/autonomy/corpus.py` lists the
exact tests; `tests/test_autonomy_corpus.py` verifies they exist and that this table matches).

| ID | Scenario | Capability | Expected behavior (enforced) |
|---|---|---|---|
| A | Unexpected second writer | external workspace mutation | detected before the candidate is committed; preserved under a quarantine ref; workspace restored; nothing adopted; next attempt succeeds |
| B | Normal main advancement | ordinary main advancement | refreshed onto the new tip; new SHA validated and independently reviewed again; integrated; conflicts are reconstructed once, then escalated with a specific question |
| C | Equivalent-tree history rewrite | history rewrite detection | recognized as rewrite; original preserved; clean replacement on the rewritten base; tree equivalence proven; re-evidenced; lineage recorded |
| D | Stacked PR | PR dependency handling | PR #2 blocks while #1 is open; after #1 lands (including squash) it is retargeted, refreshed and resumes |
| E | Base CI already red | CI diagnosis | identical gate failures are baseline failures, recorded as deferred, not remediated; replayed against the real recorded check runs of main |
| F | Candidate introduces a new failure | CI diagnosis | blocked and remediated with an instruction not to touch baseline-red gates; bounded, then a typed escalation |
| G | Incorrect CI/test fixture (NO_IMPLEMENTATION_CHANGE) | broken/fragile test detection | a fixture expecting success from a no-op provider is a test defect (production correctly returns `NO_IMPLEMENTATION_CHANGE`); production owner files are protected from the fix |
| H | Review causes remediation | independent review lifecycle | remediation → new SHA → validation → new independent review; only the re-reviewed SHA lands |
| I | Unrelated reviewer suggestion | scope discipline | recorded as deferred; candidate scope unchanged; no second candidate |
| J | Unresolved dependency | PR dependency handling | red or unmergeable dependency blocks the downstream task, which resumes by itself |
| K | Known versus unknown process identity | recovery policy | UNKNOWN is never released or marked dead; fenced onto a replacement worktree while the old one is preserved |
| L | Destructive git operation request | recovery policy | replacement branch or preserved snapshot; escalation only for the integration ref itself |

## Remaining gaps (the one list)

Ordered by value for reaching the gate:

1. **No real task has run under the supervisor.** Every claim above is proven on deterministic local fixtures and, for the GitHub
   boundary, on recorded and read-only live payloads. The ten-task streak is the only thing that can raise any capability to level 3.
2. **No autonomous driver loop.** The coordinator drives the local-ref path; nothing yet polls PRs, fetches CI, evaluates dependencies and
   calls `merge_when_ready` on a schedule, and `continue` does not select a next task after a verified DONE. (`finish_task` records the outcome.)
3. **Hosted CI is not wired into the CLI.** `GitHubHostedCI` exists and is verified read-only against live GitHub, but no configuration attaches
   it to the integration guard. Check runs carry no logs, so baseline comparison is gate-level (`GATE_LEVEL` evidence, reported in the
   trace; `baseline_requires_detail` can forbid it). Job-log retrieval is not implemented.
4. **Test-observation convention has no producer.** Scenario G relies on `STAGEMESH_TEST_OBSERVATION` lines; no StageMesh test fixture emits them yet.
5. **Destructive git requests are not intercepted.** `decide_git_operation` is policy only; StageMesh's own git calls are not routed through it.
6. **PR merge flow is not exposed in the CLI** (library entry point `Supervisor.merge_when_ready`).
7. **`recover_unknown` runs only when a task needs implementation**; there is no periodic sweep of UNKNOWN reviews/validations.
8. Escalations are persisted and shown in the trace, but there is no push notification to the founder.
9. `Coordinator.recover` and `recover-stale --release-unknown` remain available as manual operator tools and are outside the supervisor.
10. **Providers that commit on their own fail closed.** Commits are trusted only by committer identity (`trusted_committer_emails`; default
    the StageMesh identities). A provider CLI that commits itself with its own identity is treated as an external writer until its
    email is listed. This is safe but would stall a hands-off run; the right fix is to learn the provider's identity from the adapter.
11. **Refresh budget.** Supervised runs allow at least 5 automatic refreshes (`SUPERVISED_MIN_REFRESH_ATTEMPTS`); a ref that advances
    more often still ends in the typed `integration_stale_base` block and needs `retry-task`.
12. **Supervisor settings are read from `.stagemesh/autonomy.json` only**; there is no `config.json` section or CLI flag yet.

## Baseline failures observed (deferred, deliberately not fixed here)

* `main` hosted CI is red on `linux` and `windows` (StageMesh CI): `invariants`, `clean_acceptance` (`doctor missing ['schema version: 3']`)
  and `acceptance` fail because the schema-version expectation was not updated after the v4 migration. This predates this work and is
  outside its scope; it is the real instance of scenario E.
* A global editable install makes `import stagemesh` resolve to a different checkout (`C:\stagemesh-vnext`) unless `PYTHONPATH=src`; the
  isolation guard now refuses supervised runs in that state (`RUNNING_CODE_FROM_OTHER_CHECKOUT`).
* `stagemesh init --register` defaults to the shared global registry `~/.stagemesh/registry.json`; the isolation guard reports a
  registry outside the checkout as `SHARED_REGISTRY`.

## Human escalations observed

None on real tasks. This section records every escalation raised during the streak: date, task, typed reason, the specific question,
and the founder's answer. An escalation that is not one of the typed reasons, or that asks an operational question, is a defect.

| Date | Task | Reason | Question | Answer |
|---|---|---|---|---|

## Ten-task autonomy streak

Append one row per real task, after verified DONE, using `stagemesh autonomy record-outcome`. `Interventions` counts operational
instructions the founder had to give; any non-zero value resets the streak. `tests/test_autonomy_corpus.py` refuses a document that claims
the gate is met without ten consecutive clean rows.

<!-- streak:begin -->
| # | Task | Date | Interventions | Verified DONE |
|---|---|---|---|---|
<!-- streak:end -->
