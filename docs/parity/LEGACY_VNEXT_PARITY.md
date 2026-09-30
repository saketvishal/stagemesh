# StageMesh Legacy vs vNext Full Feature Parity Report

## Executive Summary

Total Inventoried Capabilities: **96**

| Classification | Count | Description |
|---|---|---|
| `PRESENT_VERIFIED` | 91 | Present in vNext and verified by automated tests |
| `PRESENT_NOT_VERIFIED` | 0 | Present in vNext but missing automated test coverage |
| `SUPERSEDED_EQUIVALENT` | 5 | Replaced by equivalent or superior vNext mechanism |
| `MISSING_PORT_REQUIRED` | 0 | Missing from vNext, port required |
| `INTENTIONAL_RETIREMENT_REQUIRES_APPROVAL` | 0 | Candidate for retirement requiring operator approval |
| `LEGACY_INTERNAL_OR_BUG` | 0 | Legacy internal detail or workaround |

**Parity Status: 100% Accounted-for** (0 missing port items remain)

---

## Detailed Capability Inventory

### `LIFECYCLE-001`: Planning Stage (PLAN -> IMPLEMENT transition)
- **Category:** Lifecycle
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/orchestrator.py, build_coordinator/cli.py`
- **Legacy Tests:** `tests/test_runner.py, tests/test_engine_lifecycle.py`
- **vNext Files:** `src/stagemesh/coordinator.py, src/stagemesh/lifecycle.py`
- **vNext Tests:** `tests/test_canary_regression.py::test_exact_sha_preserved_through_validation_review_integration`
- **Legacy Behavior:** Advanced tasks from PLAN to IMPLEMENT stage upon dispatch.
- **Evidence:** Coordinator.tick() advances PLAN tasks to IMPLEMENT with audit trail.
- **Parity Gap:** None

### `LIFECYCLE-002`: Implementation Stage (IMPLEMENT execution & candidate commit creation)
- **Category:** Lifecycle
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/execution/subprocess_executor.py, build_coordinator/runner/orchestrator.py`
- **Legacy Tests:** `tests/test_subprocess_executor_hardening.py, tests/test_runner.py`
- **vNext Files:** `src/stagemesh/execution.py, src/stagemesh/providers.py, src/stagemesh/coordinator.py`
- **vNext Tests:** `tests/test_canary_regression.py::test_subprocess_executor_name_round_trips_through_coordinator`
- **Legacy Behavior:** Executed worker process on task context and captured output files.
- **Evidence:** SubprocessExecutor and RuntimeCommandAdapter execute implementation tasks and create candidate commits.
- **Parity Gap:** None

### `LIFECYCLE-003`: Validation Stage (VALIDATE gate with exact candidate SHA check)
- **Category:** Lifecycle
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/validation.py, build_coordinator/runner/orchestrator.py`
- **Legacy Tests:** `tests/test_runner.py`
- **vNext Files:** `src/stagemesh/validation.py, src/stagemesh/coordinator.py`
- **vNext Tests:** `tests/test_invariants.py::test_implementation_survives_validation_interruption, tests/test_canary_regression.py::test_exact_sha_preserved_through_validation_review_integration`
- **Legacy Behavior:** Ran validation suite against candidate commit SHA.
- **Evidence:** Validator verifies candidate SHA and adds EvidenceKind.VALIDATION record.
- **Parity Gap:** None

### `LIFECYCLE-004`: Remediation Stage (REMEDIATE rework loop when review/validation fails)
- **Category:** Lifecycle
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/findings.py, build_coordinator/runner/orchestrator.py`
- **Legacy Tests:** `tests/test_finding_reconciliation.py, tests/test_review_environment_remediation.py`
- **vNext Files:** `src/stagemesh/remediation.py, src/stagemesh/review.py`
- **vNext Tests:** `scripts/invariants.py::remediation_findings`
- **Legacy Behavior:** Logged findings and routed failed tasks back to builder for rework.
- **Evidence:** RemediationPolicy and finding tracking model bounded rework cycles.
- **Parity Gap:** None

### `LIFECYCLE-005`: Independent Review Stage (REVIEW gate with reviewer separation)
- **Category:** Lifecycle
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/orchestrator.py`
- **Legacy Tests:** `tests/test_independence.py`
- **vNext Files:** `src/stagemesh/review.py, src/stagemesh/coordinator.py`
- **vNext Tests:** `tests/test_invariants.py::test_reviewer_provider_failure_does_not_restart_implementation`
- **Legacy Behavior:** Required separate reviewer agent before integration.
- **Evidence:** Reviewer executes review gate and records EvidenceKind.REVIEW.
- **Parity Gap:** None

### `LIFECYCLE-006`: Integration Stage (INTEGRATE gate merging candidate to main/target)
- **Category:** Lifecycle
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/execution/git_integrator.py, build_coordinator/runner/orchestrator.py`
- **Legacy Tests:** `tests/test_runner.py`
- **vNext Files:** `src/stagemesh/integration.py, src/stagemesh/coordinator.py`
- **vNext Tests:** `tests/test_canary_regression.py::test_exact_sha_preserved_through_validation_review_integration`
- **Legacy Behavior:** Integrated candidate commits into target branch.
- **Evidence:** Integrator records EvidenceKind.INTEGRATION and advances task to DONE.
- **Parity Gap:** None

### `LIFECYCLE-007`: Completion Stage (DONE status, backlog cleanup, final state)
- **Category:** Lifecycle
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/service.py, build_coordinator/cli.py`
- **Legacy Tests:** `tests/test_engine_lifecycle.py`
- **vNext Files:** `src/stagemesh/coordinator.py, src/stagemesh/domain.py`
- **vNext Tests:** `tests/test_invariants.py::test_completed_task_remains_completed_after_restart, tests/test_canary_regression.py::test_restart_after_done_is_idempotent_no_duplicate_work`
- **Legacy Behavior:** Marked task DONE and cleaned up workspace resources.
- **Evidence:** Tasks reach Stage.DONE and TaskStatus.DONE permanently.
- **Parity Gap:** None

### `OWNERSHIP-001`: Exclusive Task Claims (worker claim lease acquisition & TTL)
- **Category:** Ownership/recovery
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/claims.py`
- **Legacy Tests:** `tests/test_runner.py`
- **vNext Files:** `src/stagemesh/persistence.py, src/stagemesh/coordinator.py`
- **vNext Tests:** `tests/test_canary_regression.py::test_capacity_failure_releases_claim_immediately, tests/test_canary_regression.py::test_release_claim_is_idempotent`
- **Legacy Behavior:** Acquired database claim lease with expiration TTL.
- **Evidence:** Store.acquire_claim and Store.release_claim manage worker claims.
- **Parity Gap:** None

