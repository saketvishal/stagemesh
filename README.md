# StageMesh vNext

StageMesh is a provider-neutral control plane for autonomous and multi-agent software engineering. It owns task lifecycle, durable state, validation, review, and integration while executors remain replaceable.

Initial lifecycle:

```text
PLAN -> IMPLEMENT -> VALIDATE -> REVIEW -> INTEGRATE -> DONE
```

This repository is a clean vNext implementation with SQLite persistence, deterministic recovery, exact-SHA candidates and evidence, provider-aware routing, task-source synchronization, machine-enforced change contracts, isolated task worktrees, and an invariant test suite.

Implementation agents run in per-task Git worktrees, not the shared checkout. Validation, review, and integration all bind their evidence to the exact candidate SHA; a task cannot reach `DONE` unless the candidate passes its contract, validation gates, independent review, and integration prerequisites.

## Quick Start

```bash
python -m pip install -e ".[dev]"
stagemesh init --project .
stagemesh doctor
stagemesh continue --once   # one coordinator pass; plain `continue` supervises one task to completion
stagemesh continue --parallel 3   # up to three independent tasks at once, one worktree each
stagemesh status
pytest
python scripts/invariants.py
python scripts/acceptance.py
```

Runtime state lives in `.stagemesh/stagemesh.sqlite3` by default.

## Provider pools and task isolation

StageMesh picks providers at run time instead of one fixed provider per stage. Each stage (`IMPLEMENT`, `REVIEW`) has a pool
(default `codex, claude, grok`; a `routing.stage_routes` entry is simply tried first). Set `routing.pools` to pin an explicit list:

```json
{ "routing": { "pools": { "IMPLEMENT": ["codex", "claude", "grok"], "REVIEW": ["claude", "grok", "codex"] },
              "provider_failure_cooldown_seconds": 900 } }
```

A provider is skipped (with the reason logged to stderr) when its CLI is not callable, it lacks the stage capability, it failed for
the same task and stage within the cooldown (this is how auth and quota failures are remembered), or, for `REVIEW` with
`require_independent_review`, it produced the candidate or shares its command. Failures fall through the pool automatically; if no
independent reviewer exists StageMesh refuses and lists every provider with its skip reason. Implementation always runs in an
isolated git worktree and the integration ref only advances after validation, independent review and integration pass.

### Provider registry and selection policies

`codex`, `claude` and `grok` are built-in registry entries; any other name can be added under `providers`, as a plain command string
or an object with `command`, `capabilities` (`IMPLEMENT`, `REVIEW`), optional `priority` (lower first) and `weight`:

```json
{ "providers": { "my-agent": { "command": "my-agent run", "capabilities": ["IMPLEMENT"] },
                 "reviewer-bot": { "command": "reviewer-bot review", "capabilities": ["REVIEW"] } },
  "routing": { "pools": { "IMPLEMENT": ["codex", "claude", "grok", "my-agent"], "REVIEW": ["claude", "grok", "reviewer-bot"] },
               "provider_selection_policy": "round_robin", "provider_weights": { "claude": 2, "grok": 2 } } }
```

`provider_selection_policy` ranks the *eligible* providers of a stage (after CLI, capability, cooldown and independence checks):
`priority` (default: priority, then pool order), `round_robin` (next after the provider that last answered for the stage),
`least_recently_used` (never-used first, then oldest answer) or `weighted` (lowest `(uses+1)/weight` first). The remaining providers
form the fallback chain in the same order. `STAGEMESH_<NAME>_CMD` overrides any provider command, `--provider` pins implementation,
and SINGLE_AGENT mode still uses its one provider.

See `docs/examples/grok-provider-pools.config.json` for a project config that makes Grok a first-class IMPLEMENT and REVIEW
provider while preserving independent review.

## Choosing the next task

Plain `stagemesh continue` runs the only eligible task, or ranks several and runs the best one. `--task <id>` bypasses ranking and
is how a stale task is retried; `--choose` asks on the terminal. Ranking (best first): priority label, preferred label
(`stagemesh:prep`, `prep`, `governance`, `readiness`), valid contract over one needing auto-planning, then issue number. Tasks with an
excluded label (`stagemesh:blocked`, `stagemesh:deferred`), unmet dependencies, a stale failure (failed integration or pending
remediation) or no contract that can be auto-planned are skipped. The reason is logged and returned as `selection` in `--json`.
Tune it with:

