"""Scope persistence, source failures and actual claim boundaries; no real providers."""
import json
from dataclasses import replace

import pytest

from stagemesh.auto_plan import outside_label_selector
from stagemesh.config import load_config
from stagemesh.domain import Stage
from stagemesh.persistence import Store
from stagemesh.selection_scope import (ScopeError, attach, current, load_scope, preview, record_candidate,
    record_task, refresh, resolve_scope, task_problem)
from stagemesh.run_window import WindowOptions, _resume_argv, planned_selection
from test_run_ready import _project
from test_task_selection import _continue


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr("stagemesh.config.detect_github_repository", lambda p: None)
    project = _project(tmp_path, ["A", "B"], labels={"A": ["focus"], "B": ["other"]})
    store = Store(project / ".stagemesh/stagemesh.sqlite3")
    store.migrate()
    config = load_config(project)
    scope = resolve_scope(store, label="focus")
    attach(store, project, config, scope)
    refresh(store)
    yield store, project, config, scope
    store.close()


def test_saved_scope_survives_restart_and_unrelated_audit_volume(rig):
    store, project, config, scope = rig
    record_task(store, "A")
    for i in range(215):
        store.add_audit_event("unrelated", {"i": i})
    with_store = Store(store.db_path)
    try:
        assert resolve_scope(with_store, resume=scope.id) == scope
        assert resolve_scope(with_store, task_id="A") == scope
        assert resolve_scope(with_store) is None  # fresh run is unfiltered
    finally:
        with_store.close()
    changed = resolve_scope(store, resume=scope.id, label="other")
    assert changed.label == "other" and not current(store, scope)
    assert load_scope(store, scope.id) == scope  # history retained
    cleared = resolve_scope(store, resume=changed.id, clear=True)
    assert cleared.label is None and not current(store, changed)


def test_active_scope_change_is_refused_without_touching_owner(rig):
    store, _, _, scope = rig
    claim = store.acquire_claim("A", "worker")
    assert claim
    with pytest.raises(ScopeError, match="active"):
        resolve_scope(store, resume=scope.id, clear=True)
    assert store.conn.execute("SELECT active FROM claims WHERE id=?", (claim,)).fetchone()[0] == 1
    assert current(store, scope)


def test_failed_source_after_success_revokes_admission(rig, monkeypatch):
    store, _, _, _ = rig
    class Source:
        name = "github"
        last_status = "STALE"
        def discover(self):
            return []  # real configured GitHub adapter's failure contract
    monkeypatch.setattr("stagemesh.selection_scope.task_sources_from_config", lambda c: [Source()])
    with pytest.raises(ScopeError, match="cached labels"):
        refresh(store)
    assert outside_label_selector(store, store.get_task("A"), "focus")
    assert store.acquire_claim("A", "worker", admission_check=lambda: not task_problem(store, store.get_task("A"))) is None


def test_missing_item_in_successful_scan_does_not_reuse_cached_label(rig):
    store, project, _, _ = rig
    path = project / ".stagemesh/backlog.json"
    data = json.loads(path.read_text())
    data["tasks"] = [x for x in data["tasks"] if x["id"] != "A"]
    path.write_text(json.dumps(data))
    refresh(store)
    assert "absent" in task_problem(store, store.get_task("A"))


def test_claim_rechecks_cross_connection_label_change(rig):
    store, _, _, _ = rig
    assert not outside_label_selector(store, store.get_task("A"), "focus")
    other = Store(store.db_path)
    try:
        task = other.get_task("A")
        value = other.source_state(task["source"], task["source_id"])
        value["labels"] = ["other"]
        other.cache_source(task["source"], task["source_id"], value, "OPEN")
    finally:
        other.close()
    assert store.acquire_claim("A", "worker", admission_check=lambda: not outside_label_selector(store, store.get_task("A"), "focus")) is None
    assert store.conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0


def test_removed_label_drains_only_exact_granted_candidate(rig):
    store, project, _, scope = rig
    store.add_candidate("A", "a" * 40, "test", True)
    record_candidate(store, "A", "a" * 40)
    store.advance_task("A", Stage.VALIDATE)
    path = project / ".stagemesh/backlog.json"
    data = json.loads(path.read_text())
    data["tasks"][0]["labels"] = ["other"]
    path.write_text(json.dumps(data))
    refresh(store)
    assert not outside_label_selector(store, store.get_task("A"), "focus")
    store.advance_task("A", Stage.IMPLEMENT)  # remediation is new provider work
    assert outside_label_selector(store, store.get_task("A"), "focus")
    store.advance_task("A", Stage.VALIDATE)
    store.add_candidate("A", "b" * 40, "test", True)
    assert outside_label_selector(store, store.get_task("A"), "focus")