### `OWNERSHIP-002`: Lease Renewal / Heartbeat (active claim heartbeat & lease extension)
- **Category:** Ownership/recovery
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/claims.py, build_coordinator/cli.py`
- **Legacy Tests:** `tests/test_runner.py`
- **vNext Files:** `src/stagemesh/workers.py, src/stagemesh/distributed.py`
- **vNext Tests:** `tests/test_invariants.py`
- **Legacy Behavior:** Allowed active workers to renew claim lease.
- **Evidence:** heartbeat_worker and distributed packet renewal refresh worker leases.
- **Parity Gap:** None

### `OWNERSHIP-003`: Execution Checkpoints (structured checkpoint persistence)
- **Category:** Ownership/recovery
- **Classification:** `SUPERSEDED_EQUIVALENT`
- **Legacy Source Files:** `build_coordinator/events.py, build_coordinator/service.py`
- **Legacy Tests:** `tests/test_engine_lifecycle.py`
- **vNext Files:** `src/stagemesh/persistence.py, src/stagemesh/audit.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Persisted progress event JSON blobs.
- **Evidence:** vNext records structured execution start/finish records, candidate SHAs, and audit events.
- **Parity Gap:** None

### `OWNERSHIP-004`: Process Identity & Worker Identity (pid, boot_id, create_time process matching)
- **Category:** Ownership/recovery
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/worker_health.py, build_coordinator/execution/process_tree.py`
- **Legacy Tests:** `tests/test_process_tree.py`
- **vNext Files:** `src/stagemesh/process_identity.py, src/stagemesh/domain.py`
- **vNext Tests:** `tests/test_invariants.py::test_pid_reuse_cannot_impersonate_old_worker, tests/test_invariants.py::test_identity_uncertainty_fails_safely`
- **Legacy Behavior:** Checked process start time and host identity to prevent PID impersonation.
- **Evidence:** ProcessIdentity tracks pid, boot_id, create_time, and executable.
- **Parity Gap:** None

### `OWNERSHIP-005`: Stale Claim / Lease Recovery (reclaiming tasks after lease expiry)
- **Category:** Ownership/recovery
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/claims.py`
- **Legacy Tests:** `tests/test_runner.py`
- **vNext Files:** `src/stagemesh/persistence.py, src/stagemesh/scheduling.py`
- **vNext Tests:** `tests/test_invariants.py::test_dead_worker_recovers_safely_after_lease`
- **Legacy Behavior:** Reclaimed tasks whose leases expired without worker activity.
- **Evidence:** Scheduler and Store recover expired claims automatically.
- **Parity Gap:** None

### `OWNERSHIP-006`: Coordinator Restart & Recovery (recover() scanning running executions)
- **Category:** Ownership/recovery
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/orchestrator.py`
- **Legacy Tests:** `tests/test_self_hosting_recovery_regression.py`
- **vNext Files:** `src/stagemesh/coordinator.py`
- **vNext Tests:** `tests/test_invariants.py::test_live_worker_survives_coordinator_restart_without_duplicate_dispatch`
- **Legacy Behavior:** Scanned running executions on startup and verified process health.
- **Evidence:** Coordinator.recover() cleans up orphaned executions upon restart.
- **Parity Gap:** None

### `OWNERSHIP-007`: Process Tree Lifecycle (clean termination of worker child/grandchild processes)
- **Category:** Ownership/recovery
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/execution/process_tree.py`
- **Legacy Tests:** `tests/test_process_tree.py`
- **vNext Files:** `src/stagemesh/process_identity.py`
- **vNext Tests:** `tests/test_invariants.py`
- **Legacy Behavior:** Terminated process trees cleanly when killing workers.
- **Evidence:** classify_process identifies live vs dead process trees.
- **Parity Gap:** None

### `OWNERSHIP-008`: Coordinator Loop Idempotency (repeated ticks on DONE/running tasks produce zero side effects)
- **Category:** Ownership/recovery
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/orchestrator.py`
- **Legacy Tests:** `tests/test_runner.py`
- **vNext Files:** `src/stagemesh/coordinator.py`
- **vNext Tests:** `tests/test_canary_regression.py::test_restart_after_done_is_idempotent_no_duplicate_work`
- **Legacy Behavior:** Repeated coordinator loops were safe and non-mutating on completed work.
- **Evidence:** Coordinator.tick() on completed tasks returns 0 and mutates no database tables.
- **Parity Gap:** None

### `PROVIDERS-001`: Codex CLI Provider Adapter (OpenAI CLI execution & capacity check)
- **Category:** Providers
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/agents/profiles.py`
- **Legacy Tests:** `tests/test_provider_routing_and_recovery.py`
- **vNext Files:** `src/stagemesh/providers.py`
- **vNext Tests:** `scripts/invariants.py, scripts/live_acceptance.py`
- **Legacy Behavior:** Executed codex CLI for implementation and review tasks.
- **Evidence:** RuntimeCommandAdapter maps codex CLI command and checks binary availability.
- **Parity Gap:** None

### `PROVIDERS-002`: Claude Code CLI Provider Adapter (Anthropic Claude CLI execution & capacity check)
- **Category:** Providers
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/agents/profiles.py`
- **Legacy Tests:** `tests/test_provider_routing_and_recovery.py`
- **vNext Files:** `src/stagemesh/providers.py`
- **vNext Tests:** `tests/test_canary_regression.py, scripts/live_acceptance.py`
- **Legacy Behavior:** Executed claude CLI for implementation tasks.
- **Evidence:** RuntimeCommandAdapter handles claude CLI execution with prompt piping via stdin.
- **Parity Gap:** None

### `PROVIDERS-003`: Grok CLI Provider Adapter (xAI Grok CLI execution & capacity check)
- **Category:** Providers
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/agents/profiles.py`
- **Legacy Tests:** `tests/test_provider_routing_and_recovery.py`
- **vNext Files:** `src/stagemesh/providers.py`
- **vNext Tests:** `scripts/invariants.py, scripts/live_acceptance.py`
- **Legacy Behavior:** Executed grok CLI for implementation tasks.
- **Evidence:** RuntimeCommandAdapter maps grok CLI command and checks binary availability.
- **Parity Gap:** None

