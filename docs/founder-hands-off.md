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
| Ten-task autonomy streak | **0 / 10** recorded (11 later local completions audited; clean streak remains unverified, see 2026-10-26 update) |
| Capability readiness | **62%** (level model below; 3/3 requires proof on real tasks, which no capability has) |
| Incident scenarios with permanent regression tests | **12 of 12** (A to L), 34 mapped tests, all passing |
| First milestone (A, B, C, E/F) | implemented and passing end to end through the real coordinator |
| Human escalations observed on real tasks | **UNKNOWN** (operator attribution is incomplete; see 2026-10-26 update) |

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
| `WorkspaceOwnership` | `provenance.py`, `supervisor.py` | detects HEAD, tracked-file, candidate-ref and remote-ref changes nobody registered; quarantines, never adopts. Trust model: StageMesh registers its own candidate commits (`candidate_committed`); the worktree HEAD may otherwise advance only by commits from explicitly listed provider emails, and candidate and remote refs only move by registration, whoever commits. A committer identity the worktree declares for itself is never trusted by default |
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
  "trusted_committer_emails": [],
  "max_reconstructs": 1,
  "allow_baseline_ci_failures": true,
  "baseline_requires_detail": true,
  "unknown_identity": "FENCE",
  "code_checkout": null }
```

When enabled, `continue` / `queue-run` verify isolation first and refuse to start if it fails, run the supervisor as the coordinator
guard, replace the integrator with `SupervisedIntegrator`, and scope the reviewer. An invalid file refuses to run (it never silently
runs unsupervised). Commands: `stagemesh autonomy isolation | trace | streak | readiness | record-outcome | deliver`. `deliver --task T` publishes the validated, independently reviewed candidate as a branch and PR (never force-pushing), waits a bounded time for hosted CI, compares it with base CI, evaluates the merge policy and writes a report; it merges only with `--merge` and only when every policy condition holds. `code_checkout` names the checkout whose StageMesh code must be running when StageMesh develops another StageMesh checkout. List other StageMesh
checkouts this one must never touch in `.stagemesh/isolation.json` (`{"forbidden_checkouts": [...]}`) or
`STAGEMESH_FORBIDDEN_CHECKOUTS`; sibling StageMesh checkouts are also detected heuristically.

## Capabilities

| # | Capability | Level | Where it acts today |
|---|---|---|---|
| 1 | Candidate integrity | 2 | coordinator guard: no integration without validation and review bound to the exact candidate |
| 2 | External workspace mutation | 2 | hooks in the executors; quarantine and restore; remote branch moves via fetch |
| 3 | Ordinary main advancement | 2 | `SupervisedIntegrator` refreshes the candidate and returns the task to VALIDATE |
| 4 | History rewrite detection | 2 | retargeted replacement with tree-equivalence proof and lineage |
| 5 | PR dependency / stacked PR handling | 2 | `run-window` schedules eligible dependency work and delivery follow-through rechecks pending PRs |
| 6 | CI diagnosis (candidate vs base) | 2 | gates integration when a `HostedCI` is supplied, and runs in `autonomy deliver` against hosted check runs |
| 7 | Broken/fragile test detection | 1 | classification and remediation guard; no test harness emits observations yet |
| 8 | Independent review lifecycle | 2 | scope policy wraps the reviewer adapter in the CLI |
| 9 | Scope discipline | 2 | deferred-work ledger, scoped review (contract enforcement already existed) |
| 10 | Merge policy and post-merge verification | 2 | `autonomy deliver` evaluates the policy (merge only on `--merge`); verification precedes DONE |
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
2. **The executable driver exists but is not live-proven.** `run-window` now selects and advances multiple eligible tasks, continues
   through guarded PR delivery/landing when explicitly authorized, and delivery follow-through rechecks pending PRs. The opt-in
   Windows-local `stagemesh driver` supervisor adds single-instance scheduled ticks, durable crash checkpoints, bounded failed-tick
   suspension and status/pause/resume/stop. Deterministic fixtures are not a real provider run, a real Task Scheduler round trip or a
   ten-task streak; no persistent driver was installed while implementing this slice.
3. **Hosted CI observation is on-demand.** `GitHubHostedCI` is used by `autonomy deliver` and verified read-only against live GitHub, but the
   integration guard of `continue` does not consult it. Check runs carry no logs, so baseline comparison is gate-level (`GATE_LEVEL` evidence, reported in the
   trace) and, by default (`baseline_requires_detail: true`), is not enough to call a candidate merge-ready: the policy asks for log-level base
   evidence instead. Job-log retrieval is not implemented.
4. **Test-observation convention has no producer.** Scenario G relies on `STAGEMESH_TEST_OBSERVATION` lines; no StageMesh test fixture emits them yet.
5. **Destructive git requests are not intercepted.** `decide_git_operation` is policy only; StageMesh's own git calls are not routed through it.
6. **Reruns cannot be requested.** A failure classified as infrastructure is retried only by waiting for a new observation; StageMesh cannot yet ask the host to rerun failed jobs (the bounded budget ends in a typed escalation).
7. A scheduled driver tick runs `run-window` preflight across active executions and automatically recovers only identities proven DEAD.
   UNKNOWN identities intentionally remain a typed operator inspection gate; there is no unsafe periodic release.
8. Escalations are persisted and shown in the trace, but there is no push notification to the founder.
9. `Coordinator.recover` and `recover-stale --release-unknown` remain available as manual operator tools and are outside the supervisor.
10. **Providers that commit on their own fail closed.** StageMesh registers its own commits; a provider CLI that commits itself is treated as
    an external writer until its email is listed in `trusted_committer_emails`. This is safe but would stall a hands-off run; the right fix is to learn the provider's identity from the adapter.
11. **Refresh budget.** Supervised runs allow at least 5 automatic refreshes (`SUPERVISED_MIN_REFRESH_ATTEMPTS`); a ref that advances
    more often still ends in the typed `integration_stale_base` block and needs `retry-task`.
12. **Supervisor settings are read from `.stagemesh/autonomy.json` only**; there is no `config.json` section or CLI flag yet.

## Windows verification ledger (2026-10-23 and 2026-10-24)

Labels: IMPLEMENTED (code exists), SIMULATED (fakes or Linux), LIVE_VERIFIED (real Windows, environment named), NOT_RUN.
"Hosted Windows" is a disposable GitHub-hosted `windows-latest` runner (Python 3.11 and 3.13, a bootstrap venv and a plain
editable venv; workflow `windows-triage.yml`, which runs only on `ci/windows-triage-*` branches and publishes junit and JSON
evidence to `ci-evidence/<run_id>` branches). "Owner's PC" is the founder's Windows machine (Python 3.13, git 2.43.0.windows.1).

| Claim | Status | Evidence |
|---|---|---|
| Ownership, autonomy and driver suites on a real Windows host | LIVE_VERIFIED (owner's PC) | ownership 84 passed, autonomy 67 passed, driver 19 passed and 1 failed (hard-timeout test, see below) |
| Full suite on a real Windows host | LIVE_VERIFIED (owner's PC) | 2280 passed, 14 failed in 93 minutes; classified below; not re-run in full |
| Task Scheduler registration from XML (logon trigger, repetition, StartWhenAvailable, battery-safe, IgnoreNew, execution limit) | LIVE_VERIFIED (hosted Windows) | `schtasks /Query /XML` round trip returned every setting, both Pythons |
| `/Run` starts the tick; a second `/Run` while running starts nothing | LIVE_VERIFIED (hosted Windows) | `scheduler_started_the_task`, `overlap_prevented_ignore_new` |
| `/End` and `ExecutionTimeLimit` end the task's own process only, never descendants | LIVE_VERIFIED (hosted Windows) | grandchild survived both; with a `cmd.exe` launcher the worker survived too, so the tick is now the task's direct process |
| Temporary tasks always removed | LIVE_VERIFIED (hosted Windows) | `cleanup_verified remaining=[]` in every run |
| `kill_process_tree` (taskkill /F /T) removes a 5-process venv tree | LIVE_VERIFIED (hosted Windows) | all four environments |
| Hard timeout clears the durable child-owner record | LIVE_VERIFIED (hosted Windows) after a fix | defect: on a venv the parent compared its Popen identity (launcher stub) with the identity the wrapper recorded (real interpreter), so the record was never cleared and later ticks would report an orphaned owner. `clear_dead_child_owner` now clears a record whose process is proven dead. Fails before, passes after, in all four environments |
| Full driver lifecycle through the real Task Scheduler with MOCK providers (install, scheduler-launched repeated ticks completing tasks with independent review, pause makes zero provider calls, resume continues, one run-window at a time, whole-tree kill then automatic recovery, stop, uninstall, no process or task left) | LIVE_VERIFIED (hosted Windows, py3.11 and py3.13, run 40121844078 on commit 9b12ac4, workflow `windows-triage.yml` job `lifecycle`) | `failures: []` in both; 5 mock tasks DONE once each; after the kill no mock provider process survived; the kill took out the tick, the driver child wrapper and the run-window together; the next scheduled tick audited `recovered_after_crash` and finished the next task; `schedule_exists: false` after stop; no `StageMesh` task remained. Mock providers only: no model, no network, no real provider |
| Automatic recovery when wrapper and run-window are both proven dead | LIVE_VERIFIED (hosted Windows) and SIMULATED (unit) | owner record now names the child; no human `resume` needed after a tree kill or reboot-like loss; a dead wrapper with a live or unrecorded child still fails closed |
| Logon trigger firing after a real sign-in, reboot, sleep catch-up and battery behavior on the owner's PC | NOT_RUN | the XML is registered and read back on hosted Windows; a reboot or sign-in cannot be exercised on a hosted runner |
| Provider login-state and API-billing guard (`stagemesh provider-auth`, enforced by `driver install` for real providers) | IMPLEMENTED and SIMULATED (unit tests with fake CLIs) | classification of `claude auth status` and `codex login status`; billing variables reported by name only; `codex login status` output format is undocumented, so unrecognised output fails closed. LIVE_VERIFIED only after the native run reports it on the owner's PC |
| Persistent sandbox installer (`install-handsoff.ps1`) and ten-task textkit sandbox | IMPLEMENTED (sandbox specs verified against reference solutions); installer LIVE_VERIFIED on the owner's PC (2026-10-24, commit 318b093): gates passed, task registered | see `docs/windows-driver-setup.md` |
| Real coding-provider execution and cross-provider review | LIVE_VERIFIED for one task (SANDBOX-02: codex implemented, claude reviewed independently, DONE, integrated locally) | first live window also showed two defects, fixed in the sandbox config and `install-handsoff.ps1`: built-in `grok` and `agy` were eligible reviewers (outside the approved providers) and the Codex CLI default model `gpt-5.5` is rejected for ChatGPT sign-in; the installer now writes `routing.pools` limited to codex and claude and pins `-m gpt-5.6-terra` (parameter `-CodexModel`) |
| Real GitHub lifecycle on a disposable repository | NOT_RUN | no repository created |
| Ten-task streak | NOT VERIFIED (0 of 10 formally recorded) | 11 later local completions audited; attribution/runtime gaps remain; gate not met |

### The 14 full-run failures, re-run on pre-driver main (726a74d) and the driver in four hosted environments

| Class | Tests | Evidence |
|---|---|---|
| Demonstrated regression (fixed) | `test_hard_timeout_terminates_a_real_subprocess_tree` | fails only on the driver, original test unmodified; passes after the fix |
| Demonstrated baseline defect (identical on main and driver) | review budget exhaustion recovery ladder, failed integration preserves candidate and worktree, python gate runtime and candidate src, continue step duration, task doctor no-progress path, both `test_queue_reconcile_*`, review clone failure message, noop after failed review | same failure on both trees in every environment; not caused by the driver; not root-caused here |
| Demonstrated environment issue on the owner's PC | both `test_git_identity_policy` tests, `test_terminate_stop_reaps_provider_tree` child-cli case, `test_windows_interruption_cannot_inherit_source_readiness_marker` | pass on both trees in every hosted environment |
| Unresolved | owner's-PC variant of the hard-timeout failure (sleeping grandchild alive 10 s after the kill) | does not reproduce on hosted Windows |

Correction: an earlier reading counted the triage step's `success` conclusion as passing tests. That step is `continue-on-error`;
the junit files are the evidence, and 10 of the 14 did fail there.

## Local continuous driver evidence (2026-10-23)

Implementation branch: `feat/local-continuous-driver`, based on `main` at `726a74de6054f6e0e2d1bb90b73ffbc764fd1fda` (`v0.4.0`).
Implementation commit: `18d0eb2e7621bd93b64a1f968da970124b29806b`.

Focused command:

```powershell
$env:PYTHONPATH='src'
python -m pytest tests/test_continuous_driver.py tests/test_driver_acceptance.py tests/test_autonomy_corpus.py -q
python -m pytest tests/test_bounded_execution.py tests/test_builtin_execution_ownership.py -q
```

Observed result: **16 driver lifecycle tests and 4 deterministic multi-task acceptance fixtures passed**. Covered: exact project-bound
`run-window` command, three dependent tasks across simulated PR boundaries with one intentional first-attempt validation failure,
parent-crash continuation and recovery with real subprocesses, pre-launch ownership, live/orphan process fail-closed behavior,
idempotent durable work,
workspace and owner-file preservation, real Windows hard-timeout process-tree termination, scheduler query failures, exact cadence,
no-work `idle` rather than autonomy success, bounded failed-tick suspension, paused/stopped no-op behavior, project-owned reversible
Task Scheduler commands and explicit provider-use acknowledgement. The shared execution ownership regression suite also passed
**23 tests** after the process-tree change.

The existing `test_run_window.py`, `test_autonomous_window.py` and `test_delivery_followup.py` milestone suite produced **143 passed**
plus one Windows sandbox-only local Git-process failure; that exact test passed when rerun outside the sandbox, for **144/144 behavior
tests passing**. Independent review initially found launch ownership, timeout cleanup, scheduler diagnostics and evidence-overclaim gaps;
all were fixed, regression-tested and accepted on re-review.

Acceptance status: **VERIFIED_COMPONENT** for deterministic local fixtures. **NOT_RUN** for real provider CLIs, real Windows Task
Scheduler execution, hosted CI/PR landing and the real ten-task streak. No service/schedule was installed and no provider credit was
consumed by this evidence.

## Baseline failures observed (deferred, deliberately not fixed here)

* `main` hosted CI is red on `linux` and `windows` (StageMesh CI): `invariants`, `clean_acceptance` (`doctor missing ['schema version: 3']`)
  and `acceptance` fail because the schema-version expectation was not updated after the v4 migration. This predates this work and is
  outside its scope; it is the real instance of scenario E.
* A global editable install makes `import stagemesh` resolve to a different checkout (`C:\stagemesh-vnext`) unless `PYTHONPATH=src`; the
  isolation guard now refuses supervised runs in that state (`RUNNING_CODE_FROM_OTHER_CHECKOUT`).
* `stagemesh init --register` defaults to the shared global registry `~/.stagemesh/registry.json`; the isolation guard reports a
  registry outside the checkout as `SHARED_REGISTRY`.

## Human escalations observed

Historical escalation attribution is incomplete; no clean streak is certified. This section records every escalation raised during the streak: date, task, typed reason, the specific question,
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


## Incident 2026-10-24: unauthorized provider dispatch (blocking safety incident)

Facts from the sandbox ledger (read only): in the first live window, 2026-10-25 00:21 to 00:25 UTC, SANDBOX-01's review fell through
from Codex to `grok` (3 runs, every answer `REVIEW_INCOMPLETE`) and `agy` (3 runs, empty or malformed answers, 9.6k to 9.8k input tokens
each, 805 to 1,524 output tokens). Accounts used and whether any billing occurred are UNKNOWN: StageMesh does not record them.
Root causes: (1) the sandbox config had no `routing.pools`, so every installed built-in CLI was a fallback; installing a CLI implicitly
authorized it. (2) the sandbox ran the built-in `codex exec` with no `--model`, so Codex's own default (`gpt-5.5`) applied, which a ChatGPT
sign-in rejects; the owner's working project pins `--model gpt-6-astra` with the full executable path and `-c features.code_mode_host=true`.
Fixes: per-project `authorization.unattended_providers`, enforced at selection, at the two spawn sites (re-read at dispatch) and on the
command itself (genuine CLI only: no relative or in-project executable, no wrapper, no custom name wrapping a real CLI), fail closed
when unattended with no allowlist; unpinned Codex model blocks unattended start; installer and upgrade copy the owner's known-good Codex
command. Evidence labels: authorization boundary IMPLEMENTED and SIMULATED (stand-in CLIs, Linux and hosted Windows); native Windows proof NOT_RUN
until the pinned upgrade runs. SANDBOX-02 is kept as historical evidence and the new clean streak starts at the upgrade.


## Mission verification update (2026-10-26)

The recorded formal streak above remains unchanged. Read-only inspection of the existing owner-PC sandbox found **11** tasks
reaching DONE after its 2026-10-25 safety-upgrade marker (SANDBOX-11, SANDBOX-01, SANDBOX-12 through SANDBOX-20). Each had
exact-candidate passing validation, an invoked independent Codex/Claude review, passing local integration and candidate ancestry
in sandbox main. Across six productive windows there were 28 provider calls (15 Codex, 13 Claude), zero Grok/AGY calls after the
marker, and 2172.585 provider seconds. Eleven completions over the 3893.775-second productive interval is **10.17 tasks/hour**;
this excludes idle time and establishes neither concurrent real coding nor hosted GitHub delivery. Tokens and billing cost are
**UNKNOWN**, since all corresponding usage events report `usage.reported=false`.

The current clean deployment, scheduled action and marker agree on source `6f90948550b7125fb45d2b570120644027975d7b`.
Historical windows did not persist their runtime source, and an operator resume after the marker lacks founder-versus-agent
attribution. Consequently these are verified local delivery observations, **not a certified clean ten-task streak**. No historical
outcome or intervention was reset, invented or credited. The sanitized per-task audit is retained in the operator's local mission ledger.

Source review also reproduced three remaining defects in that deployment: authorization could be revoked during workspace setup
but the provider still spawned; login checks could inspect a different executable on PATH than the configured stage executable;
and the scheduler parent could overwrite the wrapper's stronger child-ownership record. The candidate correction checks the exact
prepared command again before both provider spawns, probes each configured stage executable, leaves postlaunch ownership publication
to the wrapper, uses unique atomic temporary files, and preserves uncertain child ownership. Explicit unattended account/profile or
config-source switches are refused; only the documented model and `features.code_mode_host`/`model_reasoning_effort` Codex config
pins are accepted. This does **not** establish billing equivalence for ambient CLI configuration files.

These corrections are source changes pending final-head CI, independent review and protected integration. They have not replaced
the existing installed runtime or changed its schedule. Setup and stop/recovery commands are in [Windows driver setup](windows-driver-setup.md)
and [continuous driver operations](continuous-driver.md). A new live worker run or persistent installation is separate evidence.

New windows also persist `window.started.runtime`: the loaded package path, interpreter, process identity, and observed source
commit/dirty state. Source checkouts must match the package's own tracked Git root; installed wheels use embedded build metadata.
Missing or unverifiable provenance stays UNKNOWN. This is a start-time observation, not proof that editable source remained
immutable for the whole window, and it does not reconstruct historical runtime evidence.

A further interruption regression showed that a false or interrupted process-tree kill still erased the child wrapper's ownership.
The correction retains the known run-window as the sole owner before attempting interruption cleanup. False/raised kill or failed
follow-up wait leaves LIVE/ORPHANED ownership that cannot automatically authorize a new window; only confirmed tree kill plus
completed wait releases it. Forty-five affected driver tests passed. Ordinary process completion is not asserted to prove that every
descendant exited.

The broader observation interval after the safety marker contained 27 blocked, six productive and 67 idle ticks. Across that
64149.545-second interval, completion rate was **0.617 local tasks/hour**, compared with the productive-only rate above; exhausted
inventory and blocked observations are not productive work. Recorded implementation/review/validation durations were 1495.023 /
685.945 / 41.1 seconds respectively. Two review failures and one local integration failure were preserved alongside correction and
successful later candidates. Provider seconds over the productive period were 55.8%; this is not CPU utilization. The founder has
confirmed that the recorded October 25 resume was performed by an authorized agent. Historical per-window source evidence remains
unverified independently of that clarified intervention attribution.
