"""StageMesh-controlled landing: real disposable Git remotes, fake hosted observations."""
from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest
from autonomy_support import TASK, git
from test_fast_forward import rig

import stagemesh.fast_forward as ff
from stagemesh.cli import build_parser
from stagemesh.config import ConfigValidationError, load_config
from stagemesh.survivor_wave import TaskRecord


def controlled(tmp_path, monkeypatch, **kwargs):
    values = rig(tmp_path, monkeypatch, **kwargs)
    repo, _remote, base, sha, store, ci, adapter, _pr = values
    adapter.configure_ff(repo, store, "main", ("web",), ci, policy="stagemesh", check_app_id=15368)
    adapter.expected_base = base
    monkeypatch.setattr(adapter, "host_policy", type(adapter).host_policy.__get__(adapter))
    calls = []
    def request(method, path, **kw):
        calls.append(path)
        if path == "":
            return 200, {"full_name":"owner/repo", "archived":False, "disabled":False, "permissions":{"push":True}}
        if "/check-runs?" in path:
            return 200, {"check_runs":[{"id":1, "name":"web", "head_sha":sha, "app":{"id":15368},
                "check_suite":{"id":1}, "status":"completed", "conclusion":"success"}]}
        if "/statuses?" in path:
            return 200, []
        pytest.fail(f"controlled mode requested protected API: {path}")
    monkeypatch.setattr(adapter, "_request", request)
    return values, calls


def test_controlled_policy_does_not_need_branch_protection_apis(tmp_path, monkeypatch):
    values, calls = controlled(tmp_path, monkeypatch)
    *_, adapter, pr = values
    assert adapter.host_policy(pr.head_ref) is None
    assert adapter.hosted_requirements == {("web",15368)}
    assert calls == [""]


@pytest.mark.parametrize("independent", [True, False])
def test_controlled_merge_preserves_exact_candidate_and_independent_review(tmp_path, monkeypatch, independent):
    values, calls = controlled(tmp_path, monkeypatch, independent=independent)
    repo, remote, base, sha, store, _ci, _adapter, _pr = values
    merger = ff.make_merger(store, repo, "refs/heads/main", ("web",), policy="stagemesh", check_app_id=15368)
    result = merger(TaskRecord(TASK, pr_url="https://github.com/owner/repo/pull/7", integrated_candidate=sha), time.time()+20)
    assert result["merged"] is independent, result
    assert git(remote, "rev-parse", "main") == (sha if independent else base)
    assert git(repo, "rev-parse", "main") == (sha if independent else base)
    assert not any("/rules" in p or "/protection" in p for p in calls)


@pytest.mark.parametrize("problem", ["publisher", "pending", "red", "missing", "wrong_sha"])
def test_controlled_checks_cannot_be_replaced_by_untrusted_or_incomplete_green(tmp_path, monkeypatch, problem):
    values, _ = controlled(tmp_path, monkeypatch)
    _repo, remote, base, sha, _store, _ci, adapter, _pr = values
    assert adapter.host_policy() is None
    request = adapter._request
    def observe(method, path, **kw):
        status, payload = request(method, path, **kw)
        if "/check-runs?" in path:
            run = payload["check_runs"][0]
            if problem == "publisher": run["app"]["id"] = 999
            if problem == "pending": run["status"] = "in_progress"
            if problem == "red": run["conclusion"] = "failure"
            if problem == "missing": payload["check_runs"] = []
            if problem == "wrong_sha": run["head_sha"] = "0"*40
        return status, payload
    monkeypatch.setattr(adapter, "_request", observe)
    result = adapter.merge(7, sha)
    assert not result.merged and result.reason == "server_required_checks_not_green"
    assert git(remote, "rev-parse", "main") == base


def test_controlled_atomic_update_refuses_a_concurrent_base_advance(tmp_path, monkeypatch):
    values, _ = controlled(tmp_path, monkeypatch, intermediate=True)
    repo, remote, base, sha, _store, _ci, adapter, _pr = values
    middle = git(repo, "rev-parse", f"{sha}^")
    original = adapter.bounded_git
    def race(*args, **kwargs):
        if args[0] == "push": git(remote, "update-ref", "refs/heads/main", middle, base)
        return original(*args, **kwargs)
    monkeypatch.setattr(adapter, "bounded_git", race)
    assert not adapter.merge(7, sha).merged
    assert git(remote, "rev-parse", "main") == middle


