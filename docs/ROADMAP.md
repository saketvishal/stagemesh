# Roadmap

## v0.1-alpha (shipped)

The initial standalone release of StageMesh. All items below are
**IMPLEMENTED AND PROVEN** unless marked otherwise.

### Core infrastructure (complete)
- [x] Task lifecycle (create, claim, checkpoint, transition, recover, integrate)
- [x] Builder capacity enforcement
- [x] Migration-capable task serialization
- [x] Independent reviewer separation
- [x] Structured JSON result ingestion
- [x] Checkpoint / resume (worker replacement)
- [x] SQLite and PostgreSQL support
- [x] FakeExecutor for deterministic testing

### Provider-neutral execution (complete)
- [x] Worker configuration: provider / model / runtime / capabilities / stages
- [x] Capability-based deterministic stage routing
- [x] SubprocessExecutor (stdin-prompt delivery, result-file contract)
- [x] Codex CLI worker execution proven

### Autonomous objective lifecycle (complete)
- [x] Planner agent decomposes goal into child tasks
- [x] Gate-triggered follow-on planning
- [x] Objective status and progress visibility

### Location-independent CLI (complete)
- [x] Works from any working directory (not just repo root)
- [x] Launcher scripts for Linux/macOS/Windows

### Pending final acceptance
- [ ] Final independent release verification
- [ ] Cross-provider independent review execution when provider runtimes are available

---

## v0.2.0a1 (current)

Self-hosting bootstrap repair, multi-provider routing, and project-owned
backlog work landed since v0.1-alpha. Items marked complete are
**IMPLEMENTED AND PROVEN**; the rest remain open.

### Self-hosting and multi-provider routing (complete)
- [x] StageMesh runs against its own backlog (self-hosting)
- [x] Multi-provider routing (Codex, Claude) with honest capability declarations
- [x] Generic dependency parsing for task backlogs (range/list expansion)
- [x] P0 priority scheduling that overrides ordinary backlog ordering
- [x] Windows process-tree termination (Job Objects) for reliable worker cleanup

### Watcher / unattended operation
- [ ] Windows Task Scheduler integration (unattended startup)
- [ ] GitHub label provisioning for task tracking
- [ ] Transient failure backoff and retry

### Expanded provider support
- [ ] Additional runtime adapters (Antigravity, Grok) beyond command-layer stubs
- [ ] Formal multi-provider acceptance matrix

### Observability
- [ ] Structured event streaming
- [ ] Coordinator metrics endpoint

### Configuration
- [ ] Hot-reload worker config without restart
- [ ] Worker health checks

### CI utilization
- [ ] Work-conserving scheduling during external CI wait (tracked in #65)

---

## Not planned for this project

The following are out of scope for the generic coordinator and belong in
consuming product layers:

- Legal reasoning, legal document processing
- Attorney marketplace or court filing automation
- Payments
- Domain-specific gate types
- Product-branded launchers
- Custom integration artifact formats

---

## Publication gate

Publication requires an explicit human approval decision. The remaining blockers
before a v0.2 stable publication are:

1. Final independent release verification
2. Human approval gate

StageMesh will not be published automatically when acceptance completes.
