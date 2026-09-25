# StageMesh

**Durable engineering execution across AI coding agents, models, and runtimes.**

StageMesh is a provider-neutral coordinator for autonomous software-engineering workflows. It gives coding agents a durable lifecycle with task ownership, recovery, validation, independent review, exact-SHA authority, provider failover, and controlled integration.

> **Status: Public Alpha**
>
> Current development version: `0.2.0a1`.
>
> StageMesh is published under the Apache 2.0 license. Public capability claims are intentionally evidence-driven.

```text
GitHub issue / objective
          ↓
       StageMesh
          ↓
  planning / routing
          ↓
 implementation
          ↓
   validation
          ↓
 independent exact-SHA review
          ↓
 remediation if required
          ↓
 safe integration
          ↓
        DONE
```

> **Stages belong to StageMesh. Agents and execution runtimes are replaceable infrastructure.**

---

## Why StageMesh?

A coding agent can write code. That does not by itself provide durable task ownership, independent review, exact-SHA authority, provider failover, restart safety, or safe concurrent integration.

StageMesh owns those engineering stages and evidence boundaries while allowing the underlying coding agents and runtimes to remain replaceable.

Use StageMesh when work must survive conditions such as:

- several engineering tasks running concurrently;
- a provider becoming unavailable or exhausting quota;
- an agent disappearing mid-task;
- implementation requiring independent review;
- remediation creating a new SHA that needs re-review;
- `main` moving between implementation and integration;
- coordinator restart;
- multiple projects sharing machine/provider capacity.

Read [Why StageMesh?](docs/WHY_STAGEMESH.md) for the architectural distinction from coding agents, session managers, CI, and task trackers.

---

## What it does

StageMesh:

- persists task state, dependencies, leases, checkpoints, review claims, executions, and events;
- routes work deterministically by stage, capability, permissions, provider health, and policy;
- keeps planning, implementation, validation, remediation, review, and integration responsibilities separate;
- enforces independent-reviewer separation;
- ties review authority to exact implementation SHAs;
- recovers work when an executor disappears;
- records typed provider/runtime failures and can route around unavailable providers;
- isolates work with git branches/worktrees;
- blocks unsafe integration instead of silently merging conflicts or SHA drift;
- accepts structured execution results rather than treating free-form agent confidence as lifecycle authority;
- escalates when automation cannot proceed safely.

---

## What is proven

> [!IMPORTANT]
> Only claims backed by execution evidence appear below. StageMesh does not claim every planned self-hosting, provider-health, conflict-recovery, or validation capability is complete.

| Capability | Status |
|---|---|
| Task lifecycle (create, claim, checkpoint, review, integrate) | **IMPLEMENTED AND PROVEN** |
| Builder capacity enforcement | **IMPLEMENTED AND PROVEN** |
| Migration serialization | **IMPLEMENTED AND PROVEN** |
| Independent reviewer separation | **IMPLEMENTED AND PROVEN** |
| Structured JSON result ingestion | **IMPLEMENTED AND PROVEN** |
| Checkpoint / resume (worker replacement) | **IMPLEMENTED AND PROVEN** |
| Provider-neutral worker configuration | **IMPLEMENTED AND PROVEN** |
| Capability-based stage routing | **IMPLEMENTED AND PROVEN** |
| Autonomous objective lifecycle | **IMPLEMENTED AND PROVEN** |
| Location-independent CLI | **IMPLEMENTED AND PROVEN** |
| SQLite and PostgreSQL support | **IMPLEMENTED AND PROVEN** |
| Codex CLI worker execution | **IMPLEMENTED AND PROVEN** |
| Cross-provider independent review | **IMPLEMENTED; FINAL INDEPENDENT VERIFICATION PENDING** |

See [Real-world validation](docs/REAL_WORLD_VALIDATION.md) for sanitized scenarios that have shaped the coordinator.

---

## Quick start

For the current public alpha, install from source:

```bash
git clone https://github.com/saketvishal/stagemesh.git
cd stagemesh
python -m pip install -e .
```