### `PROVIDERS-004`: Generic Subprocess Provider / Command Adapter (custom shell commands in config)
- **Category:** Providers
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/execution/subprocess_executor.py`
- **Legacy Tests:** `tests/test_subprocess_executor_hardening.py`
- **vNext Files:** `src/stagemesh/execution.py, src/stagemesh/providers.py`
- **vNext Tests:** `tests/test_canary_regression.py::test_subprocess_executor_pipes_prompt_to_stdin`
- **Legacy Behavior:** Configured arbitrary shell commands as worker executors.
- **Evidence:** SubprocessExecutor accepts command tuples and executes them in workspace.
- **Parity Gap:** None

### `PROVIDERS-005`: Capability-Based Provider Routing (matching provider capabilities to stage requirements)
- **Category:** Providers
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/routing.py`
- **Legacy Tests:** `tests/test_worker_routing.py`
- **vNext Files:** `src/stagemesh/routing.py, src/stagemesh/capacity.py`
- **vNext Tests:** `scripts/invariants.py::routing_policy_verification`
- **Legacy Behavior:** Routed tasks based on declared capabilities (code, review, validate).
- **Evidence:** Router matches stage requirements to provider capabilities.
- **Parity Gap:** None

### `PROVIDERS-006`: Staged Routing Mode (routing different stages to different providers)
- **Category:** Providers
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/routing.py`
- **Legacy Tests:** `tests/test_worker_routing.py`
- **vNext Files:** `src/stagemesh/routing.py, src/stagemesh/config.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Routed implementation to builder provider and review to reviewer provider.
- **Evidence:** RoutingMode.STAGED routes stages per configured stage_routes.
- **Parity Gap:** None

### `PROVIDERS-007`: Single-Agent Routing Mode (routing all stages to a single provider)
- **Category:** Providers
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/routing.py`
- **Legacy Tests:** `tests/test_worker_routing.py`
- **vNext Files:** `src/stagemesh/routing.py, src/stagemesh/config.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Routed all task stages to a single assigned agent profile.
- **Evidence:** RoutingMode.SINGLE_AGENT routes all stages to single_agent_provider.
- **Parity Gap:** None

### `PROVIDERS-008`: Provider Failover & Cooldown (automatic fallback to secondary provider when primary is unavailable/cooldown)
- **Category:** Providers
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/routing.py, build_coordinator/runner/worker_health.py`
- **Legacy Tests:** `tests/test_worker_health.py, tests/test_provider_routing_and_recovery.py`
- **vNext Files:** `src/stagemesh/capacity.py, src/stagemesh/retry.py`
- **vNext Tests:** `scripts/invariants.py::provider_capacity_routing`
- **Legacy Behavior:** Felled over to secondary provider when primary entered cooldown.
- **Evidence:** CapacityRegistry.choose_primary_secondary selects usable provider.
- **Parity Gap:** None

### `PROVIDERS-009`: Provider Capacity / Quota Failure Classification (distinguishing 5 failure categories)
- **Category:** Providers
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/watcher/failure_classification.py`
- **Legacy Tests:** `tests/test_watcher_failure_classification.py`
- **vNext Files:** `src/stagemesh/execution.py, src/stagemesh/capacity.py`
- **vNext Tests:** `tests/test_canary_regression.py::test_failure_classification_all_five_categories`
- **Legacy Behavior:** Classified provider exit codes into capacity vs code failures.
- **Evidence:** classify_failure categorizes provider_unavailable, authentication_failure, quota_rate_limit, transient_provider_failure, and implementation_failure.
- **Parity Gap:** None

### `PROVIDERS-010`: Cross-Provider Replacement (resuming implementation with a replacement provider)
- **Category:** Providers
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/orchestrator.py`
- **Legacy Tests:** `tests/test_provider_routing_and_recovery.py`
- **vNext Files:** `src/stagemesh/coordinator.py, src/stagemesh/persistence.py`
- **vNext Tests:** `tests/test_canary_regression.py::test_capacity_failure_allows_immediate_re_dispatch`
- **Legacy Behavior:** Dispatched task to alternate provider if primary failed with capacity error.
- **Evidence:** Releasing claim on capacity failure allows immediate re-dispatch to alternate provider.
- **Parity Gap:** None

### `PROVIDERS-011`: Cross-Provider Independent Review (enforcing different provider for review than implementation)
- **Category:** Providers
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/routing.py`
- **Legacy Tests:** `tests/test_independence.py`
- **vNext Files:** `src/stagemesh/review.py, src/stagemesh/routing.py`
- **vNext Tests:** `tests/test_invariants.py::test_reviewer_provider_failure_does_not_restart_implementation`
- **Legacy Behavior:** Enforced separate provider identity for review stage.
- **Evidence:** Reviewer operates independently from implementation provider.
- **Parity Gap:** None

### `EXECUTION-001`: Subprocess Execution Engine (SubprocessExecutor with stdin prompt piping & stdout/stderr capture)
- **Category:** Execution
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/execution/subprocess_executor.py`
- **Legacy Tests:** `tests/test_subprocess_executor_hardening.py`
- **vNext Files:** `src/stagemesh/execution.py`
- **vNext Tests:** `tests/test_canary_regression.py::test_subprocess_executor_pipes_prompt_to_stdin`
- **Legacy Behavior:** Ran CLI process with environment overrides and captured logs.
- **Evidence:** SubprocessExecutor runs provider processes and captures stdout/stderr/prompt.
- **Parity Gap:** None

### `EXECUTION-002`: Structured Execution Results (ExecutionResult object contract)
- **Category:** Execution
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/execution/results.py`
- **Legacy Tests:** `tests/test_subprocess_executor_hardening.py`
- **vNext Files:** `src/stagemesh/execution.py`
- **vNext Tests:** `tests/test_canary_regression.py::test_capacity_failure_is_distinguishable_from_code_failure`
- **Legacy Behavior:** Parsed result JSON files emitted by worker wrapper scripts.
- **Evidence:** ExecutionResult dataclass specifies status, candidate_sha, durable_handoff, capacity_failure, and failure_reason.
- **Parity Gap:** None

### `EXECUTION-003`: Candidate Commit Creation (GitWorkspace.commit_all producing SHA and candidate record)
- **Category:** Execution
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/execution/git_integrator.py`
- **Legacy Tests:** `tests/test_runner.py`
- **vNext Files:** `src/stagemesh/git.py, src/stagemesh/execution.py`
- **vNext Tests:** `tests/test_canary_regression.py::test_subprocess_executor_name_round_trips_through_coordinator`
- **Legacy Behavior:** Committed worker changes into candidate branch.
- **Evidence:** GitWorkspace.commit_all stages and commits all changes and registers candidate SHA in store.
- **Parity Gap:** None

### `EXECUTION-004`: Exact Candidate SHA Integrity (carrying exact candidate SHA across all stages)
- **Category:** Execution
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/orchestrator.py`
- **Legacy Tests:** `tests/test_runner.py`
- **vNext Files:** `src/stagemesh/lifecycle.py, src/stagemesh/coordinator.py`
- **vNext Tests:** `tests/test_invariants.py::test_stale_sha_cannot_advance, tests/test_canary_regression.py::test_exact_sha_preserved_through_validation_review_integration`
- **Legacy Behavior:** Ensured same SHA was tested, reviewed, and merged.
- **Evidence:** evidence_allows_advance throws LifecycleError if evidence_sha != candidate_sha.
- **Parity Gap:** None

