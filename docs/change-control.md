# AI-Native Change Control

StageMesh treats coding agents as probabilistic implementation workers, not as the
authority that decides whether work is complete.

A production task follows:

```text
PLAN -> IMPLEMENT -> VALIDATE -> REVIEW -> INTEGRATE -> DONE
          ^             |          |
          |-------------|----------|
             bounded remediation
```

## Change contract

Real-provider runs require a task contract at:

```text
.stagemesh/contracts/<task-id>.json
```

The recommended way to create contracts is through `stagemesh plan`. A task in
the objective JSON can contain:

```json
{
  "id": "SM-200",
  "title": "Fix lease recovery",
  "dependencies": [],
  "contract": {
    "objective": "Fix lease recovery without changing unrelated lifecycle semantics.",
    "acceptance_criteria": [
      "expired claims become reclaimable",
      "active claims are not stolen"
    ],
    "allowed_paths": [
      "src/stagemesh/coordinator.py",
      "src/stagemesh/persistence.py",
      "tests/test_lease_recovery.py"
    ],
    "forbidden_paths": [
      "src/stagemesh/security.py"
    ],
    "required_changed_paths": [
      "tests/test_lease_recovery.py"
    ],
    "validation_commands": [
      "python -m pytest -q tests/test_lease_recovery.py",
      "python scripts/invariants.py"
    ],
    "invariants": [
      "one active claim per task",
      "candidate evidence remains exact-SHA scoped"
    ],
    "excluded_work": [
      "dependency upgrades",
      "unrelated refactoring"
    ],
    "max_changed_files": 6,
    "max_changed_lines": 400,
    "allow_dependency_changes": false
  }
}
```

## Enforcement

For real-provider `stagemesh continue` runs:

1. A missing or invalid contract blocks the task before a coding agent executes.
2. The implementation agent runs in a detached Git worktree.
3. The contract and any remediation findings are sent to the agent.
4. The resulting candidate is committed and identified by exact SHA.
5. Validation recomputes the candidate diff, enforces scope and change-size
   limits, rejects unapproved dependency changes, runs `git diff --check`, and
   executes contract validation commands in a fresh detached worktree.
6. Review independently recomputes contract compliance and requires passing
   validation evidence for the exact candidate SHA.
7. A failed validation or review produces durable findings and can return to
   IMPLEMENT for at most three bounded repair attempts.
8. Exhausted remediation blocks the task rather than looping indefinitely.
9. Integration performs a real Git fast-forward or merge into the target
   checkout. DONE is unreachable without passing integration evidence.

The fake/dry-run executor intentionally remains lightweight for coordinator and
state-machine tests.

## Security boundary

Contracts are control-plane input. Coding agents are instructed not to edit
contracts, weaken tests, or expand scope. Contract path allowlists should
normally exclude `.stagemesh/**`.

Validation commands are parsed with `shlex` and executed without a shell.
They should be repository-owned deterministic commands, not arbitrary
untrusted input.
