# Local continuous driver

`stagemesh driver` is an opt-in Windows-local supervisor for the existing `run-window` lifecycle. It does not add another task
scheduler or coordinator. Each scheduled tick invokes one bounded `run-window`, which remains responsible for task synchronization,
eligibility, isolated implementation, exact-SHA validation and review, remediation, PR delivery, hosted checks and guarded landing.

The Windows Task Scheduler process is independent of ChatGPT and its UI. After explicit installation, closing ChatGPT does not stop
the scheduled command. The Windows user session, configured provider CLIs, repository credentials and machine must still be available.

## Safe opt-in

Installation is the only operation that creates persistent OS state. It requires an explicit provider-use acknowledgement and does
not run a development window immediately:

```powershell
stagemesh --project C:\path\to\project driver install --acknowledge-provider-use
```

The default schedule wakes every 10 minutes. Task Scheduler cadence must be an exact whole number of minutes. Each tick is bounded to
a 45-minute `run-window`, concurrency 2, 10 tasks, 8 steps per task and three consecutive failed ticks. Override those limits only on
the install command. The schedule runs at limited privilege, stores no credentials and calls the same Python interpreter and exact
StageMesh source checkout that installed it.

Automatic landing remains separately gated. The driver defaults to PR-only mode. `--auto-merge-ff` is an explicit install-time choice
and still uses `run-window`'s exact-candidate, hosted-check, branch-policy and Git-lease gates. It never authorizes a protected merge,
production change, destructive Git operation or credential change.

## What Task Scheduler does and does not guarantee

Measured on a real Windows Task Scheduler (hosted runner, Python 3.11 and 3.13): the XML definition registers with its logon trigger,
10-minute repetition, `StartWhenAvailable`, battery-safe and `IgnoreNew` settings exactly as written; `schtasks /Run` starts the task and
reports `Running`; a second `/Run` while it is running starts nothing. `schtasks /End` and the `ExecutionTimeLimit` terminate **only the
task's own process** (`cmd.exe` for the launcher, or the program itself when executed directly). Their children and grandchildren keep
running. The driver therefore never relies on Task Scheduler to stop provider work: the tick bounds its `run-window` child with
`kill_process_tree`, the child's durable ownership identity makes a surviving tree visible to the next tick (which refuses to overlap it),
and `driver stop --terminate-running` uses the verified process-tree cancellation.

## Operations

```powershell
stagemesh --project C:\path\to\project driver status
stagemesh --project C:\path\to\project driver pause --reason "maintenance"
stagemesh --project C:\path\to\project driver resume --reason "maintenance complete"
stagemesh --project C:\path\to\project driver stop --reason "stop unattended work"
stagemesh --project C:\path\to\project driver uninstall --reason "remove unattended startup"
```

`pause` blocks new queue admissions while active work drains. `resume` records an audited queue resume and recreates the exact
project-owned schedule. `stop` requests the existing queue stop policy and removes only this project's schedule. Add
`--terminate-running` only when the existing verified process-tree cancellation policy should be used.
`uninstall` also removes `.stagemesh/continuous-driver.json`; the SQLite audit ledger and local driver log are preserved.

`driver tick` is the scheduler entry point and is safe to invoke manually for diagnosis. A project-scoped OS lock prevents duplicate
ticks. Tick start and finish checkpoints live in the existing SQLite audit ledger. A small child wrapper owns the bounded
`run-window`, so closing ChatGPT or losing the scheduler parent does not terminate or replay the in-flight window. The wrapper clears
its durable process identity after a normal finish; the next tick then records deterministic recovery and lets `run-window` reconcile
dead executions and preserved task state. The owner record names both the wrapper and the `run-window` process under it. When both are proven dead (a reboot, a logoff, or a hard
kill of the whole tree) the next tick clears the record itself, audits `driver.owner_recovered`, and lets `run-window` reconcile task
state, so no human step is needed. A live or unverifiable owner fails closed; so does a dead wrapper whose child was never recorded,
which only an explicit `driver resume` releases. A dead wrapper with a live child counts as live.

## Outcomes

- `progress`: at least one task or guarded landing was handled.
- `idle`: no eligible work existed. This is an observation, not a successful autonomy task and never increments the ten-task streak.
- `blocked`: `run-window` refused or stopped; after the configured consecutive-failure budget, no provider is invoked again until an
  explicit `driver resume` records a fresh budget.
- `busy`: another tick, or a still-running window, owns the project; no duplicate work starts. A busy tick never counts toward the
  consecutive-failure budget.
- `timeout` or `crashed`: the bounded child failed to produce a usable report. Durable coordinator state is preserved for the next
  reconciliation.

## Disposable acceptance projects

`driver install --integration local` makes the window integrate into the project's own local base branch and never open a PR. It
exists so the whole lifecycle can be exercised against a throw-away repository with no GitHub remote. The default is `pr`, and a
production project should never use `local`. `scripts/windows-acceptance/driver_lifecycle.py` installs the real driver into such a
project with a deterministic mock provider (no model, no network), then drives repeated scheduled ticks, pause, resume, overlap
prevention, a hard kill of the whole process tree with automatic recovery, stop and uninstall. It refuses to run unless
`STAGEMESH_ACCEPT_DRIVER=1`, refuses if any provider command override is set, and verifies that every configured provider resolves to
the mock before installing anything.

## Evidence boundary

Deterministic tests use fake window runners and a recording Task Scheduler adapter. They prove exact command construction,
project-bound scheduling, cross-tick continuation, crash checkpoints, idempotent schedule cleanup, bounded failures, and idle semantics.
Real local subprocess tests additionally prove that an in-flight child finishes after its driver parent is killed, the next tick
recovers without overlap, and the Windows hard timeout terminates a temporary child process tree. These are not a real-provider run,
a real Task Scheduler round trip, a hosted-CI result, or a ten-task Founder Hands-Off streak.