@pytest.mark.parametrize("metadata", [
    {"permissions":{"push":False}}, {"full_name":"owner/other"}, {"archived":True}, {"disabled":True}])
def test_controlled_mode_fails_closed_on_repository_identity_or_access(tmp_path, monkeypatch, metadata):
    values, _ = controlled(tmp_path, monkeypatch)
    adapter = values[-2]
    original = adapter._request
    def request(method, path, **kw):
        code, payload = original(method, path, **kw)
        if path == "": payload.update(metadata)
        return code, payload
    monkeypatch.setattr(adapter, "_request", request)
    assert adapter.host_policy() is not None


def test_project_default_and_explicit_pr_only_override(tmp_path):
    from stagemesh.run_window import resolve_merge_defaults
    runtime = tmp_path / ".stagemesh"
    runtime.mkdir()
    (runtime / "config.json").write_text(json.dumps({"merging":{
        "mode":"stagemesh", "required_checks":["backend","web","contracts"], "check_app_id":15368}}))
    config = load_config(tmp_path)
    defaults = resolve_merge_defaults(config, build_parser().parse_args(["run-window"]))
    assert defaults["auto_merge_ff"] and defaults["merge_policy"] == "stagemesh"
    assert defaults["concurrency"] == 1 and defaults["required_checks"] == ("backend","web","contracts")
    manual = resolve_merge_defaults(config, build_parser().parse_args(["run-window", "--pr-only"]))
    assert not manual["auto_merge_ff"] and manual["concurrency"] == 2
    explicit = resolve_merge_defaults(config, build_parser().parse_args(["run-window", "--concurrency", "2"]))
    assert explicit["concurrency"] == 2  # preflight refuses, never silently reduces an explicit request
    checks = resolve_merge_defaults(config, build_parser().parse_args(["run-window", "--required-checks", "web"]))
    assert checks["required_checks"] == defaults["required_checks"]
    (tmp_path / "other").mkdir()
    fresh = resolve_merge_defaults(load_config(tmp_path / "other"), build_parser().parse_args(["run-window"]))
    assert not fresh["auto_merge_ff"] and fresh["concurrency"] == 2


@pytest.mark.parametrize("settings", [
    {"mode":"unknown"}, {"mode":"stagemesh","required_checks":["web"]},
    {"mode":"stagemesh","required_checks":[],"check_app_id":15368},
    {"mode":"stagemesh","required_checks":["web"],"check_app_id":True},
    {"mode":"stagemesh","required_checks":"web","check_app_id":15368},
    {"mode":"stagemesh","required_checks":["web"],"check_app_id":15368,"bypass":True}])
def test_invalid_project_merge_policy_is_rejected(tmp_path, settings):
    (tmp_path / ".stagemesh").mkdir()
    (tmp_path / ".stagemesh/config.json").write_text(json.dumps({"merging":settings}))
    with pytest.raises(ConfigValidationError): load_config(tmp_path)


@pytest.mark.parametrize("problem", ["validation", "review", "finding", "unbound", "self_review"])
def test_controlled_merge_preserves_local_evidence_and_blocking_finding_gates(tmp_path, monkeypatch, problem):
    from stagemesh.domain import EvidenceKind, EvidenceStatus
    values, _ = controlled(tmp_path, monkeypatch)
    repo, remote, base, sha, store, _ci, _adapter, _pr = values
    if problem == "finding":
        store.upsert_finding("F1", TASK, sha, "BLOCKING", "unresolved defect")
    elif problem in {"unbound", "self_review"}:
        from autonomy_support import contract_binding_args
        version, digest, _ = contract_binding_args()
        payload = {"contract_version":version,"contract_hash":digest,"baseline_sha":base,"candidate_sha":sha,
                   "review_provider":"codex", "implementer_provider":"codex", "independent_reviewer":False,
                   "review_execution_invoked":True}
        store.add_evidence(TASK, sha, EvidenceKind.REVIEW, EvidenceStatus.PASSED, {} if problem == "unbound" else payload)
    else:
        store.add_evidence(TASK, sha, EvidenceKind.VALIDATION if problem == "validation" else EvidenceKind.REVIEW, EvidenceStatus.FAILED)
    result = ff.make_merger(store, repo, "refs/heads/main", ("web",), policy="stagemesh", check_app_id=15368)(
        TaskRecord(TASK, pr_url="https://github.com/owner/repo/pull/7", integrated_candidate=sha), time.time()+20)
    assert not result["merged"] and git(remote, "rev-parse", "main") == base


