# Roadmap

## v0.1-alpha (current)

The initial standalone release of the Build Coordinator. All items below are
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
- [ ] Cross-provider independent review (Claude / Antigravity headless)
  — *blocked by provider availability, not by implementation*

---

## v0.2 (planned — not yet started)

> Items below are **ROADMAP only**. Nothing below has been implemented.

### Watcher / unattended operation
- [ ] Windows Task Scheduler integration (unattended startup)
- [ ] GitHub label provisioning for task tracking
- [ ] Transient failure backoff and retry

### Expanded provider support
- [ ] Additional Codex runtime adapters
- [ ] Formal multi-provider acceptance matrix

### Observability
- [ ] Structured event streaming
- [ ] Coordinator metrics endpoint

### Configuration
- [ ] Hot-reload worker config without restart
- [ ] Worker health checks

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

Publication requires an explicit human approval decision. The remaining
blocker before v0.1-alpha publication is:

1. Cross-provider independent review acceptance (one or more providers)
2. Human approval gate

The coordinator will not be published automatically when acceptance completes.