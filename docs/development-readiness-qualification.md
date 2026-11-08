# Development readiness qualification

This is the bounded qualification record for issue #769. It separates implemented controls, synthetic proof, hosted proof, and runtime queue admission. It does not claim that a GitHub issue is running in a StageMesh project queue.

## Control matrix

| Control | Implementation evidence | Synthetic acceptance | Current state |
| --- | --- | --- | --- |
| Frozen spec admission | Existing #325/#338 implementation plus `test_strict_spec_admission_rejects_a_missing_spec_before_runtime` | A missing strict spec is rejected before any runtime or provider call | Bounded pass |
| Atomic write scope | #328/#767 implementation merged by PR #770 | `test_contract_rejects_forbidden_and_out_of_scope_files` and the Atomic Scope cases in `test_change_contracts.py` | Bounded and hosted pass |
| Diff-first quarantine | #767 implementation merged by PR #770 | Exact-candidate quarantine, rename, binary, symlink, staged, unstaged, and untracked cases in `test_change_contracts.py` | Bounded and hosted pass |
| Provider-neutral coding policy | #768 implementation merged by PR #770 | Policy tampering is quarantined; unrequested abstraction, speculative code, filler comments, and documentation churn require review | Bounded and hosted pass |
| Pluggable language gates | #766 implementation merged by PR #770 | `test_mixed_python_typescript_policy_blocks_a_failing_language_gate` proves that a failing TypeScript gate blocks a mixed Python/TypeScript candidate | Bounded pass; mixed-language addition is not yet hosted |
| Crash checkpoint recovery | Existing continuous-driver recovery | Two synthetic checkpoint/child-recovery cases pass locally | Bounded pass |
| Parallel task worktrees and restart | Existing parallel runner and worktree recovery | The local restricted Windows session refuses stage dispatch before task execution; PR #770's complete hosted regression passed | Hosted pass; local restriction recorded |
| Windows and Linux CI | PR #770 checks | Exact repaired commit `a803c10` passed Linux and Windows before merge | Pass |
| Bounded operator window | Existing run-window implementation | Task/time bounds, safe PR defaults, clear reports, provider prompt transport, and recovery are covered by the hosted run-window suite | Hosted synthetic pass; native Task Scheduler acceptance was not run |

## Negative promotion checks

The focused synthetic qualification must keep these outcomes fail-closed:

- missing strict spec: rejected before runtime;
- out-of-scope edit: validation fails with typed scope findings;
- policy bypass: candidate is quarantined;
- unrequested abstraction or speculative/filler change: promotion requires independent review;
- failing language-specific gate: the whole mixed-language candidate fails.

## Evidence boundaries

- The local queue is **not admitted** in this clean worktree: there is no project-local StageMesh database or project runtime configuration to reconcile safely.
- A passing focused test is not hosted CI evidence.
- A passing hosted workflow is not proof of popup-free native Windows scheduling.
- The post-merge workflow for merge commit `2f81d7d` was the intended smoke path: it skipped both authoritative regressions and completed in 21 seconds on Linux and 52 seconds on Windows. The full regression was not run twice.
- #769 remains open until this mixed-language qualification commit has hosted Linux and Windows proof. Native Task Scheduler popup behavior remains a separate, explicitly unproven boundary.
