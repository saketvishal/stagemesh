# StageMesh

Durable engineering execution across AI agents, models, and runtimes.

StageMesh is a provider-neutral coordinator for autonomous multi-agent software
engineering workflows.

> **Status: v0.1.0-alpha (Public Alpha)**
>
> StageMesh is published under the Apache 2.0 license. This first public alpha
> establishes durable engineering orchestration across AI agents, models, and runtimes.

---

## Package and CLI name

For this first alpha, the Python distribution and primary CLI are `stagemesh`.
The Python import package remains `build_coordinator`, and the compatibility
CLI alias `build-coordinator` is retained to avoid unnecessary breakage during
early alpha adoption.

---

## What it is

StageMesh is infrastructure for running AI coding agents on structured
engineering tasks. It:

- Persists task state, dependencies, leases, checkpoints, review claims, and events
- Enforces independent-reviewer separation (the agent that built cannot review)
- Routes tasks to the right agent based on capability, not by asking an LLM to decide
- Recovers tasks whose agents disappeared without losing progress
- Accepts structured result JSON from agents, never free-form stdout
- Keeps stages (planning -> implementation -> review -> integration) strictly separated
- Escalates to a human when a decision genuinely requires one

**Stages belong to the coordinator. Agents are replaceable executors.**

---

## What is proven (v0.1-alpha)

> [!IMPORTANT]
> Only claims backed by execution evidence appear below. StageMesh does not claim
> `SELF_HOSTING_PROVEN`, and cross-provider independent final verification is
> still pending.

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
| Autonomous objective lifecycle (plan -> execute -> review -> integrate) | **IMPLEMENTED AND PROVEN** |
| Location-independent CLI | **IMPLEMENTED AND PROVEN** |
| SQLite and PostgreSQL support | **IMPLEMENTED AND PROVEN** |
| Codex CLI worker execution (OpenAI) | **IMPLEMENTED AND PROVEN** - authenticated Codex CLI, direct smoke, coordinator-to-Codex execution, structured result ingestion, concurrent worker launches |
| Cross-provider independent review | **IMPLEMENTED; FINAL INDEPENDENT VERIFICATION PENDING** |

---

## What it is not

- Not a product feature
- Not a legal-reasoning surface
- Not a case-scoped API
- Not a generic task queue (it knows about git, worktrees, code review, and integration)

---

## Quick start

The public install path after the PyPI Trusted Publishing checklist is complete:

```bash
pip install stagemesh
cd my-git-repo
stagemesh init          # makes the repo a StageMesh project and registers it
stagemesh agent setup   # finds installed coding agents and verifies each with a real headless run
stagemesh doctor        # what works, what does not, and what to do next
stagemesh continue      # works the project's .stagemesh/ backlog with real agents
```

Until the `stagemesh` project is published on PyPI, do not treat the command
above as launch-ready; follow the release checklist in
[docs/evidence/PYPI_RELEASE.md](docs/evidence/PYPI_RELEASE.md).

Run `stagemesh continue` from any directory: inside a project it works that
project; anywhere else it coordinates every registered project at once, each in
its own process and workspaces, up to that project's configured concurrency.
`stagemesh "Continue <project> development."` works too. Workers, worktrees,
branches, providers and execution directories are chosen by StageMesh's queue
and capability routing, never by you. See [docs/PROJECTS.md](docs/PROJECTS.md).

See [docs/SETUP.md](docs/SETUP.md) for full installation and configuration.

Release verification and the PyPI Trusted Publishing checklist are recorded in
[docs/evidence/PYPI_RELEASE.md](docs/evidence/PYPI_RELEASE.md).

---

## Documentation

- [Architecture](docs/ARCHITECTURE.md) - concepts, data flow, key principles
- [Setup](docs/SETUP.md) - installation, configuration, first run
- [Project-owned backlogs](docs/PROJECTS.md) - `.stagemesh/` project definitions, `stagemesh continue`, parallel execution
- [Security & Trust Boundaries](SECURITY.md) - what the coordinator trusts and why
- [Roadmap](docs/ROADMAP.md) - v0.1-alpha roadmap and what comes next
- [Contributing](CONTRIBUTING.md) - how to contribute
- [Examples](examples/README.md) - configuration examples

## Evidence

- [Codex Acceptance](docs/evidence/CODEX_ACCEPTANCE.md) - what has actually been proven

---

## Core concepts

| Concept | Description |
|---|---|
| **Task** | A bounded unit of engineering work with acceptance criteria, ownership scope, and review policy |
| **Claim** | An exclusive lease on a task for a specific worker |
| **Checkpoint** | Structured progress metadata saved during execution |
| **Objective** | A high-level goal decomposed by a planner agent into a set of child tasks |
| **Stage** | A phase of work (planning, implementation, remediation, review, integration) |
| **Worker** | A configured agent profile: provider + runtime + model + capabilities + stages |
| **Routing** | Deterministic selection of an eligible worker for a stage; no LLM makes this choice |

---

## License

Apache-2.0. See [LICENSE](LICENSE).
