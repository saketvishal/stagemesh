# Hands-off setup (Windows, isolated development project)

Two separate steps. The first proves the driver on your machine with mock providers and leaves nothing behind. The second is
the persistent installation. Neither needs you to operate StageMesh afterwards.

## Step 1: native acceptance (mock providers, removes itself)

`scripts/windows-acceptance/run-native-acceptance.ps1`, started by a four-statement bootstrap that fetches one pinned commit
into a temp directory. It builds a venv, runs the focused driver tests and the process-tree diagnostic on this host, records
provider login state without a model call, and runs `driver_lifecycle.py`, which installs the real driver into a throw-away
git project with a mock provider, lets Task Scheduler launch ticks, then tests pause, resume, overlap prevention, a hard kill of
the whole process tree with automatic recovery, stop and uninstall. Its single Task Scheduler task, its processes and its temp
files are removed even on failure. Evidence goes to `.stagemesh\acceptance-evidence\native-<timestamp>\` in the main checkout
(ignored by git): `summary.json`, `transcript.txt`, logs, and the lifecycle JSON.

## Step 2: persistent installation (`install-handsoff.ps1`)

It refuses, and changes nothing, unless the native acceptance passed for the same commit, both coding CLIs are installed and
logged in with a subscription (`stagemesh provider-auth`, no model call), and no API-billing variable
(`OPENAI_API_KEY`, `CODEX_API_KEY`, `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`) is visible in the process, user or machine
environment. `driver install` repeats the same check whenever the configured providers are real CLIs, so the guard also holds
for any later install or resume.

It creates `C:\development\stagemesh-handsoff\{runtime,textkit}`: a pinned StageMesh in its own venv, and a stdlib-only Python
package repository with ten real tasks. Each task asks a provider to add one module and its tests; each is only DONE when the
provider's own tests and an independent `spec/check_*.py` oracle (which the contract forbids the provider to edit) both pass and a
different provider approves the exact candidate. Integration is `local`, inside that sandbox repository only. Nothing is pushed,
published, merged elsewhere or deployed.

Bounds: one task at a time; 40-minute windows, one per 10 minutes; at most 2 tasks and 6 steps per task per window; 12 provider
calls per task and 30 per window; three consecutive failed ticks block the driver until `driver resume`. A running window is
never overlapped (project lock, durable child-owner record, Task Scheduler `IgnoreNew`).

Recovery: the owner record names the wrapper and its run-window. If both are provably dead (reboot, logoff, killed tree) the next
tick recovers by itself; a live or unverifiable owner fails closed. Logon and repeating triggers plus `StartWhenAvailable` bring
the driver back after a restart or sleep.

Logs: `textkit\.stagemesh\driver\driver.log` (every scheduled tick, including tracebacks), per-tick JSON reports beside it, and the
SQLite ledger. Pause, resume, stop and uninstall commands are written to `README-handsoff.txt` next to the project.

## The streak

A task counts toward the ten only when it reaches DONE through the scheduled driver with a real provider implementing and a
different real provider reviewing, with no human action on branches, commits, worktrees, rebases, merges, candidate SHAs, CI,
review cycles or recovery. Idle ticks, mock providers and simulated runs never count.
