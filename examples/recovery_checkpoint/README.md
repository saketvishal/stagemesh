# Recovery and Checkpointing

## How it works

The coordinator uses claims and checkpoints to enable safe worker replacement
without losing progress.

### Claims

A claim is an exclusive lease on a task for a specific worker. Claims have
a configurable expiry time (default: 30 minutes). If a worker disappears —
process crash, machine reboot, network failure — its claim expires.

### Recovery

When `recover_expired` runs (automatically on each coordinator loop), expired
claims are released and their tasks return to `READY` state. Any eligible
worker can then claim the task and continue.

```bash
# Manually trigger recovery
python -m build_coordinator.cli recover-expired
```

### Checkpoints

Workers write structured checkpoints during execution:

```bash
python -m build_coordinator.cli checkpoint --claim-id <id> \
  --worker-id builder-a \
  --files-changed src/main.py tests/test_main.py \
  --commits abc123 \
  --tests-run tests/test_main.py \
  --notes "Implemented the feature, tests passing"
```

A checkpoint stores:
- Files changed so far
- Commits created
- Tests run
- Decisions made
- Blockers encountered

### Resume context

When a replacement worker claims an expired task, it receives the full
resume context:

```bash
python -m build_coordinator.cli resume-context --task-id TASK-001
```

This includes all checkpoints, preserving the work done by previous workers.
The replacement worker does not need to re-do completed work.

### Git is the source of truth for source

Coordinator checkpoints store metadata only. The actual source changes
are in git. A replacement worker can inspect the feature branch to see
what has been committed.

## Heartbeat

Active workers should send heartbeats to prevent their claims from expiring:

```bash
python -m build_coordinator.cli heartbeat --claim-id <id> --worker-id builder-a
```

The default lease is 30 minutes. Each heartbeat resets the expiry timer.

## Practical implications

1. Long-running tasks are safe: checkpoints + heartbeats keep the claim alive
2. Machine reboots are safe: the coordinator recovers and a new worker continues
3. Agent failures are safe: the task returns to READY; another agent claims it
4. No work is lost: checkpoints preserve progress metadata; git preserves code