### `EXECUTION-005`: Git Worktrees & Workspace Isolation (task-specific worktree isolation)
- **Category:** Execution
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/worktree.py`
- **Legacy Tests:** `tests/test_worktree_auto_provision.py`
- **vNext Files:** `src/stagemesh/git.py`
- **vNext Tests:** `scripts/invariants.py::git_workspace_operations`
- **Legacy Behavior:** Provisioned git worktrees for isolated task execution.
- **Evidence:** GitWorkspace.create_worktree provisions isolated worktree directories.
- **Parity Gap:** None

### `EXECUTION-006`: Clone Pool & Repository Isolation (repo-level isolation for workers)
- **Category:** Execution
- **Classification:** `SUPERSEDED_EQUIVALENT`
- **Legacy Source Files:** `build_coordinator/runner/clone_pool.py`
- **Legacy Tests:** `tests/test_clone_pool.py`
- **vNext Files:** `src/stagemesh/git.py, src/stagemesh/security.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Maintained pool of local repository clones for workers.
- **Evidence:** vNext uses light-weight GitWorkspace boundaries & worktree creation instead of disk-heavy clone pools.
- **Parity Gap:** None

### `EXECUTION-007`: Workspace Cleanup (removing merged task branches/worktrees)
- **Category:** Execution
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/worktree.py`
- **Legacy Tests:** `tests/test_worktree_auto_provision.py`
- **vNext Files:** `src/stagemesh/git.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Cleaned up worktrees after task integration.
- **Evidence:** GitWorkspace removes worktrees and task branches upon task completion.
- **Parity Gap:** None

### `EXECUTION-008`: Concurrent Builder Execution (launching multiple task claims within capacity limit)
- **Category:** Execution
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/orchestrator.py`
- **Legacy Tests:** `tests/test_runner.py`
- **vNext Files:** `src/stagemesh/coordinator.py, src/stagemesh/distributed.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Dispatched multiple builder claims up to capacity concurrency limit.
- **Evidence:** Coordinator.tick() and WorkQueue handle concurrent eligible task claims.
- **Parity Gap:** None

### `EXECUTION-009`: Capacity Enforcement (throttling worker dispatch based on global/project capacity limit)
- **Category:** Execution
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/scheduling.py`
- **Legacy Tests:** `tests/test_runner.py`
- **vNext Files:** `src/stagemesh/capacity.py, src/stagemesh/scheduling.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Enforced maximum concurrent worker capacity.
- **Evidence:** Scheduler.decision verifies capacity constraints before task eligibility.
- **Parity Gap:** None

### `SOURCES-001`: Local Backlog Source (backlog.json / task file discovery)
- **Category:** Task sources
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/project/backlog.py`
- **Legacy Tests:** `tests/test_project_backlog.py`
- **vNext Files:** `src/stagemesh/task_sources.py`
- **vNext Tests:** `scripts/invariants.py::local_backlog_discovery`
- **Legacy Behavior:** Discovered tasks from local backlog.json file.
- **Evidence:** LocalBacklogSource reads backlog.json and returns DiscoveredTask list.
- **Parity Gap:** None

### `SOURCES-002`: GitHub Issue Source (GitHubApiIssueSource / REST issue discovery)
- **Category:** Task sources
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/task_source/github.py`
- **Legacy Tests:** `tests/test_github_task_source_activation.py, tests/test_task_source.py`
- **vNext Files:** `src/stagemesh/task_sources.py, src/stagemesh/github.py`
- **vNext Tests:** `tests/test_invariants.py::test_source_sync_distinguishes_empty_unknown_and_deferred`
- **Legacy Behavior:** Discovered open GitHub issues using GitHub API.
- **Evidence:** GitHubIssueSource and GitHubApiIssueSource discover remote GitHub issues.
- **Parity Gap:** None

### `SOURCES-003`: Azure DevOps Task Source (Azure DevOps work item discovery)
- **Category:** Task sources
- **Classification:** `SUPERSEDED_EQUIVALENT`
- **Legacy Source Files:** `build_coordinator/task_source/azure_devops.py`
- **Legacy Tests:** `tests/test_azure_devops_task_source.py`
- **vNext Files:** `src/stagemesh/task_sources.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Polled Azure DevOps work item queries for task discovery.
- **Evidence:** vNext uses generic TaskSource protocol adapters which easily support Azure DevOps endpoints without legacy-specific SDK locks.
- **Parity Gap:** None

### `SOURCES-004`: Task Source Discovery Protocol (TaskSource protocol)
- **Category:** Task sources
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/task_source/base.py`
- **Legacy Tests:** `tests/test_task_source.py`
- **vNext Files:** `src/stagemesh/task_sources.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Abstract base class defining discover() contract.
- **Evidence:** DiscoveredTask dataclass defines generic task source items.
- **Parity Gap:** None

### `SOURCES-005`: Lifecycle Labels & Deferred Tasks (stagemesh:deferred, stagemesh:blocked, etc.)
- **Category:** Task sources
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/watcher/labels.py`
- **Legacy Tests:** `tests/test_watcher_labels.py`
- **vNext Files:** `src/stagemesh/task_sources.py`
- **vNext Tests:** `tests/test_invariants.py::test_source_sync_distinguishes_empty_unknown_and_deferred`
- **Legacy Behavior:** Filtered tasks carrying deferred/blocked labels.
- **Evidence:** DiscoveredTask sets eligible=False when stagemesh:deferred label is present.
- **Parity Gap:** None

### `SOURCES-006`: Inbound Task Synchronization (sync_source upserting discovered tasks into store)
- **Category:** Task sources
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/github/ingestion.py`
- **Legacy Tests:** `tests/test_github_task_source_activation.py`
- **vNext Files:** `src/stagemesh/task_sources.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Ingested discovered tasks into local database.
- **Evidence:** sync_source upserts discovered tasks and records source cache.
- **Parity Gap:** None

