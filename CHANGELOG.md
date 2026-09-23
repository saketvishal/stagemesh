# Changelog

## v0.1-alpha release candidate

> Publication requires explicit human approval after final independent
> verification. This candidate has not been published.

### Summary

First standalone release candidate for StageMesh, extracted from its origin
monorepo and generalized for independent use.

The public project name is StageMesh. For this alpha, the distribution, CLI,
Python module, and config names remain `build-coordinator` / `build_coordinator`
to avoid unnecessary compatibility breakage before public release.

### What is included

**IMPLEMENTED AND PROVEN**

- Task lifecycle: create, claim, checkpoint, transition, recover, review, integrate
- Builder capacity enforcement (configurable, concurrency-safe)
- Migration-capable task serialization (one active migration at a time)
- Independent reviewer separation (implementer cannot review their own work)
- Two-reviewer policy support
- Checkpoint / resume: replacement workers continue without redoing completed work
- Structured JSON result contract: coordinator never trusts free-form stdout
- Provider-neutral worker configuration (provider / model / runtime / capabilities / stages)
- Capability-based deterministic stage routing; no LLM makes routing decisions
- Autonomous objective lifecycle: planner agent decomposes a goal into child tasks
- Location-independent CLI (`build-coordinator` works from any working directory)
- SQLite (default) and PostgreSQL support
- FakeExecutor for deterministic testing without real agent invocations
- SubprocessExecutor for real agent commands
- Codex CLI worker execution evidence: authenticated session, smoke, coordinator-to-Codex,
  structured result ingestion, concurrent worker launches
- OSS boundary scanner (`build_coordinator.oss_boundary`) to detect private-IP drift

**IMPLEMENTED; FINAL INDEPENDENT VERIFICATION PENDING**

- Cross-provider-capable independent review mechanics. Completed cross-provider
  independent final verification is not claimed for this release candidate.

### Architecture principles established

- Stages belong to the coordinator; agents are replaceable executors
- Strict separation: provider / model / runtime / worker profile / capability / stage / routing policy
- Secrets never enter config files, databases, checkpoints, events, logs, or results
- Human gates are explicit, durable, and cannot be bypassed by automation

### Generic OSS boundary

The standalone coordinator excludes product-specific application code, domain
seed data, branded launchers, and product-specific gate semantics. Product
layers can integrate through extension interfaces without coupling those
concerns into this package.

### Extension architecture

StageMesh is designed to be consumed by a product layer through extension
interfaces. Product-specific capabilities belong in the consuming layer, not in
this package.

---

All earlier development history is private to the origin repository and is not
reproduced here.
