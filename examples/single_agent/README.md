# Single Agent Example

The simplest setup: one agent handles all roles (planning, building, reviewing, integrating).
Suitable for solo development or initial experimentation.

**Note:** Using the same agent for both building and reviewing defeats the purpose
of independent review. Use this setup only for experimentation or when a single
`SELF` review policy is acceptable.

## Setup

1. Copy the worker config:
```bash
cp examples/single_agent/worker-config.example.yaml runner-config.yaml
```

2. Edit `runner-config.yaml`:
   - Set `worktree_path` to your repository path
   - Set `command` to your agent CLI executable
   - Set `branch_name` to the branch your agent will work on

3. Create the coordinator config at `~/.build-coordinator/config.json`:
```json
{
  "control_repo_root": "/path/to/your-repo",
  "worktrees": {
    "agent-1": "/path/to/your-repo"
  }
}
```

4. Initialize and run:
```bash
export BUILD_COORDINATOR_DATABASE_URL=sqlite:///./coordinator.sqlite3
export BUILD_COORDINATOR_RUNNER_CONFIG=runner-config.yaml

python -m build_coordinator.cli status
python -m build_coordinator.cli objective create TASK-001 \
  --title "My first task" \
  --description "Implement a simple feature" \
  --review-policy SELF

python -m build_coordinator.cli run --once
```