```json
{ "task_selection": { "auto_select": true, "tie_breaker": "issue_number",
                    "priority_labels": ["priority:p0", "priority:p1", "priority:p2", "priority:p3"],
                    "preferred_labels": ["stagemesh:prep"], "excluded_labels": ["stagemesh:blocked"] } }
```

## Parallel execution

`stagemesh continue --parallel N` runs up to N independent tasks at once instead of one. It cannot be combined with `--task`,
`--choose` or `--once`; tasks are picked by the same ranking as above (best first), so blocked, excluded-label, stale-failed,
dependency-blocked and unplannable tasks are never started. A task whose dependency is still running waits; it starts the moment
the dependency is DONE.

* **Isolation.** Every running task has its own worktree and its own database connection; a task that crashes or fails stops
  only itself (the run ends `PARTIAL`). By default, task worktrees live under the project-owned runtime directory
  `.stagemesh/worktrees`; a DONE task's worktree is removed.
* **Conflicts.** A candidate task is deferred while a running task conflicts with it: a shared `exclusive_resources` entry in the
  contracts (for example `"exclusive_resources": ["test-db"]`), or a `protected_files` pattern that overlaps the other task's
  allowed or protected files. Deferrals are listed under `deferred` in `--json`.
* **Providers.** Each provider runs at most `parallel.provider_max_concurrency` (default 2) things at once, or its own
  `providers.<name>.max_concurrency`; a saturated preferred provider hands work to the next one, otherwise the task waits for a
  slot. A provider failure that is not about the code (rate limit, outage, auth) puts the provider in cooldown for every task.
  Validation and review run concurrently per task.
* **Integration.** Moving the integration ref is serialized by a lock (`.stagemesh/integration.lock`; thread and process safe, and
  released by the OS if the holder dies). If the ref advanced past a candidate, it is rebased in its own worktree under the lock,
  then re-validated and re-reviewed before landing (up to `parallel.integration_rebase_attempts`, default 2). A conflicting or
  over-budget rebase leaves INTEGRATION evidence FAILED with the typed finding `integration_rebase_conflict` or
  `integration_stale_base`; the ref is never touched.
* **Output.** Human output prefixes every line with `[task-id]`. `--json` reports each task separately under `tasks` (timestamped
  `lifecycle` events, `steps`, `final`, provider events) plus `task_outcomes`, `deferred`, `skipped` and `recovered`.
* **Ctrl+C and restart.** Ctrl+C stops every task, kills the provider processes it started, fails their running executions,
  releases their claims and discards uncommitted partial edits; worktrees with committed work are kept and the next run resumes
  them (exit code 130). After a hard kill the next run releases claims whose owning process is provably dead and removes
  worktrees that belong to finished or unknown tasks.
* **Worktree root.** Configure `runtime.worktree_root` in `.stagemesh/config.json` to move task worktrees to another reviewed
  location. When that location is outside the project runtime directory, StageMesh creates a project-specific child below it so
  cleanup cannot cross into another project's task worktrees. StageMesh refuses broad roots such as the filesystem root, drive root,
  home directory or project parent, and project paths outside `.stagemesh`, unless `runtime.allow_unsafe_worktree_root` is explicitly
  set after operator review. `stagemesh doctor` shows the effective worktree root and any legacy roots it can see.
  Older worktrees from the legacy global `.sm-wt` layout are never deleted automatically; inspect and remove them manually once no
  interrupted task needs them:

```bash
stagemesh doctor
git worktree list
git worktree remove --force <legacy-worktree-path>
git worktree prune
```

```json
{ "runtime": { "worktree_root": ".stagemesh/worktrees" },
  "parallel": { "provider_max_concurrency": 2, "integration_rebase_attempts": 2 },
  "providers": { "codex": { "command": "codex exec", "max_concurrency": 3 } } }
```

### `stagemesh queue-run`

