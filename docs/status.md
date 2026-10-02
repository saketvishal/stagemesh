# StageMesh vNext Status

## Implemented

- Installable Python package with local no-network build backend.
- SQLite persistence with migrations and durable records for tasks, claims, executions, candidates, evidence, source cache, and objectives.
- Deterministic lifecycle state machine for `PLAN -> IMPLEMENT -> VALIDATE -> REVIEW -> INTEGRATE -> DONE`.
- One-active-claim enforcement per task.
- Durable candidate and exact-SHA evidence model.
- Fake and subprocess executors.
- Provider adapter SDK surface for Codex, Claude, Grok, and additional runtime commands with bounded command validation and durable execution records.
- Configured provider commands are converted into runtime adapters for plain/JSON live acceptance.
- Capacity registry with primary/secondary failover classification and structured visibility.
- Configurable provider routing with SINGLE_AGENT and STAGED modes plus per-stage provider routes.
- Local backlog, configured JSON and Google AX export task-source adapters, and GitHub issue-source models, including zero-config remote detection, deferred, unknown, stale, auth, and rate-limit semantics.
- Structured objective planner validation, including duplicate task and unknown dependency rejection.
- Contract-first AI-native change control for real-provider runs, with mandatory acceptance criteria, explicit path allowlists, deterministic validation commands, change-size bounds, dependency controls, and excluded-work/invariant guidance.
- Coding-agent implementation isolation in detached Git worktrees; target checkout mutation is deferred until the integration gate.
- Full candidate-lineage scope validation, including multi-commit changes and both source/destination sides of Git renames and copies.
- Real validation executes contract commands against the exact candidate SHA in a fresh detached worktree and records structured evidence/findings.
- Independent deterministic review recomputes contract compliance and requires exact-SHA validation evidence rather than trusting the implementation worker.
- Bounded remediation returns failed tasks to implementation for at most three attempts and bases each repair on the latest failed candidate.
- Production integration performs a real Git fast-forward or merge; failed integration blocks the task with durable findings.
- Real-provider execution can fail over across configured coding agents only for provider/capacity failures; implementation failures do not trigger provider shopping.
- Built-in validation, independent review, and integration evidence.
- Recovery behavior that preserves uncertain process identity and does not treat PID alone as proof of liveness or death.
- CLI for plain/JSON init, doctor, planning, continue, status, config, and health with blocked-task and degraded execution counts, capacity, and plain/JSON CI gates.
- Security boundary helper for workspace path checks, generated outputs, configured sources, demos, and objective input files.
- Worker registry and heartbeat records for durable plain/JSON worker visibility.
- Global multi-project registry file with ambiguity, duplicate-state, and db-path boundary validation plus structured JSON listing.
- Outbound source synchronization event log that never becomes lifecycle truth.
- Structured plain/JSON operator report command for dashboard/status integration.
- Dependency-aware scheduling for planned tasks.
- PostgreSQL-ready persistence backend interface.
- CI wait decision helper and plain/JSON CLI command that release worker capacity while checks are pending.
- Static HTML dashboard rendering with health metrics, stage/status breakdowns, attention rows, task, worker, source-event, retry, and external-evidence tables plus structured artifact summary output.
- Persistent review findings and bounded remediation attempts.
- Release archive, manifest, and SHA256SUMS generation for public/demo packaging with structured JSON summary output.
- Contributor demo project scaffold command with local objective, run instructions, and structured artifact summary output.
- Release manifests include tracked source files, sizes, and SHA-256 hashes while excluding runtime state and symlink escapes.
- Durable distributed work packets with plain/JSON enqueue, list, poll, lease renewal, stale-claim recovery, acknowledgement, packet export, and ack-envelope import commands.
- Final report generation command with candidate, acceptance, remaining-action inventory, and structured JSON summary output.
- Transport-injected GitHub client for live inbound/outbound sync acceptance.
- Git attribution helper for worker-owned authorship and StageMesh-owned commits.
- Config loader for project files and environment overrides.
- Secret redaction helpers for logs/reports, provider command display, and credentialed database URLs.
- Live acceptance harness that exercises configured GitHub/providers, reports not configured, and fails malformed provider command definitions with structured JSON.
- Live acceptance reports unproven sync/execution gates explicitly instead of treating command availability as execution proof.
- Machine-readable local acceptance report generator with structured proof gaps and proof status, with artifact and direct JSON output.
- Release packaging is constrained to the project workspace boundary.
- Idempotent migration runner with schema version reporting and migration audit table.
- SQLite durability settings enable WAL and busy timeout.
- Backend probe for sqlite/postgres configuration with redacted database URL output, explicit PostgreSQL dependency reporting, and JSON schema-contract visibility.
- Machine-readable completion audit that refuses to mark external/credentialed requirements complete without evidence, with artifact and direct JSON output.
- Persisted audit events with redacted JSONL export and JSON inspection for shareable operational evidence.
- Durable retry/backoff registry for source/provider failures with plain/JSON mutation and inspection.
- CLI-generated dashboard, reports, audits, and acceptance artifacts are constrained to the selected project boundary.
- Optional psycopg-backed PostgreSQL store facade with explicit backend probe, ping/migrate commands, schema contract, and migration path.
- Final report now summarizes machine-readable acceptance and completion-audit artifacts when present.
- Deterministic plain/JSON provider dry-run acceptance proves capacity-aware failover and failure isolation without external credentials.
- Deterministic plain/JSON GitHub dry-run acceptance proves discovery, deferred labels, outbound sync, and rate-limit classification without external credentials.
- Release-readiness report aggregates local gates, structured live proof gaps, external evidence records, and explicit evidence gaps with artifact and direct JSON output.
- External evidence registry idempotently records hosted CI, live provider, live GitHub, and database acceptance links against candidate SHAs, with deduped structured add and candidate-match listing.
- Passing external evidence must name a candidate SHA and only counts for the matching candidate.
- Acceptance matrix artifact maps each major requirement to proven local evidence or an explicit external gap, with direct JSON output.
- End-to-end acceptance artifact maps the requested 18 clean-environment acceptance steps to local proof evidence, with direct JSON output.
- Completion audit and acceptance matrix consume durable external evidence records when available.
- Final report summarizes local checks, candidate-scoped evidence, structured proof gaps, completion audit, acceptance matrix, and end-to-end acceptance state.
- GitHub Actions CI for Windows and Linux.
- Stdlib invariant and acceptance runners for dependency-free verification.
- Clean-tree acceptance copies only tracked files, installs StageMesh into an isolated target, and runs CLI smoke checks from the installed package.
- Local `stagemesh ci` covers compile, invariants, provider acceptance, GitHub acceptance, structured live acceptance, clean acceptance, adversarial AI change-control acceptance, and optional full acceptance.

