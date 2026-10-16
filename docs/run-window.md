# run-window

`stagemesh run-window` executes an unattended, bounded work window in PR mode. It wraps the survivor-wave execution engine in automatic task selection and strict safety checks designed for hands-off operation.

## Purpose

`stagemesh run-window` provides an unattended bounded work window in PR mode:
- **Automatic task selection:** Automatically selects runnable tasks (`OPEN`, not `DONE`, eligible under the scheduler, not parked, not blocked, not waiting on dependencies or an objective root, with no active claim or running execution).
- **Time-bounded operation:** Runs work within a bounded time duration. When the time budget expires, executing tasks finish within their step budget, but no new tasks are started (clean stop).
- **Safe PR integration:** Integrates finished work onto `wave/staging` and opens pull requests against `main` for completed tasks without moving local `main`.
- **Never merges:** The command never merges PRs. Merging remains an explicit operator action.

## Quick start

    stagemesh run-window

This is the whole command. It runs in PR mode for 45 minutes with concurrency 2 and 8 steps per task, never merges, does not clean
up worktrees, closes no issues (only a PR merge does), and writes a report to `.stagemesh/reports/run-window/<timestamp>.json`
(the directory is created for you). Preflight and stop rules are unchanged. Flags override only themselves, so
`stagemesh run-window --concurrency 3` changes only the concurrency.

## Options

| Option | Default | Description |
| --- | --- | --- |
| `--duration` | `45m` | Time budget for the window, e.g. `2h`, `90m`, `45s`, `1.5h`, or bare seconds. Running tasks finish within their step budget; nothing new starts after it. |
| `--concurrency` | `2` | Number of tasks running concurrently. |
| `--max-steps-per-task` | `8` | Step budget for each task. Tasks reaching this cap remain `OPEN` and marked `resumable`. |
| `--max-tasks` | `10` | Maximum number of tasks to attempt during the window. |
| `--tasks` | All eligible | Comma-separated task IDs (e.g. `155,156`) to restrict the window to specific tasks instead of automatic selection. |
| `--integration pr\|local` | `pr` | Integration mode: `pr` (default) lands work on `wave/staging`, opens PRs, and leaves local `main` untouched; `local` lands work directly on the integration branch (moving local `main`) without opening PRs. |
| `--no-pr` | Off | In PR mode, do not open PRs for tasks reaching `DONE` (work remains on the staging branch). |
| `--strict-live` | Off | Require fresh passing live acceptance results in preflight and in every child runner, for both implementation and review. Default windows still exclude providers with recorded live-check failures. |
| `--allow-stale-runtime` | Off | Development bypass: allow starting even if the installed CLI runtime lacks commands present in source. |
| `--plan` | Off | Read-only mode: run preflight checks and show which tasks would be picked or left alone without starting any work or recovering executions. |
| `--report` | Auto-generated | Path to write the JSON report (defaults to `.stagemesh/reports/run-window/<timestamp>.json`, created automatically). |

## Live Progress

While the window runs, one line per event is written to stderr and flushed immediately, for example:

```
[2m05s] task 238: started (up to 8 steps)
[2m07s] task 238: implementation provider grok (order: grok > claude)
[2m07s] task 238: implementation started
[3m09s] task 238: implementation still running (provider grok), 1m02s so far
[5m41s] task 238: candidate 3f9a1c0b2e created by grok
[5m58s] task 238: validation passed on 3f9a1c0b2e
[7m20s] task 238: review passed on 3f9a1c0b2e
[7m31s] task 238: opening PR
```

Lines come from the ledger the task's child process writes (executions, candidates, evidence, provider events); the progress reader never calls a provider and never writes. The report on stdout, human or `--json`, is unchanged. `--heartbeat SECONDS` sets how long a running step may be silent before a heartbeat line (default 60, `0` = off); `--no-progress` turns the stream off.

## Preflight Checks

Before any work begins, `stagemesh run-window` runs preflight checks and refuses to start (exit code `2`) if any required condition fails:

- **Stale installed runtime:** The installed CLI runtime is missing commands found in source (unless permitted with `--allow-stale-runtime`).
- **Damaged ledger:** The SQLite ledger fails database integrity checks (`PRAGMA quick_check`).
- **Paused queue:** Queue admission is not running (e.g. paused via queue controls).
- **Live or unknown-identity executions:** Active executions exist whose process is `LIVE` (operator must wait or terminate) or `UNKNOWN` (fails closed; requires operator verification via `stagemesh recover-stale`). Executions provably `DEAD` are recovered automatically.
- **Active claims:** Active claims exist without running executions.
- **Local main ahead of or diverged from origin:** In PR mode, local `main` must not be ahead of or diverged from `origin` (`behind` is permitted with a pull recommendation).
- **Providers that cannot implement and independently review:** The configured provider setup cannot supply both implementation and independent review capabilities, or fails live acceptance when `--strict-live` is active.
- **Unwritable report path:** The report file cannot be written to the designated `--report` location.