Then initialize a project:

```bash
cd /path/to/my-git-repo
stagemesh init
stagemesh agent setup
stagemesh doctor
stagemesh continue
```

What those commands do:

- `stagemesh init` creates/registers a StageMesh project.
- `stagemesh agent setup` discovers installed coding-agent runtimes and verifies them with real headless checks.
- `stagemesh doctor` reports what is usable and what needs attention.
- `stagemesh continue` works the project's backlog using StageMesh routing, worktrees, lifecycle rules, and configured concurrency.

Run `stagemesh continue` inside one project to work that project. From outside a project, StageMesh can coordinate registered projects independently.

See [Setup](docs/SETUP.md) and [Project-owned backlogs](docs/PROJECTS.md).

---

## Typical flow

```text
READY
  ↓
CLAIMED
  ↓
IN_PROGRESS
  ↓
VALIDATING
  ↓
REVIEW_READY
  ↓
REVIEWING
  ├──> REWORK_REQUIRED -> remediation -> validation -> review
  └──> integration
                 ↓
                DONE
```

Additional typed states cover blocked, failed, stale/resumable, waiting-for-input, external-CI, and other lifecycle conditions.

A task is not DONE because an agent says it is finished. StageMesh requires the configured engineering evidence for progression.

---

## Examples

Start with the example closest to your environment:

- [Single-agent configuration](examples/single_agent/)
- [Staged multi-agent configuration](examples/staged_multi_agent/)
- [Validation / review model](examples/validation_review_model/)
- [Recovery checkpoints](examples/recovery_checkpoint/)
- [All examples](examples/README.md)

---

## Documentation

- [Why StageMesh?](docs/WHY_STAGEMESH.md) — product/architecture positioning
- [Architecture](docs/ARCHITECTURE.md) — concepts, data flow, key principles
- [Setup](docs/SETUP.md) — installation, configuration, first run
- [Projects](docs/PROJECTS.md) — `.stagemesh/` project definitions and parallel execution
- [Real-world validation](docs/REAL_WORLD_VALIDATION.md) — sanitized evidence-driven scenarios
- [Security & trust boundaries](SECURITY.md)
- [Roadmap](docs/ROADMAP.md)
- [Contributing](CONTRIBUTING.md)
- [Code of Conduct](CODE_OF_CONDUCT.md)

### Evidence

- [Codex acceptance](docs/evidence/CODEX_ACCEPTANCE.md)

---

## Core concepts

| Concept | Description |
|---|---|
| **Task** | A bounded unit of engineering work with acceptance criteria, ownership scope, and review policy |
| **Claim** | An exclusive lease on a task for a specific worker |
| **Checkpoint** | Structured progress metadata saved during execution |
| **Objective** | A high-level goal decomposed into bounded child work |
| **Stage** | A coordinator-owned phase such as planning, implementation, remediation, review, or integration |
| **Worker** | A configured execution profile: provider + runtime + model + capabilities + stages |
| **Routing** | Deterministic selection of an eligible worker for a stage |
| **Evidence** | Recorded engineering facts that authorize lifecycle progression |

---

## What StageMesh is not

- Not a chatbot.
- Not a legal/product reasoning surface.
- Not a generic task queue.
- Not a model-selection prompt.
- Not a substitute for CI, git, coding agents, or runtime/session tools.

It coordinates those pieces into a durable engineering lifecycle.

---

## Package and compatibility names

The public project and CLI are `StageMesh` / `stagemesh`.

The Python import package and some compatibility configuration names still use `build_coordinator` during the alpha period to avoid unnecessary breakage while the public surface stabilizes.

---

## Contributing

Bug reports and feature requests use structured GitHub issue templates. Please provide deterministic, sanitized evidence where possible and never include credentials or private project data.

See [CONTRIBUTING.md](CONTRIBUTING.md), [AGENTS.md](AGENTS.md), and [SECURITY.md](SECURITY.md).

---

## License

Apache-2.0. See [LICENSE](LICENSE).
