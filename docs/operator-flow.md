# Operator flow: recovering and running the backlog

## Inspect current work without opening the ledger

Use `stagemesh operator` for text, `stagemesh operator --json` for structured output, or
`stagemesh dashboard --output dashboard.html` for the same report in a static page. Existing task, worker, queue and evidence
fields remain available. These reports do not retry work, change task state or grant permission.

For bounded event history, use `stagemesh activity`. It reads the SQLite store in read-only mode and supports text or `--json`
output plus `--state`, `--task`, `--stage`, `--provider`, `--since`/`--until`, `--last 7d`,
`--period today|yesterday|week|month`, `--limit` and `--offset`. Explicit instants require a UTC offset. Named periods use local
calendar boundaries and convert them to absolute UTC instants before querying; they do not change task or execution state.

Attention marks current failed tasks as `ACTION`; superseded failures and failures on DONE tasks remain `INFO` history.
`failed_executions` retains its lifetime meaning. Recent Failures shows the latest ten failed executions with task, candidate,
provider, reason and current/history scope. Recent Efficiency And Usage samples at most 100 records of each type: provider attempts,
purpose, duration, validation checks run/reused, validation time and provider-reported usage. These samples are not lifetime totals,
CPU utilization or a measured coding-throughput rate. Missing token receipts remain unknown; billing cost remains `UNKNOWN`.

Recorded Pending Decisions lists current typed human-decision blocks and candidate-bound external delivery-permission blocks found
in the latest 100 relevant events. Cleared or obsolete-candidate records are excluded. Approval coverage remains `UNKNOWN`: an absent
record proves neither permission nor zero pending approvals. Only existing durable blockers are shown; this view creates no approval.

The normal loop has four steps. Do not hand-run individual stages (`continue --task`, `advance-baseline`, `rebaseline-task`, manual
PRs) unless `survivor-wave` reports a task as blocked and needs a decision.

## Production operation: use `run-window`, not `queue-run`

For unattended or production work use `stagemesh run-window` (bounded, preflighted, journaled; see [run-window.md](run-window.md)):

    stagemesh run-window

The defaults are safe: PR mode, 45m, concurrency 2, 8 steps per task, no auto-merge, and a report in
`.stagemesh/reports/run-window/<timestamp>.json`. Override one thing at a time, e.g. `stagemesh run-window --concurrency 3`.
The long form is equivalent: `stagemesh run-window --duration 45m --concurrency 2 --max-steps-per-task 8 --integration pr`.

`stagemesh queue-run` is legacy/raw queue execution. It can start broad queue work, retry tasks and interrupt provider work outside
those guards, so plain `queue-run` now refuses and points here. It only runs with an explicit `--legacy-unsafe`, and even then it does
not sweep worktrees unless you also pass `--sweep-worktrees`. `stagemesh queue-control resume` only opens admission; it does not make
`queue-run` production-safe.

## Queue Reconciliation

When `doctor` shows old blocked work, duplicates or decision-only items, use `stagemesh queue reconcile` before starting another
window. It syncs task sources, classifies every ledger task, and starts in dry-run mode. It does not run providers or clean worktrees.

Use explicit write flags for cleanup:

    stagemesh queue reconcile --write --park-design-decisions --unblock-retryable

`--park-design-decisions` moves tasks blocked on unresolved review findings into audited retirement so they stop looking runnable.
`--unblock-retryable` reopens blocked tasks that are not source-retired and not waiting on a design decision. Source-closed,
source-ineligible, done and already retired tasks are only reported.

## 1. Check the checkout: `stagemesh doctor`

`stagemesh doctor` ends with an **operational readiness** section. It is read-only (`--no-network` also skips the GitHub lookup).

