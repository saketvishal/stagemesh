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
  bootstrap:                   # optional workspace preparation; see below
    timeout_seconds: 900
    required_tools: [npm, python]
    commands:
      - npm install
      - command: python -m pip install -r requirements.txt
        timeout_seconds: 300
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

`execution.bootstrap` is optional project-owned workspace preparation.
StageMesh runs its commands in order once per prepared task workspace, without
a shell, before the agent starts and separately from task validation commands.
Each command records durable `runner.setup` evidence with output tails,
duration, exit code, and failure type (`TIMEOUT`, `COMMAND_UNAVAILABLE`, or
`EXIT_CODE`). A failing bootstrap command blocks the task (`SETUP_FAILED`)
instead of launching the agent. `stagemesh doctor` checks declared
`required_tools` and command executables so missing local tools are visible
before dispatch.

For compatibility, `execution.setup: [cmd, ...]` remains accepted as shorthand
for bootstrap commands. Prefer `execution.bootstrap` for new projects because
it can declare required tools and per-command timeouts.

`execution.setup` is an optional list of commands StageMesh runs once (no
shell, with a timeout and durable evidence recorded as a `runner.setup`
event) in a task's workspace after it is prepared and before the agent
starts — for dependency-heavy projects where a fresh worktree has no
`node_modules`/virtualenv and validation commands can't otherwise run. A
failing setup command blocks the task (`SETUP_FAILED`) instead of launching
the agent. Projects without `execution.setup` behave exactly as before.

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
    review: INDEPENDENT_WORKER  # NONE | SELF | INDEPENDENT_WORKER | INDEPENDENT_PROVIDER | TWO_REVIEWERS | TWO_PROVIDERS
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
executions are genuinely live. When an older database contains stale
`LAUNCHED`/`RUNNING` rows from a dead coordinator, migration reports an
auditable stale-execution reconciliation plan. With `--apply`, StageMesh first
backs up the database, marks exactly those stale execution rows `LOST`, keeps
claims, checkpoints, branches and worktree metadata intact, then retries the
migration so normal recovery can resume the task. Legacy `INDEPENDENT` review
policy rows are rewritten to `INDEPENDENT_WORKER`, preserving the same effective
worker-independence safety with explicit semantics. SQLite only.

## Workspaces

With a project, every builder task gets its own branch (`stagemesh/<task-id>`)
started from the current `main_ref` inside a StageMesh-managed worktree
(`<state_dir>/worktrees/<worker>`). A resumed task (rework, recovery)
re-attaches to its existing branch. Uncommitted leftovers are stashed, never
discarded, so one task's commits cannot leak into another task's integration.
Reviewer and integration workers get managed worktrees too.

## Global mode and the registry

`stagemesh project add <path>` records a project root in a per-user registry
(`~/.build-coordinator/projects.json`, or `$STAGEMESH_HOME`); `list` and
`remove <name|path>` manage it. The registry is discovery metadata plus optional
machine-local execution environment (`--path-prepend <dir>`, `--env K=V`, e.g. a
virtualenv); it is never the backlog. `stagemesh continue` outside any project
starts one process per registered project, so state, workspaces and failures never
cross projects and a broken or blocked project does not stop the others. Add
`--capacity N` to allocate a bounded global builder budget fairly across those
registered project backlogs for one run; each project still treats its own
`execution.concurrency` as the maximum.

## Agents

`stagemesh agent setup` probes every known runtime (Codex CLI, Claude Code, ...)
and marks it READY only if it completed a real headless task; installed-but-not-
logged-in, and GUI-only tools (with the evidence), are reported as such and are
never used. Results live in the user's StageMesh home; no credentials are stored,
agent CLIs keep their own sessions. With no `workers:` in `project.yaml`,
StageMesh builds each project's worker pool from the verified runtimes and routes
each stage by capability and policy (`CODING`, `CODE_REVIEW`, ...), balancing across
providers and auditing every decision. `workers:` may still list explicit templates
(`runtime: codex`, or a custom `command:`) when a project needs to constrain them.
A runtime that fails (auth, rate limit, quota, network) is routed around for a
cooldown and its task resumes on another; when nothing is eligible the task waits and
`stagemesh doctor` says why.

Agents work in the task worktree and are told not to commit: StageMesh commits their
changes and derives the result (commits, files) from git, so a self-report is never
the lifecycle result. Reviewers run read-only and return a structured verdict.

## Lifecycle guarantees

* **Validation** - a task's `validation:` commands are run by StageMesh itself in the
  task workspace after the builder finishes (no shell, timeout, captured output as
  durable evidence). Failure sends the task to rework; only a pass proceeds to review.
* **Review** - `INDEPENDENT` is a legacy alias for `INDEPENDENT_WORKER`.
  `SELF` needs one eligible approval. `INDEPENDENT_WORKER` needs one approval
  from a different reviewer `worker_id` for the exact feature SHA.
  `INDEPENDENT_PROVIDER` needs one approval from a different provider.
  `TWO_REVIEWERS` needs two distinct reviewer workers, and `TWO_PROVIDERS`
  needs approvals from two distinct providers. A rejected review goes rework ->
  validation -> re-review automatically.
* **Integration** - deterministic and runner-owned: the reviewed commit is merged into
  `main_ref` in StageMesh's own worktree and the branch advanced safely. A `main_ref`
  checked out by a human is only fast-forwarded when clean; conflicts and dirty checkouts
  become typed `HUMAN_ACTION_REQUIRED` states.
* **Upstream** - local-only by default. `upstream: {remote: origin, push: true}` makes
  push part of integration: a task is DONE only after a successful push. A failed push
  is durable (task BLOCKED `UPSTREAM_PUSH_FAILED`, merge kept) and is retried on every
  `continue` or with `stagemesh project retry-push`.
* **Recovery** - a worker process that dies, or a provider that fails, releases its claim;
  another worker resumes from the task branch (uncommitted progress becomes a
  work-in-progress commit) instead of restarting. Repeated failure escalates.
* **Cleanup** - after a verified integration the task branch is deleted; unfinished,
  blocked or dirty workspaces are kept.

`delivered_by:` on a task definition closes work that landed outside StageMesh, with an
audit trail (events attributed to the sync, no invented execution evidence).

## Diagnostics and upkeep

`stagemesh doctor [project]` checks installation, agents, registration, discovery, the
backlog, git/worktrees, durable-state schema, concurrency and upstream, with a next step
for each problem. `stagemesh upgrade` runs `pip install --upgrade stagemesh`; durable
state is only migrated by the explicit `project migrate-state`. `stagemesh --version`.

## Known limitations

- Validation runs inside the coordination loop, so a long suite delays dispatch of other
  work in the same project (`SM-017`).
- Fresh workspaces have no installed dependencies; dependency-heavy validation needs
  workspace setup commands (`SM-016`).
- GUI-only agent tools (for example Antigravity IDE) have no headless mode and are not used.
- Concurrency is per role and not adaptive to provider rate limits beyond the failure
  cooldown.