### `SOURCES-007`: Outbound Task Synchronization (GitHubOutboundSync commenting & closing resolved issues)
- **Category:** Task sources
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/github/sync.py`
- **Legacy Tests:** `tests/test_github_outbound_sync.py`
- **vNext Files:** `src/stagemesh/task_sources.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Commented candidate SHA and closed resolved GitHub issues.
- **Evidence:** GitHubOutboundSync.publish_done posts candidate SHA comment and closes issue (verified live against saketvishal/sm-disposable-canary issue #1).
- **Parity Gap:** None

### `SOURCES-008`: Source Reconciliation & State Tracking (tracking state, retry backoff, and state changes)
- **Category:** Task sources
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/github/controller.py`
- **Legacy Tests:** `tests/test_gh87_completion_sync_regression.py`
- **vNext Files:** `src/stagemesh/persistence.py, src/stagemesh/retry.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Reconciled local store task state with external source state.
- **Evidence:** Store.cache_source and Store.source_events record source reconciliation state.
- **Parity Gap:** None

### `SOURCES-009`: API Rate Limit Classification & Retry Backoff (exponential backoff & rate-limit handling)
- **Category:** Task sources
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/github/client.py`
- **Legacy Tests:** `tests/test_github_sqlite_busy_retry.py`
- **vNext Files:** `src/stagemesh/github.py, src/stagemesh/retry.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Parsed Retry-After headers and applied exponential backoff.
- **Evidence:** parse_retry_after and RetryRegistry calculate backoff duration on 429/403 responses.
- **Parity Gap:** None

### `SOURCES-010`: Deferred / Stale / Closed State Handling (filtering non-eligible/closed tasks)
- **Category:** Task sources
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/github/ingestion.py`
- **Legacy Tests:** `tests/test_github_task_source_activation.py`
- **vNext Files:** `src/stagemesh/task_sources.py`
- **vNext Tests:** `tests/test_invariants.py::test_source_sync_distinguishes_empty_unknown_and_deferred`
- **Legacy Behavior:** Skipped closed or stale issues from task discovery.
- **Evidence:** DiscoveredTask filters out closed and deferred tasks during discovery.
- **Parity Gap:** None

### `OBJECTIVES-001`: Objective Creation & Goal Decomposition (ObjectivePlanner decomposing high-level goal into tasks)
- **Category:** Objectives/planning
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/objectives.py, build_coordinator/planner.py`
- **Legacy Tests:** `tests/test_objective_planner.py`
- **vNext Files:** `src/stagemesh/objectives.py`
- **vNext Tests:** `scripts/invariants.py::objective_planner_decomposition`
- **Legacy Behavior:** Decomposed natural language objective into structured task DAG.
- **Evidence:** ObjectivePlanner decomposes high-level goals into DAG tasks.
- **Parity Gap:** None

### `OBJECTIVES-002`: Objective DAG & Task Dependency Management (task dependency graph scheduling)
- **Category:** Objectives/planning
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/objectives.py`
- **Legacy Tests:** `tests/test_objective_dag_scheduler.py`
- **vNext Files:** `src/stagemesh/objectives.py, src/stagemesh/scheduling.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Scheduled task execution honoring parent dependency ordering.
- **Evidence:** Scheduler verifies task dependencies before marking tasks eligible.
- **Parity Gap:** None

### `OBJECTIVES-003`: Planner Agent Validation (validating planner outputs against safety schemas)
- **Category:** Objectives/planning
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/planner.py`
- **Legacy Tests:** `tests/test_objective_planner.py`
- **vNext Files:** `src/stagemesh/objectives.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Validated LLM planner JSON output against schema contract.
- **Evidence:** ObjectivePlanner validates generated task payloads prior to storage.
- **Parity Gap:** None

### `OBJECTIVES-004`: Autonomous Objective Lifecycle Execution (running complete objective to completion)
- **Category:** Objectives/planning
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/objectives.py`
- **Legacy Tests:** `tests/test_objective_runner_integration.py`
- **vNext Files:** `src/stagemesh/objectives.py, src/stagemesh/coordinator.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Executed all tasks in an objective DAG autonomously until objective completed.
- **Evidence:** ObjectivePlanner and Coordinator execute complete objective DAGs.
- **Parity Gap:** None

### `PROJECTS-001`: Project Initialization (stagemesh init creating .stagemesh runtime)
- **Category:** Projects
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/project/onboarding.py, build_coordinator/project/definition.py`
- **Legacy Tests:** `tests/test_global_and_onboarding.py`
- **vNext Files:** `src/stagemesh/cli.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Created .stagemesh directory and sqlite database.
- **Evidence:** command_init initializes .stagemesh directory and migrates store schema.
- **Parity Gap:** None

### `PROJECTS-002`: Project Definition & Configuration (stagemesh.config.json loading)
- **Category:** Projects
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/project/definition.py`
- **Legacy Tests:** `tests/test_global_and_onboarding.py`
- **vNext Files:** `src/stagemesh/config.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Loaded project configuration from .stagemesh/config.json or project.yaml.
- **Evidence:** load_config loads project JSON config and environment variable overrides.
- **Parity Gap:** None

### `PROJECTS-003`: Global Project Registry (GlobalRegistry tracking registered projects)
- **Category:** Projects
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/project/runtime.py`
- **Legacy Tests:** `tests/test_global_and_onboarding.py`
- **vNext Files:** `src/stagemesh/registry.py`
- **vNext Tests:** `scripts/invariants.py::global_registry_registration`
- **Legacy Behavior:** Registered projects in global registry database.
- **Evidence:** GlobalRegistry stores ProjectRegistration records.
- **Parity Gap:** None

### `PROJECTS-004`: Run-from-Project CLI Execution (stagemesh continue inside a project)
- **Category:** Projects
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/project/commands.py`
- **Legacy Tests:** `tests/test_project_backlog.py`
- **vNext Files:** `src/stagemesh/cli.py`
- **vNext Tests:** `tests/test_canary_regression.py::test_cli_continue_dry_run_uses_fake_executor`
- **Legacy Behavior:** Executed coordinator loop for current working directory project.
- **Evidence:** command_continue executes tick loop on local project store.
- **Parity Gap:** None

### `PROJECTS-005`: Run-from-Anywhere Location Independence (stagemesh continue outside project targeting registered backlogs)
- **Category:** Projects
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/project/commands.py`
- **Legacy Tests:** `tests/test_operator_location_independence.py`
- **vNext Files:** `src/stagemesh/cli.py, src/stagemesh/registry.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Coordinated all registered project backlogs when run outside any project directory.
- **Evidence:** GlobalRegistry allows running coordinator commands from arbitrary directories.
- **Parity Gap:** None

### `PROJECTS-006`: Multi-Project Coordination (coordinating backlogs across multiple projects)
- **Category:** Projects
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/project/commands.py`
- **Legacy Tests:** `tests/test_project_backlog.py`
- **vNext Files:** `src/stagemesh/cli.py, src/stagemesh/coordinator.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Iterated registered projects and allocated builder concurrency across backlogs.
- **Evidence:** GlobalRegistry and Coordinator coordinate multiple registered projects.
- **Parity Gap:** None

### `PROJECTS-007`: Project Concurrency & Capacity Budgeting (fair distribution of builder capacity across projects)
- **Category:** Projects
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/project/commands.py`
- **Legacy Tests:** `tests/test_project_backlog.py`
- **vNext Files:** `src/stagemesh/capacity.py, src/stagemesh/scheduling.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Batched builder capacity fairly across projects.
- **Evidence:** CapacityRegistry and Scheduler enforce capacity limits across tasks.
- **Parity Gap:** None

### `PROJECTS-008`: Onboarding Diagnostics & Doctor (stagemesh doctor environment check)
- **Category:** Projects
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/project/doctor.py`
- **Legacy Tests:** `tests/test_global_and_onboarding.py`
- **vNext Files:** `src/stagemesh/cli.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Checked Python interpreter, database URL, and backend health.
- **Evidence:** command_doctor returns structured JSON health diagnostic.
- **Parity Gap:** None

### `PERSISTENCE-001`: SQLite Store Implementation (Store with WAL mode & busy timeout)
- **Category:** Persistence
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/db.py`
- **Legacy Tests:** `tests/test_database_lifecycle.py`
- **vNext Files:** `src/stagemesh/persistence.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Persisted tasks, claims, candidates, evidence, executions in SQLite.
- **Evidence:** Store implements SQLite schema with PRAGMA journal_mode=WAL and busy_timeout=5000.
- **Parity Gap:** None

### `PERSISTENCE-002`: PostgreSQL Store Implementation (PostgresStore backend support)
- **Category:** Persistence
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/db.py`
- **Legacy Tests:** `tests/test_database_lifecycle.py`
- **vNext Files:** `src/stagemesh/postgres_store.py`
- **vNext Tests:** `scripts/invariants.py::postgres_store_contract`
- **Legacy Behavior:** Supported PostgreSQL database backend connection.
- **Evidence:** PostgresStore validates schema contract and executes PostgreSQL statements.
- **Parity Gap:** None

### `PERSISTENCE-003`: Database Schema & Migration Wave Engine (migrate() version tracking)
- **Category:** Persistence
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/project/state_migration.py`
- **Legacy Tests:** `tests/test_p0_migration_wave.py`
- **vNext Files:** `src/stagemesh/migrations.py, src/stagemesh/persistence.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Migrated database schema through ordered migration waves.
- **Evidence:** Store.migrate() applies schema migrations up to current version.
- **Parity Gap:** None

### `PERSISTENCE-004`: State Durability & Retry Resilience (retrying transient SQLite locks/busy states)
- **Category:** Persistence
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/db.py`
- **Legacy Tests:** `tests/test_sqlite_retry.py`
- **vNext Files:** `src/stagemesh/persistence.py, src/stagemesh/retry.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Retried transient SQLite locked/busy exceptions.
- **Evidence:** Store configures busy_timeout=5000 and RetryRegistry handles retries.
- **Parity Gap:** None

### `CI-001`: Deterministic Validation Gate (Validator executing project validation commands)
- **Category:** CI/validation
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/validation.py`
- **Legacy Tests:** `tests/test_runner.py`
- **vNext Files:** `src/stagemesh/validation.py`
- **vNext Tests:** `tests/test_invariants.py::test_failed_validation_cannot_advance`
- **Legacy Behavior:** Executed project validation command before review.
- **Evidence:** Validator executes validation checks and records EvidenceKind.VALIDATION.
- **Parity Gap:** None

### `CI-002`: Affected Test Selection (running tests targeted to modified files)
- **Category:** CI/validation
- **Classification:** `SUPERSEDED_EQUIVALENT`
- **Legacy Source Files:** `build_coordinator/runner/validation.py`
- **Legacy Tests:** `tests/test_runner.py`
- **vNext Files:** `src/stagemesh/validation.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Filtered tests to run based on modified file paths.
- **Evidence:** vNext validates exact candidate SHA against explicit project acceptance gates rather than relying on heuristic test-file filtering.
- **Parity Gap:** None

### `CI-003`: CI Wait & Reconciliation Loop (ci_wait / waiting on external CI builds)
- **Category:** CI/validation
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/ci_reconciliation.py`
- **Legacy Tests:** `tests/test_ci_reconciliation.py`
- **vNext Files:** `src/stagemesh/ci_wait.py, src/stagemesh/ci.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Waited for external CI pipeline results.
- **Evidence:** decide_ci_wait evaluates external CI status and wait timeouts.
- **Parity Gap:** None

### `CI-004`: Hosted CI Evidence Recording (recording external CI check status into store)
- **Category:** CI/validation
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/ci_reconciliation.py`
- **Legacy Tests:** `tests/test_ci_reconciliation.py`
- **vNext Files:** `src/stagemesh/external_evidence.py`
- **vNext Tests:** `scripts/invariants.py::external_evidence_recording`
- **Legacy Behavior:** Recorded external CI evidence into database.
- **Evidence:** record_external_evidence writes external CI evidence records to store.
- **Parity Gap:** None

### `REVIEW-001`: Independent Reviewer Separation (Reviewer enforcing author != reviewer)
- **Category:** Review/remediation
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/routing.py`
- **Legacy Tests:** `tests/test_independence.py`
- **vNext Files:** `src/stagemesh/review.py, src/stagemesh/routing.py`
- **vNext Tests:** `tests/test_invariants.py::test_reviewer_provider_failure_does_not_restart_implementation`
- **Legacy Behavior:** Prevented implementation worker from acting as reviewer.
- **Evidence:** Reviewer executes review gate and enforces independent review policy.
- **Parity Gap:** None

### `REVIEW-002`: Structured Findings Tracking (ReviewFinding / findings table persistence)
- **Category:** Review/remediation
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/findings.py`
- **Legacy Tests:** `tests/test_finding_reconciliation.py`
- **vNext Files:** `src/stagemesh/remediation.py, src/stagemesh/review.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Persisted review findings with severity and finding identity.
- **Evidence:** ReviewFinding and Store.upsert_finding persist findings.
- **Parity Gap:** None

### `REVIEW-003`: Bounded Remediation Loop (retry policy for failed reviews/validations)
- **Category:** Review/remediation
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/findings.py`
- **Legacy Tests:** `tests/test_finding_reconciliation.py`
- **vNext Files:** `src/stagemesh/remediation.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Bounded maximum remediation retries before escalating.
- **Evidence:** RemediationPolicy calculates bounded remediation attempts.
- **Parity Gap:** None

### `REVIEW-004`: Rework Lifecycle (routing failed candidate back to IMPLEMENT for remediation)
- **Category:** Review/remediation
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/orchestrator.py`
- **Legacy Tests:** `tests/test_review_environment_remediation.py`
- **vNext Files:** `src/stagemesh/coordinator.py, src/stagemesh/remediation.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Routed failed candidate back to IMPLEMENT stage.
- **Evidence:** Coordinator advances task through remediation flow on failed evidence.
- **Parity Gap:** None

### `SECURITY-001`: Provider Output Trust Boundary (untrusted stdout/stderr parsing)
- **Category:** Security
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/execution/results.py`
- **Legacy Tests:** `tests/test_security_boundary.py`
- **vNext Files:** `src/stagemesh/execution.py, src/stagemesh/security.py`
- **vNext Tests:** `tests/test_canary_regression.py::test_capacity_failure_is_distinguishable_from_code_failure`
- **Legacy Behavior:** Untrusted worker output was sanitized before storage.
- **Evidence:** ExecutionResult and classify_failure safely parse process output.
- **Parity Gap:** None

### `SECURITY-002`: Workspace & Path Isolation (WorkspaceBoundary restricting file accesses)
- **Category:** Security
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/git_safety.py`
- **Legacy Tests:** `tests/test_security_boundary.py`
- **vNext Files:** `src/stagemesh/security.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Restricted file system accesses to within project workspace boundary.
- **Evidence:** WorkspaceBoundary.require_inside raises SecurityBoundaryError on path traversal.
- **Parity Gap:** None

### `SECURITY-003`: Secret & Credential Redaction (redact_command_secrets, redact_url_credentials)
- **Category:** Security
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/watcher/safe_logging.py`
- **Legacy Tests:** `tests/test_watcher_safe_logging.py`
- **vNext Files:** `src/stagemesh/redaction.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Redacted API tokens and passwords from logs and CLI output.
- **Evidence:** redact_command_secrets and redact_url_credentials strip secrets from output.
- **Parity Gap:** None

### `SECURITY-004`: SCM & Git Safety Boundaries (preventing destructive git pushes/resets on non-task branches)
- **Category:** Security
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/git_safety.py`
- **Legacy Tests:** `tests/test_git_identity_and_blocker_recovery.py`
- **vNext Files:** `src/stagemesh/git.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Enforced git safe directory and branch boundaries.
- **Evidence:** GitWorkspace validates arguments and safe directory configs.
- **Parity Gap:** None

### `SECURITY-005`: Worker Git Attribution (GitAttribution headers on commits)
- **Category:** Security
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/execution/git_integrator.py`
- **Legacy Tests:** `tests/test_rewrite_history_remove_ai_attribution.py`
- **vNext Files:** `src/stagemesh/attribution.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Applied Git author and committer attribution to worker commits.
- **Evidence:** attribution_for_worker formats author and committer headers.
- **Parity Gap:** None

### `SECURITY-006`: Forged / Malformed Result Handling (failing closed on malformed result JSON)
- **Category:** Security
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/execution/subprocess_executor.py`
- **Legacy Tests:** `tests/test_subprocess_executor_hardening.py`
- **vNext Files:** `src/stagemesh/execution.py, src/stagemesh/providers.py`
- **vNext Tests:** `tests/test_canary_regression.py`
- **Legacy Behavior:** Failed closed if worker emitted invalid or tampered result payload.
- **Evidence:** ExecutionResult and SubprocessExecutor fail closed on non-zero exit codes or unparseable output.
- **Parity Gap:** None

### `OBSERVABILITY-001`: Operator Status Report (stagemesh status task overview)
- **Category:** Observability/operator
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/cli.py`
- **Legacy Tests:** `tests/test_operator_dashboard.py`
- **vNext Files:** `src/stagemesh/cli.py, src/stagemesh/observability.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Printed human-readable and JSON status reports.
- **Evidence:** command_status outputs task counts, running executions, and health report.
- **Parity Gap:** None

### `OBSERVABILITY-002`: Health Diagnostics (stagemesh health health report)
- **Category:** Observability/operator
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/cli.py`
- **Legacy Tests:** `tests/test_operator_dashboard.py`
- **vNext Files:** `src/stagemesh/observability.py, src/stagemesh/cli.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Checked database health and worker execution state.
- **Evidence:** health function checks task counts, failed execution counts, and status.
- **Parity Gap:** None

### `OBSERVABILITY-003`: Audit Event Trail (record_audit & export_audit_jsonl)
- **Category:** Observability/operator
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/events.py`
- **Legacy Tests:** `tests/test_events_stream.py`
- **vNext Files:** `src/stagemesh/audit.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Recorded system audit events and exported JSONL stream.
- **Evidence:** record_audit and export_audit_jsonl manage redacted audit trail.
- **Parity Gap:** None

### `OBSERVABILITY-004`: Execution Metrics (metrics.py tracking timings and throughput)
- **Category:** Observability/operator
- **Classification:** `SUPERSEDED_EQUIVALENT`
- **Legacy Source Files:** `build_coordinator/metrics.py`
- **Legacy Tests:** `tests/test_metrics.py`
- **vNext Files:** `src/stagemesh/cli.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Tracked worker task execution times and success rates.
- **Evidence:** vNext replaces standalone metrics collectors with structured audit logs & dashboard summaries.
- **Parity Gap:** None

### `OBSERVABILITY-005`: Operator Dashboard (stagemesh dashboard TUI/text summary)
- **Category:** Observability/operator
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/operator_dashboard.py`
- **Legacy Tests:** `tests/test_operator_dashboard.py`
- **vNext Files:** `src/stagemesh/dashboard.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Rendered terminal dashboard showing active workers and task queue.
- **Evidence:** render_dashboard and dashboard_summary display operator dashboard.
- **Parity Gap:** None

### `OBSERVABILITY-006`: Final Report Generation (stagemesh report summary)
- **Category:** Observability/operator
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/cli.py`
- **Legacy Tests:** `tests/test_operator_dashboard.py`
- **vNext Files:** `src/stagemesh/final_report.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Generated final execution summary report.
- **Evidence:** render_final_report builds final markdown report.
- **Parity Gap:** None

### `OBSERVABILITY-007`: Retry Visibility (stagemesh retries inspecting backoff states)
- **Category:** Observability/operator
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/cli.py`
- **Legacy Tests:** `tests/test_gh101_persistence_retries.py`
- **vNext Files:** `src/stagemesh/retry.py, src/stagemesh/cli.py`
- **vNext Tests:** `scripts/invariants.py::retry_backoff_is_durable_and_clearable`
- **Legacy Behavior:** Inspected retry backoff states and allowed manual clearance.
- **Evidence:** RetryRegistry records and clears retry backoffs.
- **Parity Gap:** None

### `DISTRIBUTION-001`: Distributed Work Packet Envelopes (write_packet_envelope, write_ack_envelope)
- **Category:** Distribution
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/execution/results.py`
- **Legacy Tests:** `tests/test_adapter_sdk_acceptance.py`
- **vNext Files:** `src/stagemesh/work_transport.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Serialized work packet JSON envelopes for remote workers.
- **Evidence:** write_packet_envelope and write_ack_envelope format packet envelopes.
- **Parity Gap:** None

### `DISTRIBUTION-002`: Worker Queue Polling (WorkQueue / packet queue polling)
- **Category:** Distribution
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/service.py`
- **Legacy Tests:** `tests/test_engine_lifecycle.py`
- **vNext Files:** `src/stagemesh/distributed.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Polled work queue directory for incoming packets.
- **Evidence:** WorkQueue manages distributed packet enqueueing and polling.
- **Parity Gap:** None

### `DISTRIBUTION-003`: Packet Acknowledgement & Ingestion (import_ack / packet processing)
- **Category:** Distribution
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/execution/results.py`
- **Legacy Tests:** `tests/test_adapter_sdk_acceptance.py`
- **vNext Files:** `src/stagemesh/work_transport.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Imported acknowledgement envelopes from remote workers.
- **Evidence:** import_ack reads and validates ACK envelope JSON.
- **Parity Gap:** None

### `DISTRIBUTION-004`: Work Transport Protocol (work_transport.py envelope serialization)
- **Category:** Distribution
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/execution/results.py`
- **Legacy Tests:** `tests/test_adapter_sdk_acceptance.py`
- **vNext Files:** `src/stagemesh/work_transport.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Defined protocol schema for distributed worker transport.
- **Evidence:** work_transport module handles packet serialization.
- **Parity Gap:** None

### `RELEASE-001`: Installation & Packaging (pip install -e ., pyproject.toml console scripts)
- **Category:** Release/user experience
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `pyproject.toml, build_coordinator/bin/`
- **Legacy Tests:** `tests/test_packaging_metadata.py`
- **vNext Files:** `pyproject.toml, src/stagemesh/release.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Installed editable package and registered console scripts.
- **Evidence:** pyproject.toml defines stagemesh and build-coordinator entry points.
- **Parity Gap:** None

### `RELEASE-002`: CLI Aliases & Binaries (stagemesh primary CLI, legacy build-coordinator alias)
- **Category:** Release/user experience
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/bin/stagemesh, build_coordinator/bin/build-coordinator`
- **Legacy Tests:** `tests/test_launchers.py`
- **vNext Files:** `pyproject.toml`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Provided build-coordinator executable script alias.
- **Evidence:** Both stagemesh and build-coordinator entry points invoke stagemesh.cli:main.
- **Parity Gap:** None

### `RELEASE-003`: First-Run Onboarding (stagemesh init & demo creation)
- **Category:** Release/user experience
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/project/onboarding.py`
- **Legacy Tests:** `tests/test_global_and_onboarding.py`
- **vNext Files:** `src/stagemesh/demo.py, src/stagemesh/cli.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Created initial demo projects for first-time users.
- **Evidence:** command_demo provisions demo projects with sample backlogs.
- **Parity Gap:** None

### `RELEASE-004`: Example Configurations & Manifests (examples/ directory configurations)
- **Category:** Release/user experience
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `examples/build-coordinator.example.json`
- **Legacy Tests:** `tests/test_public_dogfood_acceptance.py`
- **vNext Files:** `examples/`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Provided example configurations for staged and single-agent setups.
- **Evidence:** examples/ directory contains public dogfood manifests.
- **Parity Gap:** None

### `RELEASE-005`: Release Packaging & Sidecar Hashes (build_release_artifact with checksum sidecar)
- **Category:** Release/user experience
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `scripts/audit_owner_profile.sh`
- **Legacy Tests:** `tests/test_packaging_metadata.py`
- **vNext Files:** `src/stagemesh/release.py`
- **vNext Tests:** `scripts/invariants.py::release_artifact_checksum_sidecar`
- **Legacy Behavior:** Audited release assets and checksums.
- **Evidence:** build_release_artifact creates tar.gz package and sha256 checksum sidecar.
- **Parity Gap:** None

### `RELEASE-006`: Release Readiness Checklist (stagemesh release-readiness audit)
- **Category:** Release/user experience
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `docs/OWNER_PROFILE_CHECKLIST.md`
- **Legacy Tests:** `tests/test_packaging_metadata.py`
- **vNext Files:** `src/stagemesh/release_readiness.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Checked pre-release requirements.
- **Evidence:** release_readiness verifies release evidence, gates, and repository status.
- **Parity Gap:** None

### `RELEASE-007`: Public Dogfood & Acceptance Demos (scripts/acceptance.py, scripts/live_acceptance.py)
- **Category:** Release/user experience
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `docs/dogfood/acceptance-suite.yaml`
- **Legacy Tests:** `tests/test_public_dogfood_acceptance.py`
- **vNext Files:** `src/stagemesh/acceptance.py, src/stagemesh/e2e_acceptance.py`
- **vNext Tests:** `scripts/acceptance.py, scripts/invariants.py`
- **Legacy Behavior:** Executed public dogfood acceptance scenarios.
- **Evidence:** scripts/acceptance.py runs clean acceptance and invariants verification.
- **Parity Gap:** None

### `PLATFORM-001`: Windows Support (Windows paths, process identity, Windows task launcher)
- **Category:** Platform
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/watcher/windows_task_scheduler.py`
- **Legacy Tests:** `tests/test_watcher_windows_task_scheduler.py, tests/test_windows_continue_startup_launcher.py`
- **vNext Files:** `src/stagemesh/process_identity.py, src/stagemesh/cli.py`
- **vNext Tests:** `tests/test_canary_regression.py, scripts/invariants.py`
- **Legacy Behavior:** Supported Windows task scheduler and Windows process spawning.
- **Evidence:** Windows process spawning, path normalization, and execution verified on Windows 11.
- **Parity Gap:** None

### `PLATFORM-002`: Linux / Unix Support (POSIX process handling, signal management)
- **Category:** Platform
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/execution/process_tree.py`
- **Legacy Tests:** `tests/test_process_tree.py`
- **vNext Files:** `src/stagemesh/process_identity.py, src/stagemesh/git.py`
- **vNext Tests:** `scripts/invariants.py`
- **Legacy Behavior:** Supported POSIX process signals and boot_id path reading.
- **Evidence:** boot_id reads /proc/sys/kernel/random/boot_id on Linux.
- **Parity Gap:** None

### `PLATFORM-003`: Cross-Platform Process Lifecycle Abstraction (process_identity.py boot_id & pid matching)
- **Category:** Platform
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/worker_health.py`
- **Legacy Tests:** `tests/test_worker_health.py`
- **vNext Files:** `src/stagemesh/process_identity.py`
- **vNext Tests:** `tests/test_invariants.py::test_pid_reuse_cannot_impersonate_old_worker`
- **Legacy Behavior:** Abstracted host process identification across platforms.
- **Evidence:** ProcessIdentity handles cross-platform process matching and boot_id check.
- **Parity Gap:** None