## Verified Locally

- `python -m compileall -q src scripts build_backend.py`
- `python scripts/invariants.py`
- `python scripts/acceptance.py`
- `python scripts/clean_acceptance.py`
- `python scripts/change_control_acceptance.py`
- `python -m stagemesh.cli --project . ci --future-feature-gate`
- `python -m pip install . --target .tmp-install --no-cache-dir --upgrade`

Current generated reports show local checks passing while proof remains blocked on external evidence:

- acceptance report: `PASS proof=BLOCKED_ON_EXTERNAL_EVIDENCE`
- completion audit: `complete=False`
- acceptance matrix: `INCOMPLETE`
- end-to-end acceptance: `COMPLETE`

## Still Required For Full Product Acceptance

- Live GitHub API acceptance with credentials, outbound issue synchronization, and permission/auth matrix.
- Live end-to-end acceptance with installed and authenticated Codex/Claude/Grok (or other approved) provider CLIs on the operator machine.
- Networked distributed worker transport beyond local durable queue plus file-envelope handoff.
- Networked/live operator dashboard UI beyond the generated static HTML status dashboard.
- Live PostgreSQL acceptance against a real service behind the persistence interface.
- Candidate-scoped hosted CI evidence recorded for the release candidate.
- Public release publishing destination and hosted artifact URLs.
- Deeper security hardening for sandbox, secrets, git attribution, and multi-project registry operation.
