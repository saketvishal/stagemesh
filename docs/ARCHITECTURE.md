# Architecture

## Core principle

> **Stages belong to the coordinator. Agents are replaceable executors.**

The coordinator owns the task lifecycle. Agents receive a prompt via stdin and
write a structured JSON result to a runner-generated path. The coordinator
never reads free-form stdout for lifecycle decisions.

---

## Key concepts

### Task

A bounded unit of engineering work. Every task has:

- **task_id** — stable identifier
- **title** and **description** — human-readable
- **acceptance_criteria** — what done means
- **ownership_scope** — project, primary module, allowed/forbidden paths
- **review_policy** — `SELF`, `INDEPENDENT`, or `TWO_REVIEWERS`
- **risk_level** — `LOW`, `MEDIUM`, or `HIGH`
- **dependencies** — other task IDs that must complete first
- **migration_allowed** — whether this task may introduce schema migrations

### Claim

An exclusive lease on a task for a specific worker. Claims expire; the
coordinator recovers expired claims and makes the task available again.
Checkpoints written during a claim are preserved so a replacement worker
can continue without losing progress.

### Objective

A high-level goal decomposed by a planner agent into a validated set of child
tasks. An objective-generated task is a normal task that flows through the
same claim/review/integrate path as any other task. The objective layer adds
only: (1) planner decomposition, and (2) gate-triggered follow-on planning.

### Stage

A discrete phase of work:

```
planning → implementation → remediation* → review → integration
```

`* remediation` is triggered when a reviewer returns `REMEDIATION_REQUIRED`.
The maximum remediation cycle count is configurable (default: 2).

### Worker

An operator-configured agent profile. A worker has:

- `worker_id` — stable identifier
- `role` — `PLANNER`, `BUILDER`, `REVIEWER`, `REMEDIATION`, `INTEGRATION`
- `provider` — operator label (not a vendor enum — coordinator logic must not switch on it)
- `runtime` — the harness used to launch the agent (`subprocess`, `fake`, `unconfigured`)
- `model` — optional model hint (coordinator does not inspect or validate it)
- `capabilities` — what this worker can do (see below)
- `stages` — which stages this worker is eligible for
- `max_concurrency` — how many simultaneous tasks this worker can run
- `permissions` — additional capabilities like `SCM_WRITE`

### Capability

An abstract property of a worker, not a vendor name:

```
CHEAP            — low-cost inference
FAST             — low-latency inference
CODING           — can write code
ADVANCED_REASONING — can reason about complex problems
CODE_REVIEW      — can review code for correctness and security
SECURITY_REVIEW  — can specifically evaluate security properties
SCM_OPERATOR     — can operate git (merge, push, branch management)
ARCHITECTURE     — can evaluate architectural decisions
```

### Routing

Deterministic worker selection for a stage. The routing policy evaluates:

1. Is the worker enabled?
2. Does the worker's stage eligibility include the requested stage?
3. Does the worker have the required capabilities for the stage?
4. Does the worker have the required permissions?
5. Is the worker pinned (explicit worker/provider/model constraint)?
6. If there are multiple eligible workers, preference order and fallback policy apply.

**No LLM makes routing decisions.** Routing is a pure function of operator
configuration and current worker availability.

### Provider neutrality

`provider` is an operator label. Coordinator domain logic never switches on
Anthropic, OpenAI, xAI, Google, or any other vendor name.

Vendor-specific CLIs appear only in examples (as `command` illustrations).
Replace `codex exec --json` or `grok --json` with whatever agent CLI is
installed in your environment.

---

## Data flow

```
Operator
  └─ creates tasks (upsert_task)
       └─ coordinator: deps satisfied? capacity available? migration slot free?
            └─ worker claims task (claim_task)
                 └─ runner assembles role prompt
                      └─ agent receives prompt via stdin
                           └─ agent writes result JSON to BUILD_COORDINATOR_RESULT_PATH
                                └─ runner validates and ingests result
                                     └─ coordinator transitions task state
```

## State machine

```
PENDING → READY → IN_PROGRESS → VALIDATING → REVIEW_READY
                                              │
                             ┌────────────────┘
                             ▼
                       [REVIEWER claims]
                             │
                    ┌────────┴──────────────┐
                    ▼                       ▼
              GREEN / GREEN_WITH_NOTES   REMEDIATION_REQUIRED
                    │                       │
              INTEGRATION_READY       [back to READY, max N cycles]
                    │
              [INTEGRATION claims]
                    │
                COMPLETED
```

Human escalations are represented as task blockers. A blocked task does not
advance until the operator resolves the blocker.

## Persistence

The coordinator requires a SQL database (SQLite or PostgreSQL). SQLite is
the default for single-machine use. PostgreSQL is recommended for
production multi-machine deployments.

Schema is initialized by `build_coordinator.cli status` or
equivalently `build_coordinator.db.initialize_schema()`.

## Result contract

Workers write a single JSON object to `BUILD_COORDINATOR_RESULT_PATH` (a path
generated by the runner and passed via environment variable).

Common fields:
```json
{
  "schema_version": 1,
  "execution_id": "<must match BUILD_COORDINATOR_EXECUTION_ID>",
  "task_id": "<must match BUILD_COORDINATOR_TASK_ID>",
  "role": "<must match BUILD_COORDINATOR_ROLE>",
  "status": "SUCCEEDED | FAILED | HUMAN_ACTION_REQUIRED | TERMINATED | LOST",
  "completed_at": "<ISO-8601>"
}
```

See [examples/README.md](../examples/README.md) for role-specific fields.

## Extension architecture

The coordinator is designed to be consumed by a product layer:

```
Your product extensions
        ↑
  (extension interfaces)
        ↑
  StageMesh (this package)
```

Product-specific capabilities (custom integration artifacts, domain seed data,
custom gate types) belong in the consuming layer.