`stagemesh queue-run --concurrency N [--max-steps N] [--json]` is the strict, supervised form of `continue --parallel`, for a shared
queue. It syncs the task sources, runs the project preflight, then starts up to N tasks whose contracts do not conflict.

* **Preflight.** A project with `.stagemesh/profile.json` must pass `stagemesh project-smoke`, otherwise the whole run is refused
  (`REFUSED:preflight_failed`, exit 2, nothing started). A project without a profile has no smoke to run and relies on per-task
  contracts; the git repository and the integration target must still resolve.
* **Admission, per task (a refusal never stops the others).** No contract: refused (`missing_contract`; nothing is auto-planned).
  Uncommitted changes in the checkout at any path the task may write (its `allowed_files` minus `forbidden_files`; `.stagemesh/`
  and `.git/` excluded): refused (`dirty_working_tree`, with the paths).
* **Conflicts.** Tasks never run together when their allowed write scopes overlap (patterns are compared, so it can serialize
  tasks that would not really collide, never the reverse), or they share an `exclusive_resources` entry, or one's
  `protected_files` overlap the other's scope. Deferred tasks run after the conflicting one finishes and are listed under `deferred`.
* **Isolation and integration.** Each task works in its own worktree; validation and review run per candidate; integration to the
  target branch is one at a time under the integration lock, with rebase-and-revalidate or a typed failure when the branch moved.
* **Recovery.** Only claims whose owner process is provably dead are released; live or unknown owners are never touched.
* **Stopping.** The run ends when every selected task is DONE, BLOCKED, refused or out of steps (`--max-steps` is per task), after
  Ctrl+C, or on a global safety failure (for example the integration lock cannot be taken), which halts every task and releases
  its claims (`GLOBAL_SAFETY_FAILURE`). `--json` adds `preflight` and `refused` to the per-task lifecycle report of `--parallel`.

## Diagnosis before remediation

When a candidate fails validation, review or integration, StageMesh diagnoses the failure before it spends another implementation
attempt. It summarizes the failing evidence, compares it with the task's earlier failed candidates (shas, numbers, timings and temp
paths are ignored; reviewer wording may differ), and classifies it:

| Category | Meaning | Typical fix |
|---|---|---|
| `contract_scope` | the contract forbids or does not cover what the task needs, or its size limits are too small | widen the contract or split the task |
| `validation_gate` | a gate cannot run or is misconfigured (missing tool, timeout, no executable gate) | fix the gate or environment |
| `review_finding` | the independent reviewer keeps raising the same concern | address it, or fix the acceptance criteria |
| `implementation_defect` | a gate runs and fails on the code | read the output; the test may be wrong |
| `provider_no_progress` | the provider produced nothing, or the same tree again | check the provider, try another |
| `integration_conflict` | the integration ref moved and the candidate no longer lands | rebase or re-implement |
| `stale_baseline` | the task baseline is behind the integration ref, so other tasks' files show up in this task's diff | `rebaseline-task` |

When the same failure happens `repeat_threshold` times in a row (default 2), the task is blocked early with the diagnosis instead of
burning the remaining budget (`stopped early, contract_scope (2 identical failures): ...` in `continue`, the `diagnosis` object in
`--json`, and a `task.diagnosis_stop` audit event). A failure that differs from the previous one is progress and keeps the normal
remediation loop. `retry-task` starts a fresh comparison. Every diagnosis is also stored as a `task.diagnosis` audit event and is
passed to the next implementation attempt along with the findings.

`stagemesh diagnose --task <id> [--json] [--threshold N] [--provider NAME]` shows the same diagnosis on demand (read-only). With a
provider it also runs a separate diagnostic pass: the provider sees the recorded facts and a checkout of the candidate under the
same read-only rules as independent review, and any change it makes to the checkout is discarded as a failure. Configure it to run
automatically before each further implementation attempt:

```json
{ "diagnosis": { "repeat_threshold": 2, "stop_on_repeat": true, "provider": "claude", "dispatch": "every_failure" } }
```