def test_window_plan_and_resume_argv_keep_scope_and_exclusions(rig):
    store, project, config, scope = rig
    options = WindowOptions(required_label="focus", scope_id=scope.id)
    picked, skipped = planned_selection(store, options, project)
    assert picked == ["A"] and "B" in skipped
    argv = _resume_argv(options)
    assert argv[argv.index("--resume-scope") + 1] == scope.id
    report = preview(store, project, replace(config.task_selection, required_label="focus"))
    assert (report["matching"], report["eligible"], report["excluded"]) == (1, 1, 1)


def test_continue_scope_round_trip_and_explicit_conflict(rig):
    store, project, _, scope = rig
    code, report = _continue(project, "--resume-scope", scope.id, "--task", "B")
    assert code != 0 and report["stop_reason"] == "REFUSED:label_selector_conflict"
    assert list(store.running_executions()) == []


def test_no_matches_has_zero_provider_calls(rig, monkeypatch):
    store, project, _, _ = rig
    monkeypatch.setattr("stagemesh.execution.FakeExecutor.run", lambda *a, **k: pytest.fail("excluded work started"))
    code, report = _continue(project, "--label", "missing")
    assert code != 0
    assert report["detail"]["scope"]["matching"] == 0


def test_trusted_mechanical_lineage_drains_but_does_not_transfer_evidence(rig):
    from stagemesh.review_identity import command_snapshot, record_implementation_identity, record_implementation_derivation
    from stagemesh.domain import ExecutionKind, ExecutionStatus, EvidenceKind
    store, _, _, scope = rig
    a, b = "a" * 40, "b" * 40
    store.add_candidate("A", a, "test", True)
    execution = store.start_execution(task_id="A", claim_id=None, kind=ExecutionKind.IMPLEMENTATION, candidate_sha=a, actor="test")
    store.finish_execution(execution, ExecutionStatus.SUCCEEDED, a)
    record_implementation_identity(store, "A", a, execution, command_snapshot("test", ["fixture-tool"]))
    record_candidate(store, "A", a)
    store.add_candidate("A", b, "test", True)
    assert record_implementation_derivation(store, "A", a, b, "rebase")
    store.advance_task("A", Stage.VALIDATE)
    task = store.get_task("A")
    state = store.source_state(task["source"], task["source_id"])
    state["labels"] = []
    store.cache_source(task["source"], task["source_id"], state, "OPEN")
    assert not outside_label_selector(store, task, "focus")
    assert not store.has_evidence("A", b, EvidenceKind.VALIDATION)
    assert not store.has_evidence("A", b, EvidenceKind.REVIEW)


def test_admitted_derived_candidate_preserves_its_verified_origins(rig):
    from stagemesh.review_identity import command_snapshot, record_implementation_identity, record_implementation_derivation
    from stagemesh.domain import ExecutionKind, ExecutionStatus
    store, _, _, scope = rig
    a, b, c = "a" * 40, "b" * 40, "c" * 40
    store.add_candidate("A", a, "test", True)
    execution = store.start_execution(task_id="A", claim_id=None, kind=ExecutionKind.IMPLEMENTATION, candidate_sha=a, actor="test")
    store.finish_execution(execution, ExecutionStatus.SUCCEEDED, a)
    record_implementation_identity(store, "A", a, execution, command_snapshot("test", ["fixture-tool"]))
    store.add_candidate("A", b, "test", True)
    assert record_implementation_derivation(store, "A", a, b, "rebase")
    store.advance_task("A", Stage.VALIDATE)
    record_task(store, "A")  # the first scope admission is B, not implementation A
    store.add_candidate("A", c, "test", True)
    assert record_implementation_derivation(store, "A", b, c, "reattribution")
    task = store.get_task("A")
    state = store.source_state(task["source"], task["source_id"])
    state["labels"] = []
    store.cache_source(task["source"], task["source_id"], state, "OPEN")
    assert not outside_label_selector(store, task, "focus")


def test_explicit_unfiltered_child_is_refused_before_planning(rig, monkeypatch):
    import importlib
    module = importlib.import_module("stagemesh.run_ready")
    from stagemesh.config import TaskSelectionConfig
    store, project, _, scope = rig
    task = store.get_task("A")
    state = store.source_state(task["source"], task["source_id"])
    state["selection_scope"] = {"scope_id": scope.id, "source_objective": "90"}
    store.cache_source(task["source"], task["source_id"], state, "OPEN")
    other = Store(store.db_path)  # deliberately no scope attached, as a fresh unfiltered entry point
    monkeypatch.setattr(module, "_ensure_contract", lambda *a, **k: pytest.fail("unscoped child reached planning"))
    try:
        result = module.run_ready(other, project, lambda *a: pytest.fail("coordinator started"), task_id="A", policy=TaskSelectionConfig())
        assert result.stop_reason == "REFUSED:label_selector_conflict"
    finally:
        other.close()


