# run-window

`stagemesh run-window` runs an unattended, bounded work window over the queue. It is for PR-mode operation: StageMesh can keep advancing tasks, opening task PRs, and writing a window report while a person is away, but it never merges anything.

Use it when the queue is ready for autonomous progress and you want a fixed time, task, or step budget:

```bash
stagemesh run-window --duration 4h --concurrency 2 --max-steps-per-task 8 --report run-window.json
```

## Main options

| Option | Meaning |
| --- | --- |
| `--duration` | Maximum wall-clock window to run before stopping cleanly. |
| `--concurrency` | Number of tasks that may be worked at the same time. |
| `--max-steps-per-task` | Per-task step budget before that task is left open and resumable. |
| `--max-tasks` | Maximum number of tasks the window may start or advance. |
| `--tasks` | Explicit task IDs to consider instead of the normal queue selection. |
| `--integration pr\|local` | Integration mode. `pr` is the unattended default: finished work is prepared for PR review and local `main` must not move. `local` is for local integration workflows. |
| `--no-pr` | Keep completed work unpublished instead of opening PRs. The window still does not merge. |
| `--strict-live` | Require live runtime/provider checks instead of allowing degraded or unproven live capability. |
| `--allow-stale-runtime` | Permit a stale installed StageMesh runtime. Without this, stale runtime detection refuses to start. |
| `--plan` | Print the planned window configuration without starting runners. |
| `--report` | Path for the JSON report. The path must be writable before the window starts. |

## Preflight refusals

The command refuses to start with exit code `2` when it finds an unsafe or ambiguous starting state:

- The installed project-local StageMesh runtime is stale, unless `--allow-stale-runtime` is set.
- The durable ledger is damaged.
- The queue is paused.
- Existing executions are live, or have an unknown process identity that StageMesh cannot safely classify.
- Active task claims already exist.
- Local `main` is ahead of `origin/main`.
- Local `main` has diverged from `origin/main`.
- Configured providers cannot both implement work and perform independent review.
- The `--report` path cannot be written.

Fix the reported condition, then run `stagemesh doctor` before starting another window.

## Stop rules

After the window starts, StageMesh stops the window with exit code `1` when continued operation would be unsafe:

- New orphan-recovery events appear.
- The same infrastructure failure happens on two tasks.
- Local `main` moves while running in PR mode.
- A runner exits but leaves behind an execution or active claim.

A stopped window is intentionally conservative. Inspect the report and `stagemesh doctor` before resuming the queue.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | The window completed normally. |
| `1` | The window started, then stopped because a stop rule fired. |
| `2` | The window refused to start during preflight. |
| `3` | The report could not be written. |

## PR handling

`stagemesh run-window` never merges PRs. In PR mode, completed tasks are left for a person to review and merge. Merge open StageMesh PRs oldest first, because they are stacked, and use a normal merge commit. Do not squash them.

After merging, update local `main` and check `stagemesh doctor` before starting the next unattended window.

## Doctor visibility

`stagemesh doctor` shows when a run window was interrupted. Treat that as an operator handoff: read the last report, resolve any active claims, executions, orphan-recovery events, or repository drift it names, and confirm doctor is clean before starting another unattended window.
