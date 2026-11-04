# Bounded remediation and recovery

StageMesh treats an implementation attempt, a remediation budget, and an exhaustion recovery as different things. For a remediation limit `N`, an implementation defect can run at most:

1. one initial implementation;
2. `N` ordinary remediations in budget epoch 0;
3. `N` remediations in budget epoch 1 after the single diagnosed parent-budget reopening; and
4. one clean-worktree exhaustion recovery.

The maximum provider-call count for that path is therefore `2N + 2`. A contract-scope failure, unavailable validation environment, judgment-required finding, or other terminal diagnosis can stop sooner. A second parent-budget reopening is not allowed for the same normalized diagnosis signature, and a second exhaustion recovery round is not allowed.

## Durable evidence

Each ordinary remediation debits `task_remediations` with a task, failed stage, candidate SHA, and explicit `budget_epoch`. Clearing a current budget marks rows as cleared; it never deletes them. The next debit for that task and stage advances the epoch, so current and cumulative counts remain independently observable.

When the coordinator queues an ordinary remediation, the ledger row is bound to the `task.remediation_queued` audit event. That event creates a typed `retry_attempts` row, which is subsequently linked to the claim and execution that consumed it. `stagemesh task-doctor --task ID --json` exposes current attempts, cumulative attempts, epochs, and the latest retry/execution correlation without changing state.

The SQLite migration is restart-safe and retains legacy rows in epoch 0. The declared PostgreSQL schema contains the same remediation ledger fields.

## Terminal fence

Once the bounded ladder ends in `BLOCKED`, reopening the database or restarting the coordinator does not schedule another retry. Recovery requires an explicit supported transition such as `retry-task`; merely restarting StageMesh cannot reset an epoch or create another exhaustion round.

These boundaries run in the protected Windows and Linux `Autonomous recovery ledger` CI step. They remain part of full validation rather than a focused fast path because they cross persistence, scheduling, provider execution, audit, and restart behavior.
