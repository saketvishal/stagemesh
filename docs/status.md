# StageMesh vNext Status

## Implemented

- Installable Python package with local no-network build backend.
- SQLite persistence with migrations and durable records for tasks, claims, executions, candidates, evidence, source cache, and objectives.
- Deterministic lifecycle state machine for `PLAN -> IMPLEMENT -> VALIDATE -> REVIEW -> INTEGRATE -> DONE`.
- One-active-claim enforcement per task.
- Durable candidate and exact-SHA evidence model.
- Machine-enforced change contracts with allowed, forbidden, excluded, protected, dependency-manifest, invariant, gate-command, and change-size enforcement.
- Provider prompts include the task change contract so implementation agents receive the same scope that validators and reviewers enforce.
- Per-task implementation runs in isolated Git worktrees outside the shared project checkout; candidates are committed from the task workspace and bound to exact SHA evidence.
- Fake and subprocess executors.
- Provider adapter SDK surface for Codex, Claude, Grok, and additional runtime commands with bounded command validation and durable execution records.
- Project-local capability registry with bounded local vendoring and full-tree integrity pins for reviewed skill packages, disabled-by-default registration, bounded skill attachment, redacted process-start activation receipts for per-invocation validated skills-only Claude Code plugins, observable skill loading, and Codex/Claude read-only stdio MCP adapters that disable ambient MCP configuration, enforce exact tool allowlists, and record redacted invocation receipts.
- Configured provider commands are converted into runtime adapters for plain/JSON live acceptance.
- Capacity registry with primary/secondary failover classification and structured visibility.
- Configurable provider routing with SINGLE_AGENT and STAGED modes plus per-stage provider routes.
- Local backlog, configured JSON and Google AX export task-source adapters, and GitHub issue-source models, including zero-config remote detection, deferred, unknown, stale, auth, and rate-limit semantics. Interactive GitHub initialization can use the authenticated GitHub CLI without persisting its token and safely classify open issues that lack a StageMesh admission label, including issues with ordinary team labels. Existing installations migrate the missing safe admission default while explicit opt-outs, labels, blocking/deferred decisions, objective roots, and task-source policy are preserved; initialization cannot claim ready when required write authorization is unavailable.
- Run-window deterministically materializes and audits a missing plannable task contract before conflict-aware admission; read-only planning never writes the contract.
- Structured objective planner validation, including duplicate task and unknown dependency rejection.
- Disabled-by-default, identity-bound planning-spec and bounded challenge gates with typed revise/human-decision outcomes, human approval binding, challenger write isolation, read-only operator status, and exact-baseline accepted context delivered to implementation and independent review with digest-only evidence. Optional configured planner/challenger roles run in disposable exact-baseline checkouts, require distinct provider and command identities, return bounded stdout JSON, and receive execution-bound receipts before StageMesh alone writes new artifacts; manual artifacts remain supported.
- Provider-neutral [objective governance](objective-governance.md) freezes bounded append-only objective plans, success-criterion-linked dependency DAGs, exact-plan independent challenges, finding-preserving revision reconciliation, and deterministic independent outcome evaluations in workspace-isolated immutable artifacts with digest-only audit receipts. Hermetic controls qualify the policy only; Gate 0, executed-role independence, the full Gate 1 demonstration and adaptive research remain unproven.
- Built-in deterministic validation, independent contract review, and integration evidence.
- Opt-in provider-neutral visual-reference governance binds approved project revisions, typed screen/state/viewport coverage, immutable reference/fixture/tolerance/gate/package inputs, comparison thresholds, actual/reference/diff artifacts, functional gates, remediation classification, operator status, and checksum-revalidated independent-review handoff without adding a browser or design-provider dependency to core.
- Integration refuses `DONE` unless exact-SHA validation and review evidence already passed and the candidate still satisfies the contract.
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
- Bounded remediation state supports terminal rework semantics instead of unbounded repair loops.
- Release archive, manifest, and SHA256SUMS generation for public/demo packaging with structured JSON summary output.
- Contributor demo project scaffold command with local objective, run instructions, and structured artifact summary output.
- Release manifests include tracked source files, sizes, and SHA-256 hashes while excluding runtime state and symlink escapes.
- Durable distributed work packets with plain/JSON enqueue, list, poll, lease renewal, stale-claim recovery, acknowledgement, packet export, and ack-envelope import commands.
- Final report generation command with candidate, acceptance, remaining-action inventory, and structured JSON summary output.
- Transport-injected GitHub client for live inbound/outbound sync acceptance.
- Git attribution helper for worker-owned authorship and StageMesh-owned commits.
- Config loader for project files and environment overrides.
- Secret redaction helpers for logs/reports, provider command display, credentialed database URLs, and bounded custom-provider failure/review evidence with defense-in-depth legacy audit and cooldown projections.
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
- Hosted CI diagnosis can enrich failed GitHub Actions checks with bounded, credential-safe, redacted job logs; third-party checks and unavailable logs remain gate-level evidence and cannot satisfy strict baseline-detail policy.
- Supervised integration consults exact candidate/base hosted checks when explicit GitHub credentials are configured; bounded failed-job reruns spend a durable budget only after GitHub accepts the request.
- StageMesh-owned destructive reset/clean recovery paths preserve work first, apply deterministic policy, and record command receipts; arbitrary provider-issued Git commands remain outside that interception boundary.
- Passing external evidence must name a candidate SHA and only counts for the matching candidate.
- Acceptance matrix artifact maps each major requirement to proven local evidence or an explicit external gap, with direct JSON output.
- End-to-end acceptance artifact maps the requested 18 clean-environment acceptance steps to local proof evidence, with direct JSON output.
- Completion audit and acceptance matrix consume durable external evidence records when available.
- Final report summarizes local checks, candidate-scoped evidence, structured proof gaps, completion audit, acceptance matrix, and end-to-end acceptance state.
- GitHub Actions CI for Windows and Linux.
- Stdlib invariant and acceptance runners for dependency-free verification.
- Clean-tree acceptance copies only tracked files, installs StageMesh into an isolated target, and runs CLI smoke checks from the installed package.
- Local `stagemesh ci` covers compile, invariants, provider acceptance, GitHub acceptance, structured live acceptance, clean acceptance, and optional full acceptance.
- Canary regression tests cover forbidden/out-of-scope edits, dependency-manifest changes without gates, unrelated change-size expansion, exact-SHA evidence mismatch prevention, reviewer and validator false-positive prevention, integration evidence prerequisites, provider prompt contract injection, and isolated task workspaces.

