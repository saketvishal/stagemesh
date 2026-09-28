from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stagemesh.coordinator import Coordinator
from stagemesh.domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus, ProcessIdentity, Stage
from stagemesh.execution import ExecutionResult, FakeExecutor
from stagemesh.lifecycle import LifecycleError, evidence_allows_advance
from stagemesh.persistence import Store
from stagemesh.process_identity import classify_process
from stagemesh.review import Reviewer
from stagemesh.task_sources import DiscoveredTask, GitHubIssueSource, LocalBacklogSource, OutboundSync, TaskSourceValidationError, sync_source
from stagemesh.workers import heartbeat_worker, register_worker
from stagemesh.scheduling import Scheduler
from stagemesh.remediation import RemediationPolicy, finding_identity
from stagemesh.distributed import WorkQueue, WorkQueueError
from stagemesh.github import GitHubClient, parse_retry_after
from stagemesh.task_sources import GitHubOutboundSync
from stagemesh.attribution import attribution_for_worker
from stagemesh.redaction import redact_mapping, redact_text
from stagemesh.config import ConfigValidationError, load_config
from stagemesh.release import ReleaseValidationError, build_release_artifact, release_files
from stagemesh.security import SecurityBoundaryError
from stagemesh.persistence_backends import probe_backend
from stagemesh.completion_audit import completion_audit
from stagemesh.audit import AuditValidationError, record_audit, export_audit_jsonl
from stagemesh.retry import RetryRegistry, RetryValidationError, backoff_seconds
from stagemesh.postgres_store import (
    POSTGRES_SCHEMA_TABLES,
    PostgresStore,
    PostgresUnavailable,
    postgres_available,
    postgres_schema_contract,
    postgres_schema_statements,
)
from stagemesh.final_report import render_final_report
from stagemesh.provider_acceptance import run_provider_acceptance
from stagemesh.github_acceptance import run_github_acceptance
from stagemesh.release_readiness import release_readiness
from stagemesh.external_evidence import ExternalEvidenceValidationError, record_external_evidence, external_evidence_records
from stagemesh.acceptance_matrix import acceptance_matrix
from stagemesh.routing import Provider, Router, RoutingMode, RoutingValidationError
from stagemesh.github import parse_github_remote
from stagemesh.operator import operator_report
from stagemesh.dashboard import render_dashboard
from stagemesh.ci_wait import decide_ci_wait
from stagemesh.objectives import ObjectivePlanner, ObjectiveValidationError
from stagemesh.registry import GlobalRegistry, ProjectRegistration, RegistryConflictError
from stagemesh.capacity import CapacityKind, CapacityRegistry, CapacityValidationError
from stagemesh.providers import ProviderValidationError, RuntimeCommandAdapter, approved_default_adapters


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

    def __enter__(self) -> "FakePostgresCursor":
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
        section_names = {section.name for section in report.sections}
        assert {"Tasks", "Workers", "Source Events", "Retries", "External Evidence"}.issubset(section_names)
        dashboard = render_dashboard(store)
        assert "<h2>Tasks</h2>" in dashboard
        assert "<h2>Retries</h2>" in dashboard
        assert "worker-observe" in dashboard
        assert "https://example.invalid/ci" in dashboard

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

    def distributed_work_packets_are_claimed_once(store: Store, project: Path) -> None:
        task_id = store.upsert_task("distributed")
        queue = WorkQueue(store)
        packet_id = queue.enqueue(task_id, "IMPLEMENT")
        first = queue.poll("worker-a")
        second = queue.poll("worker-b")
        assert [packet.id for packet in first] == [packet_id]
        assert second == []
        queue.ack(packet_id, "SUCCEEDED", {"candidate_sha": "abc"})

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

    def capacity_registry_rejects_invalid_provider_state(store: Store, project: Path) -> None:
        registry = CapacityRegistry()
        registry.record("primary", CapacityKind.CAPACITY, retry_after_seconds=30)
        registry.record("secondary", CapacityKind.AVAILABLE)
        assert registry.choose_primary_secondary("primary", "secondary") == "secondary"
        assert registry.get("missing").kind == CapacityKind.UNKNOWN
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

        assert parse_retry_after(None) == 60
        assert parse_retry_after("") == 60
        assert parse_retry_after("not-a-number") == 60
        assert parse_retry_after("-1") == 60
        assert parse_retry_after("15") == 15
        result = GitHubClient("owner", "repo", InvalidRetryAfterTransport()).list_open_issues()
        assert result.status == "UNKNOWN"
        assert result.retry_after == 60

    def git_attribution_is_worker_owned(store: Store, project: Path) -> None:
        attribution = attribution_for_worker("worker 1", "codex")
        assert attribution.author_email == "codex+worker-1@stagemesh.invalid"
        assert attribution.committer_email == "stagemesh@stagemesh.invalid"

    def secrets_are_redacted(store: Store, project: Path) -> None:
        redacted = redact_mapping({"github_token": "abc", "nested": {"password": "def"}, "safe": "ok"})
        assert redacted["github_token"] == "***REDACTED***"
        assert redacted["nested"] == {"password": "***REDACTED***"}
        assert redact_text("token abc", ["abc"]) == "token ***REDACTED***"

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
        assert config.routing_mode == "SINGLE_AGENT"
        assert config.single_agent_provider == "codex"
        assert config.stage_routes["REVIEW"] == "claude"

    def config_rejects_invalid_routing_and_provider_shapes(store: Store, project: Path) -> None:
        config_dir = project / ".stagemesh"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "config.json"
        config_file.write_text('{"routing":{"mode":"ROUND_ROBIN"}}', encoding="utf-8")
        assert_raises(ConfigValidationError, load_config, project)
        config_file.write_text('{"routing":{"stage_routes":{"BOGUS":"codex"}}}', encoding="utf-8")
        assert_raises(ConfigValidationError, load_config, project)
        config_file.write_text('{"providers":{"codex":""}}', encoding="utf-8")
        assert_raises(ConfigValidationError, load_config, project)
        config_file.write_text('{"providers":[]}', encoding="utf-8")
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

    def release_files_reject_symlink_escape(store: Store, project: Path) -> None:
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
        subprocess.run(["git", "add", "-A"], cwd=project, text=True, capture_output=True, check=True)
        files = {path.name for path in release_files(project)}
        assert "inside.txt" in files
        assert "linked-secret.txt" not in files

    def migrations_are_idempotent(store: Store, project: Path) -> None:
        first = store.schema_version()
        store.migrate()
        second = store.schema_version()
        assert first == second == 2

    def backend_probe_reports_postgres_dependency(store: Store, project: Path) -> None:
        probe = probe_backend("postgresql://example/db")
        assert probe.name == "postgres"
        assert probe.available in {True, False}
        assert probe.reason
        if not postgres_available():
            assert_raises(PostgresUnavailable, PostgresStore, "postgresql://example/db")

    def postgres_schema_contract_covers_authoritative_tables(store: Store, project: Path) -> None:
        contract = postgres_schema_contract()
        assert contract["dialect"] == "postgresql"
        assert tuple(contract["tables"]) == POSTGRES_SCHEMA_TABLES
        schema_sql = str(contract["schema_sql"])
        for table in POSTGRES_SCHEMA_TABLES:
            assert f"CREATE TABLE IF NOT EXISTS {table}" in schema_sql
        statements = postgres_schema_statements()
        assert len(statements) >= len(POSTGRES_SCHEMA_TABLES)
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

    def audit_events_are_redacted_and_exportable(store: Store, project: Path) -> None:
        record_audit(store, "secret.test", {"token": "abc", "safe": "ok"})
        record_audit(store, "secret.second", {"safe": "later"})
        output = project / "audit.jsonl"
        export_audit_jsonl(store, output)
        text = output.read_text(encoding="utf-8")
        assert "***REDACTED***" in text
        assert "abc" not in text
        limited = project / "audit-limited.jsonl"
        export_audit_jsonl(store, limited, limit=1)
        text = limited.read_text(encoding="utf-8")
        lines = text.strip().splitlines()
        assert len(lines) == 1
        assert "secret.second" in lines[0]
        assert_raises(AuditValidationError, record_audit, store, "", {"safe": "ok"})
        assert_raises(AuditValidationError, export_audit_jsonl, store, output, 0)
        assert_raises(AuditValidationError, export_audit_jsonl, store, output, 10001)

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
        assert_raises(RetryValidationError, backoff_seconds, 0)
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
        assert "completion audit status: not generated" in report

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
        old = os.environ.get("STAGEMESH_CODEX_CMD")
        os.environ["STAGEMESH_CODEX_CMD"] = '"python" "-m" "stagemesh.cli"'
        try:
            codex = approved_default_adapters()[0]
            assert codex.command == ("python", "-m", "stagemesh.cli")
        finally:
            if old is None:
                os.environ.pop("STAGEMESH_CODEX_CMD", None)
            else:
                os.environ["STAGEMESH_CODEX_CMD"] = old

    def github_acceptance_models_sync_contract(store: Store, project: Path) -> None:
        result = run_github_acceptance(store)
        assert result.status == "PASS"
        assert result.deferred_skipped is True
        assert result.rate_limit_status == "UNKNOWN"

    def release_readiness_reports_external_gaps(store: Store, project: Path) -> None:
        data = release_readiness(ROOT, include_acceptance=False, run_checks=False, store=store)
        assert data["overall_status"] in {"BLOCKED_ON_EXTERNAL_EVIDENCE", "FAIL"}
        assert "external_gaps" in data

    def external_evidence_is_durable(store: Store, project: Path) -> None:
        evidence_id = record_external_evidence(
            store,
            "hosted-ci",
            "pass",
            "https://example.invalid/run/1",
            candidate_sha="ABC1234",
            notes="synthetic",
        )
        records = external_evidence_records(store)
        assert records[0].id == evidence_id
        assert records[0].kind == "hosted-ci"
        assert records[0].status == "PASS"
        assert records[0].candidate_sha == "abc1234"
        assert_raises(ExternalEvidenceValidationError, record_external_evidence, store, "unknown", "PASS", "https://example.invalid")
        assert_raises(ExternalEvidenceValidationError, record_external_evidence, store, "hosted-ci", "MAYBE", "https://example.invalid")
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

    def external_evidence_updates_audit_rows(store: Store, project: Path) -> None:
        record_external_evidence(store, "hosted-ci", "PASS", "https://example.invalid/linux", "abc1234")
        audit = completion_audit(store)
        linux = [item for item in audit["items"] if item["requirement"] == "Linux acceptance"][0]
        assert linux["status"] == "PROVEN"
        matrix = acceptance_matrix(store)
        linux_row = [row for row in matrix["rows"] if row["area"] == "Linux acceptance"][0]
        assert linux_row["status"] == "PROVEN"
        readiness = release_readiness(ROOT, include_acceptance=False, run_checks=False, store=store)
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
        targeted_ops,
        source_semantics,
        local_backlog_source_rejects_malformed_tasks,
        worker_heartbeat_and_outbound_sync,
        operator_dashboard_exposes_structured_state,
        dependency_scheduling,
        objective_planner_rejects_invalid_dependencies,
        global_registry_rejects_ambiguous_projects,
        finding_convergence_is_bounded,
        distributed_work_packets_are_claimed_once,
        distributed_work_ack_requires_claimed_terminal_status,
        distributed_work_packets_have_renewable_leases,
        ci_wait_releases_worker_capacity_while_pending,
        capacity_registry_rejects_invalid_provider_state,
        github_outbound_sync_records_capacity_separately,
        github_retry_after_parsing_is_defensive,
        git_attribution_is_worker_owned,
        secrets_are_redacted,
        config_loads_from_project_file,
        config_rejects_invalid_routing_and_provider_shapes,
        github_remote_detection_supports_zero_config,
        routing_modes_select_expected_provider,
        release_output_stays_inside_workspace,
        release_artifact_rejects_unsafe_metadata,
        release_artifact_contains_tracked_source_manifest,
        release_files_reject_symlink_escape,
        migrations_are_idempotent,
        backend_probe_reports_postgres_dependency,
        postgres_schema_contract_covers_authoritative_tables,
        completion_audit_is_not_falsely_complete,
        audit_events_are_redacted_and_exportable,
        retry_backoff_is_durable_and_clearable,
        workspace_boundary_rejects_outside_outputs,
        final_report_mentions_missing_evidence,
        provider_acceptance_isolates_capacity_failure,
        runtime_provider_adapter_validates_definition,
        github_acceptance_models_sync_contract,
        release_readiness_reports_external_gaps,
        external_evidence_is_durable,
        acceptance_matrix_has_external_gaps,
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
