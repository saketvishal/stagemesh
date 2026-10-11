# run-window

`stagemesh run-window` executes an unattended, bounded work window in PR mode. It wraps the survivor-wave execution engine in automatic task selection and strict safety checks designed for hands-off operation.

## Purpose

`stagemesh run-window` provides an unattended bounded work window in PR mode:
- **Automatic task selection:** Automatically selects runnable tasks (`OPEN`, not `DONE`, eligible under the scheduler, not parked, not blocked, not waiting on dependencies or an objective root, with no active claim or running execution).
- **Time-bounded operation:** Runs work within a bounded time duration. When the time budget expires, executing tasks finish within their step budget, but no new tasks are started (clean stop).
- **Safe PR integration:** Integrates finished work onto `wave/staging` and opens pull requests against `main` for completed tasks without moving local `main`.
- **Never merges:** The command never merges PRs. Merging remains an explicit operator action.

## Options

| Option | Default | Description |
| --- | --- | --- |
| `--duration` | `2h` | Time budget for the window, e.g. `2h`, `90m`, `45s`, `1.5h`, or bare seconds. Running tasks finish within their step budget; nothing new starts after it. |
| `--concurrency` | `2` | Number of tasks running concurrently. |
| `--max-steps-per-task` | `8` | Step budget for each task. Tasks reaching this cap remain `OPEN` and marked `resumable`. |
| `--max-tasks` | `10` | Maximum number of tasks to attempt during the window. |
| `--tasks` | All eligible | Comma-separated task IDs (e.g. `155,156`) to restrict the window to specific tasks instead of automatic selection. |
| `--integration pr\|local` | `pr` | Integration mode: `pr` (default) lands work on `wave/staging`, opens PRs, and leaves local `main` untouched; `local` lands work directly on the integration branch (moving local `main`) without opening PRs. |
| `--no-pr` | Off | In PR mode, do not open PRs for tasks reaching `DONE` (work remains on the staging branch). |
| `--strict-live` | Off | Release mode check: refuse to start unless providers have fresh passing live acceptance test results. |
| `--allow-stale-runtime` | Off | Development bypass: allow starting even if the installed CLI runtime lacks commands present in source. |
| `--plan` | Off | Read-only mode: run preflight checks and show which tasks would be picked or left alone without starting any work or recovering executions. |
| `--report` | Auto-generated | Path to write the JSON report (defaults to `.stagemesh/reports/window-<timestamp>.json`). |

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

`stagemesh run-window` never merges pull requests.

In PR mode:
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
