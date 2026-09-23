# Codex Execution Acceptance Evidence

## Status: PROVEN (cross-provider review pending)

Real Codex CLI execution through the Build Coordinator has been proven.
This document records what was actually demonstrated.

---

## What was proven

### 1. Authenticated Codex CLI execution

A real Codex CLI session authenticated with a ChatGPT/Codex account was used
to execute coordinator-dispatched work. The session was not a fake or stub.

### 2. Direct Codex smoke

A direct Codex CLI smoke test confirmed the CLI executable runs, accepts
a prompt via stdin (or via the prompt-file adapter), and produces output.

### 3. Coordinator → Codex execution

The build coordinator's SubprocessExecutor successfully launched the Codex CLI
as a worker, delivered the role prompt, and received a structured result JSON
at the coordinator-generated result path.

### 4. Structured result ingestion

The coordinator successfully parsed, validated, and ingested the structured
JSON result produced by the Codex worker. Identity fields (execution_id,
task_id, role) were verified against the coordinator's expected values.
Contradictory or malformed results were rejected.

### 5. Concurrent worker launches

Multiple Codex worker instances were launched concurrently by the coordinator.
Capacity enforcement (max_active_builders) and claim isolation were verified
under concurrent execution.

---

## What remains pending

### Cross-provider independent review

Independent review execution via Claude (Anthropic) and Antigravity (Google)
headless modes was not completed during the initial acceptance window because
those provider runtimes were unavailable for unattended/headless use at the
time.

This item is **PENDING**, not abandoned. The coordinator is designed to be
provider-neutral; the independent review gate applies equally to any provider.

**Claim NOT made:** `SELF_HOSTING_PROVEN` — this claim requires cross-provider
acceptance to be complete.

---

## Evidence integrity note

This document describes real execution results. It does not claim capabilities
that were not exercised. The result-contract hardening candidate at commit
`9baed1cddc510f46af322e1a53455d3711e15d1f` is a separate, unreviewed candidate
that is **not** represented as accepted here.

---

## Acceptance architecture

The coordinator uses a separate independent-reviewer model to validate
implementation quality. The independent reviewer:
- Is a different worker (different worker_id) than the implementer
- May be a different agent/provider
- Cannot be the same worker that performed the implementation
- Reviews the feature branch SHA captured at review-claim time
- Has their review invalidated if the feature branch HEAD changes after review

This architecture is what "cross-provider independent review" tests: the
reviewer is not the implementer, and they may be running on a completely
different provider.