def test_objective_children_are_bound_by_operator_not_planner_labels(rig):
    from stagemesh.objectives import ObjectivePlanner
    from stagemesh.selection_scope import authorize_children
    from stagemesh.task_sources import LocalBacklogSource, sync_source
    store, project, _, scope = rig
    store.cache_source("github", "90", {"state": "OPEN", "labels": ["focus", "type:objective"],
        "eligible": False, "eligibility_reason": "source objective root"}, "OPEN")
    store.selection_seen.add(("github", "90"))
    payload = {"id": "objective", "title": "Objective", "tasks": [{"id": "CHILD", "title": "child", "labels": ["other"]}]}
    planner = ObjectivePlanner()
    objective = planner.parse(payload)
    bound = authorize_children(store, scope, "90", objective.tasks)
    path = project / ".stagemesh/backlog.json"
    planner.write_backlog(objective, payload, path, selection_scope=bound)
    children = LocalBacklogSource(path).discover()
    assert children[0].labels == ("focus",)
    sync_source(store, children)
    store.selection_seen.add(("local-backlog", "CHILD"))
    child = store.get_task("CHILD")
    assert task_problem(store, child) is None
    store.cache_source("github", "90", {"state": "CLOSED", "labels": ["focus"]}, "CLOSED")
    assert "closed" in task_problem(store, child)


@pytest.mark.parametrize("override", [
    {"labels": ["focus", "stagemesh:deferred"]},
    {"labels": ["focus", "status:blocked"]},
    {"admission_pending": False},
    {"validation_errors": ["bad source"]},
    {"dependencies": ["missing"]},
])
def test_parent_safety_gates_cannot_be_bypassed_by_child_planning(rig, override):
    from stagemesh.selection_scope import authorize_children
    store, _, _, scope = rig
    store.cache_source("github", "90", {"state": "OPEN", "labels": ["focus"], "eligible": False,
        "eligibility_reason": "source objective root", **override}, "OPEN")
    store.selection_seen.add(("github", "90"))
    with pytest.raises(ScopeError):
        authorize_children(store, scope, "90", ["CHILD"])


@pytest.mark.parametrize("labels,required,excluded,allowed", [
    (["focus", "type:objective"], ["stagemesh:ready"], [], True),
    (["focus", "type:objective", "stagemesh:ready"], ["stagemesh:ready"], [], False),
    (["focus"], ["stagemesh:ready"], [], False),
    (["focus", "type:objective"], ["stagemesh:ready", "approved"], [], False),
    (["focus", "type:objective", "hold"], ["stagemesh:ready"], ["hold"], False),
])
def test_real_github_objective_planning_preserves_source_policy(rig, monkeypatch, labels, required, excluded, allowed):
    from stagemesh.config import GitHubConfig, TaskSourceConfig
    from stagemesh.task_sources import _github_issue_to_task
    from stagemesh.selection_scope import authorize_children
    store, project, config, scope = rig
    source = _github_issue_to_task({"number": 90, "title": "objective", "state": "open", "labels": [{"name": x} for x in labels]})
    monkeypatch.setattr("stagemesh.task_sources.GitHubApiIssueSource.discover", lambda self: ([source], "OK", None))
    monkeypatch.setattr("stagemesh.selection_scope.label_catalog", lambda self: set(labels))
    config = replace(config, github=GitHubConfig(owner="fixture", repo="fixture"),
        task_sources=(TaskSourceConfig("github", "github", labels=tuple(required), excluded_labels=tuple(excluded)),))
    attach(store, project, config, scope)
    refresh(store)
    if allowed:
        assert authorize_children(store, scope, "90", ["CHILD"])["scope_id"] == scope.id
        assert store.get_task("90") is None  # root remains non-dispatchable
    else:
        with pytest.raises(ScopeError):
            authorize_children(store, scope, "90", ["CHILD"])


def test_diagnostics_keep_objective_roots_and_report_unknown_vs_empty(rig, monkeypatch):
    from stagemesh.task_sources import DiscoveredTask
    store, project, config, _ = rig
    class Source:
        name = "github"
        last_status = "OK"
        def discover(self):
            return [DiscoveredTask("github", "90", "root", eligible=False,
                labels=("focus", "type:objective"), eligibility_reason="source objective root")]
    monkeypatch.setattr("stagemesh.selection_scope.task_sources_from_config", lambda c: [Source()])
    refresh(store)
    policy = replace(config.task_selection, required_label="focus")
    report = preview(store, project, policy)
    assert report["matching"] == 2 and report["eligible"] == 1 and report["blocked"] == 1
    assert any(row["task_id"] == "90" and "objective" in row["reason"] for row in report["exclusions"])
    empty = resolve_scope(store, label="empty")
    attach(store, project, config, empty)
    refresh(store)
    store.selection_catalog = {"empty"}
    assert preview(store, project, replace(policy, required_label="empty"))["label_state"] == "known_empty"
    store.selection_catalog = {"other"}
    assert preview(store, project, replace(policy, required_label="empty"))["label_state"] == "unknown_label"


