# Real-world validation

StageMesh is developed against deterministic tests and real engineering runs. This page records sanitized scenarios that influenced the design.

No private product source, user data, legal data, credentials, or proprietary domain logic is included here.

## Provider failover under real execution

Observed pattern:

```text
primary provider unavailable/exhausted
        ↓
provider failure recorded
        ↓
ineligible provider routed around
        ↓
another eligible provider continues work
```

This demonstrated that provider failover can keep the engineering lifecycle moving rather than stranding the task.

It also exposed an important requirement: provider-health classification must be trustworthy. A false rate-limit classification is still a bad routing input even when fallback succeeds. StageMesh therefore treats failure classification, diagnostics, cooldown, recovery, and task-attempt accounting as separate concerns.

Relevant work is tracked in the public issue backlog.

## Concurrent work and integration conflicts

Observed pattern:

```text
tasks start from main SHA A
        ↓
task 1 integrates and advances main
        ↓
task 2 reaches integration against older assumptions
        ↓
mechanical merge conflict detected
        ↓
StageMesh refuses unsafe integration
```

The safe behavior is to stop rather than merge unreviewed conflict resolution.

The next lifecycle step is automated conflict recovery that produces a new SHA, validates it, invalidates the old approval, requires exact-SHA re-review, and retries integration.

## Worker loss and durable recovery

StageMesh persists task ownership, checkpoints, git state, evidence, and execution history so a replacement worker can resume work after an agent/runtime disappears.

The recovery contract is more important than keeping one model conversation alive: work should survive the executor.

## Exact-SHA review

A review verdict is authority over a specific implementation SHA, not a branch name or abstract task.

If remediation, conflict resolution, or any other change creates a new SHA, the previous approval cannot authorize integration of the new code.

## Review-environment failures

A reviewer can fail because its environment is broken even when the implementation is correct.

StageMesh distinguishes review-environment failure from implementation defects so infrastructure problems do not create fake remediation cycles or corrupt the task's engineering history.

## What counts as evidence

Examples include:

- git SHA and branch/worktree identity;
- deterministic validation results;
- structured review verdicts;
- provider/runtime failure diagnostics;
- lifecycle transition events;
- integration/merge assessment;
- merged-main acceptance where configured.

The project intentionally avoids treating free-form agent confidence as lifecycle authority.

## Evidence-first claims

Public capability claims in StageMesh documentation should be backed by deterministic tests, execution records, or reproducible acceptance evidence. If a capability is incomplete or still being hardened, documentation should say so explicitly.
