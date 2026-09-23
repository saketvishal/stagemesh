# Changelog

## v0.1-alpha (preparation complete — not yet published)

> Publication requires an explicit human approval gate after final cross-provider
> acceptance is complete.

### Summary

First standalone release of the Build Coordinator, extracted from its
origin monorepo and generalized for independent use.

### What is included

**IMPLEMENTED AND PROVEN**

- Task lifecycle: create, claim, checkpoint, transition, recover, review, integrate
- Builder capacity enforcement (configurable, concurrency-safe)
- Migration-capable task serialization (one active migration at a time)
- Independent reviewer separation (implementer cannot review their own work)
- Two-reviewer policy support
- Checkpoint / resume — replacement workers continue without re-doing completed work
- Structured JSON result contract — coordinator never trusts free-form stdout
- Provider-neutral worker configuration (provider / model / runtime / capabilities / stages)
- Capability-based deterministic stage routing — no LLM makes routing decisions
- Autonomous objective lifecycle: planner agent decomposes goal into child tasks
- Location-independent CLI (`build-coordinator` works from any working directory)
- SQLite (default) and PostgreSQL support
- FakeExecutor for deterministic testing without real agent invocations
- SubprocessExecutor for real agent commands
- Codex CLI worker execution proven: authenticated session, smoke, coordinator→Codex,
  structured result ingestion, concurrent worker launches
- OSS boundary scanner (`build_coordinator.oss_boundary`) to detect private-IP drift

**IMPLEMENTED — FINAL ACCEPTANCE PENDING**

- Cross-provider independent review (Claude / Antigravity headless execution pending
  due to provider unavailability during initial acceptance window)

### Architecture principles established

- Stages belong to the coordinator; agents are replaceable executors
- Strict separation: provider / model / runtime / worker profile / capability / stage / routing policy
- Secrets never enter config files, databases, checkpoints, events, logs, or results
- Human gates are explicit, durable, and cannot be bypassed by automation

### Private-IP boundary

The following Caventra-private components were excluded from the extraction:
- Product seed data and domain taxonomy
- Q-record integration artifact (extension point, not part of the generic coordinator)
- Product-branded launchers
- Caventra-specific gate semantics

### Extension architecture

The coordinator is designed to be consumed by a product layer through extension
interfaces. Product-specific capabilities (integration artifacts, custom gate types,
domain seed data) belong in the consuming layer, not in this package.

---

All earlier development history is private to the origin repository and
is not reproduced here.