# Workspace ownership and task recovery

This guide preserves the operational detail intentionally removed from the README. These commands use StageMesh state transitions and audit events; do not edit `.stagemesh/stagemesh.sqlite3` by hand.

## Workspace ownership and external mutation

Each task worktree is written by one StageMesh execution at a time. An execution creates an owner record atomically in the worktree's private Git directory. Claims and dead-owner takeover are serialized by a per-workspace OS lock. Cleanup never removes a foreign owner's record, and a release that cannot be confirmed fails closed.

When an execution finishes, StageMesh seals:

- the worktree HEAD;
- a tracked-content fingerprint;
- the candidate SHA produced by the execution.

StageMesh checks the seal before and after provider execution, validation, independent review, integration, and integration rebase. Review uses a throwaway checkout of the exact candidate SHA. A validation gate that moves HEAD cannot pass.

The following are external mutations:

- a new unrelated commit or moved HEAD;
- modified, added, or deleted tracked content that differs from the seal;
- a lost or foreign lease;
- a candidate row that does not match the sealed candidate;
- missing passed evidence for the exact candidate and contract at a protected boundary;
- Git output that cannot be decoded as UTF-8 where the ownership guard requires it.

Detection records an `EXTERNAL_WORKSPACE_MUTATION` audit event and blocks the task. The mutated workspace is not reset, committed, or adopted, and existing evidence is not deleted.

### Dead owners and older worktrees

A dead owner's lease is recovered only when the workspace still matches its seal. If HEAD or tracked content changed, recovery refuses the workspace as an external mutation. An interrupted provider execution may seal linear work it left only after the normal before-agent check; an unrelated HEAD is refused.

A legacy worktree with no ownership record is adopted only when its tracked files are clean and its HEAD matches the recorded candidate or baseline when one exists. Untracked files in such a legacy worktree are not proven by that adoption check.

### Guarantee boundary

StageMesh provides exclusive StageMesh ownership, exact starting-state checks, a bounded authorized execution window, a linear sealed candidate, and fail-closed checks at lifecycle boundaries. Git cannot prove which same-user process wrote during the authorized provider window. This is not cryptographic, container, or process isolation.

Known gaps include index-only state, skip-worktree and assume-unchanged flags, ignored files, a branch switch at the same SHA, and gates that dirty their checkout without moving HEAD. Some explicit recovery paths have separate ownership rules. Treat an unreadable owner record as uncertainty and fail closed.

## Inspect before repair

Use:

```bash
stagemesh task-doctor --task <id> --json
```

The doctor is read-only. It reports stage and status, claims and executions, candidate and baseline state, frozen contract identity, validation and review evidence, typed tool failures, diagnosis, planning and research receipt metadata, and the next bounded command. It does not expose prompts, planning contents, gate environments, or E2E artifact contents.

Repair commands refuse an active claim or running execution. They retain candidates, findings, evidence, and audit history.

## Rebind a contract

```bash
stagemesh rebind-contract --task <id> --plan
stagemesh rebind-contract --task <id> --validate
```

Rebinding rereads `.stagemesh/contracts/<id>.json`, canonicalizes it, and freezes the supported contract version. It refuses silent invalidation of passed evidence unless `--force` is explicit. Fresh validation and independent review are required under the new contract hash.

Ambiguous invariant strings are never guessed to be prose or commands. Use the explicit one-based `--review-invariant` or `--command-invariant` options with a reason, preview with `--plan`, and preserve the authorization receipt. Recognized executable checks cannot be downgraded to review.

## Rebaseline or advance a baseline

Use `rebaseline-task` when other work has already landed and pollutes a task's diff:

```bash
stagemesh rebaseline-task --task <id> --to <integration-ref> --validate
```

It moves the stale baseline to the candidate's merge-base with the integration ref. It refuses when the baseline is not stale, the candidate is already integrated, or history is ambiguous unless the documented force boundary applies.

Use `advance-baseline` when the task must start again from current integrated code:

```bash
stagemesh advance-baseline --task <id> --to <ref> --reason "..." --yes
```

Before changing state, StageMesh preserves the worktree and earlier candidates under `refs/stagemesh/preserved/<task>/<sha>`. Old evidence remains historical and cannot satisfy a candidate built on the new baseline. The baseline move, stage reset, remediation-budget reset, and audit event are one transaction.

## Recover ownership explicitly

Inspect the mutation event and the worktree. Either restore the event's expected SHA or remove the task worktree, then request a typed retry:

```bash
git worktree remove --force <workspace>
stagemesh retry-task --task <id>
```

For executions with unknown process identity, verify externally that no provider or child process remains before using the exact `recover-stale --release-unknown` command printed by StageMesh. A retained review checkout must remain until its process tree is confirmed gone.

## Run a bounded survivor wave

For a reviewed set of stuck or open tasks:

```bash
stagemesh survivor-wave --tasks 155,156,159 --concurrency 2 --max-steps-per-task 12 --report wave.json
```

The wave skips objective roots and live-owned tasks, uses bounded per-task execution, preserves validated progress, and carries on when one task needs a human decision unless `--stop-on-blocked` is set. In PR mode it may publish task branches and PRs, but it never merges or cleans worktrees. Repeated infrastructure signatures stop new admissions while already running tasks finish.

For production queue operation, prefer [run-window](run-window.md) and the [operator flow](operator-flow.md). Raw `queue-run` requires explicit legacy unsafe opt-in.