def test_child_runner_receives_saved_scope(rig, monkeypatch):
    from stagemesh.survivor_wave import subprocess_task_runner
    from types import SimpleNamespace
    _, project, _, scope = rig
    calls = []
    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout='{"stop_reason":"DONE"}', stderr="", returncode=0)
    monkeypatch.setattr("stagemesh.survivor_wave.subprocess.run", fake_run)
    subprocess_task_runner(project, scope_id=scope.id)("A", 3)
    assert calls[0][calls[0].index("--resume-scope") + 1] == scope.id


def test_later_refresh_failure_never_reports_fresh(rig, monkeypatch):
    import importlib
    module = importlib.import_module("stagemesh.run_ready")
    store, project, _, scope = rig
    original = module.choose_task
    def choose(*args, **kwargs):
        result = original(*args, **kwargs)
        def fail(self):
            raise ScopeError("source unavailable")
        monkeypatch.setattr("stagemesh.selection_scope.LocalBacklogSource.discover", fail)
        return result
    monkeypatch.setattr(module, "choose_task", choose)
    code, report = _continue(project, "--resume-scope", scope.id)
    assert code != 0
    assert report["detail"]["scope"]["source_fresh"] is False


@pytest.mark.parametrize("fail_after_first", [False, True])
@pytest.mark.parametrize("attach_context", [False, True])
def test_real_window_selection_topup_and_refresh_failure(tmp_path, monkeypatch, fail_after_first, attach_context):
    from test_run_window import window, FRESH
    import stagemesh.run_window as rw
    monkeypatch.setattr("stagemesh.config.detect_github_repository", lambda p: None)
    monkeypatch.setattr("stagemesh.readiness.check_runtime", lambda p: FRESH)
    store, project, hooks_rig, hooks, options, config, kwargs = window(tmp_path, ["A", "B", "C"], concurrency=1)
    try:
        path = project / ".stagemesh/backlog.json"
        payload = json.loads(path.read_text())
        for item in payload["tasks"]:
            item["labels"] = ["other" if item["id"] == "B" else "focus"]
        path.write_text(json.dumps(payload))
        scope = resolve_scope(store, label="focus")
        if attach_context:
            attach(store, project, config, scope)
            refresh(store)
        options.scope_id, options.required_label = scope.id, scope.label
        original = hooks.run_task
        def run(task_id, max_steps):
            result = original(task_id, max_steps)
            if fail_after_first:
                def fail():
                    store.selection_fresh = False
                    raise ScopeError("source outage after first task")
                store.refresh_selection_scope = fail
            return result
        hooks.run_task = run
        report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main",
            staging_ref="refs/heads/wave/staging", **kwargs)
        assert "B" not in hooks_rig.runs
        if fail_after_first:
            assert hooks_rig.runs == ["A"]
            assert "source outage" in report.stop_reason and report.scope["source_fresh"] is False
        else:
            assert hooks_rig.runs == ["A", "C"]
        assert report.to_dict()["options"]["scope_id"] == scope.id
    finally:
        store.close()


def test_window_rejects_mismatched_attached_scope_even_with_candidate_grant(tmp_path, monkeypatch):
    from test_run_window import window, FRESH
    import stagemesh.run_window as rw
    monkeypatch.setattr("stagemesh.config.detect_github_repository", lambda p: None)
    monkeypatch.setattr("stagemesh.readiness.check_runtime", lambda p: FRESH)
    store, project, hooks_rig, hooks, options, config, kwargs = window(tmp_path, ["A"], concurrency=1)
    try:
        path = project / ".stagemesh/backlog.json"
        payload = json.loads(path.read_text())
        payload["tasks"][0]["labels"] = ["focus"]
        path.write_text(json.dumps(payload))
        scope = resolve_scope(store, label="focus")
        attach(store, project, config, scope)
        refresh(store)
        store.add_candidate("A", "a" * 40, "test", True)
        record_candidate(store, "A", "a" * 40)
        store.advance_task("A", Stage.VALIDATE)
        other = resolve_scope(store, label="other")
        options.scope_id, options.required_label = other.id, other.label
        report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main",
            staging_ref="refs/heads/wave/staging", **kwargs)
        assert report.refused and "attached scope" in report.stop_reason
        assert hooks_rig.runs == [] and store.selection_scope == scope
    finally:
        store.close()
