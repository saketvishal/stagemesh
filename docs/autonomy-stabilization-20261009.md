# Autonomy stabilization after PR #258

Base: `06a200485bbab984043fa6a65f2e38efcca37b2a`.

## Changes

- Automatic validation rechecks are bounded by candidate and contract, rather than the latest failure row. A failing recheck cannot renew its own budget.
- Parallel runs use the missing-gate and validation recovery hooks already used by single-task runs.
- Parallel admission gives running validation/review/integration work priority over fresh tasks. Five tasks with three workers previously exhausted the default two-rebase limit because new tasks kept landing ahead of older candidates.
- Gate launch failures and timeouts carry structured classifications. Ordinary test messages containing `timeout`, `not found`, or `no such file` do not alone identify an environment failure.
- Capacity waits remain diagnosable without counting as repeated unsuccessful coding attempts or exhausting the implementation budget.
- Rebase runtime failures are distinguished from content conflicts and preserve the underlying error. A blocked runtime failure receives one automatic integration retry for the same candidate and contract. Content conflicts and exhausted rebase budgets remain blocked.
- Reviewer clone failures include the underlying Git error.

Exact candidate validation, independent-review requirements, ownership protection, and Git identity policy remain enforced. Recovery does not manufacture passing evidence.

## Scoped verification

All commands used workspace-owned temporary directories and a command-scoped `core.longpaths=true` setting on Windows; no global Git settings were changed.

- `tests/test_task_recovery.py`: 26 passed.
- `tests/test_validation_plan.py`: 7 passed.
- `tests/test_single_task_stale_rebase.py`: 3 passed.
- Focused provider checks: quota classification, quota followed by timeout, all-capacity exhaustion, and no-progress followed by quota passed.
- Focused diagnosis and blocked-gate recovery checks passed.
- `tests/test_recovery_loop_regressions.py`: 11 passed, covering bounded retry budgets, capacity polling, failure classification, runtime error diagnostics, and five tasks with three concurrent workers.
- Five-task acceptance passed with the default two-rebase limit. This uses scripted implementation providers and the built-in synthetic reviewer, real Git worktrees, validation subprocesses, and serialized integration. It is not live external-provider acceptance.
- The dependent-task ordering check passed after the admission change.
- Python compilation and `git diff --check` passed.

No full repository test suite was run.

## Remaining acceptance gate

Live external-provider acceptance is **not proven**. The synthetic CLI-provider matrix also cannot complete its independent review stage in this Work session: Git clone launches an MSYS shell that the Windows sandbox rejects with `NtCreateDirectoryObject ... 0xC0000022`. Native Git network transport also fails with `getaddrinfo() thread failed to start`. Python and native Git operations that do not require that shell were usable for scoped verification.

The hosted synthetic independent-review/provider matrix passed on Linux and Windows. Keep this PR in draft until five real tasks with two or three concurrent providers and live outage recovery complete in an execution environment that permits the required subprocesses. Preserve normal checks; do not replace independent review with the synthetic reviewer for live acceptance.

Queue runs now wait through recorded temporary provider outages and retry automatically at the cooldown deadline. Implementation and independent review recovery are covered by scoped tests, including an outage already recorded before startup, exact cooldown expiry, and operator pause/stop. Review recovery retains the same candidate SHA and does not reimplement. Authentication, missing tools, and coding no-progress do not enter an indefinite capacity wait. queue-run --no-wait-for-providers retains finite exhaustion behavior. Live-provider outage recovery remains unproven in this sandbox.

The initial Linux/Windows hosted stabilization matrix passed on PR #259 at caee016. The local outage recovery checks passed (12 cases), and four focused queue-control/CLI/provider-preflight compatibility checks passed. No full suite was run.

Admission priority trades some pipeline throughput for bounded integration progress. External actors can still advance the integration ref; the configured rebase limit continues to protect against unbounded retries.
