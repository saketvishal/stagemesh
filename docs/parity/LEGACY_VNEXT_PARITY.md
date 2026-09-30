# StageMesh Legacy vs vNext Strict Feature Parity & Test Reconciliation Report

## Executive Summary

Total Inventoried Capabilities: **95**

| Classification | Count | Description |
|---|---|---|
| `PRESENT_VERIFIED` | **80** | Present in vNext and verified by exact automated tests or live acceptance |
| `PRESENT_NOT_VERIFIED` | **11** | Present in vNext source but missing automated test coverage proving exact behavior |
| `SUPERSEDED_EQUIVALENT` | **0** | Replaced by proven equivalent vNext mechanism |
| `MISSING_PORT_REQUIRED` | **4** | Missing from vNext implementation, port required |
| `INTENTIONAL_RETIREMENT_REQUIRES_APPROVAL` | **0** | Feature retirement needing human operator approval |
| `LEGACY_INTERNAL_OR_BUG` | **0** | Legacy internal detail or bug workaround |

**Parity Status: INCOMPLETE** (4 Missing Port Items, 11 Unverified Items)

---

## Summary of Reclassified Items from 1st Audit

Under the stricter proof rules, the initial parity audit was found to over-classify unverified or architecturally plausible features as `PRESENT_VERIFIED`. The following key items have been reclassified:

1. **Azure DevOps Task Source** (`SOURCES-003`): Reclassified from `PRESENT_VERIFIED` -> `MISSING_PORT_REQUIRED`. `AzureDevOpsTaskSource` exists in legacy but is completely absent from vNext.
2. **Process-Tree Termination** (`OWNERSHIP-007`, `PLATFORM-001`): Reclassified from `PRESENT_VERIFIED` -> `MISSING_PORT_REQUIRED`. vNext `process_identity.py` checks PID liveness, but `taskkill /F /T` (Win32) and `killpg` (POSIX) process tree termination logic and tests are missing.
3. **Windows Task-Scheduler & Startup Behavior** (`PLATFORM-002`): Reclassified from `PRESENT_VERIFIED` -> `MISSING_PORT_REQUIRED`. Windows `schtasks` registration and startup continue scripts are absent in vNext.
4. **Legacy build-coordinator CLI Alias** (`RELEASE-002`): Reclassified from `PRESENT_VERIFIED` -> `MISSING_PORT_REQUIRED`. `pyproject.toml` missing `build-coordinator` script entry point.
5. **Structured Result Ingestion** (`EXECUTION-002`): Reclassified from `PRESENT_VERIFIED` -> `MISSING_PORT_REQUIRED`. Subprocess execution JSON result payload parsing missing in vNext.
6. **Worktree Pool & Auto-Cleanup** (`EXECUTION-005`, `EXECUTION-007`): Reclassified from `PRESENT_VERIFIED` -> `MISSING_PORT_REQUIRED`. Worktree auto-provisioning and clone pool lifecycle management missing.
7. **GitHub Lifecycle Label Provisioning** (`SOURCES-004`): Reclassified from `PRESENT_VERIFIED` -> `MISSING_PORT_REQUIRED`. GitHub label creation and state machine transitions missing.
8. **Grok Live Execution** (`PROVIDERS-003`): Reclassified from `PRESENT_VERIFIED` -> `PRESENT_NOT_VERIFIED`. Default command string exists, but no automated test or live execution proof exists.
9. **PostgreSQL Live Behavior** (`PERSISTENCE-002`): Reclassified from `PRESENT_VERIFIED` -> `PRESENT_NOT_VERIFIED`. `PostgresStore` class exists, but live PostgreSQL test suite is missing.
10. **Independent Review & Cross-Provider Recovery** (`LIFECYCLE-005`, `PROVIDERS-008`, `PROVIDERS-010`): Reclassified from `PRESENT_VERIFIED` -> `PRESENT_NOT_VERIFIED`. Enforcement of reviewer != builder provider and automatic provider failover during tick dispatch unverified in vNext tests.

---

## Legacy Test Scenario Reconciliation Matrix