A `stale_baseline` stops the task at once, and a repeated `contract_scope`, `validation_gate` or `provider_no_progress` stops it
before the implementation provider is called again: none of them can be fixed by more code. Findings are stored verbatim, shown in
`continue`, `queue-run` and `diagnose`, and quoted unabridged (with the candidate sha) in the next implementation prompt.

### Workspace ownership and external-mutation detection

Each task's worktree is written by one StageMesh execution at a time. An execution takes an exclusive lease on the worktree (an owner
file in its private git dir, so a second execution is refused) and, when it finishes, *seals* the result: the HEAD, a fingerprint of any
uncommitted files, and the candidate SHA it produced. Nothing is added to the database. The agent's own edits, and linear commits it makes
while it holds the lease, are the authorized result and become the sealed candidate.

The worktree and the candidate rows are compared with the seal before the agent starts and after it exits, before and after validation,
before and after independent review, before integration, and before a rebase rewrites the worktree. Review always runs in a throwaway
checkout of the exact candidate SHA, never in the implementation worktree. Any difference nobody authorized (a new commit, an edited or
added file, a moved HEAD, a lost lease, a candidate that is not the sealed one, validation evidence missing for this exact SHA) records an
`EXTERNAL_WORKSPACE_MUTATION` audit event (task, execution or claim, workspace, stage, expected and observed SHA, changed paths, remedy),
blocks the task and stops: no validation, review, merge or evidence acceptance happens against the unexpected state, and nothing in the
workspace is reset, adopted or committed.

To recover, inspect the workspace and either reset it to the event's `expected_sha` or remove it (`git worktree remove --force <workspace>`),
then `stagemesh retry-task --task <id>`. Worktrees created before this feature are adopted the first time they are used, but only when HEAD
is the task's recorded candidate (or baseline). Limit: git cannot tell the lease holder's agent commit from another process's commit made
*while that agent runs*; that window is bounded (exclusive lease, exact state at start, linear history, one sealed result), not closed.

### Repairing a stuck task without touching SQLite

All three commands go through the store, refuse while the task has an active claim or running execution, never delete candidates,
findings, evidence or audit events, and write an audit event (`task.contract_rebound`, `task.rebaselined`).

* `stagemesh task-doctor --task <id> [--json]` (read-only): stage/status, claims and executions, latest candidate, baseline (and
  whether it is stale), contract digest/version, latest validation failures and review findings, diagnosis, and the next command.
* `stagemesh rebind-contract --task <id> [--validate] [--force] [--reason TEXT] [--json]` re-reads
  `.stagemesh/contracts/<id>.json`, canonicalizes it and replaces the frozen contract. The stored version is always this build's
  supported version, never copied from the old row (this repairs `unsupported bound contract version: 2`). It refuses when passed
  evidence bound to the old contract would stop counting, unless `--force` (the evidence is kept). `--validate` validates the
  latest candidate and advances the task if it passes.
* `stagemesh rebaseline-task --task <id> --to <integration-ref> [--validate] [--force] [--json]` moves a stale baseline to the
  candidate's merge-base with the integration ref, removing files integrated by other work from the task diff. It refuses when the
  baseline is not stale, the candidate is already integrated, or the history is ambiguous (`--force` only overrides the ambiguous
  cases).

`queue-run` auto-plans a task that has no contract with the same deterministic path as `continue` (log line
`task <id>: auto-planned contract ...`), and still refuses it when no safe bounded contract can be derived or with `--no-auto-plan`.

`dispatch` is `every_failure` (before each further attempt), `on_repeat` (only when the failure repeats) or `never`. Without
`provider` there is no provider pass. A provider that is unavailable or fails never blocks the lifecycle; the diagnosis is still
recorded without its analysis. `stop_on_repeat: false` keeps diagnosing but spends the whole budget as before.

## Project profiles

A project that cannot be validated by guessing root-level test commands (a monorepo) ships `.stagemesh/profile.json`: task types
(prep, frontend, backend, schema, full), their gates, allowed/forbidden files and size limits, and label behavior. Auto-planning
builds contracts from it, `stagemesh profile --task <id>` shows what a task would get, and a hand-written contract still wins.
See `docs/profiles.md` and the Caventra profile in `docs/profiles/caventra.md`.

