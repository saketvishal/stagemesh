# Codex Execution Acceptance Evidence

## Status: PROVEN for accepted public alpha execution

Real Codex CLI execution through StageMesh has been proven. The accepted
post-alpha public evidence also covers the multi-provider coordinator behavior
that does not require secrets or network access: Claude runtime support through
the headless profile/result contract, provider failover, cross-provider
recovery, independent review, concurrency, cleanup, global invocation, and
deterministic validation.

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

### 6. Claude Code runtime support

Claude Code is represented as a supported headless runtime profile for the
Anthropic provider. Acceptance coverage verifies the profile, stage matrix,
structured result contract, provider routing, and recovery semantics. Readiness
on an operator machine still requires `stagemesh agent setup` to complete a
live headless probe.

### 7. Provider failover and interruption recovery

Accepted public dogfood coverage proves that a provider failure preserves
claim history and checkpoints, then lets a replacement provider receive resume
context and continue without losing prior work.

### 8. Cross-provider independent review

Cross-provider independent review is implemented and acceptance-covered. The
coordinator rejects same-provider review for `INDEPENDENT_PROVIDER`, counts
distinct providers for `TWO_PROVIDERS`, records exact reviewed SHAs, and audits
the routing preference for a reviewer from a different provider.

### 9. Global invocation, cleanup, and deterministic validation

Accepted public dogfood coverage proves global capacity allocation across
registered projects, post-integration cleanup that refuses unmerged branches,
and deterministic validation gates that run before review and fail closed.

---

## Claims not made

- **Self-hosting proven:** not claimed. `SELF_HOSTING_PROVEN` requires
  additional acceptance evidence.
- **Live GitHub delivery:** not claimed. Public evidence covers sanitized
  dry-run outbound payloads only.
- **Antigravity/headless support:** not claimed. GUI-only runtimes remain
  unsupported until a reliable headless automation path is proven.
- **Universal provider readiness:** not claimed. Each operator machine must
  pass runtime-specific live headless probes before a runtime is marked ready.

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