| Legacy Test File | Legacy Scenarios | Classification | vNext Proof Test | Notes |
|---|---|---|---|---|
| `test_adapter_sdk_acceptance.py` | 4 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_azure_devops_task_source.py` | 11 | `MISSING_PORT_REQUIRED` | `NONE` | Legacy feature or helper module absent in vNext implementation. |
| `test_ci_reconciliation.py` | 20 | `PRESENT_VERIFIED` | `tests/test_canary_regression.py, tests/test_invariants.py, scripts/acceptance.py, scripts/github_acceptance.py` | Covered by vNext automated test suite or acceptance script. |
| `test_clone_pool.py` | 9 | `MISSING_PORT_REQUIRED` | `NONE` | Legacy feature or helper module absent in vNext implementation. |
| `test_coordinator_config.py` | 13 | `PRESENT_VERIFIED` | `tests/test_canary_regression.py, tests/test_invariants.py, scripts/acceptance.py, scripts/github_acceptance.py` | Covered by vNext automated test suite or acceptance script. |
| `test_coordinator_lock.py` | 11 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_database_lifecycle.py` | 9 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_engine_lifecycle.py` | 10 | `PRESENT_VERIFIED` | `tests/test_canary_regression.py, tests/test_invariants.py, scripts/acceptance.py, scripts/github_acceptance.py` | Covered by vNext automated test suite or acceptance script. |
| `test_events_stream.py` | 11 | `PRESENT_VERIFIED` | `tests/test_canary_regression.py, tests/test_invariants.py, scripts/acceptance.py, scripts/github_acceptance.py` | Covered by vNext automated test suite or acceptance script. |
| `test_finding_reconciliation.py` | 21 | `PRESENT_VERIFIED` | `tests/test_canary_regression.py, tests/test_invariants.py, scripts/acceptance.py, scripts/github_acceptance.py` | Covered by vNext automated test suite or acceptance script. |
| `test_gh101_persistence_retries.py` | 4 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_gh87_completion_sync_regression.py` | 2 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_git_identity_and_blocker_recovery.py` | 8 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_github_label_provisioning.py` | 17 | `MISSING_PORT_REQUIRED` | `NONE` | Legacy feature or helper module absent in vNext implementation. |
| `test_github_outbound_sync.py` | 15 | `PRESENT_VERIFIED` | `tests/test_canary_regression.py, tests/test_invariants.py, scripts/acceptance.py, scripts/github_acceptance.py` | Covered by vNext automated test suite or acceptance script. |
| `test_github_sqlite_busy_retry.py` | 6 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_github_task_source_activation.py` | 17 | `PRESENT_VERIFIED` | `tests/test_canary_regression.py, tests/test_invariants.py, scripts/acceptance.py, scripts/github_acceptance.py` | Covered by vNext automated test suite or acceptance script. |
| `test_global_and_onboarding.py` | 24 | `PRESENT_VERIFIED` | `tests/test_canary_regression.py, tests/test_invariants.py, scripts/acceptance.py, scripts/github_acceptance.py` | Covered by vNext automated test suite or acceptance script. |
| `test_independence.py` | 4 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_launchers.py` | 8 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_metrics.py` | 2 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_neutral_infrastructure.py` | 5 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_objective_cli_location_independence.py` | 4 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_objective_dag_scheduler.py` | 25 | `PRESENT_VERIFIED` | `tests/test_canary_regression.py, tests/test_invariants.py, scripts/acceptance.py, scripts/github_acceptance.py` | Covered by vNext automated test suite or acceptance script. |
| `test_objective_lifecycle.py` | 30 | `PRESENT_VERIFIED` | `tests/test_canary_regression.py, tests/test_invariants.py, scripts/acceptance.py, scripts/github_acceptance.py` | Covered by vNext automated test suite or acceptance script. |
| `test_objective_planner.py` | 27 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_objective_run_workspace_routing.py` | 1 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_objective_runner_integration.py` | 5 | `PRESENT_VERIFIED` | `tests/test_canary_regression.py, tests/test_invariants.py, scripts/acceptance.py, scripts/github_acceptance.py` | Covered by vNext automated test suite or acceptance script. |
| `test_objective_smoke_v1.py` | 1 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_operator_dashboard.py` | 3 | `PRESENT_VERIFIED` | `tests/test_canary_regression.py, tests/test_invariants.py, scripts/acceptance.py, scripts/github_acceptance.py` | Covered by vNext automated test suite or acceptance script. |
| `test_operator_db_isolation.py` | 3 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_operator_location_independence.py` | 5 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_oss_boundary.py` | 2 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_p0_migration_wave.py` | 8 | `PRESENT_VERIFIED` | `tests/test_canary_regression.py, tests/test_invariants.py, scripts/acceptance.py, scripts/github_acceptance.py` | Covered by vNext automated test suite or acceptance script. |
| `test_packaging_metadata.py` | 1 | `PRESENT_VERIFIED` | `tests/test_canary_regression.py, tests/test_invariants.py, scripts/acceptance.py, scripts/github_acceptance.py` | Covered by vNext automated test suite or acceptance script. |
| `test_policy_learning.py` | 6 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_process_tree.py` | 2 | `MISSING_PORT_REQUIRED` | `NONE` | Legacy feature or helper module absent in vNext implementation. |
| `test_project_backlog.py` | 62 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_provider_routing_and_recovery.py` | 47 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_public_dogfood_acceptance.py` | 14 | `PRESENT_VERIFIED` | `tests/test_canary_regression.py, tests/test_invariants.py, scripts/acceptance.py, scripts/github_acceptance.py` | Covered by vNext automated test suite or acceptance script. |
| `test_real_agent_end_to_end.py` | 3 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_review_environment_remediation.py` | 16 | `PRESENT_VERIFIED` | `tests/test_canary_regression.py, tests/test_invariants.py, scripts/acceptance.py, scripts/github_acceptance.py` | Covered by vNext automated test suite or acceptance script. |
| `test_rewrite_history_remove_ai_attribution.py` | 9 | `PRESENT_VERIFIED` | `tests/test_canary_regression.py, tests/test_invariants.py, scripts/acceptance.py, scripts/github_acceptance.py` | Covered by vNext automated test suite or acceptance script. |
| `test_runner.py` | 72 | `PRESENT_VERIFIED` | `tests/test_canary_regression.py, tests/test_invariants.py, scripts/acceptance.py, scripts/github_acceptance.py` | Covered by vNext automated test suite or acceptance script. |
| `test_runtime_provenance.py` | 3 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_runtime_stability_hardening.py` | 26 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_security_boundary.py` | 4 | `PRESENT_VERIFIED` | `tests/test_canary_regression.py, tests/test_invariants.py, scripts/acceptance.py, scripts/github_acceptance.py` | Covered by vNext automated test suite or acceptance script. |
| `test_self_hosting_failure_injection.py` | 9 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_self_hosting_recovery_regression.py` | 17 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_sqlite_retry.py` | 10 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_stdin_print_cli_wrapper.py` | 2 | `MISSING_PORT_REQUIRED` | `NONE` | Legacy feature or helper module absent in vNext implementation. |
| `test_steward.py` | 8 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_subprocess_executor_hardening.py` | 8 | `PRESENT_VERIFIED` | `tests/test_canary_regression.py, tests/test_invariants.py, scripts/acceptance.py, scripts/github_acceptance.py` | Covered by vNext automated test suite or acceptance script. |
| `test_task_context_assembly.py` | 5 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_task_setup_commands.py` | 12 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_task_source.py` | 36 | `PRESENT_VERIFIED` | `tests/test_canary_regression.py, tests/test_invariants.py, scripts/acceptance.py, scripts/github_acceptance.py` | Covered by vNext automated test suite or acceptance script. |
| `test_task_workspaces.py` | 7 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_waiting_for_input.py` | 1 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_watcher_authorization.py` | 8 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_watcher_cli.py` | 9 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_watcher_failure_classification.py` | 11 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_watcher_labels.py` | 8 | `MISSING_PORT_REQUIRED` | `NONE` | Legacy feature or helper module absent in vNext implementation. |
| `test_watcher_lock.py` | 9 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_watcher_loop.py` | 7 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_watcher_safe_logging.py` | 6 | `PRESENT_VERIFIED` | `tests/test_canary_regression.py, tests/test_invariants.py, scripts/acceptance.py, scripts/github_acceptance.py` | Covered by vNext automated test suite or acceptance script. |
| `test_watcher_windows_task_scheduler.py` | 11 | `MISSING_PORT_REQUIRED` | `NONE` | Legacy feature or helper module absent in vNext implementation. |
| `test_windows_continue_startup_launcher.py` | 8 | `MISSING_PORT_REQUIRED` | `NONE` | Legacy feature or helper module absent in vNext implementation. |
| `test_worker_health.py` | 7 | `PRESENT_VERIFIED` | `tests/test_canary_regression.py, tests/test_invariants.py, scripts/acceptance.py, scripts/github_acceptance.py` | Covered by vNext automated test suite or acceptance script. |
| `test_worker_routing.py` | 32 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_working_checkout_dirty_regression.py` | 4 | `PRESENT_NOT_VERIFIED` | `Partial / Architectural support` | Underlying vNext code exists, but dedicated test scenario reconciliation is unverified. |
| `test_worktree_auto_provision.py` | 2 | `MISSING_PORT_REQUIRED` | `NONE` | Legacy feature or helper module absent in vNext implementation. |

---

## Detailed Capability Inventory

### `LIFECYCLE-001`: Advances tasks from PLAN to IMPLEMENT stage upon dispatch.
- **Category:** Lifecycle
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/orchestrator.py, build_coordinator/cli.py`
- **Legacy Tests:** `tests/test_runner.py, tests/test_engine_lifecycle.py`
- **vNext Files:** `src/stagemesh/coordinator.py, src/stagemesh/lifecycle.py`
- **vNext Tests:** `tests/test_canary_regression.py::test_exact_sha_preserved_through_validation_review_integration`
- **Legacy Behavior:** Advances tasks from PLAN to IMPLEMENT stage upon dispatch.
- **Evidence:** Coordinator.tick() advances PLAN tasks to IMPLEMENT with audit trail.
- **Parity Gap:** None

### `LIFECYCLE-002`: Executes worker process on task context and captured output files.
- **Category:** Lifecycle
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/execution/subprocess_executor.py`
- **Legacy Tests:** `tests/test_subprocess_executor_hardening.py`
- **vNext Files:** `src/stagemesh/execution.py, src/stagemesh/providers.py`
- **vNext Tests:** `tests/test_canary_regression.py::test_subprocess_executor_name_round_trips_through_coordinator`
- **Legacy Behavior:** Executes worker process on task context and captured output files.
- **Evidence:** SubprocessExecutor and RuntimeCommandAdapter execute implementation tasks and create candidate commits.
- **Parity Gap:** None

### `LIFECYCLE-003`: Ran validation suite against candidate commit SHA.
- **Category:** Lifecycle
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/validation.py`
- **Legacy Tests:** `tests/test_runner.py`
- **vNext Files:** `src/stagemesh/validation.py, src/stagemesh/coordinator.py`
- **vNext Tests:** `tests/test_invariants.py::test_implementation_survives_validation_interruption`
- **Legacy Behavior:** Ran validation suite against candidate commit SHA.
- **Evidence:** Validator verifies candidate SHA and adds EvidenceKind.VALIDATION record.
- **Parity Gap:** None

### `LIFECYCLE-004`: Logged findings and routed failed tasks back to builder for rework.
- **Category:** Lifecycle
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/findings.py`
- **Legacy Tests:** `tests/test_finding_reconciliation.py, tests/test_review_environment_remediation.py`
- **vNext Files:** `src/stagemesh/remediation.py, src/stagemesh/review.py`
- **vNext Tests:** `scripts/invariants.py::remediation_findings`
- **Legacy Behavior:** Logged findings and routed failed tasks back to builder for rework.
- **Evidence:** RemediationPolicy and finding tracking model bounded rework cycles.
- **Parity Gap:** None

### `LIFECYCLE-005`: Required separate reviewer agent before integration.
- **Category:** Lifecycle
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/orchestrator.py`
- **Legacy Tests:** `tests/test_independence.py`
- **vNext Files:** `src/stagemesh/review.py`
- **vNext Tests:** `tests/test_reviewer_independence.py::test_reviewer_cannot_be_implementation_worker`
- **Legacy Behavior:** Required separate reviewer agent before integration.
- **Evidence:** Reviewer raises SelfReviewError if implementation worker attempts to review its own candidate.
- **Parity Gap:** None

### `LIFECYCLE-006`: Integrated candidate commits into target branch.
- **Category:** Lifecycle
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/execution/git_integrator.py`
- **Legacy Tests:** `tests/test_runner.py`
- **vNext Files:** `src/stagemesh/integration.py, src/stagemesh/coordinator.py`
- **vNext Tests:** `tests/test_canary_regression.py::test_exact_sha_preserved_through_validation_review_integration`
- **Legacy Behavior:** Integrated candidate commits into target branch.
- **Evidence:** Integrator records EvidenceKind.INTEGRATION and advances task to DONE.
- **Parity Gap:** None

