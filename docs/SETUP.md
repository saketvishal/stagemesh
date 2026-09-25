# Setup Guide

This guide sets up the current StageMesh public alpha from source.

The public CLI is `stagemesh`. The Python import package and some compatibility environment/configuration names still use `build_coordinator` during the alpha period.

## Prerequisites

- Python 3.11 or later
- Git
- At least one supported coding-agent CLI if you want real agent execution
- Optional: PostgreSQL 14+; SQLite works out of the box

## Install from source

```bash
git clone https://github.com/saketvishal/stagemesh.git
cd stagemesh
python -m pip install -e .
```

For development:

```bash
python -m pip install -e ".[dev,postgres]"
```

Verify:

```bash
stagemesh --help
```

## Initialize a project

From a git repository you want StageMesh to coordinate:

```bash
cd /path/to/my-project
stagemesh init
```

This creates the project-owned `.stagemesh/` definition and registers the repository unless `--no-register` is used.

The project owns version-controlled intent such as task/objective definitions and concurrency configuration. Runtime lifecycle state remains in the git-ignored StageMesh state directory.

See [PROJECTS.md](PROJECTS.md).

## Verify coding-agent runtimes

```bash
stagemesh agent setup
```

StageMesh discovers installed coding-agent CLIs and performs a small real headless verification for each candidate runtime.

Inspect what was found:

```bash
stagemesh agent list
```

If a runtime needs authentication, authenticate with that runtime's own CLI and run `stagemesh agent setup` again.

## Diagnose the installation

```bash
stagemesh doctor
```

`doctor` checks project discovery, durable state, and verified coding-agent runtimes and reports what needs attention.

For machine-readable output:

```bash
stagemesh doctor --json
```

## Run StageMesh

Inside a StageMesh project:

```bash
stagemesh continue
```

For a registered project by name:

```bash
stagemesh continue my-project
```

To target one imported task while debugging or hardening a specific issue:

```bash
stagemesh continue --task GH-123
```

From outside a project, `stagemesh continue --all` coordinates registered projects independently.

## Project registration and machine-local environment

Useful commands:

```bash
stagemesh project list
stagemesh project show
stagemesh project register /path/to/project
stagemesh project add /path/to/project
stagemesh project status
```

Machine-specific PATH/environment configuration can be attached during registration without committing it to the project:

```bash
stagemesh project add /path/to/project --path-prepend /path/to/venv/bin
```

On Windows PowerShell, use the virtualenv `Scripts` directory.

## Project configuration

A minimal `.stagemesh/project.yaml` looks like:

```yaml
schema_version: 1
id: my-project
name: My Project

repository:
  main_ref: main

state_dir: .build-coordinator

execution:
  concurrency: 2
  reviewers: 1
  default_review_policy: INDEPENDENT
```

Optional task-source adapters such as GitHub can be enabled in the same file. See [PROJECTS.md](PROJECTS.md) for the full model.

## Legacy/direct coordinator configuration

StageMesh still supports lower-level coordinator and runner configuration for compatibility and advanced use cases. Those surfaces use names such as:

- `BUILD_COORDINATOR_CONFIG`
- `BUILD_COORDINATOR_RUNNER_CONFIG`
- `BUILD_COORDINATOR_DATABASE_URL`
- `BUILD_COORDINATOR_DATA_DIR`
- `BUILD_COORDINATOR_AUTO_PUSH_ALLOWED`

New users should prefer the project-owned `.stagemesh/` workflow unless they have a specific reason to configure the lower-level runner directly.

Examples remain under [../examples/](../examples/).

## Database support

SQLite is the default local path for project state.

PostgreSQL support is available with the optional dependency:

```bash
python -m pip install -e ".[postgres]"
```

Durable state migrations are explicit. StageMesh does not silently migrate project state during upgrade.

Useful commands:

```bash
stagemesh project migrate-state
stagemesh doctor
```

## Useful commands

```bash
stagemesh project status
stagemesh workers list
stagemesh routing explain --stage implementation
stagemesh recover-expired
stagemesh objective status
```

## Troubleshooting

### A coding agent is not selected

Run:

```bash
stagemesh agent list
stagemesh doctor
```

Check whether the runtime is verified, enabled, authenticated, and currently healthy.

### A task is blocked

Run:

```bash
stagemesh project status
```

StageMesh reports typed reasons such as provider failure, merge conflict, review-environment failure, retry exhaustion, or coordinator invariant failure. Do not erase durable state just to force progress.

### State schema needs migration

Run the explicit migration review path:

```bash
stagemesh project migrate-state
```

Apply only after reviewing the reported migration.

### Security or credential issue

Do not post credentials or private source in a public issue. Follow [../SECURITY.md](../SECURITY.md).
