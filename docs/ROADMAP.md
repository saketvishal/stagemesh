# Roadmap

## v0.1-alpha (current)

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
- [x] Claude Code worker execution path accepted through the headless runtime profile and structured result contract
- [x] Provider failover and cross-provider recovery
- [x] Cross-provider independent review enforcement
- [x] Concurrent execution under configured capacity

### Autonomous objective lifecycle (complete)
- [x] Planner agent decomposes goal into child tasks
- [x] Gate-triggered follow-on planning
- [x] Objective status and progress visibility

### Location-independent CLI (complete)
- [x] Works from any working directory (not just repo root)
- [x] Launcher scripts for Linux/macOS/Windows

### Public alpha evidence complete
- [x] Independent release review
- [x] Global invocation across registered projects
- [x] Cleanup after integration
- [x] Deterministic validation before review
- [x] Sanitized GitHub delivery dry-run

---

## v0.2 (planned; not yet started)

> Items below are **ROADMAP only**. Nothing below has been implemented.

### Watcher / unattended operation
- [ ] Windows Task Scheduler integration (unattended startup)
- [ ] GitHub label provisioning for task tracking
- [ ] Transient failure backoff and retry

### Expanded provider support
- [ ] Additional runtime adapters
- [ ] Published live GitHub delivery evidence beyond dry-run payload validation
- [ ] Antigravity or other GUI-first runtime support, only after reliable headless automation is proven

### Observability
- [ ] Structured event streaming
- [ ] Coordinator metrics endpoint

### Configuration
- [ ] Hot-reload worker config without restart
- [ ] Worker health checks

### Optimization experiments (research)
- [ ] Optional Google Ax adapter for controlled routing/policy experiments,
      built on evidence-driven routing (#49); see
      [docs/design/GOOGLE_AX_INTEGRATION.md](design/GOOGLE_AX_INTEGRATION.md).
      Google Ax is not, and will not become, a required StageMesh dependency.

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
before v0.1-alpha publication are:

1. Human approval gate
2. PyPI release checklist completion

StageMesh will not be published automatically when acceptance completes.