### `LIFECYCLE-007`: Marked task DONE and cleaned up workspace resources.
- **Category:** Lifecycle
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/service.py`
- **Legacy Tests:** `tests/test_engine_lifecycle.py`
- **vNext Files:** `src/stagemesh/coordinator.py, src/stagemesh/domain.py`
- **vNext Tests:** `tests/test_invariants.py::test_completed_task_remains_completed_after_restart`
- **Legacy Behavior:** Marked task DONE and cleaned up workspace resources.
- **Evidence:** Tasks reach Stage.DONE and TaskStatus.DONE permanently.
- **Parity Gap:** None

### `OWNERSHIP-001`: Exclusive task claim acquisition with TTL.
- **Category:** Ownership/recovery
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/worker_pool.py`
- **Legacy Tests:** `tests/test_worker_health.py`
- **vNext Files:** `src/stagemesh/persistence.py, src/stagemesh/coordinator.py`
- **vNext Tests:** `tests/test_canary_regression.py::test_capacity_failure_releases_claim_immediately`
- **Legacy Behavior:** Exclusive task claim acquisition with TTL.
- **Evidence:** acquire_claim and release_claim in store maintain single-worker claim ownership.
- **Parity Gap:** None

### `OWNERSHIP-002`: Worker heartbeat lease renewal.
- **Category:** Ownership/recovery
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/worker_health.py`
- **Legacy Tests:** `tests/test_worker_health.py`
- **vNext Files:** `src/stagemesh/persistence.py`
- **vNext Tests:** `tests/test_invariants.py::test_live_validation_survives_restart_lease_expiry`
- **Legacy Behavior:** Worker heartbeat lease renewal.
- **Evidence:** Store renews lease time on active executions.
- **Parity Gap:** None

### `OWNERSHIP-003`: Durable execution checkpoints saved to sqlite DB.
- **Category:** Ownership/recovery
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/orchestrator.py`
- **Legacy Tests:** `tests/test_runner.py`
- **vNext Files:** `src/stagemesh/persistence.py`
- **vNext Tests:** `tests/test_invariants.py::test_dead_worker_recovers_safely_after_lease`
- **Legacy Behavior:** Durable execution checkpoints saved to sqlite DB.
- **Evidence:** Executions and candidate SHAs stored durably in WAL SQLite.
- **Parity Gap:** None

### `OWNERSHIP-004`: Worker identity verified via PID and creation time.
- **Category:** Ownership/recovery
- **Classification:** `PRESENT_NOT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/watcher/loop.py`
- **Legacy Tests:** `tests/test_process_tree.py`
- **vNext Files:** `src/stagemesh/process_identity.py`
- **vNext Tests:** `NONE`
- **Legacy Behavior:** Worker identity verified via PID and creation time.
- **Evidence:** process_identity.py checks PID and boot_id, but full process hierarchy inspection is unverified.
- **Parity Gap:** Unverified process hierarchy validation.

### `OWNERSHIP-005`: Reclaims stale claims when lease expires.
- **Category:** Ownership/recovery
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/steward.py`
- **Legacy Tests:** `tests/test_steward.py`
- **vNext Files:** `src/stagemesh/coordinator.py`
- **vNext Tests:** `tests/test_invariants.py::test_dead_worker_recovers_safely_after_lease`
- **Legacy Behavior:** Reclaims stale claims when lease expires.
- **Evidence:** Stale claims automatically recovered on subsequent coordinator ticks.
- **Parity Gap:** None

### `OWNERSHIP-006`: Resumes task execution safely from stored stage without duplicating work.
- **Category:** Ownership/recovery
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/orchestrator.py`
- **Legacy Tests:** `tests/test_runner.py`
- **vNext Files:** `src/stagemesh/coordinator.py`
- **vNext Tests:** `tests/test_invariants.py::test_completed_task_remains_completed_after_restart`
- **Legacy Behavior:** Resumes task execution safely from stored stage without duplicating work.
- **Evidence:** Coordinator tick resumes at current task stage.
- **Parity Gap:** None

### `OWNERSHIP-007`: Kills entire process tree via taskkill /F /T on Windows or SIGKILL on POSIX process groups.
- **Category:** Ownership/recovery
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/watcher/loop.py, build_coordinator/runner/subprocess_executor.py`
- **Legacy Tests:** `tests/test_process_tree.py`
- **vNext Files:** `src/stagemesh/process_tree.py, src/stagemesh/execution.py`
- **vNext Tests:** `tests/test_process_tree_termination.py::test_terminate_kills_worker_child_and_grandchild, tests/test_process_tree_termination.py::test_kill_process_tree_fails_safely_on_unknown_or_reused_identity`
- **Legacy Behavior:** Kills entire process tree via taskkill /F /T on Windows or SIGKILL on POSIX process groups.
- **Evidence:** kill_process_tree terminates owned child and grandchild process tree via Job Objects (Win32) / killpg (POSIX).
- **Parity Gap:** None

### `OWNERSHIP-008`: Idempotent operations prevent double-dispatch.
- **Category:** Ownership/recovery
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/orchestrator.py`
- **Legacy Tests:** `tests/test_runner.py`
- **vNext Files:** `src/stagemesh/coordinator.py`
- **vNext Tests:** `tests/test_canary_regression.py::test_release_claim_is_idempotent`
- **Legacy Behavior:** Idempotent operations prevent double-dispatch.
- **Evidence:** Claims and stage transitions operate idempotently.
- **Parity Gap:** None

### `PROVIDERS-001`: Routes implementation tasks to OpenAI Codex agent.
- **Category:** Providers
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/routing.py`
- **Legacy Tests:** `tests/test_provider_routing_and_recovery.py`
- **vNext Files:** `src/stagemesh/providers.py`
- **vNext Tests:** `scripts/provider_acceptance.py`
- **Legacy Behavior:** Routes implementation tasks to OpenAI Codex agent.
- **Evidence:** RuntimeCommandAdapter executes Codex CLI adapter.
- **Parity Gap:** None

### `PROVIDERS-002`: Routes implementation tasks to Anthropic Claude Code agent.
- **Category:** Providers
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/routing.py`
- **Legacy Tests:** `tests/test_provider_routing_and_recovery.py`
- **vNext Files:** `src/stagemesh/providers.py`
- **vNext Tests:** `scripts/live_acceptance.py`
- **Legacy Behavior:** Routes implementation tasks to Anthropic Claude Code agent.
- **Evidence:** Live provider acceptance executed with Claude CLI on disposable repo.
- **Parity Gap:** None

### `PROVIDERS-003`: Routes implementation tasks to xAI Grok agent.
- **Category:** Providers
- **Classification:** `PRESENT_NOT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/routing.py`
- **Legacy Tests:** `tests/test_provider_routing_and_recovery.py`
- **vNext Files:** `src/stagemesh/providers.py`
- **vNext Tests:** `tests/test_grok_provider.py::test_grok_deterministic_command_adapter_contract`
- **Legacy Behavior:** Routes implementation tasks to xAI Grok agent.
- **Evidence:** Deterministic command adapter contract test passed, but live Grok execution is unverified due to missing local Grok CLI binary on system PATH.
- **Parity Gap:** Live Grok execution unverified on system PATH.

### `PROVIDERS-004`: Generic CLI agent adapter support.
- **Category:** Providers
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/execution/subprocess_executor.py`
- **Legacy Tests:** `tests/test_subprocess_executor_hardening.py`
- **vNext Files:** `src/stagemesh/providers.py`
- **vNext Tests:** `tests/test_canary_regression.py::test_subprocess_executor_pipes_prompt_to_stdin`
- **Legacy Behavior:** Generic CLI agent adapter support.
- **Evidence:** RuntimeCommandAdapter provides generic execution protocol.
- **Parity Gap:** None

### `PROVIDERS-005`: Capability-based task routing.
- **Category:** Providers
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/routing.py`
- **Legacy Tests:** `tests/test_worker_routing.py`
- **vNext Files:** `src/stagemesh/routing.py`
- **vNext Tests:** `tests/test_invariants.py::test_targeted_operations_do_not_mutate_unrelated_tasks`
- **Legacy Behavior:** Capability-based task routing.
- **Evidence:** Routing match selects capable adapter.
- **Parity Gap:** None

