from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import ClassVar, Self

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import stagemesh.cli as cli_module
from stagemesh.acceptance import (
    AcceptanceCheck,
    AcceptanceValidationError,
    local_acceptance_report,
    proof_gaps,
    run_check,
    write_acceptance_report,
)
from stagemesh.acceptance_matrix import (
    AcceptanceMatrixValidationError,
    acceptance_matrix,
    write_acceptance_matrix,
)
from stagemesh.attribution import AttributionValidationError, attribution_for_worker
from stagemesh.audit import AuditValidationError, export_audit_jsonl, record_audit
from stagemesh.capacity import CapacityKind, CapacityRegistry, CapacityValidationError
from stagemesh.ci import (
    CIValidationError,
    broken_future_feature_gate,
    default_gate_commands,
    default_gates,
    run_gate,
)
from stagemesh.ci_wait import decide_ci_wait
from stagemesh.completion_audit import (
    CompletionAuditValidationError,
    completion_audit,
    write_completion_audit,
)
from stagemesh.config import ConfigValidationError, load_config
from stagemesh.coordinator import Coordinator
from stagemesh.dashboard import dashboard_summary, render_dashboard
from stagemesh.demo import DemoValidationError, create_demo_project
from stagemesh.distributed import WorkQueue, WorkQueueError
from stagemesh.domain import (
    EvidenceKind,
    EvidenceStatus,
    ExecutionKind,
    ExecutionStatus,
    ProcessIdentity,
    Stage,
    TaskStatus,
)
from stagemesh.e2e_acceptance import (
    EndToEndAcceptanceValidationError,
    end_to_end_acceptance,
    write_end_to_end_acceptance,
)
from stagemesh.execution import ExecutionResult, FakeExecutor
from stagemesh.external_evidence import (
    ExternalEvidenceValidationError,
    external_evidence_records,
    record_external_evidence,
)
from stagemesh.final_report import FinalReportValidationError, render_final_report
from stagemesh.git import GitValidationError, GitWorkspace
from stagemesh.github import GitHubClient, parse_github_remote, parse_retry_after
from stagemesh.github_acceptance import run_github_acceptance
from stagemesh.lifecycle import LifecycleError, evidence_allows_advance
from stagemesh.objectives import ObjectivePlanner, ObjectiveValidationError
from stagemesh.observability import health
from stagemesh.operator import operator_report
from stagemesh.persistence import SCHEMA_VERSION, Store, StoreValidationError
from stagemesh.persistence_backends import probe_backend
from stagemesh.postgres_store import (
    POSTGRES_SCHEMA_TABLES,
    PostgresStore,
    PostgresUnavailable,
    postgres_available,
    postgres_declared_tables,
    postgres_schema_contract,
    postgres_schema_statements,
)
from stagemesh.process_identity import classify_process
from stagemesh.provider_acceptance import run_provider_acceptance
from stagemesh.providers import (
    ProviderValidationError,
    RuntimeCommandAdapter,
    adapters_from_commands,
    adapters_from_config,
    approved_default_adapters,
)
from stagemesh.redaction import (
    redact_command_secrets,
    redact_mapping,
    redact_text,
    redact_url_credentials,
)
from stagemesh.registry import (
    GlobalRegistry,
    ProjectRegistration,
    RegistryConflictError,
    RegistryValidationError,
)
from stagemesh.release import ReleaseValidationError, build_release_artifact, release_files
from stagemesh.release_readiness import (
    ReleaseReadinessValidationError,
    release_readiness,
    run_command_check,
    write_release_readiness,
)
from stagemesh.remediation import RemediationPolicy, RemediationValidationError, finding_identity
from stagemesh.retry import RetryRegistry, RetryValidationError, backoff_seconds
from stagemesh.review import Reviewer
from stagemesh.routing import Provider, Router, RoutingMode, RoutingValidationError
from stagemesh.scheduling import Scheduler
from stagemesh.security import SecurityBoundaryError
from stagemesh.task_sources import (
    DiscoveredTask,
    GitHubApiIssueSource,
    GitHubIssueSource,
    GitHubOutboundSync,
    GoogleAxTaskSource,
    LocalBacklogSource,
    OutboundSync,
    TaskSourceValidationError,
    sync_source,
    task_sources_from_config,
)
from stagemesh.work_transport import (
    WorkTransportError,
    import_ack,
    read_ack_envelope,
    write_ack_envelope,
    write_packet_envelope,
)
from stagemesh.workers import WorkerValidationError, heartbeat_worker, register_worker
from stagemesh.workspaces import NO_IMPLEMENTATION_CHANGE


def assert_raises(exc_type, fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except exc_type:
        return
    raise AssertionError(f"expected {exc_type.__name__}")


def run_until_idle(coord: Coordinator, limit: int = 20) -> None:
    for _ in range(limit):
        if coord.tick() == 0:
            return
    raise AssertionError("coordinator did not become idle")


class FakePostgresCursor:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[object, ...] | None]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, statement: str, params: tuple[object, ...] | None = None) -> None:
        self.statements.append((statement, params))

    def fetchone(self) -> tuple[int]:
        return (1,)


class FakePostgresConnection:
    def __init__(self) -> None:
        self.cursor_instance = FakePostgresCursor()
        self.commits = 0
        self.closed = False

    def cursor(self) -> FakePostgresCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.closed = True


class HandoffExecutor(FakeExecutor):
    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        result = super().run(store, task_id, claim_id, project)
        return ExecutionResult(result.status, result.candidate_sha, durable_handoff=True)


WRITE_PROVIDER_OUTPUT = "from pathlib import Path; Path('provider-output.txt').write_text('durable provider output', encoding='utf-8')"


def write_explicit_contract(project: Path, task_id: str, allowed_file: str) -> None:
    contracts = project / ".stagemesh" / "contracts"
    contracts.mkdir(parents=True, exist_ok=True)
    contract = {
        "objective": f"write {allowed_file}",
        "allowed_files": [allowed_file],
        "required_tests": [{"name": "smoke", "command": [sys.executable, "-c", "pass"]}],
    }
    (contracts / f"{task_id}.json").write_text(json.dumps(contract), encoding="utf-8")


def with_store(fn) -> None:
    with tempfile.TemporaryDirectory(prefix="stagemesh-invariant-") as raw:
        tmp = Path(raw)
        store = Store(tmp / "state.sqlite3")
        store.migrate()
        try:
            fn(store, tmp / "project")
        finally:
            store.close()
            shutil.rmtree(tmp / "project" / ".git", ignore_errors=True)


