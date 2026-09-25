# Why StageMesh?

Coding agents are increasingly capable of implementing real software changes. The hard problem shifts from "can a model write code?" to "can engineering work remain safe, durable, reviewable, and recoverable across many agents, providers, failures, and repository changes?"

StageMesh is built for that second problem.

## The distinction

A coding agent reasons about and changes code.

A runtime or session manager keeps an agent process alive.

CI checks repository conditions.

A task tracker stores work items.

**StageMesh coordinates the engineering lifecycle across those systems.**

It owns durable task state, stage routing, leases, checkpoints, validation evidence, independent review, exact-SHA review authority, integration safety, provider failover, and restart recovery.

## Core principle

> **Stages belong to StageMesh. Agents and execution runtimes are replaceable infrastructure.**

An implementation can be performed by Codex, Claude, Grok, another coding agent, or a future runtime without changing the meaning of implementation, review, or integration.

The coordinator decides what stage exists and what evidence is required. The agent performs the bounded work assigned to that stage.

## Why not just run one coding agent?

For a small interactive task, that may be enough.

StageMesh becomes useful when engineering work must survive conditions such as:

- multiple tasks executing concurrently;
- a provider hitting quota or becoming temporarily unavailable;
- an agent process disappearing mid-task;
- implementation requiring independent review;
- review applying only to one exact commit SHA;
- main moving between implementation and integration;
- a task needing remediation and re-review;
- work resuming after coordinator restart;
- multiple projects sharing the same machine and agent runtimes.

Those conditions require durable coordination rather than a longer prompt.

## Why not treat agent output as authoritative?

Because "done" is a claim, not evidence.

StageMesh advances work from recorded engineering evidence: task state, git state, validation results, review verdicts tied to exact SHAs, integration checks, and durable lifecycle events.

That separation lets an agent be useful without giving it authority over the entire engineering process.

## Relationship to adjacent tools

StageMesh is intentionally complementary.

| Tool category | Primary job | StageMesh relationship |
|---|---|---|
| Coding agent | Reason about and modify code | StageMesh assigns bounded stage work |
| Agent/session runtime | Keep processes/sessions running | StageMesh treats runtimes as replaceable executors |
| CI | Run repository checks | StageMesh can consume validation/integration evidence |
| GitHub Issues | Express objectives/tasks | StageMesh can import and synchronize work |
| Git/worktrees | Isolate source changes | StageMesh uses them as engineering safety primitives |

A useful shorthand is:

> Session infrastructure keeps agents alive. StageMesh makes their engineering work durable, governed, and reviewable.

## What StageMesh is not

StageMesh is not a chatbot, not a model router disguised as a workflow engine, and not a generic queue.

It understands software-engineering stages, git/worktrees, validation, review authority, integration, recovery, and provider/runtime failure semantics.

## Design goals

StageMesh aims to make these properties boring and deterministic:

- provider-neutral execution;
- durable task ownership;
- resumable work;
- explicit stage boundaries;
- independent review;
- exact-SHA authority;
- safe concurrent integration;
- observable provider failover;
- restart/idempotency safety;
- minimal human intervention for recoverable engineering failures.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the internal model and [REAL_WORLD_VALIDATION.md](REAL_WORLD_VALIDATION.md) for evidence-driven scenarios.