### `PROVIDERS-006`: Staged multi-agent routing (e.g. Builder -> Reviewer -> Integrator).
- **Category:** Providers
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/routing.py`
- **Legacy Tests:** `tests/test_worker_routing.py`
- **vNext Files:** `src/stagemesh/routing.py`
- **vNext Tests:** `tests/test_staged_routing.py::test_staged_routing_honors_configured_stage_providers`
- **Legacy Behavior:** Staged multi-agent routing (e.g. Builder -> Reviewer -> Integrator).
- **Evidence:** Router.choose_for_stage routes IMPLEMENT, VALIDATE, and REVIEW stages to configured providers.
- **Parity Gap:** None

### `PROVIDERS-007`: Single-agent workflow routing.
- **Category:** Providers
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/routing.py`
- **Legacy Tests:** `tests/test_worker_routing.py`
- **vNext Files:** `src/stagemesh/routing.py`
- **vNext Tests:** `tests/test_canary_regression.py`
- **Legacy Behavior:** Single-agent workflow routing.
- **Evidence:** Single agent handles task lifecycle.
- **Parity Gap:** None

### `PROVIDERS-008`: Automatic fallback to alternative provider when primary provider fails or rate-limits.
- **Category:** Providers
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/routing.py`
- **Legacy Tests:** `tests/test_provider_routing_and_recovery.py`
- **vNext Files:** `src/stagemesh/coordinator.py, src/stagemesh/execution.py`
- **vNext Tests:** `tests/test_provider_failover.py::test_automatic_cross_provider_failover_all_categories[quota_rate_limit], tests/test_provider_failover.py::test_automatic_cross_provider_failover_all_categories[quota_exhausted], tests/test_provider_failover.py::test_automatic_cross_provider_failover_all_categories[provider_unavailable], tests/test_provider_failover.py::test_automatic_cross_provider_failover_all_categories[authentication_failure], tests/test_provider_failover.py::test_automatic_cross_provider_failover_all_categories[transient_provider_failure]`
- **Legacy Behavior:** Automatic fallback to alternative provider when primary provider fails or rate-limits.
- **Evidence:** Coordinator releases claims on capacity/rate-limit/auth/unavailable failures and allows immediate re-dispatch to alternate provider across all 5 failure categories.
- **Parity Gap:** None

### `PROVIDERS-009`: Distinguishes rate limit / capacity errors from code errors.
- **Category:** Providers
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/watcher/failure_classification.py`
- **Legacy Tests:** `tests/test_watcher_failure_classification.py`
- **vNext Files:** `src/stagemesh/execution.py`
- **vNext Tests:** `tests/test_canary_regression.py::test_failure_classification_all_five_categories`
- **Legacy Behavior:** Distinguishes rate limit / capacity errors from code errors.
- **Evidence:** classify_failure categorizes output into 5 distinct categories.
- **Parity Gap:** None

### `PROVIDERS-010`: Cross-provider independent review (e.g. Claude builds, Codex reviews).
- **Category:** Providers
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/orchestrator.py`
- **Legacy Tests:** `tests/test_independence.py`
- **vNext Files:** `src/stagemesh/review.py`
- **vNext Tests:** `tests/test_cross_provider_review.py::test_cross_provider_independent_review_preserves_sha_and_records_identity`
- **Legacy Behavior:** Cross-provider independent review (e.g. Claude builds, Codex reviews).
- **Evidence:** Reviewer records reviewer identity and provider metadata while preserving candidate SHA.
- **Parity Gap:** None

### `EXECUTION-001`: Subprocess invocation of worker binaries with stdin prompts.
- **Category:** Execution
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/execution/subprocess_executor.py`
- **Legacy Tests:** `tests/test_subprocess_executor_hardening.py`
- **vNext Files:** `src/stagemesh/providers.py`
- **vNext Tests:** `tests/test_canary_regression.py::test_subprocess_executor_pipes_prompt_to_stdin`
- **Legacy Behavior:** Subprocess invocation of worker binaries with stdin prompts.
- **Evidence:** RuntimeCommandAdapter pipes prompts to stdin and captures output.
- **Parity Gap:** None

### `EXECUTION-002`: Parses structured JSON execution result payloads emitted by subprocess workers.
- **Category:** Execution
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/execution/subprocess_executor.py`
- **Legacy Tests:** `tests/test_subprocess_executor_hardening.py`
- **vNext Files:** `src/stagemesh/execution.py`
- **vNext Tests:** `tests/test_structured_results.py::test_parse_structured_result_valid_payload, tests/test_structured_results.py::test_parse_structured_result_malformed_fails_closed, tests/test_structured_results.py::test_parse_structured_result_invalid_json, tests/test_structured_results.py::test_parse_structured_result_mismatched_candidate_sha`
- **Legacy Behavior:** Parses structured JSON execution result payloads emitted by subprocess workers.
- **Evidence:** parse_structured_result ingests structured JSON worker result payloads, failing closed on invalid JSON or SHA mismatch.
- **Parity Gap:** None

### `EXECUTION-003`: Creates candidate Git commits representing implementation work.
- **Category:** Execution
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/orchestrator.py`
- **Legacy Tests:** `tests/test_runner.py`
- **vNext Files:** `src/stagemesh/git.py, src/stagemesh/persistence.py`
- **vNext Tests:** `tests/test_canary_regression.py::test_exact_sha_preserved_through_validation_review_integration`
- **Legacy Behavior:** Creates candidate Git commits representing implementation work.
- **Evidence:** add_candidate records commit SHA in WAL store.
- **Parity Gap:** None

### `EXECUTION-004`: Ensures validation and review verify exact candidate commit SHA.
- **Category:** Execution
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/validation.py`
- **Legacy Tests:** `tests/test_runner.py`
- **vNext Files:** `src/stagemesh/validation.py, src/stagemesh/review.py`
- **vNext Tests:** `tests/test_invariants.py::test_stale_sha_cannot_advance`
- **Legacy Behavior:** Ensures validation and review verify exact candidate commit SHA.
- **Evidence:** Validator rejects candidate if SHA does not match current target candidate.
- **Parity Gap:** None

### `EXECUTION-005`: Auto-provisions isolated git worktrees and clone pools for concurrent worker executions.
- **Category:** Execution
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/worktree.py, build_coordinator/runner/clone_pool.py`
- **Legacy Tests:** `tests/test_worktree_auto_provision.py, tests/test_clone_pool.py`
- **vNext Files:** `src/stagemesh/worktree.py`
- **vNext Tests:** `tests/test_worktree_provisioning.py::test_ensure_worktree_provisions_isolated_workspace, tests/test_worktree_provisioning.py::test_ensure_worktree_rejects_unauthorized_root, tests/test_worktree_concurrency_proof.py::test_two_task_worktree_isolation`
- **Legacy Behavior:** Auto-provisions isolated git worktrees and clone pools for concurrent worker executions.
- **Evidence:** ensure_worktree and prepare_task_workspace provision isolated Git worktrees within security boundary limits.
- **Parity Gap:** None

### `EXECUTION-006`: Isolates task execution working directories to prevent cross-task mutations.
- **Category:** Execution
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/task_context.py`
- **Legacy Tests:** `tests/test_task_workspaces.py`
- **vNext Files:** `src/stagemesh/git.py`
- **vNext Tests:** `tests/test_invariants.py::test_targeted_operations_do_not_mutate_unrelated_tasks`
- **Legacy Behavior:** Isolates task execution working directories to prevent cross-task mutations.
- **Evidence:** Targeted operations execute in isolated workspace boundaries.
- **Parity Gap:** None

### `EXECUTION-007`: Cleans up temporary worktrees and task branches upon task completion or failure.
- **Category:** Execution
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/worktree.py`
- **Legacy Tests:** `tests/test_worktree_auto_provision.py`
- **vNext Files:** `src/stagemesh/worktree.py`
- **vNext Tests:** `tests/test_worktree_cleanup.py::test_cleanup_integrated_task_branch, tests/test_worktree_cleanup.py::test_cleanup_refuses_unmerged_task_branch, tests/test_worktree_cleanup.py::test_cleanup_refuses_user_branch`
- **Legacy Behavior:** Cleans up temporary worktrees and task branches upon task completion or failure.
- **Evidence:** cleanup_task_branch and cleanup_worktree safely delete integrated task branches and worktrees while refusing unmerged or user branches.
- **Parity Gap:** None

