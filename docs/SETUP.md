# Setup Guide

This guide sets up StageMesh v0.2.0a1 from source. The public Python
distribution and primary CLI are `stagemesh`. The Python import package
remains `build_coordinator`, and the compatibility CLI alias
`build-coordinator` is retained for early alpha adopters.

## Prerequisites

- Python 3.11 or later
- Git
- Optional: PostgreSQL 14+ for production use; SQLite works out of the box

## Installation

### From source

```bash
git clone <repo-url>
cd <repo-directory>
pip install -e ".[dev]"
```

### Verify installation

```bash
stagemesh --help
build-coordinator --help
```

---

## Configuration

The coordinator reads configuration from two sources.

### 1. Coordinator config file

This file controls workspace paths and the database location. The default path
is `~/.build-coordinator/config.json`.

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

PowerShell:

```powershell
$env:BUILD_COORDINATOR_CONFIG = "C:\absolute\path\to\config.json"
```

### 2. Runner config file

This YAML or JSON file controls worker definitions: what agents to launch for
each role and stage. Set it with:

```bash
export BUILD_COORDINATOR_RUNNER_CONFIG=/path/to/runner-config.yaml
```

PowerShell:

```powershell
$env:BUILD_COORDINATOR_RUNNER_CONFIG = "C:\path\to\runner-config.yaml"
```

See [examples/](../examples/) for complete runner config examples.

### Environment variable overrides

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

### SQLite

```bash
export BUILD_COORDINATOR_DATABASE_URL=sqlite:///./coordinator.sqlite3
python -m build_coordinator.cli status
```

PowerShell:

```powershell
$env:BUILD_COORDINATOR_DATABASE_URL = "sqlite:///./coordinator.sqlite3"
python -m build_coordinator.cli status
```

### PostgreSQL

```bash
createdb build_coordinator
export BUILD_COORDINATOR_DATABASE_URL=postgresql://localhost/build_coordinator
python -m build_coordinator.cli status
```

---

## Adding the CLI to PATH

Installing with `pip install -e .` exposes the `stagemesh` console script
(and the `build-coordinator` compatibility alias) in the active Python
environment. You can also use the repository launchers directly.

### Linux / macOS

```bash
export PATH="$PATH:/path/to/repo/build_coordinator/bin"
build-coordinator --help
build-coordinator status
build-coordinator list
```

### Windows PowerShell

```powershell
$env:PATH += ";C:\path\to\repo\build_coordinator\bin"
build-coordinator --help
build-coordinator status
build-coordinator list
```

Or use the launcher directly:

```powershell
C:\path\to\repo\build_coordinator\bin\build-coordinator.ps1 status
```

---

## Windows unattended startup

On Windows, install a per-user Task Scheduler entry that runs
`stagemesh continue` every time the operator account logs on:

```powershell
C:\path\to\repo\build_coordinator\bin\stagemesh-windows-startup.ps1 install `
  -ProjectDir C:\path\to\project
```

The installer is idempotent. Running it again updates the same deterministic
task in place with `schtasks.exe /Create /F`; it does not create duplicates.
The task runs with the `ONLOGON` trigger and limited user privileges, and the
scheduled action invokes the checked-in launcher:

```text
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ...\stagemesh.ps1 continue --project-dir C:\path\to\project --max-cycles 2000
```

Optional flags:

```powershell
# Name a registered project explicitly.
C:\path\to\repo\build_coordinator\bin\stagemesh-windows-startup.ps1 install `
  -ProjectDir C:\path\to\project `
  -ProjectName my-product

# Coordinate every registered project after logon.
C:\path\to\repo\build_coordinator\bin\stagemesh-windows-startup.ps1 install -All

# Include the optional GitHub task-source adapter.
C:\path\to\repo\build_coordinator\bin\stagemesh-windows-startup.ps1 install `
  -ProjectDir C:\path\to\project `
  -Github
```

Check or remove the entry:

```powershell
C:\path\to\repo\build_coordinator\bin\stagemesh-windows-startup.ps1 status `
  -ProjectDir C:\path\to\project

C:\path\to\repo\build_coordinator\bin\stagemesh-windows-startup.ps1 uninstall `
  -ProjectDir C:\path\to\project
```

Uninstall is also idempotent: if the task is already absent, it exits
successfully and reports `installed: false`.

After a reboot, Task Scheduler starts the same durable
`stagemesh continue` loop. Because `continue` reconciles stale executions and
resumes from the project's `.build-coordinator/` state directory, recovered
work is picked back up instead of being duplicated. Keep Python, Git, and any
worker CLIs available on the operator account's logon `PATH`, and keep secrets
in the usual credential stores or environment, not in the scheduled task.

---

## First run

### 1. Initialize the database

```bash
export BUILD_COORDINATOR_DATABASE_URL=sqlite:///./coordinator.sqlite3
python -m build_coordinator.cli status
```

### 2. Create a task

```bash
python -m build_coordinator.cli objective create TASK-001 \
  --title "Add installation docs" \
  --description "Write a clear installation guide in README.md" \
  --review-policy INDEPENDENT
```

### 3. Configure a worker

```bash
cp examples/single_agent/worker-config.example.yaml runner-config.yaml
# Edit runner-config.yaml so the worker command points at your agent executable.

export BUILD_COORDINATOR_RUNNER_CONFIG=runner-config.yaml
python -m build_coordinator.cli run --once --dry-run
```

`--dry-run` uses configured fake workers and validates the runner path without
launching external subprocess workers.

### 4. Check status

```bash
python -m build_coordinator.cli status
python -m build_coordinator.cli list
```

### 5. Run continuously

```bash
python -m build_coordinator.cli run
```

---

## Useful commands

```bash
python -m build_coordinator.cli workers list
python -m build_coordinator.cli routing explain --stage implementation
python -m build_coordinator.cli recover-expired
python -m build_coordinator.cli objective status
```

---

## Troubleshooting

### "CoordinatorConfigError: BUILD_COORDINATOR_CONFIG must be an absolute path"

The `BUILD_COORDINATOR_CONFIG` environment variable must be an absolute path.
Use:

```bash
export BUILD_COORDINATOR_CONFIG=$(realpath ./config.json)
```

### "DatabaseSchemaError"

The schema has not been initialized. Run:

```bash
python -m build_coordinator.cli status
```

### Worker not launching

1. Check that `BUILD_COORDINATOR_RUNNER_CONFIG` points to a valid file.
2. Verify that each subprocess worker `worktree_path` exists and is a git repository.
3. Run `python -m build_coordinator.cli run --once --dry-run` to verify config parsing.
4. Check the execution logs in `data_dir/execution-logs/`.

### "EXTERNAL_EXECUTOR_CONFIGURATION_REQUIRED"

The worker has `adapter: unconfigured`. Update the runner config to set a real
`command` and `adapter: subprocess`.
