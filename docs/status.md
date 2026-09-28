# StageMesh vNext Status

## Implemented

- Installable Python package with local no-network build backend.
- SQLite persistence with migrations and durable records for tasks, claims, executions, candidates, evidence, source cache, and objectives.
- Deterministic lifecycle state machine for `PLAN -> IMPLEMENT -> VALIDATE -> REVIEW -> INTEGRATE -> DONE`.
- One-active-claim enforcement per task.
- Durable candidate and exact-SHA evidence model.
- Fake and subprocess executors.
- Provider adapter SDK surface for Codex, Claude, Grok, and additional runtime commands.
- Capacity registry with primary/secondary failover classification.
- Local backlog source and GitHub issue-source models, including deferred, unknown, stale, auth, and rate-limit semantics.
- Structured objective planner validation.
- Built-in validation, independent review, and integration evidence.
- Recovery behavior that preserves uncertain process identity and does not treat PID alone as proof of liveness or death.
- CLI for init, doctor, planning, continue, status, health, capacity, and CI gates.
- Security boundary helper for workspace path checks.
- Worker registry and heartbeat records for durable worker visibility.
- Global multi-project registry file.
- Outbound source synchronization event log that never becomes lifecycle truth.
- Operator report command for dashboard/status integration.
- Dependency-aware scheduling for planned tasks.
- PostgreSQL-ready persistence backend interface.
- CI wait decision helper that releases worker capacity while checks are pending.
- Static HTML dashboard rendering from operator status.
- Persistent review findings and bounded remediation attempts.
- Release archive and manifest generation for public/demo packaging.
- Durable distributed work packets with poll and acknowledgement commands.
- Final report generation command with candidate, acceptance, and remaining-action inventory.
- Transport-injected GitHub client for live inbound/outbound sync acceptance.
- Git attribution helper for worker-owned authorship and StageMesh-owned commits.
- Config loader for project files and environment overrides.
- Secret redaction helpers for logs/reports.
- Live acceptance harness that exercises configured GitHub/providers or reports not configured.
- Machine-readable local acceptance report generator.
- Release packaging is constrained to the project workspace boundary.
- Idempotent migration runner with schema version reporting and migration audit table.
- SQLite durability settings enable WAL and busy timeout.
- Backend probe for sqlite/postgres configuration with explicit PostgreSQL dependency reporting.
- Machine-readable completion audit that refuses to mark external/credentialed requirements complete without evidence.
- Persisted audit events with redacted JSONL export for shareable operational evidence.
- GitHub Actions CI for Windows and Linux.
- Stdlib invariant and acceptance runners for dependency-free verification.

## Verified Locally

- `python -m compileall -q src scripts build_backend.py`
- `python scripts/invariants.py`
- `python scripts/acceptance.py`
- `python -m stagemesh.cli --project . ci --future-feature-gate`
- `python -m pip install . --target .tmp-install --no-cache-dir --upgrade`

## Still Required For Full Product Acceptance

- Live GitHub API acceptance with credentials, outbound issue synchronization, and permission/auth matrix.
- Full production provider adapters for Codex, Claude, Grok, and other approved workers.
- Distributed worker transport and remote lease renewal.
- Operator dashboard and richer status UI.
- PostgreSQL implementation behind the persistence interface.
- Real CI execution results from GitHub-hosted Windows and Linux runners.
- Public release artifacts and contributor demo packaging.
- Deeper security hardening for sandbox, secrets, git attribution, and multi-project registry operation.
