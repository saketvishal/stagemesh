# Project-owned backlogs (`.stagemesh/`)

A StageMesh **project** is a git repository that carries its own StageMesh
configuration and task definitions under `.stagemesh/`. The repository owns
*what* to build; StageMesh owns *how far along it is*.

```
my-product/
└── .stagemesh/
    ├── project.yaml          # identity, concurrency, worker templates
    └── tasks/
        └── *.yaml            # task definitions (one task, or a `tasks:` list, per file)
```

| Owned by the project (version-controlled) | Owned by StageMesh (runtime, never in Git) |
|---|---|
| objective, requirements, acceptance criteria | READY / CLAIMED / IN_PROGRESS / validation / review / DONE |
| dependencies, priority, review requirement | claims, leases, heartbeats, worker assignment |
| risk, scope, validation commands | executions, checkpoints, retries, recovery |
| concurrency limit, worker templates | validation and review evidence |

Runtime state lives in the project's *state directory* (default
`.build-coordinator/`, git-ignored) in the existing durable queue. There is no
second coordinator and no lifecycle state in task files.

External systems (GitHub Issues, Azure DevOps, ...) are optional
import/export adapters behind `TaskSource`. Nothing in the local flow needs
them; a project enables one under `task_sources:` in `project.yaml`, and an
adapter failure never blocks local execution.

## Use it

```
stagemesh "Continue My Product development."    # any directory
stagemesh continue my-product
stagemesh continue                              # inside a project
```

`continue` resolves the project, synchronizes its backlog into the queue, then
runs the ordinary runner loop until nothing more can make progress. Up to
`execution.concurrency` dependency-eligible tasks run at once, each in its own
StageMesh-provisioned worktree and branch, with workers chosen by the existing
deterministic routing. There is nothing to pick: no worker, provider,
worktree or execution directory.

```
stagemesh project register <path>     # remember a project root for name lookup
stagemesh project list
stagemesh project discover [name]     # resolve + validate the backlog
stagemesh project sync [name] [--dry-run]
stagemesh project status [name]
stagemesh project migrate-state [name] [--apply]
stagemesh continue [name] --dry-run   # plan only; writes nothing
```

A project is found by (in order) `--project-dir`, a name/alias in the registry
(`~/.build-coordinator/projects.json`, override with
`STAGEMESH_PROJECT_REGISTRY`), or the `.stagemesh/project.yaml` above the
current directory. Linked git worktrees resolve to the main project root, so
agents running inside a workspace never fork runtime state.

## `project.yaml`

```yaml
schema_version: 1
id: my-product                 # ^[a-z0-9][a-z0-9_-]*$
name: My Product
aliases: [my-product]          # extra names accepted by "Continue <name> development."
repository: {main_ref: main, remote_name: origin}
state_dir: .build-coordinator  # runtime state; keep git-ignored
execution:
  concurrency: 3               # parallel builders (1..32)
  reviewers: 1
  default_review_policy: INDEPENDENT
workers:                       # templates, expanded into builder-1..N, reviewer-1, integration-1
  builder:
    provider: local-agent
    adapter: subprocess
    command: [python, -P, examples/stdin_print_cli_wrapper.py]
    env:
      BUILD_COORDINATOR_PRINT_CLI: {source: environment, variable: STAGEMESH_AGENT_CLI}
  reviewer:    {...}
  integration: {...}
task_sources:                  # optional adapters; off unless enabled
  github: {enabled: false, repo: owner/name}
```

A role with no template becomes an `unconfigured` worker, so the runner reports
`EXTERNAL_EXECUTOR_CONFIGURATION_REQUIRED` rather than pretending to work.
`runner_config:` may name a full runner config file instead of templates, and
`BUILD_COORDINATOR_RUNNER_CONFIG` still overrides both.

## Task definitions

```yaml
tasks:
  - id: PROJ-001                # stable identity; never reuse or rename
    title: Short imperative title
    objective: What and why.
    requirements: [...]
    acceptance_criteria: [...]  # at least one
    dependencies: [PROJ-000]
    priority: 10                # lower runs earlier among eligible tasks
    review: INDEPENDENT         # NONE | SELF | INDEPENDENT (TWO_REVIEWERS: see gaps)
    risk: MEDIUM                # LOW | MEDIUM | HIGH | CRITICAL
    scope: {allowed_paths: [src/feature/]}
    validation: [pytest tests/feature -q]
```

The whole backlog is validated before anything is written: malformed fields,
unknown keys, duplicate ids, self-dependencies and dependency cycles are all
reported together and nothing is synchronized.

## Synchronization rules

Synchronization is deterministic (sorted by task id) and idempotent (a task
that already matches makes no writes and emits no event). It reconciles by
stable task id, so tasks already in the durable queue are never duplicated.

| Result | Meaning |
|---|---|
| `CREATED` | new task queued as READY |
| `SKIPPED` | already in sync |
| `ADOPTED` | pre-existing identical task is now tracked by the project |
| `UPDATED` | definition refreshed on a task that is READY, BLOCKED or FAILED |
| `DEFERRED` | definition changed while the task has live work; not applied |
| `FINISHED` | definition changed after the task was DONE; ignored |
| `ORPHANED` | previously synced, no longer defined; left untouched |
| `ERROR` | e.g. a dependency defined nowhere; that task is not synchronized |

State, claims, leases, executions, checkpoints and evidence are never modified
by synchronization. Declared `priority` is recorded on the sync event and used
to order dispatch.

## Durable state written by an older coordinator

The database layer refuses a database that has coordinator tables but no
schema-version record. `stagemesh project migrate-state` is the explicit,
opt-in migration: it reports what would change, and with `--apply` takes a
consistent SQLite backup, rebuilds only tables whose definition changed
(rows copied verbatim), stamps the schema version, and refuses to run while
executions are live. SQLite only.

## Workspaces

With a project, every builder task gets its own branch (`stagemesh/<task-id>`)
started from the current `main_ref` inside a StageMesh-managed worktree
(`<state_dir>/worktrees/<worker>`). A resumed task (rework, recovery)
re-attaches to its existing branch. Uncommitted leftovers are stashed, never
discarded, so one task's commits cannot leak into another task's integration.
Reviewer and integration workers get managed worktrees too.

## Known gaps

- `TWO_REVIEWERS` is accepted and requires review, but the runner performs a
  single review, exactly as for `INDEPENDENT` (backlog task `SM-011`). Use
  `INDEPENDENT` until it is enforced.
- Task branches and their worktrees are not garbage-collected after
  integration.
- Concurrency is bounded per worker role (builders by `execution.concurrency`,
  reviewers by `execution.reviewers`, one integrator); it is not adaptive to
  provider rate limits (`SM-002`, `SM-003`).
