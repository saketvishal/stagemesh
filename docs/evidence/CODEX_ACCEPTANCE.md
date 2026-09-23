# Codex Execution Acceptance Evidence

## Status: PROVEN for Codex execution; published under v0.1.0-alpha

Real Codex CLI execution through StageMesh has been proven. This document
records what was actually demonstrated.

---

## What was proven

### 1. Authenticated Codex CLI execution

A real Codex CLI session authenticated with a ChatGPT/Codex account was used
to execute coordinator-dispatched work. The session was not a fake or stub.

### 2. Direct Codex smoke

A direct Codex CLI smoke test confirmed the CLI executable runs, accepts a
prompt via stdin or via the prompt-file adapter, and produces output.

### 3. Coordinator-to-Codex execution

StageMesh's SubprocessExecutor successfully launched the Codex CLI as a worker,
delivered the role prompt, and received a structured result JSON at the
coordinator-generated result path.

### 4. Structured result ingestion

StageMesh successfully parsed, validated, and ingested the structured JSON
result produced by the Codex worker. Identity fields (`execution_id`, `task_id`,
`role`) were verified against the coordinator's expected values. Contradictory
or malformed results were rejected.

### 5. Concurrent worker launches

Multiple Codex worker instances were launched concurrently by the coordinator.
Capacity enforcement (`max_active_builders`) and claim isolation were verified
under concurrent execution.

---

## What remains pending

### Final independent release verification

This baseline completed independent final verification prior to publication of v0.1.0-alpha.

### Cross-provider independent review

Cross-provider independent review execution has not been completed for this
candidate. StageMesh is designed to be provider-neutral; the independent review
gate applies equally to any provider once that provider is configured and
available.

**Claim NOT made:** `SELF_HOSTING_PROVEN`. That claim requires additional
acceptance evidence and is not asserted for this release candidate.

---

## Evidence integrity note

This document describes real execution results. It does not claim capabilities
that were not exercised. The result-contract hardening candidate at commit
`9baed1cddc510f46af322e1a53455d3711e15d1f` is a separate, unreviewed candidate
that is **not** represented as accepted here.

---

## Acceptance architecture

StageMesh uses a separate independent-reviewer model to validate implementation
quality. The independent reviewer:
- Is a different worker (`worker_id`) than the implementer
- May be a different agent/provider
- Cannot be the same worker that performed the implementation
- Reviews the feature branch SHA captured at review-claim time
- Has their review invalidated if the feature branch HEAD changes after review

This architecture supports cross-provider independent review: the reviewer is
not the implementer, and they may run on a different provider.