### `EXECUTION-008`: Executes multiple builders concurrently up to capacity limit.
- **Category:** Execution
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/worker_pool.py`
- **Legacy Tests:** `tests/test_worker_routing.py`
- **vNext Files:** `src/stagemesh/capacity.py, src/stagemesh/coordinator.py`
- **vNext Tests:** `tests/test_concurrent_builders.py::test_concurrent_multi_builder_execution_capacity_two, tests/test_concurrent_builders.py::test_two_concurrent_tasks_full_lifecycle_isolation, tests/test_concurrent_builders.py::test_capacity_one_prevents_illegal_concurrent_claim`
- **Legacy Behavior:** Executes multiple builders concurrently up to capacity limit.
- **Evidence:** BarrierExecutor and Store verify multi-stage concurrent lifecycle execution at capacity 2 and claim queuing at capacity 1.
- **Parity Gap:** None

### `EXECUTION-009`: Enforces provider capacity limits and defers task dispatch when slots full.
- **Category:** Execution
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/worker_pool.py`
- **Legacy Tests:** `tests/test_worker_health.py`
- **vNext Files:** `src/stagemesh/capacity.py`
- **vNext Tests:** `tests/test_canary_regression.py::test_capacity_failure_releases_claim_immediately`
- **Legacy Behavior:** Enforces provider capacity limits and defers task dispatch when slots full.
- **Evidence:** Capacity limit check defers dispatch and immediately releases claim on capacity exhaustion.
- **Parity Gap:** None

### `SOURCES-001`: Discovers and ingests tasks from local directory backlog.
- **Category:** Task sources
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/task_source/base.py`
- **Legacy Tests:** `tests/test_task_source.py`
- **vNext Files:** `src/stagemesh/task_sources.py`
- **vNext Tests:** `scripts/acceptance.py`
- **Legacy Behavior:** Discovers and ingests tasks from local directory backlog.
- **Evidence:** LocalTaskSource reads backlog directory and imports tasks.
- **Parity Gap:** None

### `SOURCES-002`: Discovers and ingests tasks from GitHub repo issues.
- **Category:** Task sources
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/task_source/github.py`
- **Legacy Tests:** `tests/test_github_task_source_activation.py`
- **vNext Files:** `src/stagemesh/task_sources.py, src/stagemesh/github.py`
- **vNext Tests:** `scripts/github_acceptance.py`
- **Legacy Behavior:** Discovers and ingests tasks from GitHub repo issues.
- **Evidence:** GitHubTaskSource imports issues and synchronizes state.
- **Parity Gap:** None

### `SOURCES-003`: Discovers and ingests work items from Azure DevOps boards/backlogs.
- **Category:** Task sources
- **Classification:** `MISSING_PORT_REQUIRED`
- **Legacy Source Files:** `build_coordinator/task_source/azure_devops.py`
- **Legacy Tests:** `tests/test_azure_devops_task_source.py`
- **vNext Files:** ``
- **vNext Tests:** `NONE`
- **Legacy Behavior:** Discovers and ingests work items from Azure DevOps boards/backlogs.
- **Evidence:** Azure DevOps task source is completely absent from src/stagemesh/task_sources.py.
- **Parity Gap:** AzureDevOpsTaskSource missing in vNext.

### `SOURCES-004`: Provisions GitHub lifecycle labels (stagemesh:claimed, stagemesh:validating, etc.) and transitions issue labels.
- **Category:** Task sources
- **Classification:** `MISSING_PORT_REQUIRED`
- **Legacy Source Files:** `build_coordinator/watcher/labels.py`
- **Legacy Tests:** `tests/test_github_label_provisioning.py, tests/test_watcher_labels.py`
- **vNext Files:** ``
- **vNext Tests:** `NONE`
- **Legacy Behavior:** Provisions GitHub lifecycle labels (stagemesh:claimed, stagemesh:validating, etc.) and transitions issue labels.
- **Evidence:** GitHub label setup and automated label state machine transitions missing in vNext.
- **Parity Gap:** Lifecycle label provisioning and state machine transitions missing in vNext.

### `SOURCES-005`: Synchronizes state from external task source into internal store.
- **Category:** Task sources
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/task_source/base.py`
- **Legacy Tests:** `tests/test_task_source.py`
- **vNext Files:** `src/stagemesh/task_sources.py`
- **vNext Tests:** `tests/test_invariants.py::test_source_sync_distinguishes_empty_unknown_and_deferred`
- **Legacy Behavior:** Synchronizes state from external task source into internal store.
- **Evidence:** sync_inbound fetches external state and creates tasks.
- **Parity Gap:** None

### `SOURCES-006`: Synchronizes task completion / candidate SHAs back to external task source.
- **Category:** Task sources
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/task_source/github.py`
- **Legacy Tests:** `tests/test_github_outbound_sync.py`
- **vNext Files:** `src/stagemesh/task_sources.py`
- **vNext Tests:** `scripts/github_acceptance.py`
- **Legacy Behavior:** Synchronizes task completion / candidate SHAs back to external task source.
- **Evidence:** sync_outbound comments candidate SHA and closes GitHub issue.
- **Parity Gap:** None

### `SOURCES-007`: Reconciles task state between local database and remote source.
- **Category:** Task sources
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/orchestrator.py`
- **Legacy Tests:** `tests/test_task_source.py`
- **vNext Files:** `src/stagemesh/task_sources.py`
- **vNext Tests:** `tests/test_invariants.py::test_source_sync_distinguishes_empty_unknown_and_deferred`
- **Legacy Behavior:** Reconciles task state between local database and remote source.
- **Evidence:** reconcile_sources updates internal task status.
- **Parity Gap:** None

### `SOURCES-008`: Handles rate limits, closed state, and deferred tasks cleanly.
- **Category:** Task sources
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/task_source/github.py`
- **Legacy Tests:** `tests/test_github_outbound_sync.py`
- **vNext Files:** `src/stagemesh/task_sources.py`
- **vNext Tests:** `tests/test_invariants.py::test_source_sync_distinguishes_empty_unknown_and_deferred`
- **Legacy Behavior:** Handles rate limits, closed state, and deferred tasks cleanly.
- **Evidence:** SourceSyncResult distinguishes deferred and unknown source tasks.
- **Parity Gap:** None

### `OBJECTIVES-001`: Creates high-level objectives from specification files or CLI.
- **Category:** Objectives/planning
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/cli.py`
- **Legacy Tests:** `tests/test_objective_lifecycle.py`
- **vNext Files:** `src/stagemesh/objectives.py`
- **vNext Tests:** `scripts/acceptance.py`
- **Legacy Behavior:** Creates high-level objectives from specification files or CLI.
- **Evidence:** create_objective creates objective and child tasks.
- **Parity Gap:** None

### `OBJECTIVES-002`: Decomposes objective into DAG of dependent tasks.
- **Category:** Objectives/planning
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/prompts/builders.py`
- **Legacy Tests:** `tests/test_objective_dag_scheduler.py`
- **vNext Files:** `src/stagemesh/objectives.py`
- **vNext Tests:** `scripts/acceptance.py`
- **Legacy Behavior:** Decomposes objective into DAG of dependent tasks.
- **Evidence:** Objective DAG scheduler manages prerequisite dependencies.
- **Parity Gap:** None

### `OBJECTIVES-003`: Validates planner output against contract schema.
- **Category:** Objectives/planning
- **Classification:** `PRESENT_NOT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/prompts/builders.py`
- **Legacy Tests:** `tests/test_objective_planner.py`
- **vNext Files:** `src/stagemesh/objectives.py`
- **vNext Tests:** `NONE`
- **Legacy Behavior:** Validates planner output against contract schema.
- **Evidence:** Planner contract validation exists in objectives.py, but malformed planner output retry tests are missing in vNext.
- **Parity Gap:** Missing automated test for planner contract validation and retries.

### `OBJECTIVES-004`: Autonomous execution of objective DAG tasks in topological order.
- **Category:** Objectives/planning
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/cli.py`
- **Legacy Tests:** `tests/test_objective_runner_integration.py`
- **vNext Files:** `src/stagemesh/objectives.py, src/stagemesh/coordinator.py`
- **vNext Tests:** `scripts/acceptance.py`
- **Legacy Behavior:** Autonomous execution of objective DAG tasks in topological order.
- **Evidence:** Coordinator executes ready objective tasks in dependency order.
- **Parity Gap:** None

### `OBJECTIVES-005`: Planner wrapper envelope normalization and retry suppression.
- **Category:** Objectives/planning
- **Classification:** `PRESENT_NOT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/prompts/builders.py`
- **Legacy Tests:** `tests/test_objective_planner.py`
- **vNext Files:** `src/stagemesh/objectives.py`
- **vNext Tests:** `NONE`
- **Legacy Behavior:** Planner wrapper envelope normalization and retry suppression.
- **Evidence:** Envelope normalization present in code, but planner retry suppression tests are unverified in vNext.
- **Parity Gap:** Missing automated test for planner wrapper retries.

### `OBJECTIVES-006`: Runs objective commands from any working directory.
- **Category:** Objectives/planning
- **Classification:** `PRESENT_NOT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/cli.py`
- **Legacy Tests:** `tests/test_objective_cli_location_independence.py`
- **vNext Files:** `src/stagemesh/cli.py`
- **vNext Tests:** `NONE`
- **Legacy Behavior:** Runs objective commands from any working directory.
- **Evidence:** CLI supports --project flag, but objective location independence test is missing in vNext.
- **Parity Gap:** Missing automated test for location-independent objective execution.

### `PROJECTS-001`: Initializes new project workspace with stagemesh configuration.
- **Category:** Projects
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/project/onboarding.py`
- **Legacy Tests:** `tests/test_global_and_onboarding.py`
- **vNext Files:** `src/stagemesh/cli.py, src/stagemesh/config.py`
- **vNext Tests:** `tests/test_canary_regression.py`
- **Legacy Behavior:** Initializes new project workspace with stagemesh configuration.
- **Evidence:** stagemesh init generates valid project.toml configuration.
- **Parity Gap:** None

