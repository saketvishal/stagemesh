# Roadmap

StageMesh is in public alpha. The current development package version is `0.2.0a1`.

This roadmap distinguishes proven capability from active hardening and longer-term work. Public claims should remain evidence-driven.

## Proven foundation

The following capabilities have deterministic tests and/or execution evidence:

- durable task lifecycle, claims, leases, checkpoints, events, and recovery;
- SQLite and PostgreSQL state backends;
- project-owned `.stagemesh/` configuration and backlog discovery;
- location-independent `stagemesh` CLI;
- provider-neutral worker configuration;
- capability/stage-based deterministic routing;
- subprocess execution with structured result contracts;
- independent-reviewer separation;
- exact-SHA review/integration invariants;
- autonomous objective planning/execution lifecycle;
- Codex CLI execution acceptance;
- git/worktree isolation and guarded integration.

See [REAL_WORLD_VALIDATION.md](REAL_WORLD_VALIDATION.md) and [evidence/](evidence/) for the evidence-oriented documentation surface.

## Active reliability hardening

Current work focuses on making real multi-provider and concurrent execution safer and more autonomous.

Areas include:

- objective/task source correctness and idempotent reconciliation;
- risk-aware affected-test selection and modular validation;
- provider failure/quota/rate-limit classification and auditable diagnostics;
- provider-health recovery and failover accounting;
- automatic merge-conflict recovery followed by validation and exact-SHA re-review;
- lifecycle-label provisioning and outbound GitHub synchronization hardening;
- external-CI reconciliation;
- self-development acceptance and restart/idempotency coverage.

The GitHub issue tracker is the authoritative source for exact task status and acceptance criteria.

## Public adoption and distribution

Planned/ongoing repository-readiness work includes:

- clear public positioning and examples;
- structured bug/feature issue intake;
- reproducible real-world validation evidence;
- consistent release/version metadata;
- automated CI suitable for pull requests and main;
- first tagged GitHub prerelease;
- verified package publication/install path;
- repository discovery metadata (topics/homepage);
- community support/discussion surface when outside usage begins.

See [PUBLIC_RELEASE_CHECKLIST.md](PUBLIC_RELEASE_CHECKLIST.md).

## Future execution/runtime work

Potential future areas:

- additional coding-agent/runtime adapters;
- richer provider acceptance matrix;
- executor integrations for persistent/session-oriented runtimes;
- structured event streaming and observability;
- coordinator metrics;
- configurable/hot-reload worker policy;
- broader external task-source integrations.

## Out of scope for the generic coordinator

The following belong in consuming products rather than StageMesh core:

- legal reasoning or legal document processing;
- product-specific domain intelligence;
- attorney/court/payment workflows;
- private customer logic;
- domain-specific orchestration that cannot be expressed generically.

StageMesh should remain a generic engineering coordination layer.
