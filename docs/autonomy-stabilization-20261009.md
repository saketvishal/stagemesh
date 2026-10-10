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

## Live acceptance

Live external-provider acceptance was proven in a separate synthetic canary repository so external provider CLIs only received canary tasks and not the StageMesh source checkout. StageMesh ran this PR branch against five local backlog tasks with three-way concurrency. Each task required one provider-created `stagemesh-task-live-*.txt` file, a focused validation gate that checked the exact file contents, independent review, and serialized integration into the canary repository's `master` branch.

Final canary status:

- `LIVE-1`: `DONE`; implemented by Claude, independently reviewed by Codex after Grok outage and Agy malformed-review fallback, validated, rebased/integrated.
- `LIVE-2`: `DONE`; implemented by Codex, independently reviewed by Claude, validated, rebased/integrated.
- `LIVE-3`: `DONE`; implemented by Codex, independently reviewed and integrated.
- `LIVE-4`: `DONE`; implemented by Codex, independently reviewed by Claude, validated, rebased/integrated.
- `LIVE-5`: `DONE`; implemented by Claude, independently reviewed by Codex after Agy malformed-review fallback, validated, integrated.

Observed provider behavior:

- Codex and Claude both produced durable live implementation candidates.
- Codex and Claude both served as independent reviewers.
- Grok was classified as `provider_unavailable` and placed in provider cooldown instead of poisoning task state.
- Agy failures were classified as `provider_permission_denied` for implementation and `malformed_review_output` for review fallback.
- The queue continued through provider capacity/fallback conditions and finished all five tasks without manual SQLite edits or task hand-holding.

Final canary state was healthy: `done_count: 5`, `current_problems: []`, `current_failed_execution_count: 0`, `unknown_execution_count: 0`, `stale_execution_count: 0`. Historical provider failures remained recorded as audit/evidence, as expected.

The remaining unproven external item is real GitHub outbound source synchronization against a live issue. The synthetic GitHub acceptance path remains covered, and hosted Linux/Windows checks for PR #259 are green. Do not treat GitHub source mutation as proven until a real issue/comment/close acceptance run is intentionally authorized.

Queue runs now wait through recorded temporary provider outages and retry automatically at the cooldown deadline. Implementation and independent review recovery are covered by scoped tests, including an outage already recorded before startup, exact cooldown expiry, and operator pause/stop. Review recovery retains the same candidate SHA and does not reimplement. Authentication, missing tools, and coding no-progress do not enter an indefinite capacity wait. queue-run --no-wait-for-providers retains finite exhaustion behavior. The live canary also observed a real Grok `provider_unavailable` event and continued through cooldown/fallback to complete all tasks.

The initial Linux/Windows hosted stabilization matrix passed on PR #259 at caee016. The local outage recovery checks passed (13 cases), including shared cross-stage capacity deadlines, and seven focused queue-control/CLI/provider-preflight compatibility checks passed. No full suite was run.

Admission priority trades some pipeline throughput for bounded integration progress. External actors can still advance the integration ref; the configured rebase limit continues to protect against unbounded retries.