## Verified evidence snapshot (2026-11-05)

- The latest recorded complete local Windows baseline before PR #680 was `4247 passed, 3 skipped, 26 subtests passed` in
  `6451.68s` on commit `123aa659`; it is performance evidence for that exact commit, not current-main full-suite evidence.
- PR #680 exact head `7dd2e32a5d76e7d4c12985ab8ac1da4ef26191ae` passed the protected full StageMesh CI on Linux and Windows before merge.
- PR #680 merged to `main` as `08c671e3b7093eb4dc67a939e781eef4ec5f2dae`; its post-merge push smoke run
  `41939786942` completed successfully in about one minute rather than repeating the full PR regression.
- The exact PR head also had `82` focused planner/runner/workflow/documentation tests passing locally, plus separately recorded
  component evidence of `459` capability tests and `107` task-source tests.

These observations do not prove real-provider operation, the ten-task Founder Hands-Off streak, every external service, or the current
full-regression performance target. See `founder-hands-off.md` and `validation-components.md` for those boundaries.

## Still Required For Full Product Acceptance

- Project-specific live GitHub permission/outbound synchronization acceptance; read-only check-run and job-log behavior already has
  bounded live evidence.
- Full production provider adapters for Codex, Claude, Grok, and other approved workers.
- Networked distributed worker transport beyond local durable queue plus file-envelope handoff.
- Networked/live operator dashboard UI beyond the generated static HTML status dashboard.
- Live PostgreSQL acceptance against a real service behind the persistence interface.
- Faster authoritative full-regression execution without weakening Windows/Linux coverage (tracked separately by the regression-performance program).
- Public release publishing destination and hosted artifact URLs.
- Deeper security hardening for sandbox, secrets, git attribution, and multi-project registry operation.
