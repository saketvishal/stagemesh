# Roadmap

## v0.2.0a1 (current)

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

## Next alpha stabilization (planned; not yet started)

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

## Post-1.0 research / future work

> Items below are explicitly deferred from the current roadmap cut.

### Evidence-based policy learning with operator approval

StageMesh may use accumulated execution evidence to propose routing/policy
improvements after 1.0. The initial implementation is intentionally a proposal
and audit/reporting boundary, not a self-modifying policy engine.

The proposal model is:

- collect normalized evidence for provider/runtime success and failure rates,
  latency, remediation frequency, review outcomes, capability fit, trustworthy
  cost data, task/risk class, and environment/platform compatibility;
- produce explainable candidate changes, limited initially to routing preference
  recommendations over the existing deterministic policy inputs;
- evaluate proposals against held-out historical evidence or controlled
  experiments before an operator considers them;
- require explicit operator approval before any enforced policy changes;
- version the proposed policy and preserve rollback/audit metadata.

Guardrails:

- no autonomous self-modifying routing policy in the stabilization/current
  implementation;
- no black-box LLM provider choice: recommendations are derived from structured
  evidence summaries and deterministic ranking;
- learned preferences may never weaken review, security, capability, or
  permission requirements;
- sparse data is labeled as limited evidence and is not presented as a strong
  recommendation.

This builds on #49 evidence-driven routing and future cost/usage telemetry.
The inert implementation boundary lives in `build_coordinator/policy_learning.py`;
the coordinator runner does not import it to enforce policy.

### Bounded multi-model deliberation and council workflows

Optional deliberation may be explored for decisions where several independent
reasoning paths could improve operator confidence, without turning ordinary
implementation work into an expensive council.

Principles for any future design:

- Opt-in and policy-driven, never the default for ordinary implementation.
- Bounded participant and round counts.
- Exact task, context, and version identity for every participant.
- Separately preserved participant proposals and evidence.
- Deterministic, coordinator-owned final lifecycle decision.
- Explicit disagreement as evidence, not something silently averaged away.
- Operator policy controls when deliberation is allowed or required.
- Enforceable cost and latency budgets.
- Consensus is not proof of correctness; deterministic validation, exact-SHA
  review, and existing governance gates remain authoritative.

Candidate use cases:

- Architecture or security review.
- Ambiguous remediation findings.
- Planning alternatives for high-risk objectives.
- Tie-break review after documented reviewer disagreement.

Out of scope:

- Unconstrained swarm behavior.
- Recursive self-delegation.
- Replacing deterministic validation or exact-SHA review.
- Automatic consensus being treated as proof of correctness.

This should compose with review governance (#44) and later evidence/routing
work, while remaining a distinct optional workflow capability.

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
before v0.2.0a1 publication are:

1. Human approval gate
2. PyPI release checklist completion

StageMesh will not be published automatically when acceptance completes.
