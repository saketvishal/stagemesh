# Setup Guide

## Prerequisites

- Python 3.11 or later
- Git
- (Optional) PostgreSQL 14+ for production use; SQLite works out of the box

## Installation

### From source (recommended for v0.1-alpha)

```bash
git clone <repo-url>
cd build-coordinator
pip install -e ".[dev]"
```

### Verify installation

```bash
python -m build_coordinator.cli --help
```

---

## Configuration

The coordinator reads configuration from two sources:

### 1. Coordinator config file

Controls workspace paths and database location. The default path is
`~/.build-coordinator/config.json`.

```json
{
  "control_repo_root": "/path/to/your-repo",
  "database_url": "sqlite:////path/to/coordinator.sqlite3",
  "data_dir": "/path/to/.build-coordinator",
  "max_active_builders": 2,
  "project_roots": {
    "my-project": "."
  },
  "worktrees": {
    "builder-a": "/path/to/worktree-a",
    "builder-b": "/path/to/worktree-b",
    "reviewer-1": "/path/to/worktree-a",
    "integration-1": "/path/to/your-repo"
  }
}
```

Override the config file path with:
```bash
export BUILD_COORDINATOR_CONFIG=/absolute/path/to/config.json
```

### 2. Runner config file (YAML or JSON)

Controls worker definitions — what agents to launch for each role and stage.
Set with:
```bash
export BUILD_COORDINATOR_RUNNER_CONFIG=/path/to/runner-config.yaml
```

See [examples/](../examples/) for complete runner config examples.

### Environment variable overrides

All config values can be overridden with environment variables:

| Variable | Description |
|---|---|
| `BUILD_COORDINATOR_DATABASE_URL` | SQLAlchemy database URL |
| `BUILD_COORDINATOR_REPO_ROOT` | Repository root path |
| `BUILD_COORDINATOR_DATA_DIR` | Data directory path |
| `BUILD_COORDINATOR_MAX_ACTIVE_BUILDERS` | Maximum concurrent builders |
| `BUILD_COORDINATOR_RUNNER_CONFIG` | Runner config file path |
| `BUILD_COORDINATOR_CONFIG` | Coordinator config file path |
| `BUILD_COORDINATOR_AUTO_PUSH_ALLOWED` | Allow automatic git push |

---

## Database setup

### SQLite (default)

```bash
export BUILD_COORDINATOR_DATABASE_URL=sqlite:///./coordinator.sqlite3
python -m build_coordinator.cli ensure-state
```

### PostgreSQL

```bash
createdb build_coordinator
export BUILD_COORDINATOR_DATABASE_URL=postgresql://localhost/build_coordinator
python -m build_coordinator.cli ensure-state
```

---

## Adding the CLI to PATH

### Linux / macOS

```bash
# Add to PATH once (e.g., in ~/.bashrc)
export PATH="$PATH:/path/to/build-coordinator/build_coordinator/bin"
```

Then use from anywhere:
```bash
build-coordinator status
build-coordinator list
```

### Windows (PowerShell)

```powershell
# Add to PATH (e.g., in $PROFILE)
$env:PATH += ";C:\path\to\build-coordinator\build_coordinator\bin"

# Or use the PowerShell launcher directly:
C:\path\to\build-coordinator\build_coordinator\bin\build-coordinator.ps1 status
```

---

## First run

### 1. Initialize the database

```bash
export BUILD_COORDINATOR_DATABASE_URL=sqlite:///./coordinator.sqlite3
python -m build_coordinator.cli ensure-state
```

### 2. Create a task

```bash
python -m build_coordinator.cli upsert \
  --task-id TASK-001 \
  --title "Add installation docs" \
  --description "Write a clear installation guide in README.md" \
  --review-policy INDEPENDENT \
  --risk-level LOW
```

### 3. Configure a worker (dry-run mode)

```bash
cp examples/single_agent/worker-config.example.yaml runner-config.yaml
# Edit runner-config.yaml — point command at your agent executable

export BUILD_COORDINATOR_RUNNER_CONFIG=runner-config.yaml
python -m build_coordinator.cli run --once --dry-run
```

### 4. Check status

```bash
python -m build_coordinator.cli status
python -m build_coordinator.cli list
```

---

## Troubleshooting

### "CoordinatorConfigError: BUILD_COORDINATOR_CONFIG must be an absolute path"

The `BUILD_COORDINATOR_CONFIG` env var must be an absolute path. Use:
```bash
export BUILD_COORDINATOR_CONFIG=$(realpath ./config.json)
```

### "DatabaseSchemaError"

The schema has not been initialized. Run:
```bash
python -m build_coordinator.cli ensure-state
```

### Worker not launching

1. Check `BUILD_COORDINATOR_RUNNER_CONFIG` points to a valid file
2. Verify `worktree_path` in the runner config exists and is a git repository
3. Run with `--dry-run` first to verify config parsing
4. Check the execution logs in `data_dir/execution-logs/`

### "EXTERNAL_EXECUTOR_CONFIGURATION_REQUIRED"

The worker has `adapter: unconfigured`. Update the runner config to set a real
`command` and `adapter: subprocess`.