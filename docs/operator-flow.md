# Operator flow: recovering and running the backlog

The normal loop has four steps. Do not hand-run individual stages (`continue --task`, `advance-baseline`, `rebaseline-task`, manual
PRs) unless `survivor-wave` reports a task as blocked and needs a decision.

## Production operation: use `run-window`, not `queue-run`

For unattended or production work use `stagemesh run-window` (bounded, preflighted, journaled; see [run-window.md](run-window.md)):

    stagemesh run-window --duration 45m --concurrency 2 --max-steps-per-task 8 --integration pr

`stagemesh queue-run` is legacy/raw queue execution. It can start broad queue work, retry tasks and interrupt provider work outside
those guards, so plain `queue-run` now refuses and points here. It only runs with an explicit `--legacy-unsafe`, and even then it does
not sweep worktrees unless you also pass `--sweep-worktrees`. `stagemesh queue-control resume` only opens admission; it does not make
`queue-run` production-safe.

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

## If a run-window was interrupted

`stagemesh doctor` shows `last run-window ...: INTERRUPTED` when a window started and never finished (crash, kill, power loss).

Rerunning `stagemesh run-window` is safe because its preflight automatically recovers executions whose process is provably dead; executions that are LIVE are waited for, and executions with an unknown process identity fail closed and print the exact `stagemesh recover-stale --task <id> --release-unknown --execution <id> --reason ...` command to run after the operator has checked they are dead.

Nobody should open the SQLite ledger by hand.