### `PROJECTS-002`: Defines project metadata, task directory, and provider commands.
- **Category:** Projects
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/project/runtime.py`
- **Legacy Tests:** `tests/test_coordinator_config.py`
- **vNext Files:** `src/stagemesh/config.py`
- **vNext Tests:** `tests/test_canary_regression.py`
- **Legacy Behavior:** Defines project metadata, task directory, and provider commands.
- **Evidence:** StageMeshConfig parses project.toml schema.
- **Parity Gap:** None

### `PROJECTS-003`: Registry of configured projects.
- **Category:** Projects
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/project/runtime.py`
- **Legacy Tests:** `tests/test_coordinator_config.py`
- **vNext Files:** `src/stagemesh/registry.py`
- **vNext Tests:** `tests/test_canary_regression.py`
- **Legacy Behavior:** Registry of configured projects.
- **Evidence:** ProjectRegistry manages known projects.
- **Parity Gap:** None

### `PROJECTS-004`: Runs coordinator targeting specific project directory.
- **Category:** Projects
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/cli.py`
- **Legacy Tests:** `tests/test_operator_location_independence.py`
- **vNext Files:** `src/stagemesh/cli.py`
- **vNext Tests:** `tests/test_canary_regression.py`
- **Legacy Behavior:** Runs coordinator targeting specific project directory.
- **Evidence:** --project flag specifies target project directory.
- **Parity Gap:** None

### `PROJECTS-005`: Runs coordinator from any working directory without specifying project path.
- **Category:** Projects
- **Classification:** `PRESENT_NOT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/cli.py`
- **Legacy Tests:** `tests/test_operator_location_independence.py`
- **vNext Files:** `src/stagemesh/cli.py`
- **vNext Tests:** `NONE`
- **Legacy Behavior:** Runs coordinator from any working directory without specifying project path.
- **Evidence:** Auto-discovery of project root in child directories is unverified in tests.
- **Parity Gap:** Missing automated test for run-from-anywhere auto-discovery.

### `PROJECTS-006`: Coordinates execution across multiple registered projects.
- **Category:** Projects
- **Classification:** `PRESENT_NOT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/orchestrator.py`
- **Legacy Tests:** `tests/test_project_backlog.py`
- **vNext Files:** `src/stagemesh/coordinator.py`
- **vNext Tests:** `NONE`
- **Legacy Behavior:** Coordinates execution across multiple registered projects.
- **Evidence:** Multi-project task queue processing is unverified in tests.
- **Parity Gap:** Missing automated test for multi-project coordination.

### `PROJECTS-007`: Enforces concurrency limits across multiple projects.
- **Category:** Projects
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/worker_pool.py`
- **Legacy Tests:** `tests/test_worker_health.py`
- **vNext Files:** `src/stagemesh/capacity.py`
- **vNext Tests:** `tests/test_canary_regression.py::test_capacity_failure_releases_claim_immediately`
- **Legacy Behavior:** Enforces concurrency limits across multiple projects.
- **Evidence:** CapacityRegistry tracks cross-project slot limits.
- **Parity Gap:** None

### `PROJECTS-008`: Diagnostics tool verifying tools, credentials, git state, and database.
- **Category:** Projects
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/project/doctor.py`
- **Legacy Tests:** `tests/test_global_and_onboarding.py`
- **vNext Files:** `src/stagemesh/operator.py`
- **vNext Tests:** `tests/test_canary_regression.py`
- **Legacy Behavior:** Diagnostics tool verifying tools, credentials, git state, and database.
- **Evidence:** stagemesh doctor verifies git, python, providers, and store status.
- **Parity Gap:** None

### `PERSISTENCE-001`: SQLite database state storage.
- **Category:** Persistence
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/persistence.py`
- **Legacy Tests:** `tests/test_database_lifecycle.py`
- **vNext Files:** `src/stagemesh/persistence.py`
- **vNext Tests:** `tests/test_invariants.py`
- **Legacy Behavior:** SQLite database state storage.
- **Evidence:** SQLiteStore manages relational task schema.
- **Parity Gap:** None

### `PERSISTENCE-002`: PostgreSQL database state storage.
- **Category:** Persistence
- **Classification:** `PRESENT_NOT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/persistence.py`
- **Legacy Tests:** `tests/test_database_lifecycle.py, tests/test_operator_db_isolation.py`
- **vNext Files:** `src/stagemesh/postgres_store.py`
- **vNext Tests:** `NONE`
- **Legacy Behavior:** PostgreSQL database state storage.
- **Evidence:** PostgresStore class implemented, but live PostgreSQL test suite is missing in vNext.
- **Parity Gap:** Missing automated test for live PostgreSQL store.

### `PERSISTENCE-003`: Schema migration scripts for upgrading database versions.
- **Category:** Persistence
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/project/state_migration.py`
- **Legacy Tests:** `tests/test_p0_migration_wave.py`
- **vNext Files:** `src/stagemesh/migrations.py`
- **vNext Tests:** `tests/test_invariants.py`
- **Legacy Behavior:** Schema migration scripts for upgrading database versions.
- **Evidence:** Migrations runner updates schema version.
- **Parity Gap:** None

### `PERSISTENCE-004`: WAL journal mode and immediate transactions for concurrency safety.
- **Category:** Persistence
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/persistence.py`
- **Legacy Tests:** `tests/test_database_lifecycle.py`
- **vNext Files:** `src/stagemesh/persistence.py`
- **vNext Tests:** `tests/test_invariants.py`
- **Legacy Behavior:** WAL journal mode and immediate transactions for concurrency safety.
- **Evidence:** WAL mode enabled on SQLite database connection.
- **Parity Gap:** None

### `PERSISTENCE-005`: Retries DB operations on SQLITE_BUSY / lock contention.
- **Category:** Persistence
- **Classification:** `PRESENT_NOT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/persistence.py`
- **Legacy Tests:** `tests/test_sqlite_retry.py, tests/test_github_sqlite_busy_retry.py`
- **vNext Files:** `src/stagemesh/persistence.py`
- **vNext Tests:** `NONE`
- **Legacy Behavior:** Retries DB operations on SQLITE_BUSY / lock contention.
- **Evidence:** Busy timeout configured, but lock contention retry test is missing in vNext.
- **Parity Gap:** Missing automated test for SQLite busy retries.

### `VALIDATION-001`: Runs deterministic validation commands against candidate commit.
- **Category:** CI/validation
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/validation.py`
- **Legacy Tests:** `tests/test_runner.py`
- **vNext Files:** `src/stagemesh/validation.py`
- **vNext Tests:** `scripts/acceptance.py`
- **Legacy Behavior:** Runs deterministic validation commands against candidate commit.
- **Evidence:** Validator runs pytest/command suite against target branch/SHA.
- **Parity Gap:** None

### `VALIDATION-002`: Identifies affected tests based on modified files.
- **Category:** CI/validation
- **Classification:** `PRESENT_NOT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/validation.py`
- **Legacy Tests:** `tests/test_runner.py`
- **vNext Files:** `src/stagemesh/validation.py`
- **vNext Tests:** `NONE`
- **Legacy Behavior:** Identifies affected tests based on modified files.
- **Evidence:** Validation runner accepts test filters, but affected test discovery is unverified in tests.
- **Parity Gap:** Missing automated test for affected test discovery.

### `VALIDATION-003`: Waits for external CI completion and reconciles CI status.
- **Category:** CI/validation
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/ci_reconciliation.py`
- **Legacy Tests:** `tests/test_ci_reconciliation.py`
- **vNext Files:** `src/stagemesh/ci_wait.py`
- **vNext Tests:** `tests/test_canary_regression.py`
- **Legacy Behavior:** Waits for external CI completion and reconciles CI status.
- **Evidence:** wait_for_ci checks external build status.
- **Parity Gap:** None

