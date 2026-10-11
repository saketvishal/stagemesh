# survivor-wave

`stagemesh survivor-wave --tasks 155,156 --concurrency 2 --max-steps-per-task 8 --report wave.json` runs a bounded set of stuck or open
tasks through the normal `continue` loop. It never merges a PR and never cleans a worktree.

## Integration modes

| `--integration` | Where finished work lands | Local `main` | PRs |
| --- | --- | --- | --- |
| `pr` (default) | `refs/heads/wave/staging`, stacked across tasks and across waves until their PRs merge | never moves | one per DONE task, from `wave/task-<id>`, for a person to merge |
| `local` | the integration branch itself | moves (the report says so) | none |

In PR mode a task that reaches DONE is reported as `pr_opened` / ready for merge, not as landed. If local `main` moves anyway, the wave
fails with `LOCAL MAIN MOVED IN PR MODE`. The staging ref is only ever moved forward onto a merged base; if it has diverged from the
base the wave refuses to start (merge or delete the old wave branch first). `--no-pr` keeps the work on the staging ref without opening PRs.

## What the report tells you

`repo state` is one of `ready`, `needs_pr_merges`, `ahead`, `diverged`, `local_main_advanced`, `main_moved_unexpectedly`. Alongside it:
orphan-recovery events (a ledger count), worktrees split into cleanable / done-but-not-in-main-yet / resumable / other (a read-only
census, nothing is removed), active claims, running executions, and local `main` ahead/behind `origin`. Hitting `--max-steps-per-task`
leaves the task OPEN and marked `resumable`; that is not a failure.

## Stale installed runtime

The project-local install (`.stagemesh/tooling/venv`) does not follow the checkout. The wave compares the commands of the installed
`stagemesh` with source and warns when the install lacks some. To run a command that only exists in source:

    PYTHONPATH=src python -c "from stagemesh.cli import main; main()" survivor-wave --tasks ...

To refresh the install, use the `pip install --upgrade` command the warning prints (nothing reinstalls automatically).