In PR mode, preflight also verifies that the project checkout is on the integration branch (`main`) and that `wave/staging` has not diverged from `main`.

## Stop Rules

While running, the window monitors safety invariants and stops immediately (exit code `1`) if any of these occur:

- **New orphan-recovery events:** A new orphan event is recorded in the ledger during the window.
- **The same infrastructure failure on two tasks:** Two tasks experience the same shared infrastructure failure.
- **Local main moving in PR mode:** Local `main` moves or advances while executing in PR mode.
- **An execution or claim left after a runner exited:** A runner terminates but leaves an active execution or claim behind (process cleanup failure).

## Exit Codes

| Exit Code | Name | Description |
| --- | --- | --- |
| `0` | OK | Window completed cleanly, time or task budget was reached without errors, or no eligible work remained. |
| `1` | STOPPED | Stopped early due to a stop rule violation or execution fault. |
| `2` | REFUSED | Preflight checks failed or arguments were invalid; no work started. |
| `3` | REPORT ERROR | Work completed, but the final JSON report could not be written to disk. |

## PR Merging

`stagemesh run-window` does not merge pull requests unless explicitly opted in.

In default PR-only mode (without `--auto-merge-ff`):
- Tasks landing work on `wave/staging` have pull requests opened against `main`.
- Wave PR branches are stacked across tasks and windows.
- PRs must be merged **oldest first** with a **normal merge commit** (never squash or rebase, which breaks the stacked branch history).
- After merging on GitHub, update your local checkout with `git pull --ff-only` before the next window.

## Interrupted Windows and `stagemesh doctor`

The window records lifecycle journal events (`window.started` and `window.finished`). If a run window process terminates abruptly (such as SIGINT, process kill, or machine crash), `stagemesh doctor` detects the interrupted state in its operational readiness report:

```text
operational readiness: ATTENTION
  needs attention: run-window <id> was interrupted (started, never finished)
...
last run-window <id>: INTERRUPTED (started 1.2h ago, never finished). Rerun `stagemesh run-window`: its preflight recovers dead executions and refuses, with the exact command, on any it cannot prove dead
```

Rerunning `stagemesh run-window` safely resolves the interruption: preflight automatically cleans up dead executions and resumes unfinished work where it stopped.


## Opt-in fast-forward merge and continue

The default remains PR-only. For a repository that authorizes fast-forward main
updates, explicitly opt in to serial, hosted-CI-gated landing:

```console
stagemesh run-window --duration 2h --concurrency 1 --auto-merge-ff --required-checks backend,web,contracts
```

Use `--tasks` to restrict the queue and `--max-tasks` to bound work. Each task is
implemented, validated and independently reviewed through the existing lifecycle,
then published as a PR. The window waits within its deadline for every named
hosted check and every observed check to pass on the exact candidate. The existing
supervisor merge policy still checks bound evidence, freshness, dependencies,
findings and independent review. Baseline CI failures are not waived in this mode.

The landing adapter rechecks the PR head, base, mergeability, clean tracked
checkout, queue admission and hosted results under the existing integration lock.
It proves ancestry and uses a literal-base Git lease (compare-and-swap) to push
the exact candidate to the integration branch. The lease uses Git's
`--force-with-lease=<ref>:<observed-base>` option, but history rewrites are prohibited
both by an ancestry check and mandatory server-side no-rewrite rules. A competing
push, even to an intermediate ancestor, refuses the update. It never squashes,
rebases, creates a merge commit, deletes
branches or resets work. The supervisor verifies the remote landing; local main
then fast-forwards before another task is selected. Reports separately record
these authorized main movements and the merged PRs.

This mode requires PR publishing, concurrency 1 and explicit required check names.
One pending same-repository wave PR may be resumed before new tasks when its
canonical task is DONE and has durable candidate, validation, review and staging
evidence. It must be among `--tasks` when restricted. The same supervisor policy
rechecks it; no candidate reconstruction or refresh is performed to resume it.
Multiple legacy PRs, unknown tasks or changed heads fail closed for reconciliation. A failed/missing check, changed head/base, dirty checkout,
queue pause, expired window or unverified landing retains the PR and stops the
window before dependent work. True design/security decisions remain human gates.
Review evidence must cover all repository-required checks, including relevant
visual comparisons; this option never authorizes new screenshot baselines.