| Line | What to do when it is not clean |
| --- | --- |
| `runtime: STALE` | Use the printed source command now (`PYTHONPATH=src python -c "from stagemesh.cli import main; main()" <command>`) and refresh the install with the printed `python scripts/bootstrap_stagemesh.py`. Nothing reinstalls automatically. The section names the installed command, the source command and the commands the install lacks. |
| `open wave PRs` | Merge them in order, oldest first (they are stacked). See step 3. |
| `local main: ahead / behind / diverged` | Do not start a wave until local `main` matches `origin/main`. |
| `running executions / active claims` | Work is in flight. Wait, or run `stagemesh recover-stale` if it is stale. |
| `worktrees: cleanable / done but not in main yet / resumable / other` | Informational. `cleanable` ones can be removed with `stagemesh worktrees sweep` (preview with `--dry-run`); `done but not in main yet` are waiting on a PR; `resumable` belong to unfinished tasks; `other` is a leftover directory with no git worktree in it. |
| `tasks needing a design decision` | These are blocked on review findings that were not resolved in the remediation budget. A person decides; the wave never retries them. |
| `provider ... COOLING DOWN` | Wait for the reset or `stagemesh cooldown clear <provider>` once it has reset. "last recorded live check" shows whether a live acceptance run ever passed for that provider. |

## 2. Run a wave: `stagemesh survivor-wave`

    stagemesh survivor-wave --tasks 155,156 --concurrency 2 --max-steps-per-task 8 --report wave.json

PR mode is the default: work lands on `wave/staging`, local `main` does not move, and each DONE task gets a PR. See
[survivor-wave.md](survivor-wave.md).

## 3. Inspect the report, then merge the PRs

Read `repo state` and each task line in the report (or `wave.json`):

- `needs_pr_merges` - PRs are open; merge them, oldest first, with a normal merge commit. Never squash: the PRs are stacked.
- `unpublished_work` - a PR was not opened (`--no-pr` or a `gh` failure); open it from the `wave/task-<id>` branch.
- `resumable` tasks hit the step cap; they are not failures. Run another wave with the same `--tasks`.
- `blocked_needs_decision` - go to the design-decision list in `doctor`.
- `main_moved_unexpectedly` or `diverged` - stop and look at `git log origin/main..main` before anything else.

After merging, pull `main`; the next wave moves `wave/staging` up to it by itself.

## 4. Confirm: `stagemesh doctor` again

Expect `operational readiness: READY` (or only the informational lines above).

## Routine failures repair themselves

A routine engineering failure is not an operator task. StageMesh classifies it, repairs it, validates and reviews the result again, and retries, within bounded budgets. Every choice is one of a fixed set of actions (`RETRY_SAME`, `REFRESH_REVALIDATE`, `REFRESH_REVALIDATE_REREVIEW`, `SWITCH_PROVIDER`, `REPAIR_WORKTREE`, `RETRY_INTEGRATION`, `BLOCK_EXTERNAL`, `BLOCK_HUMAN_DECISION`, `ABORT_UNSAFE`) made by deterministic rules in `recovery_decision.py`; an optional advisor (for example a decision model) is only asked about contexts the rules call ambiguous and cannot step outside the fixed set. Each decision is a `recovery.decision` audit event.

## Worker discipline

Every implementation provider receives a built-in discipline prompt before it edits. The rule is StageMesh-native, but borrows the useful parts of the Ponytail and pstack style:

- Make the smallest complete change that satisfies the task and its change contract.
- Identify the touched surfaces before editing: callers, tests, fixtures, config, docs, exports and migrations.
- Prefer existing project helpers and patterns before platform features, installed dependencies or new abstractions.
- Keep validation, error handling, security, accessibility, data-loss protection and requested behavior intact.
- Prove non-trivial logic with a focused test, assertion or runnable check.
- Leave out-of-contract discoveries as deferred work instead of expanding the diff.

This discipline lives in `src/stagemesh/providers.py` so it applies to Codex, Claude, Grok, Agy and any configured command provider. It is covered by `tests/test_canary_regression.py`.

StageMesh also selects [bundled agent skills](agent-skills.md) automatically from the task title, bound contract and remediation state.
Frontend tasks receive Anthropic's full `frontend-design` skill alongside the compact frontend discipline. Superpowers supplies
`systematic-debugging` for failures/remediation, `test-driven-development` for behavior changes, and `verification-before-completion`
for implementations and reviews. Documentation and simple copy/style tasks receive fewer skills. Generic backend paths such as
`app/services/cache.py` do not trigger frontend guidance. No manual chat attachment or global plugin install is required.

