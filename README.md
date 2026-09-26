# StageMesh

StageMesh is a provider-neutral control plane for multi-agent software engineering:
it owns the stages (plan -> build -> independent review -> integrate), routes work
by capability, and recovers when agents disappear.

> **Status: v0.2.0a1 public alpha**
>
> StageMesh is Apache-2.0 and usable for alpha testing, but it is still early:
> PyPI publication is gated by the release checklist, cross-provider independent
> final verification is pending, and operators should expect rough edges around
> setup, provider configuration, and production hardening.

**Demo:** the launch-readiness demo asset is not present in this repository yet.
When it lands, this section should link to that artifact rather than duplicating
the walkthrough.

```bash
git clone <repo-url>
cd <repo-directory>
pip install -e ".[dev]"
stagemesh --version
```

This from-source install is the path exercised by this repository's own test
suite and is the verified alpha install path today. PyPI publication
(`python -m pip install stagemesh`) is gated by the release checklist; see
[PyPI Release Evidence](docs/evidence/PYPI_RELEASE.md) for the current
publication status and the brand-new environment smoke that will be recorded
once the package is published.

Once installed, initialize a repository and let the coordinator own execution:

```bash
cd my-git-repo
stagemesh init
stagemesh agent setup
stagemesh continue
```

## Why StageMesh

| Question | StageMesh | Swarm harnesses | CrewAI/LangGraph apps |
|---|---|---|---|
| Stage owner | Coordinator-owned stages. | Often prompt/script emergent. | App-authored graph flow. |
| Worker choice | Deterministic capability routing. | Often agent or prompt policy. | Graph/tool routing. |
| Self-review | Builder cannot review itself. | Harness dependent. | Graph dependent. |
| Results | Structured JSON contracts. | Often transcripts/logs. | App-specific state. |
| Recovery | Durable leases, checkpoints, state. | Usually custom glue. | Persistence dependent. |
| Provider swaps | Same process, replaceable executors. | Adapter work likely. | Tied to app graph. |

StageMesh is not trying to be another "agent swarm." The public wedge is the
control plane: stages belong to the coordinator, agents are replaceable
executors, and the engineering process survives provider changes.

Start deeper with [Architecture](docs/ARCHITECTURE.md) or
[Setup](docs/SETUP.md).

---

## Package and CLI name

The public Python distribution and primary CLI are `stagemesh`. The Python
import package remains `build_coordinator`, and the compatibility CLI alias
`build-coordinator` is retained for early alpha adopters.

---

## What it is

StageMesh is infrastructure for running AI coding agents on structured
engineering tasks. It:

- Persists task state, dependencies, leases, checkpoints, review claims, and events
- Enforces independent-reviewer separation: the agent that built cannot review
- Routes tasks to the right agent based on declared capability, not by asking an LLM to decide
- Recovers tasks whose agents disappeared without losing progress
- Accepts structured result JSON from agents, never free-form stdout
- Keeps stages (planning -> implementation -> review -> integration) strictly separated
- Escalates to a human when a decision genuinely requires one

**Stages belong to the coordinator. Agents are replaceable executors.**

Run `stagemesh continue` from any directory: inside a project it works that
project; anywhere else it coordinates every registered project at once, each in
its own process and workspaces, up to that project's configured concurrency.
Use `stagemesh continue --capacity N` outside a project to allocate a bounded
global builder budget fairly across registered project backlogs for that run.
`stagemesh "Continue <project> development."` works too. Workers, worktrees,
branches, providers, and execution directories are chosen by StageMesh's queue
and capability routing, never by you. See [Project-owned backlogs](docs/PROJECTS.md).

---

## What is proven (v0.2.0a1 alpha)

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
| Codex CLI worker execution (OpenAI) | **IMPLEMENTED AND PROVEN** - authenticated CLI, smoke, JSON ingestion |
| Cross-provider independent review | **IMPLEMENTED; FINAL INDEPENDENT VERIFICATION PENDING** |

Evidence:

- [Codex Acceptance](docs/evidence/CODEX_ACCEPTANCE.md) - what has actually been demonstrated
- [PyPI Release Evidence](docs/evidence/PYPI_RELEASE.md) - package naming and release checklist

---

## What it is not

- Not a product feature
- Not a legal-reasoning surface
- Not a case-scoped API
- Not a generic task queue: it knows about git, worktrees, code review, and integration

---

## Documentation

- [Architecture](docs/ARCHITECTURE.md) - concepts, data flow, key principles
- [Setup](docs/SETUP.md) - installation, configuration, first run
- [Project-owned backlogs](docs/PROJECTS.md) - project definitions and parallel execution
- [Security & Trust Boundaries](SECURITY.md) - what the coordinator trusts and why
- [Roadmap](docs/ROADMAP.md) - alpha roadmap and what comes next
- [Contributing](CONTRIBUTING.md) - how to contribute
- [Examples](examples/README.md) - configuration examples

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