Replacement task PRs use the publisher's `wave/task-<id>-<candidate[:7]>` branch
when retaining an older task branch. Both delivery and pending-PR resume accept
only that exact candidate-derived suffix or the original task branch. The full
PR head SHA, latest durable candidate and normal supervisor gates still match.


### Stopping active provider work

From the project directory, `stagemesh --project . queue-control stop --terminate-running`
requests an audited stop. Updated task workers observe it while waiting for an
implementation or review provider, verify the identity of that worker's provider,
terminate its process tree, and reap it before releasing execution ownership.
An unverified identity is retained until the process exits; it is never killed by
PID alone. Partial workspace edits and the last accepted candidate are preserved.
Cancellation records `operator_stop`, without provider fallback, remediation, or
review approval. New stages, task admission, publication, and fast-forward landing
are blocked for the stopping run.

Without `--terminate-running`, stop drains the current stage before stopping;
pause leaves active work running. A subsequent `queue-control resume` enables a
new run and cannot revive an existing stopping run. Workers already launched from
an older installed runtime must finish before upgrading; installing this version
does not retrofit cancellation into an existing process.

If cleanup cannot be verified within its bounded wait, the runner reports a
cleanup failure and keeps the execution and claim for recovery. It does not
report a successful stop or admit more work; inspect `stagemesh doctor` before
using the explicit recovery controls.

### Required hosted protection

This mode is intentionally unavailable unless GitHub exposes active repository
rulesets with a visible empty bypass list, prohibiting non-fast-forward updates to
the integration branch and prohibiting updates/deletions of published task-head
branches. Fetch-and-merge exceptions on head branches are not accepted. Unknown,
hidden, bypassable, inherited or incomplete policy refuses before task execution.
StageMesh does not install or weaken rules. Classic protection must apply to
administrators too. Server-required status checks, including their expected apps,
remain enforced for the push credential. A required-PR/merge-queue rule is not
bypassed: such repositories must keep using PR-only mode instead.
Git URL rewriting (`insteadOf` or `pushInsteadOf`) and multiple push destinations
are unsupported in this mode; they refuse before any landing write.

This assumes trusted repository administrators keep protection policy stable
during the window. StageMesh rechecks observed policy and rejects changes, but
does not claim an atomic snapshot of all GitHub metadata, approvals or CI reruns.
See [Git's explicit lease semantics](https://git-scm.com/docs/git-push) and
[GitHub's active branch rules](https://docs.github.com/en/rest/repos/rules#get-rules-for-a-branch).

A durable intent binds repository, base ref, task, PR, expected base and candidate.
Restart discovery includes unfinished intents even when a landed PR disappeared
from the open-PR list. Recovery rechecks named CI, independent review and findings;
it never treats an external merge as proof those checks passed. Reports retain
each `landing_results` entry, separating `remote_landed`, `local_synced` and a
fully verified `merged` result. An uncertain acknowledgment or blocked local sync
stops the window before dependent work and retains the journal for reconciliation.

### Validation Environment Failures

A validation gate with an established launch failure (for example, an uncaught
missing nested executable) blocks the task on its first failure. Timeouts, missing
candidate imports and missing data files still receive bounded code remediation.
StageMesh preserves
the candidate and failure output instead of spending coding attempts on tooling
the task cannot change. Ordinary test assertions still follow code remediation.
Fix the gate environment or its checked contract before retrying the task.

Live start, result and heartbeat lines identify each execution's recorded actor,
including fallback providers; the preferred provider order is not proof of which
provider is currently running. If cleanup cannot verify ownership, the window
stops and retains the execution and claim for inspected recovery.


## Interrupted PR delivery

When a PR was requested but publication fails or is stopped by a safety guard, verified work remains `INTEGRATE/OPEN` with its candidate and evidence intact. The next normal window can finish delivery through the existing integration path. An unchanged, already-integrated candidate reuses its passing evidence; a changed integration target still follows the existing promotion and verification rules.

A publication retry reuses an existing open or merged PR only when its head and base match the verified candidate. Closed, conflicting, or ambiguous PRs are not recreated. A missing opener or publication failure is reported as a stopped window, not successful delivery. Explicit `--no-pr` and local integration retain their existing behavior. Main-movement, queue-control, and live or unknown ownership guards remain in force; retained ownership is never cleared to retry publication.