def test_controlled_landing_records_partial_success_when_pr_head_changes_during_push(tmp_path, monkeypatch):
    values, _ = controlled(tmp_path, monkeypatch)
    repo, remote, base, sha, store, _ci, adapter, pr = values
    original = adapter.bounded_git
    def race(*args, **kw):
        result = original(*args, **kw)
        if args[0] == "push" and result.returncode == 0:
            git(remote, "update-ref", f"refs/heads/{pr.head_ref}", base)
            monkeypatch.setattr(adapter, "get", lambda number: replace(pr, head_sha=base))
        return result
    monkeypatch.setattr(adapter, "bounded_git", race)
    result = ff.make_merger(store, repo, "refs/heads/main", ("web",), policy="stagemesh", check_app_id=15368)(
        TaskRecord(TASK, pr_url="https://github.com/owner/repo/pull/7", integrated_candidate=sha), time.time()+20)
    assert not result["merged"] and result["remote_landed"] is True
    assert git(remote, "rev-parse", "main") == sha
    assert git(repo, "rev-parse", "main") == base  # no automatic continuation or repeat push


def test_failed_evidence_arriving_during_final_observation_prevents_push(tmp_path, monkeypatch):
    from stagemesh.domain import EvidenceKind, EvidenceStatus
    values, _ = controlled(tmp_path, monkeypatch)
    _repo, remote, base, sha, store, _ci, adapter, pr = values
    adapter.task_id = TASK
    original = adapter.remote_tip
    observations = 0
    def observe(ref, **kw):
        nonlocal observations
        result = original(ref, **kw)
        if ref == f"refs/heads/{pr.head_ref}":
            observations += 1
            if observations == 2:
                store.add_evidence(TASK, sha, EvidenceKind.REVIEW, EvidenceStatus.FAILED)
        return result
    monkeypatch.setattr(adapter, "remote_tip", observe)
    result = adapter.merge(7, sha)
    assert result.reason == "latest_validation_or_review_not_passed"
    assert git(remote, "rev-parse", "main") == base


def test_automatic_mode_cannot_disable_independent_review(tmp_path):
    (tmp_path / ".stagemesh").mkdir()
    (tmp_path / ".stagemesh/config.json").write_text(json.dumps({
        "routing":{"require_independent_review":False},
        "merging":{"mode":"stagemesh","required_checks":["web"],"check_app_id":15368}}))
    with pytest.raises(ConfigValidationError, match="independent"):
        load_config(tmp_path)


def test_policy_changes_preserve_pending_landing_but_allow_completed_history(tmp_path, monkeypatch):
    values, _ = controlled(tmp_path, monkeypatch)
    repo, _remote, base, sha, store, _ci, _adapter, _pr = values
    merger = ff.make_merger(store, repo, "refs/heads/main", ("web",), policy="stagemesh", check_app_id=15368)
    payload = {"repository":"owner/repo", "base_ref":"refs/heads/main", "task_id":TASK,
               "candidate_sha":sha, "base_sha":base, "pr":7, "merge_policy":"hosted"}
    store.add_audit_event("window.fast_forward_push_intent", payload)
    with pytest.raises(RuntimeError, match="different merge policy"):
        merger.pending_records()
    store.add_audit_event("window.fast_forward_merged", payload)
    assert merger.pending_records() == []