The installed project's guidance, referenced specs, approved UI and change contract take precedence over upstream skill procedures.
StageMesh keeps ownership of scheduling, Git and review, and requires scoped verification plus contract gates. Skills cannot authorize
a redesign, broaden scope, refresh a baseline to hide drift, or require repeated full-suite runs. Their licensed resources ship inside
the runtime wheel, so workers do not depend on a StageMesh source checkout. Missing or corrupted selected resources stop dispatch
with `skill_pack_unavailable` instead of silently dropping the guidance.

`no-mistakes` is not a replacement for this runtime discipline. Use it as an optional downstream ship gate after StageMesh has produced a committed feature branch: validate the branch in an isolated worktree, then publish the PR only if the external gate passes.

**Integration** (3 recoveries per task):

| Classification | What StageMesh does |
| --- | --- |
| `transient_integration_failure` (git/ref update trouble, no content conflict) | `RETRY_INTEGRATION`: integrates the same reviewed candidate again |
| `target_advanced_after_review` (the integration ref moved after review) | `REFRESH_REVALIDATE_REREVIEW`: replays only the task's own commits (`baseline..candidate`) onto the current ref, so commits that reached the ref by another route are not replayed; the new SHA is validated and independently reviewed before it integrates. The original candidate is kept under `refs/stagemesh/preserved/` |
| `merge_conflict_with_current_target` | `REPAIR_WORKTREE`: redoes the task on a clean worktree at the current ref; the provider is told which files conflict and to keep what landed |

**Remediation budget.** A spent budget (or an identical repeat) is not a question for a person. StageMesh first tries a different healthy provider (`SWITCH_PROVIDER`), then a narrowed attempt on the open findings only (`REPAIR_WORKTREE`), at most 2 recovery rounds. A finding that itself says a product, spec, security or legal decision is needed skips the attempts and is asked at once.

**Step cap.** A task that hits `--max-steps-per-task` while still making progress is resumed from its recorded stage, candidate and evidence in the same window (up to 3 continuations), never restarted.

Nothing is force-pushed, no gate is skipped and no test is weakened. A task blocks only when repair is unsafe or exhausted, and the block has a kind:

| Block kind | Meaning | Who clears it |
| --- | --- | --- |
| `BLOCKED_RECOVERABLE_OPERATIONAL` | recovery budget spent, provider/runtime trouble, an ordinary failure that kept coming back | `run-window` reconsiders it automatically after the StageMesh runtime or `.stagemesh` configuration changes (3 times at most per `retry-task`) |
| `BLOCKED_EXTERNAL_DEPENDENCY` | something outside StageMesh must exist first, e.g. the integration branch | fix the dependency |
| `BLOCKED_HUMAN_DECISION` | a design/spec/product/legal choice, e.g. the same file conflicts again after a redo, or a conflict reaches outside the task contract | a person answers the question shown by `stagemesh doctor` and runs `stagemesh retry-task --task <id>` |
| `BLOCKED_POLICY_SAFETY` | a workspace or ref changed outside StageMesh | never retried automatically |

`stagemesh doctor`, `task-doctor` and the run-window report show the kind, what was tried and why further automatic action is unsafe. Only `BLOCKED_HUMAN_DECISION` is listed under "tasks needing a design decision".

## If a run-window was interrupted

`stagemesh doctor` shows `last run-window ...: INTERRUPTED` when a window started and never finished (crash, kill, power loss).

Rerunning `stagemesh run-window` is safe because its preflight automatically recovers executions whose process is provably dead; executions that are LIVE are waited for, and executions with an unknown process identity fail closed and print the exact `stagemesh recover-stale --task <id> --release-unknown --execution <id> --reason ...` command to run after the operator has checked they are dead.

Nobody should open the SQLite ledger by hand.

When a review provider stops with unconfirmed process-tree cleanup, its temporary candidate checkout is retained. The raised stop reason and the `review.workspace_retained` audit event include the exact retained path. Keep that directory until the process tree is verified gone; then use the existing explicit unknown-execution recovery command and remove the retained temporary directory. Completed reviews and stops with confirmed cleanup still remove their temporary checkout.