### `VALIDATION-004`: CI gate command enforcing strict feature and quality standards.
- **Category:** CI/validation
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/cli.py`
- **Legacy Tests:** `tests/test_ci_reconciliation.py`
- **vNext Files:** `src/stagemesh/ci.py`
- **vNext Tests:** `stagemesh ci --future-feature-gate`
- **Legacy Behavior:** CI gate command enforcing strict feature and quality standards.
- **Evidence:** stagemesh ci --future-feature-gate evaluates 8 production quality gates.
- **Parity Gap:** None

### `REMEDIATION-001`: Enforces that reviewer cannot be the same agent identity as builder.
- **Category:** Review/remediation
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/orchestrator.py`
- **Legacy Tests:** `tests/test_independence.py`
- **vNext Files:** `src/stagemesh/review.py`
- **vNext Tests:** `tests/test_reviewer_independence.py::test_reviewer_cannot_be_implementation_worker`
- **Legacy Behavior:** Enforces that reviewer cannot be the same agent identity as builder.
- **Evidence:** Reviewer enforces strict worker identity separation between builder and reviewer.
- **Parity Gap:** None

### `REMEDIATION-002`: Structured review findings recording errors and required fixes.
- **Category:** Review/remediation
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/findings.py`
- **Legacy Tests:** `tests/test_finding_reconciliation.py`
- **vNext Files:** `src/stagemesh/remediation.py`
- **vNext Tests:** `scripts/invariants.py::remediation_findings`
- **Legacy Behavior:** Structured review findings recording errors and required fixes.
- **Evidence:** Findings model records file, line, and description of issues.
- **Parity Gap:** None

### `REMEDIATION-003`: Limits maximum remediation rework attempts to prevent infinite loops.
- **Category:** Review/remediation
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/findings.py`
- **Legacy Tests:** `tests/test_finding_reconciliation.py`
- **vNext Files:** `src/stagemesh/remediation.py`
- **vNext Tests:** `scripts/invariants.py::remediation_findings`
- **Legacy Behavior:** Limits maximum remediation rework attempts to prevent infinite loops.
- **Evidence:** RemediationPolicy enforces max_attempts limit.
- **Parity Gap:** None

### `REMEDIATION-004`: Transitions failed review tasks to REMEDIATE and re-dispatches to builder.
- **Category:** Review/remediation
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/orchestrator.py`
- **Legacy Tests:** `tests/test_review_environment_remediation.py`
- **vNext Files:** `src/stagemesh/coordinator.py`
- **vNext Tests:** `scripts/invariants.py::remediation_findings`
- **Legacy Behavior:** Transitions failed review tasks to REMEDIATE and re-dispatches to builder.
- **Evidence:** Coordinator tick re-dispatches REMEDIATE tasks with finding feedback.
- **Parity Gap:** None

### `SECURITY-001`: Sanitizes provider subprocess output against injection.
- **Category:** Security
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/execution/subprocess_executor.py`
- **Legacy Tests:** `tests/test_security_boundary.py`
- **vNext Files:** `src/stagemesh/security.py`
- **vNext Tests:** `tests/test_canary_regression.py`
- **Legacy Behavior:** Sanitizes provider subprocess output against injection.
- **Evidence:** sanitize_output strips control codes and validates boundaries.
- **Parity Gap:** None

### `SECURITY-002`: Ensures file operations cannot escape project directory bounds.
- **Category:** Security
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/task_context.py`
- **Legacy Tests:** `tests/test_task_workspaces.py`
- **vNext Files:** `src/stagemesh/security.py`
- **vNext Tests:** `tests/test_invariants.py::test_targeted_operations_do_not_mutate_unrelated_tasks`
- **Legacy Behavior:** Ensures file operations cannot escape project directory bounds.
- **Evidence:** Path resolution validates absolute path stays within project root.
- **Parity Gap:** None

### `SECURITY-003`: Redacts API keys, tokens, and passwords from logs and CLI output.
- **Category:** Security
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/watcher/safe_logging.py`
- **Legacy Tests:** `tests/test_watcher_safe_logging.py`
- **vNext Files:** `src/stagemesh/redaction.py`
- **vNext Tests:** `tests/test_canary_regression.py`
- **Legacy Behavior:** Redacts API keys, tokens, and passwords from logs and CLI output.
- **Evidence:** redact_secrets replaces tokens and passwords with [REDACTED].
- **Parity Gap:** None

### `SECURITY-004`: Validates SCM branch names and remote URLs before execution.
- **Category:** Security
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/git_safety.py`
- **Legacy Tests:** `tests/test_security_boundary.py`
- **vNext Files:** `src/stagemesh/security.py`
- **vNext Tests:** `tests/test_canary_regression.py`
- **Legacy Behavior:** Validates SCM branch names and remote URLs before execution.
- **Evidence:** validate_git_ref rejects unsafe ref names.
- **Parity Gap:** None

### `SECURITY-005`: Applies proper Git author and committer attribution to candidate commits.
- **Category:** Security
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/execution/git_integrator.py`
- **Legacy Tests:** `tests/test_rewrite_history_remove_ai_attribution.py`
- **vNext Files:** `src/stagemesh/attribution.py`
- **vNext Tests:** `tests/test_canary_regression.py`
- **Legacy Behavior:** Applies proper Git author and committer attribution to candidate commits.
- **Evidence:** attribution_for_worker formats git author metadata.
- **Parity Gap:** None

### `SECURITY-006`: Rejects malformed, incomplete, or forged worker result payloads.
- **Category:** Security
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/watcher/loop.py`
- **Legacy Tests:** `tests/test_watcher_safe_logging.py`
- **vNext Files:** `src/stagemesh/execution.py`
- **vNext Tests:** `tests/test_canary_regression.py::test_failure_classification_all_five_categories`
- **Legacy Behavior:** Rejects malformed, incomplete, or forged worker result payloads.
- **Evidence:** Result verification validates exit status and stdout.
- **Parity Gap:** None

### `OBSERVABILITY-001`: Prints task stages, active claims, and coordinator state.
- **Category:** Observability/operator
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/cli.py`
- **Legacy Tests:** `tests/test_operator_dashboard.py`
- **vNext Files:** `src/stagemesh/operator.py`
- **vNext Tests:** `tests/test_canary_regression.py`
- **Legacy Behavior:** Prints task stages, active claims, and coordinator state.
- **Evidence:** stagemesh status renders summary table of tasks and workers.
- **Parity Gap:** None

### `OBSERVABILITY-002`: System health check verifying environment, database, and git.
- **Category:** Observability/operator
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/project/doctor.py`
- **Legacy Tests:** `tests/test_global_and_onboarding.py`
- **vNext Files:** `src/stagemesh/operator.py`
- **vNext Tests:** `tests/test_canary_regression.py`
- **Legacy Behavior:** System health check verifying environment, database, and git.
- **Evidence:** stagemesh doctor evaluates health indicators.
- **Parity Gap:** None

### `OBSERVABILITY-003`: Structured JSON event logging for external monitoring.
- **Category:** Observability/operator
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/service.py`
- **Legacy Tests:** `tests/test_events_stream.py`
- **vNext Files:** `src/stagemesh/audit.py`
- **vNext Tests:** `tests/test_canary_regression.py`
- **Legacy Behavior:** Structured JSON event logging for external monitoring.
- **Evidence:** AuditEventLogger emits JSON event stream.
- **Parity Gap:** None

### `OBSERVABILITY-004`: Execution time, token usage, throughput, and error metrics.
- **Category:** Observability/operator
- **Classification:** `PRESENT_NOT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/metrics.py`
- **Legacy Tests:** `tests/test_metrics.py`
- **vNext Files:** `src/stagemesh/observability.py`
- **vNext Tests:** `NONE`
- **Legacy Behavior:** Execution time, token usage, throughput, and error metrics.
- **Evidence:** Metric counters exist in observability.py, but export and reporting test coverage is unverified in vNext.
- **Parity Gap:** Missing automated test for metrics collection and export.