def main() -> int:
    def live_worker_restart(store: Store, project: Path) -> None:
        task_id = store.upsert_task("work")
        store.advance_task(task_id, Stage.IMPLEMENT)
        assert store.acquire_claim(task_id, "worker-a", lease_seconds=60)
        assert Coordinator(store, project).tick() == 0

    def dead_worker_recovers(store: Store, project: Path) -> None:
        task_id = store.upsert_task("work")
        store.advance_task(task_id, Stage.IMPLEMENT)
        assert store.acquire_claim(task_id, "worker-a", lease_seconds=-1)
        assert Coordinator(store, project).tick() == 1
        assert store.get_task(task_id)["stage"] == Stage.VALIDATE

    def implementation_survives_validation(store: Store, project: Path) -> None:
        task_id = store.upsert_task("work")
        coord = Coordinator(store, project)
        coord.tick()
        coord.tick()
        candidate = store.latest_candidate(task_id)
        assert candidate is not None
        Coordinator(store, project).tick()
        assert store.latest_candidate(task_id)["sha"] == candidate["sha"]

    def live_validation_survives(store: Store, project: Path) -> None:
        task_id = store.upsert_task("work")
        sha = "abc123"
        store.add_candidate(task_id, sha, "fake", True)
        store.advance_task(task_id, Stage.VALIDATE)
        store.start_execution(task_id=task_id, claim_id=None, kind=ExecutionKind.VALIDATION, candidate_sha=sha)
        Coordinator(store, project).recover()
        assert next(store.running_executions())["status"] == ExecutionStatus.RUNNING

    def dead_validation_restarts_same_sha(store: Store, project: Path) -> None:
        task_id = store.upsert_task("work")
        sha = "abc123"
        store.add_candidate(task_id, sha, "fake", True)
        store.advance_task(task_id, Stage.VALIDATE)
        execution_id = store.start_execution(task_id=task_id, claim_id=None, kind=ExecutionKind.VALIDATION, candidate_sha=sha)
        store.finish_execution(execution_id, ExecutionStatus.UNKNOWN, sha)
        Coordinator(store, project).tick()
        assert store.has_evidence(task_id, sha, EvidenceKind.VALIDATION)

    def reviewer_capacity_no_reimplementation(store: Store, project: Path) -> None:
        task_id = store.upsert_task("work")
        coord = Coordinator(store, project, reviewer=Reviewer(fail_capacity=True))
        for _ in range(5):
            coord.tick()
        assert store.get_task(task_id)["stage"] == Stage.REVIEW
        assert len(list(store.conn.execute("SELECT * FROM candidates WHERE task_id=?", (task_id,)))) == 1

    def completed_not_redispatched(store: Store, project: Path) -> None:
        task_id = store.upsert_task("work")
        run_until_idle(Coordinator(store, project))
        assert store.get_task(task_id)["stage"] == Stage.DONE
        assert Coordinator(store, project).tick() == 0

    def durable_handoff(store: Store, project: Path) -> None:
        task_id = store.upsert_task("work")
        coord = Coordinator(store, project, executor=HandoffExecutor())
        coord.tick()
        coord.tick()
        assert store.latest_candidate(task_id)["durable_handoff"] == 1

    def persistence_rejects_invalid_core_inputs(store: Store, project: Path) -> None:
        task_id = store.upsert_task("valid", source="local", source_id="valid")
        store.add_candidate(task_id, "abc123", "fake", True)
        store.add_evidence(task_id, "abc123", EvidenceKind.VALIDATION, EvidenceStatus.PASSED, {"ok": True})
        assert_raises(StoreValidationError, store.upsert_task, "")
        assert_raises(StoreValidationError, store.upsert_task, "valid", "")
        assert_raises(StoreValidationError, store.add_dependency, "", task_id)
        assert_raises(StoreValidationError, store.acquire_claim, task_id, "")
        assert_raises(StoreValidationError, store.start_execution, task_id=task_id, claim_id=None, kind="BAD")
        assert_raises(StoreValidationError, store.finish_execution, "", ExecutionStatus.SUCCEEDED)
        assert_raises(StoreValidationError, store.finish_execution, "execution", "BAD")
        assert_raises(StoreValidationError, store.add_candidate, task_id, "", "fake", True)
        assert_raises(StoreValidationError, store.add_candidate, task_id, "abc123", "", True)
        assert_raises(StoreValidationError, store.add_candidate, task_id, "abc123", "fake", "yes")
        assert_raises(StoreValidationError, store.add_evidence, task_id, "abc123", "BAD", EvidenceStatus.PASSED)
        assert_raises(StoreValidationError, store.add_evidence, task_id, "abc123", EvidenceKind.VALIDATION, "BAD")
        assert_raises(StoreValidationError, store.add_evidence, task_id, "abc123", EvidenceKind.VALIDATION, EvidenceStatus.PASSED, [])
        assert_raises(StoreValidationError, store.advance_task, task_id, "BAD")
        assert_raises(StoreValidationError, store.cache_source, "", "1", {}, "OK")
        assert_raises(StoreValidationError, store.cache_source, "source", "", {}, "OK")
        assert_raises(StoreValidationError, store.cache_source, "source", "1", [], "OK")
        assert_raises(StoreValidationError, store.cache_source, "source", "1", {}, "")
        assert_raises(StoreValidationError, store.save_objective, "", "objective", {})
        assert_raises(StoreValidationError, store.save_objective, "objective", "", {})
        assert_raises(StoreValidationError, store.save_objective, "objective", "objective", [])
        assert_raises(
            StoreValidationError,
            store.upsert_worker,
            worker_id="",
            provider="codex",
            capabilities=["code"],
            pid=None,
            process_create_time=None,
            boot_id=None,
            executable=None,
            heartbeat_at=1,
            lease_expires_at=2,
        )
        assert_raises(
            StoreValidationError,
            store.upsert_worker,
            worker_id="worker",
            provider="codex",
            capabilities=["code", "code"],
            pid=None,
            process_create_time=None,
            boot_id=None,
            executable=None,
            heartbeat_at=1,
            lease_expires_at=2,
        )
        assert_raises(StoreValidationError, store.heartbeat_worker, "", 1, 2)
        assert_raises(StoreValidationError, store.add_source_event, "", "1", "outbound", "OK")
        assert_raises(StoreValidationError, store.add_source_event, "source", "", "outbound", "OK")
        assert_raises(StoreValidationError, store.add_source_event, "source", "1", "", "OK")
        assert_raises(StoreValidationError, store.add_source_event, "source", "1", "outbound", "")
        assert_raises(StoreValidationError, store.add_source_event, "source", "1", "outbound", "OK", [])
        assert_raises(StoreValidationError, store.source_events, 0)
        assert_raises(StoreValidationError, store.upsert_finding, "", task_id, "abc123", "HIGH", "message")
        assert_raises(StoreValidationError, store.upsert_finding, "finding", "", "abc123", "HIGH", "message")
        assert_raises(StoreValidationError, store.upsert_finding, "finding", task_id, "", "HIGH", "message")
        assert_raises(StoreValidationError, store.upsert_finding, "finding", task_id, "abc123", "", "message")
        assert_raises(StoreValidationError, store.upsert_finding, "finding", task_id, "abc123", "HIGH", "")
        assert_raises(StoreValidationError, store.get_finding, "")
        assert_raises(StoreValidationError, store.close_finding, "")
        assert_raises(StoreValidationError, store.open_findings_for_candidate, "", "abc123")
        assert_raises(StoreValidationError, store.add_remediation_attempt, "", "attempted")
        assert_raises(StoreValidationError, store.add_remediation_attempt, "finding", "")
        assert_raises(StoreValidationError, store.add_remediation_attempt, "finding", "attempted", [])
        assert_raises(StoreValidationError, store.remediation_attempt_count, "")
        assert_raises(StoreValidationError, store.enqueue_work, "", Stage.PLAN, None, None)
        assert_raises(StoreValidationError, store.enqueue_work, task_id, "BAD", None, None)
        assert_raises(StoreValidationError, store.enqueue_work, task_id, Stage.PLAN, "", None)
        assert_raises(StoreValidationError, store.enqueue_work, task_id, Stage.PLAN, None, "", [])
        assert_raises(StoreValidationError, store.claim_work_packets, "", 1, 1)
        assert_raises(StoreValidationError, store.claim_work_packets, "worker", 0, 1)
        assert_raises(StoreValidationError, store.claim_work_packets, "worker", 1, 0)
        assert_raises(StoreValidationError, store.renew_work_packet, "", "worker")
        assert_raises(StoreValidationError, store.renew_work_packet, "packet", "")
        assert_raises(StoreValidationError, store.ack_work_packet, "", "SUCCEEDED")
        assert_raises(StoreValidationError, store.ack_work_packet, "packet", "")
        assert_raises(StoreValidationError, store.ack_work_packet, "packet", "SUCCEEDED", [])
        assert_raises(StoreValidationError, store.add_audit_event, "", {})
        assert_raises(StoreValidationError, store.add_audit_event, "audit", [])
        assert_raises(StoreValidationError, store.audit_events, 0)
        assert_raises(StoreValidationError, store.get_retry_state, "")
        assert_raises(StoreValidationError, store.upsert_retry_state, "", 1, 10, "reason")
        assert_raises(StoreValidationError, store.upsert_retry_state, "retry", -1, 10, "reason")
        assert_raises(StoreValidationError, store.upsert_retry_state, "retry", 1, 10, "")
        assert_raises(StoreValidationError, store.clear_retry_state, "")
        assert_raises(StoreValidationError, store.add_external_evidence, "", "PASS", "https://example.invalid")
        assert_raises(StoreValidationError, store.add_external_evidence, "hosted-ci", "", "https://example.invalid")
        assert_raises(StoreValidationError, store.add_external_evidence, "hosted-ci", "PASS", "")
        assert_raises(StoreValidationError, store.add_external_evidence, "hosted-ci", "PASS", "https://example.invalid", "")

    def targeted_ops(store: Store, project: Path) -> None:
        first = store.upsert_task("first")
        second = store.upsert_task("second")
        store.advance_task(first, Stage.IMPLEMENT)
        assert store.get_task(second)["stage"] == Stage.PLAN

    def source_semantics(store: Store, project: Path) -> None:
        assert sync_source(store, [DiscoveredTask("github", "1", "deferred", eligible=False)]) == []
        tasks, status = GitHubIssueSource(error="rate-limit").discover()
        assert tasks == []
        assert status == "UNKNOWN"
        tasks, status = GitHubIssueSource(
            [
                {"number": 1, "title": "ready", "labels": []},
                {"number": 2, "title": "deferred", "labels": [{"name": "stagemesh:deferred"}]},
                {"number": 3, "title": "closed", "labels": [], "state": "closed"},
                {"number": 4, "title": "pull", "pull_request": {}, "labels": []},
            ]
        ).discover()
        assert status == "OK"
        assert [task.source_id for task in tasks] == ["1", "2", "3"]
        assert [task.eligible for task in tasks] == [True, False, False]
        assert_raises(TaskSourceValidationError, GitHubIssueSource([{"number": "1", "title": "bad"}]).discover)
        assert_raises(TaskSourceValidationError, GitHubIssueSource([{"number": 1, "title": ""}]).discover)
        assert_raises(TaskSourceValidationError, GitHubIssueSource([{"number": 1, "title": "bad", "labels": "bug"}]).discover)

    def local_backlog_source_rejects_malformed_tasks(store: Store, project: Path) -> None:
        project.mkdir(parents=True)
        backlog = project / "backlog.json"
        backlog.write_text(
            json.dumps(
                {
                    "tasks": [
                        {"id": "one", "title": "one"},
                        {"id": "two", "title": "two", "dependencies": ["one"]},
                    ]
                }
            ),
            encoding="utf-8",
        )
        tasks = LocalBacklogSource(backlog).discover()
        assert [task.source_id for task in tasks] == ["one", "two"]
        assert tasks[1].dependencies == ("one",)
        backlog.write_text("{not-json", encoding="utf-8")
        assert_raises(TaskSourceValidationError, LocalBacklogSource(backlog).discover)
        backlog.write_text('{"tasks":[{"id":"one","title":"one"},{"id":"one","title":"again"}]}', encoding="utf-8")
        assert_raises(TaskSourceValidationError, LocalBacklogSource(backlog).discover)
        backlog.write_text('{"tasks":[{"id":"one","title":"one","dependencies":"two"}]}', encoding="utf-8")
        assert_raises(TaskSourceValidationError, LocalBacklogSource(backlog).discover)
        backlog.write_text('{"tasks":[{"id":"one","title":"one","dependencies":["missing"]}]}', encoding="utf-8")
        assert_raises(TaskSourceValidationError, LocalBacklogSource(backlog).discover)
        backlog.write_text('{"tasks":[{"id":"one","title":"one","eligible":"yes"}]}', encoding="utf-8")
        assert_raises(TaskSourceValidationError, LocalBacklogSource(backlog).discover)
        backlog.write_text('{"tasks":[{"id":"one","title":"one","state":"MAYBE"}]}', encoding="utf-8")
        assert_raises(TaskSourceValidationError, LocalBacklogSource(backlog).discover)

    def configured_json_task_source_syncs_with_distinct_source(store: Store, project: Path) -> None:
        project.mkdir(parents=True)
        config_dir = project / ".stagemesh"
        config_dir.mkdir()
        source_file = project / "linear.json"
        source_file.write_text(
            '{"tasks":[{"id":"L-1","title":"adapter task"},{"id":"L-2","title":"blocked adapter task","dependencies":["L-1"]}]}',
            encoding="utf-8",
        )
        (config_dir / "config.json").write_text(
            '{"task_sources":[{"name":"linear","type":"json","path":"linear.json"}]}',
            encoding="utf-8",
        )
        config = load_config(project)
        sources = task_sources_from_config(config)
        assert [source.name for source in sources] == ["linear"]
        ids = sync_source(store, sources[0].discover())
        assert len(ids) == 2
        rows = store.tasks()
        assert {row["source"] for row in rows} == {"linear"}
        assert {row["source_id"] for row in rows} == {"L-1", "L-2"}

    def worker_heartbeat_and_outbound_sync(store: Store, project: Path) -> None:
        register_worker(
            store,
            "worker-1",
            "codex",
            {"code"},
            ProcessIdentity(pid=1, create_time=2.0, boot_id="boot", executable="codex"),
            lease_seconds=1,
        )
        heartbeat_worker(store, "worker-1", lease_seconds=10)
        assert store.workers()[0]["provider"] == "codex"
        OutboundSync(store).publish("github", "1", "DONE", {"sha": "abc"})
        assert store.source_events()[0]["direction"] == "outbound"

    def worker_registration_rejects_invalid_identity(store: Store, project: Path) -> None:
        identity = ProcessIdentity(pid=1, create_time=2.0, boot_id="boot", executable="codex")
        assert_raises(WorkerValidationError, register_worker, store, "", "codex", {"code"}, identity)
        assert_raises(WorkerValidationError, register_worker, store, "worker", "", {"code"}, identity)
        assert_raises(WorkerValidationError, register_worker, store, "worker", "codex", set(), identity)
        assert_raises(WorkerValidationError, register_worker, store, "worker", "codex", {"code", " code "}, identity)
        assert_raises(WorkerValidationError, register_worker, store, "worker", "codex", {"code"}, identity, "soon")
        assert_raises(WorkerValidationError, register_worker, store, "worker", "codex", {"code"}, identity, 0)
        assert_raises(WorkerValidationError, heartbeat_worker, store, "", 10)
        assert_raises(WorkerValidationError, heartbeat_worker, store, "worker", "soon")
        assert_raises(WorkerValidationError, heartbeat_worker, store, "worker", 0)

    def operator_dashboard_exposes_structured_state(store: Store, project: Path) -> None:
        task_id = store.upsert_task("observe me", source="local", source_id="observe")
        register_worker(
            store,
            "worker-observe",
            "codex",
            {"code"},
            ProcessIdentity(pid=1, create_time=1.0, boot_id="boot", executable="codex"),
            lease_seconds=10,
        )
        OutboundSync(store).publish("github", "42", "UNKNOWN", {"task_id": task_id})
        RetryRegistry(store).record_failure("github:42", "rate-limit", now=100)
        record_external_evidence(store, "hosted-ci", "PASS", "https://example.invalid/ci", "abc1234")
        report = operator_report(store)
        assert "retry_states=1" in report.lines
        assert "external_evidence=1" in report.lines
        assert "blocked_tasks=0" in report.lines
        section_names = {section.name for section in report.sections}
        assert {
            "Stage Summary",
            "Task Status Summary",
            "Attention",
            "Tasks",
            "Workers",
            "Source Events",
            "Retries",
            "External Evidence",
        }.issubset(section_names)
        stage_rows = next(section.rows for section in report.sections if section.name == "Stage Summary")
        assert stage_rows == ({"stage": "PLAN", "count": 1},)
        summary = dashboard_summary(store)
        assert summary["tasks"] == "1"
        assert summary["blocked tasks"] == "0"
        assert summary["workers"] == "1"
        assert summary["external evidence"] == "1"
        dashboard = render_dashboard(store)
        assert "<h2>Status Summary</h2>" in dashboard
        assert "<strong>tasks</strong><span>1</span>" in dashboard
        assert "<strong>blocked tasks</strong><span>0</span>" in dashboard
        assert "<h2>Stage Summary</h2>" in dashboard
        assert "<h2>Task Status Summary</h2>" in dashboard
        assert "<h2>Attention</h2>" in dashboard
        assert "<h2>Tasks</h2>" in dashboard
        assert "<h2>Retries</h2>" in dashboard
        assert "worker-observe" in dashboard
        assert "https://example.invalid/ci" in dashboard

    def health_degrades_on_blocked_tasks_or_failed_executions(store: Store, project: Path) -> None:
        task_id = store.upsert_task("observe failure", source="local", source_id="failure")
        store.conn.execute("UPDATE tasks SET status=? WHERE id=?", (TaskStatus.BLOCKED, task_id))
        store.conn.commit()
        failed_id = store.start_execution(task_id=task_id, claim_id=None, kind=ExecutionKind.VALIDATION)
        unknown_id = store.start_execution(task_id=task_id, claim_id=None, kind=ExecutionKind.REVIEW)
        store.finish_execution(failed_id, ExecutionStatus.FAILED)
        store.finish_execution(unknown_id, ExecutionStatus.UNKNOWN)
        report = health(store)
        assert report.ok is False
        assert report.blocked_task_count == 1
        assert report.failed_execution_count == 1
        assert report.unknown_execution_count == 1

    def dependency_scheduling(store: Store, project: Path) -> None:
        first = store.upsert_task("first", source="local", source_id="first")
        second = store.upsert_task("second", source="local", source_id="second")
        store.add_dependency(second, first)
        scheduler = Scheduler(store)
        assert scheduler.decision(second).eligible is False
        store.advance_task(first, Stage.DONE)
        assert scheduler.decision(second).eligible is True

    def objective_planner_rejects_invalid_dependencies(store: Store, project: Path) -> None:
        planner = ObjectivePlanner()
        valid = planner.parse(
            {
                "id": "obj",
                "title": "valid",
                "tasks": [
                    {"id": "a", "title": "a"},
                    {"id": "b", "title": "b", "dependencies": ["a"]},
                ],
            }
        )
        assert valid.tasks == ("a", "b")
        backlog = project / "objective-backlog.json"
        planner.write_backlog(
            valid,
            {
                "id": "obj",
                "title": "valid",
                "tasks": [
                    {"id": "a", "title": "a"},
                    {"id": "b", "title": "b", "dependencies": ["a"], "eligible": True, "state": "OPEN"},
                ],
            },
            backlog,
        )
        backlog_data = json.loads(backlog.read_text(encoding="utf-8"))
        assert backlog_data["objective"] == "obj"
        assert [task["id"] for task in backlog_data["tasks"]] == ["a", "b"]
        assert_raises(ObjectiveValidationError, planner.parse, "{not-json")
        assert_raises(
            ObjectiveValidationError,
            planner.parse,
            {"id": "blank", "title": "blank", "tasks": [{"id": " ", "title": "bad"}]},
        )
        assert_raises(
            ObjectiveValidationError,
            planner.parse,
            {"id": "blank", "title": "blank", "tasks": [{"id": "a", "title": ""}]},
        )
        assert_raises(
            ObjectiveValidationError,
            planner.parse,
            {"id": "dup", "title": "dup", "tasks": [{"id": "a", "title": "a"}, {"id": "a", "title": "again"}]},
        )
        assert_raises(
            ObjectiveValidationError,
            planner.parse,
            {"id": "missing", "title": "missing", "tasks": [{"id": "a", "title": "a", "dependencies": ["nope"]}]},
        )
        assert_raises(
            ObjectiveValidationError,
            planner.parse,
            {"id": "baddeps", "title": "baddeps", "tasks": [{"id": "a", "title": "a", "dependencies": "nope"}]},
        )
        assert_raises(
            ObjectiveValidationError,
            planner.parse,
            {"id": "baddep", "title": "baddep", "tasks": [{"id": "a", "title": "a", "dependencies": [1]}]},
        )
        assert_raises(ObjectiveValidationError, planner.write_backlog, valid, [], backlog)
        assert_raises(ObjectiveValidationError, planner.write_backlog, valid, {"tasks": [{"id": "a", "title": "a"}]}, backlog)
        assert_raises(
            ObjectiveValidationError,
            planner.write_backlog,
            valid,
            {"tasks": [{"id": "a", "title": "a"}, {"id": "b", "title": "b", "dependencies": "a"}]},
            backlog,
        )
        assert_raises(
            ObjectiveValidationError,
            planner.write_backlog,
            valid,
            {"tasks": [{"id": "a", "title": "a"}, {"id": "b", "title": "b", "eligible": "yes"}]},
            backlog,
        )

    def global_registry_rejects_ambiguous_projects(store: Store, project: Path) -> None:
        registry = GlobalRegistry(project.parent / "registry.json")
        first = project / "first"
        second = project / "second"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        registry.register(ProjectRegistration("first", first, first / ".stagemesh" / "stagemesh.sqlite3"))
        registry.register(ProjectRegistration("first", first, first / ".stagemesh" / "stagemesh.sqlite3"))
        projects = registry.load()
        assert len(projects) == 1
        assert projects[0].path == first.resolve()
        assert projects[0].db_path == (first / ".stagemesh" / "stagemesh.sqlite3").resolve()
        assert_raises(
            RegistryConflictError,
            registry.register,
            ProjectRegistration("first", second, second / ".stagemesh" / "stagemesh.sqlite3"),
        )
        assert_raises(
            RegistryConflictError,
            registry.register,
            ProjectRegistration("second", first, first / ".stagemesh" / "stagemesh.sqlite3"),
        )
        malformed = project.parent / "malformed-registry.json"
        malformed.write_text("{not-json", encoding="utf-8")
        assert_raises(RegistryValidationError, GlobalRegistry(malformed).load)
        malformed.write_text("[]", encoding="utf-8")
        assert_raises(RegistryValidationError, GlobalRegistry(malformed).load)
        malformed.write_text('{"projects":{}}', encoding="utf-8")
        assert_raises(RegistryValidationError, GlobalRegistry(malformed).load)
        malformed.write_text('{"projects":[[]]}', encoding="utf-8")
        assert_raises(RegistryValidationError, GlobalRegistry(malformed).load)
        malformed.write_text('{"projects":[{"name":"","path":"x","db_path":"db"}]}', encoding="utf-8")
        assert_raises(RegistryValidationError, GlobalRegistry(malformed).load)
        malformed.write_text(
            json.dumps(
                {
                    "projects": [
                        {"name": "dup", "path": str(first), "db_path": str(first / ".stagemesh" / "db.sqlite3")},
                        {"name": "dup", "path": str(second), "db_path": str(second / ".stagemesh" / "db.sqlite3")},
                    ]
                }
            ),
            encoding="utf-8",
        )
        assert_raises(RegistryValidationError, GlobalRegistry(malformed).load)
        malformed.write_text(
            json.dumps(
                {
                    "projects": [
                        {"name": "first", "path": str(first), "db_path": str(first / ".stagemesh" / "db.sqlite3")},
                        {"name": "second", "path": str(first), "db_path": str(first / ".stagemesh" / "other.sqlite3")},
                    ]
                }
            ),
            encoding="utf-8",
        )
        assert_raises(RegistryValidationError, GlobalRegistry(malformed).load)
        malformed.write_text(
            json.dumps(
                {
                    "projects": [
                        {"name": "escape", "path": str(first), "db_path": str(project.parent / "escape.sqlite3")},
                    ]
                }
            ),
            encoding="utf-8",
        )
        assert_raises(RegistryValidationError, GlobalRegistry(malformed).load)
        assert_raises(RegistryValidationError, registry.register, ProjectRegistration("", first, first / ".stagemesh" / "db.sqlite3"))
        assert_raises(
            RegistryValidationError,
            registry.register,
            ProjectRegistration("escape", first, project.parent / "escape.sqlite3"),
        )

    def finding_convergence_is_bounded(store: Store, project: Path) -> None:
        task_id = store.upsert_task("review task")
        sha = "review-sha"
        finding_id = finding_identity(sha, "bug")
        store.upsert_finding(finding_id, task_id, sha, "P1", "bug")
        policy = RemediationPolicy(max_attempts=2)
        assert policy.should_remediate(store, finding_id) is True
        policy.record_attempt(store, finding_id, "FAILED")
        assert policy.should_remediate(store, finding_id) is True
        policy.record_attempt(store, finding_id, "FAILED")
        assert policy.should_remediate(store, finding_id) is False
        store.close_finding(finding_id)
        assert policy.should_remediate(store, finding_id) is False
        assert_raises(RemediationValidationError, RemediationPolicy, 0)
        assert_raises(RemediationValidationError, RemediationPolicy, "three")
        assert_raises(RemediationValidationError, finding_identity, "", "bug")
        assert_raises(RemediationValidationError, finding_identity, sha, "")
        assert_raises(RemediationValidationError, finding_identity, sha, "bug", "")

    def distributed_work_packets_are_claimed_once(store: Store, project: Path) -> None:
        task_id = store.upsert_task("distributed")
        queue = WorkQueue(store)
        packet_id = queue.enqueue(task_id, "IMPLEMENT")
        queued = queue.list()
        assert [packet.id for packet in queued] == [packet_id]
        assert queued[0].status == "QUEUED"
        assert queued[0].worker_id is None
        first = queue.poll("worker-a")
        second = queue.poll("worker-b")
        assert [packet.id for packet in first] == [packet_id]
        assert second == []
        claimed = queue.list()
        assert claimed[0].status == "CLAIMED"
        assert claimed[0].worker_id == "worker-a"
        queue.ack(packet_id, "SUCCEEDED", {"candidate_sha": "abc"})
        done = queue.list()
        assert done[0].status == "SUCCEEDED"
        assert done[0].payload == {"candidate_sha": "abc"}

    def distributed_work_ack_requires_claimed_terminal_status(store: Store, project: Path) -> None:
        task_id = store.upsert_task("distributed ack")
        queue = WorkQueue(store)
        unclaimed = queue.enqueue(task_id, "VALIDATE")
        assert_raises(WorkQueueError, queue.ack, unclaimed, "SUCCEEDED")
        claimed = queue.poll("worker-a")
        assert [packet.id for packet in claimed] == [unclaimed]
        assert_raises(WorkQueueError, queue.ack, unclaimed, "BOGUS")
        queue.ack(unclaimed, "failed", {"reason": "test"})
        row = store.conn.execute("SELECT * FROM work_packets WHERE id=?", (unclaimed,)).fetchone()
        assert row["status"] == "FAILED"
        assert_raises(WorkQueueError, queue.ack, unclaimed, "SUCCEEDED")

    def distributed_work_rejects_invalid_queue_inputs(store: Store, project: Path) -> None:
        task_id = store.upsert_task("distributed validation")
        queue = WorkQueue(store)
        assert_raises(WorkQueueError, queue.enqueue, "", "VALIDATE")
        assert_raises(WorkQueueError, queue.enqueue, task_id, "BOGUS")
        assert_raises(WorkQueueError, queue.enqueue, task_id, "VALIDATE", "")
        assert_raises(WorkQueueError, queue.poll, "")
        assert_raises(WorkQueueError, queue.poll, "worker", "one")
        assert_raises(WorkQueueError, queue.poll, "worker", 0)
        assert_raises(WorkQueueError, queue.poll, "worker", 101)
        assert_raises(WorkQueueError, queue.poll, "worker", 1, "soon")
        assert_raises(WorkQueueError, queue.poll, "worker", 1, 0)
        assert_raises(WorkQueueError, queue.renew, "", "worker")
        assert_raises(WorkQueueError, queue.renew, "packet", "")

    def distributed_work_packets_have_renewable_leases(store: Store, project: Path) -> None:
        task_id = store.upsert_task("leased distributed")
        queue = WorkQueue(store)
        packet_id = queue.enqueue(task_id, "VALIDATE")
        assert [packet.id for packet in queue.poll("worker-a", lease_seconds=60)] == [packet_id]
        assert queue.poll("worker-b", lease_seconds=60) == []
        assert queue.renew(packet_id, "worker-b") is False
        assert queue.renew(packet_id, "worker-a") is True
        store.conn.execute(
            "UPDATE work_packets SET updated_at=? WHERE id=?",
            (0, packet_id),
        )
        store.conn.commit()
        reclaimed = queue.poll("worker-b", lease_seconds=1)
        assert [packet.id for packet in reclaimed] == [packet_id]

    def distributed_work_transport_round_trips_ack(store: Store, project: Path) -> None:
        project.mkdir(parents=True)
        task_id = store.upsert_task("transported distributed")
        queue = WorkQueue(store)
        packet_id = queue.enqueue(task_id, "REVIEW", candidate_sha="abc123")
        assert [packet.id for packet in queue.poll("worker-a")] == [packet_id]
        packet_path = project / "packet.json"
        packet = queue.export(packet_id)
        packet_envelope = write_packet_envelope(packet, packet_path)
        assert packet_envelope["kind"] == "stagemesh.work_packet"
        assert packet_envelope["packet"]["status"] == "CLAIMED"
        assert json.loads(packet_path.read_text(encoding="utf-8"))["packet"]["candidate_sha"] == "abc123"
        ack_path = project / "ack.json"
        write_ack_envelope(packet_id, "succeeded", ack_path, {"review": "passed"})
        ack = import_ack(queue, ack_path)
        assert ack.status == "SUCCEEDED"
        done = queue.export(packet_id)
        assert done.status == "SUCCEEDED"
        assert done.payload == {"review": "passed"}
        invalid = project / "invalid-ack.json"
        invalid.write_text('{"version":1,"kind":"stagemesh.work_packet"}', encoding="utf-8")
        assert_raises(WorkTransportError, read_ack_envelope, invalid)
        assert_raises(WorkQueueError, queue.export, "missing")

    def ci_wait_releases_worker_capacity_while_pending(store: Store, project: Path) -> None:
        pending = decide_ci_wait("pending", elapsed_seconds=30)
        assert pending.should_wait is True
        assert pending.release_worker is True
        assert pending.poll_after_seconds >= 5
        assert pending.reason == "ci pending"
        passed = decide_ci_wait("success", elapsed_seconds=30)
        assert passed.should_wait is False
        assert passed.release_worker is False
        failed = decide_ci_wait("failed", elapsed_seconds=30)
        assert failed.should_wait is False
        assert failed.release_worker is False
        timed_out = decide_ci_wait("pending", elapsed_seconds=1800, max_seconds=1800)
        assert timed_out.should_wait is False
        assert timed_out.release_worker is True

    def acceptance_and_ci_gates_validate_inputs(store: Store, project: Path) -> None:
        project.mkdir(parents=True)
        assert_raises(AcceptanceValidationError, run_check, "", [sys.executable, "--version"], project)
        assert_raises(AcceptanceValidationError, run_check, "python", [], project)
        assert_raises(AcceptanceValidationError, run_check, "python", [sys.executable, ""], project)
        assert_raises(AcceptanceValidationError, run_check, "python", [sys.executable], project / "missing")
        assert_raises(AcceptanceValidationError, local_acceptance_report, project / "missing", False)
        gaps = proof_gaps([AcceptanceCheck("live", "PASS", "provider:codex:execution: NOT_PROVEN\n")])
        assert gaps == [{"check": "live", "evidence": "provider:codex:execution: NOT_PROVEN"}]
        structured_gaps = proof_gaps(
            [
                AcceptanceCheck(
                    "live",
                    "PASS",
                    '{"status":"PASS","checks":[{"name":"github","status":"NOT_CONFIGURED"},{"name":"provider:codex","status":"AVAILABLE"},{"name":"provider:codex:execution","status":"NOT_PROVEN"}]}',
                )
            ]
        )
        assert structured_gaps == [
            {"check": "live", "evidence": "github: NOT_CONFIGURED"},
            {"check": "live", "evidence": "provider:codex:execution: NOT_PROVEN"},
        ]
        assert_raises(SecurityBoundaryError, write_acceptance_report, project, project.parent / "acceptance.json", False)
        assert_raises(CIValidationError, run_gate, "", [sys.executable, "--version"], project)
        assert_raises(CIValidationError, run_gate, "python", [], project)
        assert_raises(CIValidationError, run_gate, "python", [sys.executable, ""], project)
        assert_raises(CIValidationError, run_gate, "python", [sys.executable], project / "missing")
        assert_raises(CIValidationError, default_gates, project / "missing", False)
        assert_raises(CIValidationError, broken_future_feature_gate, project / "missing")
        gate_names = [name for name, _ in default_gate_commands(include_acceptance=False)]
        assert "provider_acceptance" in gate_names
        assert "github_acceptance" in gate_names
        assert "live_acceptance" in gate_names
        assert "acceptance" not in gate_names

    def capacity_registry_rejects_invalid_provider_state(store: Store, project: Path) -> None:
        registry = CapacityRegistry()
        registry.record("primary", CapacityKind.CAPACITY, retry_after_seconds=30)
        registry.record("secondary", CapacityKind.AVAILABLE)
        assert registry.choose_primary_secondary("primary", "secondary") == "secondary"
        assert registry.get("missing").kind == CapacityKind.UNKNOWN
        snapshot = registry.snapshot(("primary", "secondary", "missing"))
        assert [item["provider"] for item in snapshot] == ["primary", "secondary", "missing"]
        assert snapshot[0]["usable"] is False
        assert snapshot[1]["usable"] is True
        assert snapshot[2]["kind"] == CapacityKind.UNKNOWN
        assert_raises(CapacityValidationError, registry.record, "", CapacityKind.AVAILABLE)
        assert_raises(CapacityValidationError, registry.record, "provider", "BOGUS")
        assert_raises(CapacityValidationError, registry.record, "provider", CapacityKind.CAPACITY, -1)

    def github_outbound_sync_records_capacity_separately(store: Store, project: Path) -> None:
        class RateLimitedTransport:
            def request(self, method, path, body=None):
                return 403, {"Retry-After": "120"}, {"message": "rate limit"}

        client = GitHubClient("owner", "repo", RateLimitedTransport())
        event_id = GitHubOutboundSync(store, client).publish_done("1", "abc")
        event = store.source_events()[0]
        assert event["id"] == event_id
        assert event["status"] == "UNKNOWN"

    def github_retry_after_parsing_is_defensive(store: Store, project: Path) -> None:
        class InvalidRetryAfterTransport:
            def request(self, method, path, body=None):
                return 429, {"Retry-After": "not-a-number"}, {"message": "rate limit"}

        class MalformedIssuesTransport:
            def request(self, method, path, body=None):
                return 200, {}, {"message": "not a list"}

        class InvalidJsonResponse:
            status = 200
            headers: ClassVar[dict[str, str]] = {}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def read(self):
                return b"{not-json"

        assert parse_retry_after(None) == 60
        assert parse_retry_after("") == 60
        assert parse_retry_after("not-a-number") == 60
        assert parse_retry_after("-1") == 60
        assert parse_retry_after("15") == 15
        result = GitHubClient("owner", "repo", InvalidRetryAfterTransport()).list_open_issues()
        assert result.status == "UNKNOWN"
        assert result.retry_after == 60
        result = GitHubClient("owner", "repo", MalformedIssuesTransport()).list_open_issues()
        assert result.status == "UNKNOWN"
        original_urlopen = urllib.request.urlopen
        urllib.request.urlopen = lambda request, timeout=20: InvalidJsonResponse()
        try:
            discovered, status, retry_after = GitHubApiIssueSource("owner", "repo").discover()
            assert discovered == []
            assert status == "UNKNOWN"
            assert retry_after is None
        finally:
            urllib.request.urlopen = original_urlopen

    def git_attribution_is_worker_owned(store: Store, project: Path) -> None:
        attribution = attribution_for_worker("worker 1", "codex")
        assert attribution.author_email == "codex+worker-1@stagemesh.invalid"
        assert attribution.committer_email == "stagemesh@stagemesh.invalid"
        spaced = attribution_for_worker(" worker\t1\n", " codex ")
        assert spaced.author_name == "StageMesh codex worker worker 1"
        assert spaced.author_email == "codex+worker-1@stagemesh.invalid"
        assert_raises(AttributionValidationError, attribution_for_worker, "", "codex")
        assert_raises(AttributionValidationError, attribution_for_worker, "worker", "")
        assert_raises(AttributionValidationError, attribution_for_worker, "!!!", "codex")
        assert_raises(AttributionValidationError, attribution_for_worker, "worker", "!")

    def git_workspace_rejects_unsafe_inputs(store: Store, project: Path) -> None:
        workspace = GitWorkspace(project)
        assert workspace.path == project.resolve()
        workspace.init_if_needed()
        (project / "proof.txt").write_text("hello\n", encoding="utf-8")
        sha = workspace.commit_all("record proof", attribution_for_worker("worker 1", "codex"))
        assert len(sha) == 40
        assert workspace.head() == sha
        assert_raises(GitValidationError, workspace.run)
        assert_raises(GitValidationError, workspace.run, "")
        assert_raises(GitValidationError, workspace.run, "status", " ")
        assert_raises(GitValidationError, workspace.run, "status", env={"": "value"})
        assert_raises(GitValidationError, workspace.run, "status", env={"KEY": ""})
        assert_raises(GitValidationError, workspace.commit_all, "")
        assert_raises(GitValidationError, workspace.create_worktree, project, "HEAD")
        assert_raises(GitValidationError, workspace.create_worktree, project / "nested", "HEAD")
        assert_raises(GitValidationError, workspace.create_worktree, project.parent, "HEAD")
        assert_raises(GitValidationError, workspace.create_worktree, project.parent / "other", "")

    def secrets_are_redacted(store: Store, project: Path) -> None:
        redacted = redact_mapping(
            {
                "github_token": "abc",
                "nested": {"password": "def"},
                "events": [{"authorization": "Bearer ghi"}, {"safe": "ok"}],
                "safe": "ok",
            }
        )
        assert redacted["github_token"] == "***REDACTED***"
        assert redacted["nested"] == {"password": "***REDACTED***"}
        assert redacted["events"] == [{"authorization": "***REDACTED***"}, {"safe": "ok"}]
        assert redact_text("token abc", ["abc"]) == "token ***REDACTED***"
        assert (
            redact_url_credentials("postgresql://user:secret@example.invalid:5432/db?sslmode=require")
            == "postgresql://***REDACTED***@example.invalid:5432/db?sslmode=require"
        )
        assert redact_url_credentials("postgresql://example.invalid/db") == "postgresql://example.invalid/db"
        assert (
            redact_command_secrets("runner --api-key abc --token=def --safe ok")
            == "runner --api-key '***REDACTED***' '--token=***REDACTED***' --safe ok"
        )
        assert redact_command_secrets('"runner --token abc') == "***REDACTED***"

    def config_loads_from_project_file(store: Store, project: Path) -> None:
        config_dir = project / ".stagemesh"
        config_dir.mkdir(parents=True)
        (config_dir / "config.json").write_text(
            '{"github":{"owner":"o","repo":"r","token":"t"},"providers":{"codex":"codex --test"},"routing":{"mode":"SINGLE_AGENT","single_agent_provider":"codex","stage_routes":{"REVIEW":"claude"}}}',
            encoding="utf-8",
        )
        config = load_config(project)
        assert config.github.configured is True
        assert config.provider_commands["codex"] == "codex --test"
        assert config.task_sources == ()
        assert config.routing_mode == "SINGLE_AGENT"
        assert config.single_agent_provider == "codex"
        assert config.stage_routes["REVIEW"] == "claude"

    def config_supports_google_ax_export_source(store: Store, project: Path) -> None:
        project.mkdir(parents=True)
        (project / "google-ax.json").write_text(
            json.dumps({"tasks": [{"id": "ax-1", "title": "Google AX exported task"}]}),
            encoding="utf-8",
        )
        config_dir = project / ".stagemesh"
        config_dir.mkdir(parents=True)
        (config_dir / "config.json").write_text(
            json.dumps(
                {
                    "task_sources": [
                        {
                            "name": "google-ax",
                            "type": "google-ax",
                            "path": "google-ax.json",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        config = load_config(project)
        sources = task_sources_from_config(config)

        assert len(sources) == 1
        assert isinstance(sources[0], GoogleAxTaskSource)
        tasks = sources[0].discover()
        assert len(tasks) == 1
        assert tasks[0].source == "google-ax"
        assert tasks[0].title == "Google AX exported task"

    def config_rejects_invalid_routing_and_provider_shapes(store: Store, project: Path) -> None:
        config_dir = project / ".stagemesh"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "config.json"
        config_file.write_text("{not-json", encoding="utf-8")
        assert_raises(ConfigValidationError, load_config, project)
        config_file.write_text('{"routing":{"mode":"ROUND_ROBIN"}}', encoding="utf-8")
        assert_raises(ConfigValidationError, load_config, project)
        config_file.write_text('{"routing":{"stage_routes":{"BOGUS":"codex"}}}', encoding="utf-8")
        assert_raises(ConfigValidationError, load_config, project)
        config_file.write_text('{"providers":{"codex":""}}', encoding="utf-8")
        assert_raises(ConfigValidationError, load_config, project)
        config_file.write_text('{"providers":[]}', encoding="utf-8")
        assert_raises(ConfigValidationError, load_config, project)
        config_file.write_text('{"task_sources":{}}', encoding="utf-8")
        assert_raises(ConfigValidationError, load_config, project)
        config_file.write_text('{"task_sources":[{"name":"linear","type":"api","path":"tasks.json"}]}', encoding="utf-8")
        assert_raises(ConfigValidationError, load_config, project)
        config_file.write_text('{"task_sources":[{"name":"linear","type":"json","path":""}]}', encoding="utf-8")
        assert_raises(ConfigValidationError, load_config, project)
        config_file.write_text(
            json.dumps({"task_sources": [{"name": "linear", "type": "json", "path": str(project.parent / "outside.json")}]}),
            encoding="utf-8",
        )
        assert_raises(ConfigValidationError, load_config, project)

    def github_remote_detection_supports_zero_config(store: Store, project: Path) -> None:
        assert parse_github_remote("https://github.com/openai/stagemesh.git").owner == "openai"
        assert parse_github_remote("git@github.com:openai/stagemesh.git").repo == "stagemesh"
        assert parse_github_remote("ssh://git@github.com/openai/stagemesh.git").repo == "stagemesh"
        assert parse_github_remote("https://example.invalid/openai/stagemesh.git") is None
        project.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init"], cwd=project, text=True, capture_output=True, check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "git@github.com:stage/mesh.git"],
            cwd=project,
            text=True,
            capture_output=True,
            check=True,
        )
        config = load_config(project)
        assert config.github.owner == "stage"
        assert config.github.repo == "mesh"
        assert config.github.configured is False

    def routing_modes_select_expected_provider(store: Store, project: Path) -> None:
        providers = [
            Provider("codex", frozenset({"code", "review"}), True, priority=2),
            Provider("claude", frozenset({"review"}), True, priority=1),
        ]
        staged = Router(providers, mode=RoutingMode.STAGED, stage_routes={"REVIEW": "claude"})
        assert staged.choose_for_stage(Stage.REVIEW, "review").name == "claude"
        single = Router(providers, mode=RoutingMode.SINGLE_AGENT, single_agent_provider="codex")
        assert single.choose_for_stage(Stage.REVIEW, "review").name == "codex"
        down = Router(providers, mode=RoutingMode.SINGLE_AGENT, single_agent_provider="missing")
        assert down.choose_for_stage(Stage.REVIEW, "review") is None
        assert_raises(RoutingValidationError, Router, [Provider("", frozenset({"code"}))])
        assert_raises(RoutingValidationError, Router, [Provider("codex", frozenset())])
        assert_raises(
            RoutingValidationError,
            Router,
            [Provider("codex", frozenset({"code"})), Provider("codex", frozenset({"review"}))],
        )
        assert_raises(RoutingValidationError, Router, providers, "ROUND_ROBIN")
        assert_raises(RoutingValidationError, Router, providers, RoutingMode.STAGED, {"BOGUS": "codex"})
        assert_raises(RoutingValidationError, staged.choose, "")

    def release_output_stays_inside_workspace(store: Store, project: Path) -> None:
        project.mkdir(parents=True, exist_ok=True)
        assert_raises(
            SecurityBoundaryError,
            build_release_artifact,
            project,
            project.parent / "outside",
            "0.1.0",
            "abc1234",
        )

    def demo_project_is_workspace_bound_and_runnable_shape(store: Store, project: Path) -> None:
        project.mkdir(parents=True, exist_ok=True)
        demo = create_demo_project(project, project / ".stagemesh" / "demo")
        assert demo.objective.exists()
        payload = json.loads(demo.objective.read_text(encoding="utf-8"))
        assert [task["id"] for task in payload["tasks"]] == ["demo-plan", "demo-validate"]
        assert payload["tasks"][1]["dependencies"] == ["demo-plan"]
        assert "status --json" in demo.readme.read_text(encoding="utf-8")
        assert_raises(SecurityBoundaryError, create_demo_project, project, project.parent / "demo")
        assert_raises(DemoValidationError, create_demo_project, project, demo.root)

    def release_artifact_rejects_unsafe_metadata(store: Store, project: Path) -> None:
        project.mkdir(parents=True, exist_ok=True)
        assert_raises(
            ReleaseValidationError,
            build_release_artifact,
            ROOT,
            ROOT / ".stagemesh" / "bad-release",
            "0.1.0",
            "../escape",
        )
        assert_raises(
            ReleaseValidationError,
            build_release_artifact,
            ROOT,
            ROOT / ".stagemesh" / "bad-release",
            "../version",
            "abcdef1",
        )

    def release_artifact_contains_tracked_source_manifest(store: Store, project: Path) -> None:
        artifact = build_release_artifact(ROOT, ROOT / ".stagemesh" / "invariant-release", "0.1.0", "abc12345")
        manifest = json.loads(artifact.manifest.read_text(encoding="utf-8"))
        checksums = artifact.checksums.read_text(encoding="utf-8").splitlines()
        paths = {entry["path"] for entry in manifest["files"]}
        assert manifest["file_count"] == len(manifest["files"])
        assert "pyproject.toml" in paths
        assert "src/stagemesh/cli.py" in paths
        assert "scripts/clean_acceptance.py" in paths
        assert all(".stagemesh" not in Path(path).parts for path in paths)
        assert all(".tmp-install" not in Path(path).parts for path in paths)
        assert all(entry["sha256"] for entry in manifest["files"])
        with zipfile.ZipFile(artifact.archive) as archive:
            names = set(archive.namelist())
        assert "pyproject.toml" in names
        assert "stagemesh-release-manifest.json" in names
        assert not any(name.startswith(".stagemesh/") for name in names)
        assert len(checksums) == 2
        assert any(line.endswith(f"  {artifact.archive.name}") for line in checksums)
        assert any(line.endswith("  stagemesh-release-manifest.json") for line in checksums)

    def release_files_reject_symlink_escape(store: Store, project: Path) -> None:
        if os.name == "nt":
            return
        project.mkdir(parents=True, exist_ok=True)
        outside = project.parent / "outside-secret.txt"
        outside.write_text("secret", encoding="utf-8")
        inside = project / "inside.txt"
        inside.write_text("inside", encoding="utf-8")
        link = project / "linked-secret.txt"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            return
        subprocess.run(["git", "init"], cwd=project, text=True, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=project, text=True, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "StageMesh Test"], cwd=project, text=True, capture_output=True, check=True)
        subprocess.run(["git", "add", "-A"], cwd=project, text=True, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "release files"], cwd=project, text=True, capture_output=True, check=True)
        files = {path.name for path in release_files(project)}
        assert "inside.txt" in files
        assert "linked-secret.txt" not in files

    def migrations_are_idempotent(store: Store, project: Path) -> None:
        first = store.schema_version()
        store.migrate()
        second = store.schema_version()
        assert first == second == SCHEMA_VERSION

    def backend_probe_reports_postgres_dependency(store: Store, project: Path) -> None:
        sqlite_probe = probe_backend(None, project / ".stagemesh" / "stagemesh.sqlite3")
        assert sqlite_probe.name == "sqlite"
        probe = probe_backend("postgresql://example/db", project / ".stagemesh" / "stagemesh.sqlite3")
        assert probe.name == "postgres"
        assert probe.available in {True, False}
        assert probe.reason
        unknown = probe_backend("mysql://example/db", project / ".stagemesh" / "stagemesh.sqlite3")
        assert unknown.name == "unknown"
        if not postgres_available():
            assert_raises(PostgresUnavailable, PostgresStore, "postgresql://example/db")

    def backend_command_can_apply_postgres_migrations(store: Store, project: Path) -> None:
        project.mkdir(parents=True)
        config_file = project / "postgres-config.json"
        config_file.write_text('{"database_url":"postgresql://user:secret@example/db"}', encoding="utf-8")

        class FakeCommandPostgresStore:
            instances: ClassVar[list[FakeCommandPostgresStore]] = []

            def __init__(self, dsn: str) -> None:
                self.dsn = dsn
                self.migrations = 0
                self.pings = 0
                self.closed = False
                self.instances.append(self)

            def migrate(self) -> None:
                self.migrations += 1

            def ping(self) -> bool:
                self.pings += 1
                return True

            def close(self) -> None:
                self.closed = True

        original_store = cli_module.PostgresStore
        cli_module.PostgresStore = FakeCommandPostgresStore
        try:
            stdout = io.StringIO()
            args = argparse.Namespace(
                project=str(project),
                config=str(config_file),
                ping=True,
                migrate=True,
                json=True,
            )
            with contextlib.redirect_stdout(stdout):
                assert cli_module.command_backend(args) == 0
        finally:
            cli_module.PostgresStore = original_store

        data = json.loads(stdout.getvalue())
        assert data["name"] == "postgres"
        assert data["database_url"] == "postgresql://***REDACTED***@example/db"
        assert data["ping"] is True
        assert data["migration_applied"] is True
        assert data["postgres_schema_contract"]["table_count"] == len(POSTGRES_SCHEMA_TABLES)
        instance = FakeCommandPostgresStore.instances[0]
        assert instance.dsn == "postgresql://user:secret@example/db"
        assert instance.migrations == 1
        assert instance.pings == 1
        assert instance.closed is True

    def postgres_schema_contract_covers_authoritative_tables(store: Store, project: Path) -> None:
        contract = postgres_schema_contract()
        assert contract["dialect"] == "postgresql"
        assert tuple(contract["tables"]) == POSTGRES_SCHEMA_TABLES
        assert tuple(contract["declared_tables"]) == POSTGRES_SCHEMA_TABLES
        assert tuple(postgres_declared_tables()) == POSTGRES_SCHEMA_TABLES
        schema_sql = str(contract["schema_sql"])
        for table in POSTGRES_SCHEMA_TABLES:
            assert f"CREATE TABLE IF NOT EXISTS {table}" in schema_sql
        statements = postgres_schema_statements()
        assert len(statements) >= len(POSTGRES_SCHEMA_TABLES)
        assert all(statement for statement in statements)
        assert all(not statement.endswith(";") for statement in statements)
        fake_conn = FakePostgresConnection()
        pg_store = object.__new__(PostgresStore)
        pg_store.conn = fake_conn
        pg_store.migrate()
        executed = [statement for statement, _ in fake_conn.cursor_instance.statements]
        assert any("CREATE TABLE IF NOT EXISTS tasks" in statement for statement in executed)
        assert any("CREATE UNIQUE INDEX IF NOT EXISTS one_active_claim" in statement for statement in executed)
        assert any("INSERT INTO schema_migrations" in statement for statement in executed)
        assert fake_conn.commits == 1

    def completion_audit_is_not_falsely_complete(store: Store, project: Path) -> None:
        audit = completion_audit()
        assert audit["complete"] is False
        statuses = {item["status"] for item in audit["items"]}
        assert "REQUIRES_CREDENTIALS" in statuses or "MISSING_EXTERNAL_EVIDENCE" in statuses
        project.mkdir(parents=True, exist_ok=True)
        output = project / ".stagemesh" / "completion-audit.json"
        write_completion_audit(output, store, root=project)
        assert output.exists()
        assert_raises(SecurityBoundaryError, write_completion_audit, project.parent / "completion-audit.json", store, project)
        assert_raises(CompletionAuditValidationError, write_completion_audit, Path(""), store)

    def audit_events_are_redacted_and_exportable(store: Store, project: Path) -> None:
        record_audit(store, "secret.test", {"token": "abc", "events": [{"password": "def"}], "safe": "ok"})
        record_audit(store, "secret.second", {"safe": "later"})
        output = project / "audit.jsonl"
        export_audit_jsonl(store, output)
        text = output.read_text(encoding="utf-8")
        exported = [json.loads(line) for line in text.strip().splitlines()]
        secret = next(event for event in exported if event["event_type"] == "secret.test")
        assert secret["payload"]["token"] == "***REDACTED***"
        assert secret["payload"]["events"] == [{"password": "***REDACTED***"}]
        assert secret["payload"]["safe"] == "ok"
        limited = project / "audit-limited.jsonl"
        export_audit_jsonl(store, limited, limit=1)
        text = limited.read_text(encoding="utf-8")
        lines = text.strip().splitlines()
        assert len(lines) == 1
        assert "secret.second" in lines[0]
        rooted = project / ".stagemesh" / "audit-rooted.jsonl"
        export_audit_jsonl(store, rooted, root=project)
        assert rooted.exists()
        assert_raises(AuditValidationError, record_audit, store, "", {"safe": "ok"})
        assert_raises(AuditValidationError, record_audit, store, "bad.payload", [])
        assert_raises(AuditValidationError, export_audit_jsonl, store, output, 0)
        assert_raises(AuditValidationError, export_audit_jsonl, store, output, "10")
        assert_raises(AuditValidationError, export_audit_jsonl, store, output, 10001)
        assert_raises(AuditValidationError, export_audit_jsonl, store, Path(""))
        assert_raises(SecurityBoundaryError, export_audit_jsonl, store, project.parent / "audit.jsonl", 500, project)

    def retry_backoff_is_durable_and_clearable(store: Store, project: Path) -> None:
        retries = RetryRegistry(store)
        first = retries.record_failure("github:1", "rate-limit", now=100)
        second = retries.record_failure("github:1", "rate-limit", now=100)
        assert first.attempts == 1
        assert second.attempts == 2
        assert retries.decision("github:1", now=101).allowed is False
        retries.record_success("github:1")
        assert retries.decision("github:1", now=101).allowed is True
        assert_raises(RetryValidationError, retries.record_failure, "", "rate-limit")
        assert_raises(RetryValidationError, retries.record_failure, "github:2", "")
        assert_raises(RetryValidationError, retries.record_success, "")
        assert_raises(RetryValidationError, backoff_seconds, "one")
        assert_raises(RetryValidationError, backoff_seconds, 0)
        assert_raises(RetryValidationError, backoff_seconds, 1, "fast")
        assert_raises(RetryValidationError, backoff_seconds, 1, 0)

    def workspace_boundary_rejects_outside_outputs(store: Store, project: Path) -> None:
        project.mkdir(parents=True, exist_ok=True)
        boundary = __import__("stagemesh.security", fromlist=["WorkspaceBoundary"]).WorkspaceBoundary(project)
        assert_raises(SecurityBoundaryError, boundary.require_inside, project.parent / "outside.txt")

    def final_report_mentions_missing_evidence(store: Store, project: Path) -> None:
        project.mkdir(parents=True, exist_ok=True)
        report = render_final_report(project, store)
        assert "## Final Architecture" in report
        assert "## Complete Feature Inventory" in report
        assert "## Acceptance Evidence" in report
        assert "## Remaining Human-Only Actions" in report
        assert "## Roadmap Preservation" in report
        assert "acceptance report status: not generated" in report
        assert "external evidence records for candidate: 0" in report
        assert "completion audit status: complete=False" in report
        assert "acceptance matrix status: INCOMPLETE" in report
        assert_raises(FinalReportValidationError, render_final_report, project / "missing", store)
        report_dir = project / ".stagemesh"
        report_dir.mkdir()
        (report_dir / "acceptance-report.json").write_text("{not-json", encoding="utf-8")
        (report_dir / "completion-audit.json").write_text('{"items": ["bad"]}', encoding="utf-8")
        (report_dir / "acceptance-matrix.json").write_text("[]", encoding="utf-8")
        invalid_report = render_final_report(project)
        assert "acceptance report status: invalid report" in invalid_report
        assert "completion audit status: invalid report" in invalid_report
        assert "acceptance matrix status: invalid report" in invalid_report
        (report_dir / "acceptance-report.json").write_text(
            '{"status":"PASS","proof_status":"BLOCKED_ON_EXTERNAL_EVIDENCE","proof_gaps":[{"check":"live","evidence":"provider:codex:execution: NOT_PROVEN"}],"checks":[{"name":"live","status":"PASS"}]}',
            encoding="utf-8",
        )
        report_with_gaps = render_final_report(project)
        assert "acceptance report status: PASS proof=BLOCKED_ON_EXTERNAL_EVIDENCE (1/1 checks passing, 1 proof gaps)" in report_with_gaps

    def provider_acceptance_isolates_capacity_failure(store: Store, project: Path) -> None:
        result = run_provider_acceptance(store, project)
        assert result.status == "PASS"
        assert result.chosen_provider == "secondary"
        assert result.capacity_failure_isolated is True

    def runtime_provider_adapter_validates_definition(store: Store, project: Path) -> None:
        adapter = RuntimeCommandAdapter("codex", ("python", "--version"), frozenset({" code ", "review"}))
        assert adapter.name == "codex"
        assert adapter.command == ("python", "--version")
        assert adapter.capabilities == frozenset({"code", "review"})
        assert_raises(ProviderValidationError, RuntimeCommandAdapter, "", ("python",))
        assert_raises(ProviderValidationError, RuntimeCommandAdapter, "codex", ())
        assert_raises(ProviderValidationError, RuntimeCommandAdapter, "codex", ("python", ""), frozenset({"code"}))
        assert_raises(ProviderValidationError, RuntimeCommandAdapter, "codex", ("python",), frozenset())
        assert_raises(ProviderValidationError, RuntimeCommandAdapter, "codex", ("python",), frozenset({""}))
        assert_raises(ProviderValidationError, RuntimeCommandAdapter, "bad name", ("python",))
        assert_raises(ProviderValidationError, RuntimeCommandAdapter, "codex", tuple(["python"] * 101))
        assert_raises(ProviderValidationError, adapters_from_commands, {"bad": '"unterminated'})
        assert_raises(ProviderValidationError, adapters_from_commands, {"bad": "x" * 2001})
        old = os.environ.get("STAGEMESH_CODEX_CMD")
        os.environ["STAGEMESH_CODEX_CMD"] = '"python" "-m" "stagemesh.cli"'
        try:
            codex = next(adapter for adapter in approved_default_adapters() if adapter.name == "codex")
            assert codex.command == ("python", "-m", "stagemesh.cli")
        finally:
            if old is None:
                os.environ.pop("STAGEMESH_CODEX_CMD", None)
            else:
                os.environ["STAGEMESH_CODEX_CMD"] = old
        config_dir = project / ".stagemesh"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.json").write_text(
            '{"providers":{"custom":"python --version","codex":"python -m stagemesh.cli"}}',
            encoding="utf-8",
        )
        adapters = {adapter.name: adapter.command for adapter in adapters_from_config(load_config(project))}
        assert adapters["custom"] == ("python", "--version")
        assert adapters["codex"] == ("python", "-m", "stagemesh.cli")

    def runtime_provider_execution_is_durable(store: Store, project: Path) -> None:
        project.mkdir(parents=True)
        task_id = store.upsert_task("provider execution")
        write_explicit_contract(project, task_id, "provider-output.txt")  # execution fails closed without an explicit contract
        adapter = RuntimeCommandAdapter("custom", (sys.executable, "-c", WRITE_PROVIDER_OUTPUT))
        result = adapter.execute(store, task_id, None, project)
        assert result.status is ExecutionStatus.SUCCEEDED
        assert result.durable_handoff is True
        assert result.candidate_sha is not None
        candidate = store.latest_candidate(task_id)
        assert candidate is not None
        assert candidate["sha"] == result.candidate_sha
        assert candidate["produced_by"] == "custom"
        execution = store.conn.execute("SELECT * FROM executions WHERE task_id=?", (task_id,)).fetchone()
        assert execution["status"] == ExecutionStatus.SUCCEEDED
        assert execution["kind"] == ExecutionKind.IMPLEMENTATION
        assert execution["candidate_sha"] == result.candidate_sha
        assert execution["pid"] is not None and execution["executable"]
        committed = subprocess.run(
            ["git", "show", f"{result.candidate_sha}:provider-output.txt"], cwd=project, text=True, capture_output=True, check=True
        )
        assert committed.stdout == "durable provider output"  # the candidate is a real commit of the deterministic change
        failed_task = store.upsert_task("provider execution fails")
        write_explicit_contract(project, failed_task, "provider-output.txt")
        failing = RuntimeCommandAdapter("custom", (sys.executable, "-c", "import sys; sys.exit(7)"))
        failed = failing.execute(store, failed_task, None, project)
        assert failed.status is ExecutionStatus.FAILED
        failed_execution = store.conn.execute("SELECT * FROM executions WHERE task_id=?", (failed_task,)).fetchone()
        assert failed_execution["status"] == ExecutionStatus.FAILED
        assert failed_execution["candidate_sha"] is None
        no_change_task = store.upsert_task("provider execution changes nothing")
        write_explicit_contract(project, no_change_task, "provider-output.txt")
        no_change = RuntimeCommandAdapter("custom", (sys.executable, "--version")).execute(store, no_change_task, None, project)
        assert no_change.status is ExecutionStatus.FAILED and no_change.failure_reason == NO_IMPLEMENTATION_CHANGE
        assert no_change.candidate_sha is None and store.latest_candidate(no_change_task) is None
        no_change_execution = store.conn.execute("SELECT * FROM executions WHERE task_id=?", (no_change_task,)).fetchone()
        assert no_change_execution["status"] == ExecutionStatus.FAILED and no_change_execution["candidate_sha"] is None
        assert no_change_execution["result"] == NO_IMPLEMENTATION_CHANGE

    def github_acceptance_models_sync_contract(store: Store, project: Path) -> None:
        result = run_github_acceptance(store)
        assert result.status == "PASS"
        assert result.deferred_skipped is True
        assert result.rate_limit_status == "UNKNOWN"

    def release_readiness_reports_external_gaps(store: Store, project: Path) -> None:
        data = release_readiness(ROOT, include_acceptance=False, run_checks=False, store=store)
        assert data["overall_status"] in {"BLOCKED_ON_EXTERNAL_EVIDENCE", "FAIL"}
        assert "external_gaps" in data
        assert data["local_proof_gaps"] == []
        assert data["external_evidence"] == []
        live_check = run_command_check(
            "live_acceptance_json",
            [sys.executable, "scripts/live_acceptance.py", "--json"],
            ROOT,
        )
        live_gap_data = proof_gaps([AcceptanceCheck(live_check.name, live_check.status, live_check.detail)])
        assert any(gap["evidence"].endswith("NOT_PROVEN") for gap in live_gap_data)
        record_external_evidence(store, "hosted-ci", "PASS", "https://example.invalid/stale", "abc1234")
        with_evidence = release_readiness(ROOT, include_acceptance=False, run_checks=False, store=store, candidate_sha="def5678")
        evidence = with_evidence["external_evidence"]
        assert len(evidence) == 1
        assert evidence[0]["kind"] == "hosted-ci"
        assert evidence[0]["candidate_match"] is False
        assert_raises(ReleaseReadinessValidationError, run_command_check, "", [sys.executable, "--version"], ROOT)
        assert_raises(ReleaseReadinessValidationError, run_command_check, "python", [], ROOT)
        assert_raises(ReleaseReadinessValidationError, run_command_check, "python", [sys.executable, ""], ROOT)
        assert_raises(ReleaseReadinessValidationError, run_command_check, "python", [sys.executable], ROOT / "missing")
        assert_raises(ReleaseReadinessValidationError, release_readiness, ROOT / "missing", False, False, store)
        project.mkdir(parents=True)
        outside = project.parent / "outside" / "readiness.json"
        assert_raises(SecurityBoundaryError, write_release_readiness, project, outside, False, False, store)

    def external_evidence_is_durable(store: Store, project: Path) -> None:
        evidence_id = record_external_evidence(
            store,
            "hosted-ci",
            "pass",
            "https://example.invalid/run/1",
            candidate_sha="ABC1234",
            notes="synthetic",
        )
        duplicate_id = record_external_evidence(
            store,
            "hosted-ci",
            "PASS",
            "https://example.invalid/run/1",
            candidate_sha="abc1234",
            notes="synthetic",
        )
        records = external_evidence_records(store)
        assert duplicate_id == evidence_id
        assert len(records) == 1
        assert records[0].id == evidence_id
        assert records[0].kind == "hosted-ci"
        assert records[0].status == "PASS"
        assert records[0].candidate_sha == "abc1234"
        store.conn.execute(
            "INSERT INTO external_evidence VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-duplicate",
                "hosted-ci",
                "PASS",
                "https://example.invalid/run/1",
                "abc1234",
                "synthetic",
                0.0,
            ),
        )
        store.conn.commit()
        deduped_records = external_evidence_records(store)
        assert len(deduped_records) == 1
        assert deduped_records[0].id == evidence_id
        assert_raises(ExternalEvidenceValidationError, record_external_evidence, store, "unknown", "PASS", "https://example.invalid")
        assert_raises(ExternalEvidenceValidationError, record_external_evidence, store, "hosted-ci", "MAYBE", "https://example.invalid")
        assert_raises(ExternalEvidenceValidationError, record_external_evidence, store, "hosted-ci", "PASS", "https://example.invalid")
        assert_raises(ExternalEvidenceValidationError, record_external_evidence, store, "hosted-ci", "PASS", "file:///tmp/proof")
        assert_raises(
            ExternalEvidenceValidationError,
            record_external_evidence,
            store,
            "hosted-ci",
            "PASS",
            "https://example.invalid",
            "not-a-sha",
        )

    def acceptance_matrix_has_external_gaps(store: Store, project: Path) -> None:
        matrix = acceptance_matrix()
        assert matrix["status"] == "INCOMPLETE"
        rows = matrix["rows"]
        assert any(row["status"] == "PROVEN" for row in rows)
        assert any(row["status"] != "PROVEN" for row in rows)
        project.mkdir(parents=True, exist_ok=True)
        output = project / ".stagemesh" / "acceptance-matrix.json"
        write_acceptance_matrix(output, store, root=project)
        assert output.exists()
        assert_raises(SecurityBoundaryError, write_acceptance_matrix, project.parent / "acceptance-matrix.json", store, project)
        assert_raises(AcceptanceMatrixValidationError, write_acceptance_matrix, Path(""), store)

    def end_to_end_acceptance_tracks_requested_steps(store: Store, project: Path) -> None:
        data = end_to_end_acceptance()
        assert data["status"] == "COMPLETE"
        assert data["proven"] == data["total"] == 18
        steps = data["steps"]
        assert [step["step"] for step in steps] == list(range(1, 19))
        requirements = {step["requirement"] for step in steps}
        assert "interrupt validation" in requirements
        assert "prove a deliberately broken future feature is rejected by CI" in requirements
        project.mkdir(parents=True, exist_ok=True)
        output = project / ".stagemesh" / "end-to-end-acceptance.json"
        write_end_to_end_acceptance(output, root=project)
        assert output.exists()
        assert_raises(SecurityBoundaryError, write_end_to_end_acceptance, project.parent / "e2e.json", project)
        assert_raises(EndToEndAcceptanceValidationError, write_end_to_end_acceptance, Path(""))

    def external_evidence_updates_audit_rows(store: Store, project: Path) -> None:
        record_external_evidence(store, "hosted-ci", "PASS", "https://example.invalid/linux", "abc1234")
        stale_audit = completion_audit(store, candidate_sha="def5678")
        stale_linux = next(item for item in stale_audit["items"] if item["requirement"] == "Linux acceptance")
        assert stale_linux["status"] == "MISSING_EXTERNAL_EVIDENCE"
        audit = completion_audit(store, candidate_sha="abc1234")
        linux = next(item for item in audit["items"] if item["requirement"] == "Linux acceptance")
        assert linux["status"] == "PROVEN"
        matrix = acceptance_matrix(store, candidate_sha="abc1234")
        linux_row = next(row for row in matrix["rows"] if row["area"] == "Linux acceptance")
        assert linux_row["status"] == "PROVEN"
        readiness = release_readiness(ROOT, include_acceptance=False, run_checks=False, store=store, candidate_sha="abc1234")
        gaps = {item["requirement"] for item in readiness["external_gaps"]}
        assert "Linux acceptance" not in gaps

    cases = [
        live_worker_restart,
        dead_worker_recovers,
        implementation_survives_validation,
        live_validation_survives,
        dead_validation_restarts_same_sha,
        reviewer_capacity_no_reimplementation,
        completed_not_redispatched,
        durable_handoff,
        persistence_rejects_invalid_core_inputs,
        targeted_ops,
        source_semantics,
        local_backlog_source_rejects_malformed_tasks,
        configured_json_task_source_syncs_with_distinct_source,
        worker_heartbeat_and_outbound_sync,
        worker_registration_rejects_invalid_identity,
        operator_dashboard_exposes_structured_state,
        health_degrades_on_blocked_tasks_or_failed_executions,
        dependency_scheduling,
        objective_planner_rejects_invalid_dependencies,
        global_registry_rejects_ambiguous_projects,
        finding_convergence_is_bounded,
        distributed_work_packets_are_claimed_once,
        distributed_work_ack_requires_claimed_terminal_status,
        distributed_work_rejects_invalid_queue_inputs,
        distributed_work_packets_have_renewable_leases,
        distributed_work_transport_round_trips_ack,
        ci_wait_releases_worker_capacity_while_pending,
        acceptance_and_ci_gates_validate_inputs,
        capacity_registry_rejects_invalid_provider_state,
        github_outbound_sync_records_capacity_separately,
        github_retry_after_parsing_is_defensive,
        git_attribution_is_worker_owned,
        git_workspace_rejects_unsafe_inputs,
        secrets_are_redacted,
        config_loads_from_project_file,
        config_supports_google_ax_export_source,
        config_rejects_invalid_routing_and_provider_shapes,
        github_remote_detection_supports_zero_config,
        routing_modes_select_expected_provider,
        release_output_stays_inside_workspace,
        demo_project_is_workspace_bound_and_runnable_shape,
        release_artifact_rejects_unsafe_metadata,
        release_artifact_contains_tracked_source_manifest,
        release_files_reject_symlink_escape,
        migrations_are_idempotent,
        backend_probe_reports_postgres_dependency,
        backend_command_can_apply_postgres_migrations,
        postgres_schema_contract_covers_authoritative_tables,
        completion_audit_is_not_falsely_complete,
        audit_events_are_redacted_and_exportable,
        retry_backoff_is_durable_and_clearable,
        workspace_boundary_rejects_outside_outputs,
        final_report_mentions_missing_evidence,
        provider_acceptance_isolates_capacity_failure,
        runtime_provider_adapter_validates_definition,
        runtime_provider_execution_is_durable,
        github_acceptance_models_sync_contract,
        release_readiness_reports_external_gaps,
        external_evidence_is_durable,
        acceptance_matrix_has_external_gaps,
        end_to_end_acceptance_tracks_requested_steps,
        external_evidence_updates_audit_rows,
    ]
    for case in cases:
        with_store(case)

    assert classify_process(
        ProcessIdentity(pid=100, create_time=1.0, boot_id="a", executable="worker"),
        ProcessIdentity(pid=100, create_time=2.0, boot_id="a", executable="worker"),
    ) == "DEAD"
    assert classify_process(ProcessIdentity(pid=100, create_time=None, boot_id="a"), None) == "UNKNOWN"
    assert_raises(
        LifecycleError,
        evidence_allows_advance,
        current=Stage.VALIDATE,
        candidate_sha="sha-b",
        evidence_sha="sha-a",
        kind=EvidenceKind.VALIDATION,
        status=EvidenceStatus.PASSED,
    )
    assert_raises(
        LifecycleError,
        evidence_allows_advance,
        current=Stage.VALIDATE,
        candidate_sha="sha",
        evidence_sha="sha",
        kind=EvidenceKind.VALIDATION,
        status=EvidenceStatus.FAILED,
    )
    print("invariants: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