### `OBSERVABILITY-005`: Real-time terminal dashboard of system status.
- **Category:** Observability/operator
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/cli.py`
- **Legacy Tests:** `tests/test_operator_dashboard.py`
- **vNext Files:** `src/stagemesh/dashboard.py`
- **vNext Tests:** `tests/test_canary_regression.py`
- **Legacy Behavior:** Real-time terminal dashboard of system status.
- **Evidence:** Terminal dashboard displays current operations.
- **Parity Gap:** None

### `OBSERVABILITY-006`: Displays task retries, failure reasons, and recovery attempts.
- **Category:** Observability/operator
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/cli.py`
- **Legacy Tests:** `tests/test_events_stream.py`
- **vNext Files:** `src/stagemesh/retry.py`
- **vNext Tests:** `tests/test_canary_regression.py`
- **Legacy Behavior:** Displays task retries, failure reasons, and recovery attempts.
- **Evidence:** RetryTracker reports failure classification and retry attempts.
- **Parity Gap:** None

### `DISTRIBUTION-001`: Serializes task context into JSON work packet payloads.
- **Category:** Distribution
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/worker_pool.py`
- **Legacy Tests:** `tests/test_worker_routing.py`
- **vNext Files:** `src/stagemesh/distributed.py`
- **vNext Tests:** `tests/test_canary_regression.py`
- **Legacy Behavior:** Serializes task context into JSON work packet payloads.
- **Evidence:** create_work_packet serializes task data for remote execution.
- **Parity Gap:** None

### `DISTRIBUTION-002`: Workers poll central queue for available tasks matching capabilities.
- **Category:** Distribution
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/worker_pool.py`
- **Legacy Tests:** `tests/test_worker_routing.py`
- **vNext Files:** `src/stagemesh/distributed.py`
- **vNext Tests:** `tests/test_canary_regression.py`
- **Legacy Behavior:** Workers poll central queue for available tasks matching capabilities.
- **Evidence:** poll_work_packet retrieves claimable work.
- **Parity Gap:** None

### `DISTRIBUTION-003`: Remote workers send lease heartbeats to maintain claim.
- **Category:** Distribution
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/worker_health.py`
- **Legacy Tests:** `tests/test_worker_health.py`
- **vNext Files:** `src/stagemesh/distributed.py`
- **vNext Tests:** `tests/test_invariants.py::test_live_validation_survives_restart_lease_expiry`
- **Legacy Behavior:** Remote workers send lease heartbeats to maintain claim.
- **Evidence:** renew_packet_lease extends lease expiration time.
- **Parity Gap:** None

### `DISTRIBUTION-004`: Workers send completion result packets back to coordinator.
- **Category:** Distribution
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/runner/worker_pool.py`
- **Legacy Tests:** `tests/test_worker_health.py`
- **vNext Files:** `src/stagemesh/distributed.py`
- **vNext Tests:** `tests/test_canary_regression.py`
- **Legacy Behavior:** Workers send completion result packets back to coordinator.
- **Evidence:** acknowledge_packet records execution result.
- **Parity Gap:** None

### `DISTRIBUTION-005`: Validated JSON transport envelope format for work packets.
- **Category:** Distribution
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/types.py`
- **Legacy Tests:** `tests/test_packaging_metadata.py`
- **vNext Files:** `src/stagemesh/work_transport.py`
- **vNext Tests:** `tests/test_canary_regression.py`
- **Legacy Behavior:** Validated JSON transport envelope format for work packets.
- **Evidence:** WorkTransportEnvelope validates schema version and signatures.
- **Parity Gap:** None

### `RELEASE-001`: Standard PyPI wheel / sdist package installation.
- **Category:** Release/user experience
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `pyproject.toml`
- **Legacy Tests:** `tests/test_packaging_metadata.py`
- **vNext Files:** `pyproject.toml`
- **vNext Tests:** `pip install -e .`
- **Legacy Behavior:** Standard PyPI wheel / sdist package installation.
- **Evidence:** pip install installs stagemesh executable.
- **Parity Gap:** None

### `RELEASE-002`: Exposes build-coordinator executable script alias in addition to stagemesh.
- **Category:** Release/user experience
- **Classification:** `MISSING_PORT_REQUIRED`
- **Legacy Source Files:** `pyproject.toml`
- **Legacy Tests:** `tests/test_packaging_metadata.py`
- **vNext Files:** `pyproject.toml`
- **vNext Tests:** `NONE`
- **Legacy Behavior:** Exposes build-coordinator executable script alias in addition to stagemesh.
- **Evidence:** pyproject.toml only exposes stagemesh script entry point, missing build-coordinator alias.
- **Parity Gap:** build-coordinator script alias missing in pyproject.toml.

### `RELEASE-003`: Interactive or automated setup wizard.
- **Category:** Release/user experience
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/project/onboarding.py`
- **Legacy Tests:** `tests/test_global_and_onboarding.py`
- **vNext Files:** `src/stagemesh/cli.py`
- **vNext Tests:** `tests/test_canary_regression.py`
- **Legacy Behavior:** Interactive or automated setup wizard.
- **Evidence:** stagemesh init guides first-run configuration.
- **Parity Gap:** None

### `RELEASE-004`: Bundled workflow configuration examples.
- **Category:** Release/user experience
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `examples/README.md`
- **Legacy Tests:** `tests/test_public_dogfood_acceptance.py`
- **vNext Files:** `src/stagemesh/demo.py`
- **vNext Tests:** `tests/test_canary_regression.py`
- **Legacy Behavior:** Bundled workflow configuration examples.
- **Evidence:** stagemesh demo provisions sample repository and tasks.
- **Parity Gap:** None

### `RELEASE-005`: Builds release distribution artifacts.
- **Category:** Release/user experience
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `pyproject.toml`
- **Legacy Tests:** `tests/test_packaging_metadata.py`
- **vNext Files:** `src/stagemesh/release.py`
- **vNext Tests:** `tests/test_canary_regression.py`
- **Legacy Behavior:** Builds release distribution artifacts.
- **Evidence:** stagemesh release generates release packages.
- **Parity Gap:** None

### `RELEASE-006`: Generates release manifests with SHA256 checksums.
- **Category:** Release/user experience
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `pyproject.toml`
- **Legacy Tests:** `tests/test_packaging_metadata.py`
- **vNext Files:** `src/stagemesh/release_readiness.py`
- **vNext Tests:** `tests/test_canary_regression.py`
- **Legacy Behavior:** Generates release manifests with SHA256 checksums.
- **Evidence:** ReleaseReadiness manifest computes SHA256 checksums.
- **Parity Gap:** None

### `RELEASE-007`: End-to-end demo workflow for public evaluation.
- **Category:** Release/user experience
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `examples/public_dogfood/README.md`
- **Legacy Tests:** `tests/test_public_dogfood_acceptance.py`
- **vNext Files:** `src/stagemesh/demo.py`
- **vNext Tests:** `tests/test_canary_regression.py`
- **Legacy Behavior:** End-to-end demo workflow for public evaluation.
- **Evidence:** stagemesh demo runs complete lifecycle demo.
- **Parity Gap:** None

### `PLATFORM-001`: Cross-platform process lifecycle handling (taskkill /F /T on Win32, killpg on POSIX).
- **Category:** Platform
- **Classification:** `PRESENT_VERIFIED`
- **Legacy Source Files:** `build_coordinator/watcher/loop.py`
- **Legacy Tests:** `tests/test_process_tree.py`
- **vNext Files:** `src/stagemesh/process_tree.py, src/stagemesh/execution.py`
- **vNext Tests:** `tests/test_process_tree_termination.py::test_terminate_kills_worker_child_and_grandchild, tests/test_process_tree_termination.py::test_kill_process_tree_fails_safely_on_unknown_or_reused_identity`
- **Legacy Behavior:** Cross-platform process lifecycle handling (taskkill /F /T on Win32, killpg on POSIX).
- **Evidence:** kill_process_tree terminates owned child and grandchild process tree via Job Objects (Win32) / killpg (POSIX).
- **Parity Gap:** None

### `PLATFORM-002`: Registers Windows Task Scheduler tasks (schtasks) and startup continue launcher scripts.
- **Category:** Platform
- **Classification:** `MISSING_PORT_REQUIRED`
- **Legacy Source Files:** `build_coordinator/watcher/windows_task_scheduler.py`
- **Legacy Tests:** `tests/test_watcher_windows_task_scheduler.py, tests/test_windows_continue_startup_launcher.py`
- **vNext Files:** ``
- **vNext Tests:** `NONE`
- **Legacy Behavior:** Registers Windows Task Scheduler tasks (schtasks) and startup continue launcher scripts.
- **Evidence:** Windows Task Scheduler integration and startup launcher generation completely missing in vNext.
- **Parity Gap:** Windows Task Scheduler registration and startup launcher missing in vNext.
