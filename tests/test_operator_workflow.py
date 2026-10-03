from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import stagemesh.cli as cli_module
from stagemesh.contracts import is_noise_path
from stagemesh.domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus, Stage, TaskStatus
from stagemesh.git import GitWorkspace
from stagemesh.observability import health
from stagemesh.operator_actions import OperatorActionError, adopt_candidate, recover_stale, task_details

from test_bounded_execution import SLEEPER, TASK, _running_execution, _setup


def _commit(project: Path, files: dict[str, str], message: str = "change") -> str:
    for name, text in files.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git = GitWorkspace(project)
    git.run("add", "--", *files)  # never sweep in the runtime database
    git.run("commit", "-m", message)
    return git.head()


def _with_baseline(tmp_path: Path) -> tuple[Path, object, str]:
    project, store = _setup(
        tmp_path,
        {
            "objective": "docs only",
            "allowed_files": ["docs/**"],
            "required_tests": [{"name": "docs-static", "command": [sys.executable, "-c", "pass"]}],
        },
    )
    base = GitWorkspace(project).head()
    store.set_task_baseline(TASK, base)
    return project, store, base


def _cli(project: Path, *argv: str) -> tuple[int, str]:
    import contextlib
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli_module.main(["--project", str(project), *argv])
    return code, out.getvalue()


# --- status --json -----------------------------------------------------------------------------


def test_status_json_exposes_candidate_evidence_claim_and_execution(tmp_path: Path) -> None:
    project, store, _ = _with_baseline(tmp_path)
    sha = _commit(project, {"docs/a.md": "new\n"})
    store.add_candidate(TASK, sha, "manual-reconciliation", True)
    store.add_evidence(TASK, sha, EvidenceKind.VALIDATION, EvidenceStatus.PASSED, {})
    store.add_evidence(TASK, sha, EvidenceKind.REVIEW, EvidenceStatus.FAILED, {})
    proc = subprocess.Popen(SLEEPER)
    try:
        execution_id = _running_execution(store, proc)
        store.conn.execute("UPDATE claims SET lease_expires_at=lease_expires_at+1000 WHERE task_id=?", (TASK,))
        store.conn.commit()
        store.close()
        code, out = _cli(project, "status", "--json")
    finally:
        proc.kill()
        proc.wait()
    assert code == 0
    task = next(t for t in json.loads(out)["tasks"] if t["id"] == TASK)
    assert task["latest_candidate"]["sha"] == sha and task["latest_candidate"]["producer"] == "manual-reconciliation"
    assert task["latest_validation"]["status"] == "PASSED"
    assert task["latest_review"]["status"] == "FAILED"
    claim = task["active_claim"]
    assert claim["stage"] == Stage.IMPLEMENT and claim["pid"] == proc.pid
    assert claim["age_seconds"] >= 0 and claim["lease_expires_at"] > 0
    (execution,) = task["active_executions"]
    assert execution["id"] == execution_id and execution["kind"] == ExecutionKind.IMPLEMENTATION
    assert execution["status"] == ExecutionStatus.RUNNING and execution["pid"] == proc.pid
    assert execution["process_state"] == "LIVE"


def test_status_json_for_task_without_candidate_has_null_details(tmp_path: Path) -> None:
    project, store, _ = _with_baseline(tmp_path)
    details = task_details(store, TASK)
    assert details["latest_candidate"] is None and details["active_claim"] is None
    assert details["active_executions"] == []


# --- recover-stale -----------------------------------------------------------------------------


def test_recover_stale_releases_dead_process_and_records_audit(tmp_path: Path) -> None:
    project, store, _ = _with_baseline(tmp_path)
    proc = subprocess.Popen(SLEEPER)
    execution_id = _running_execution(store, proc)
    proc.kill()
    proc.wait()
    store.close()

    code, out = _cli(project, "recover-stale", "--task", TASK, "--json")

    assert code == 0
    payload = json.loads(out)
    assert payload["released"] == 1 and payload["actions"][0]["action"] == "RELEASED"
    assert (payload["stage"], payload["status"]) == (Stage.IMPLEMENT, TaskStatus.OPEN)
    from stagemesh.persistence import Store

    reopened = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    assert reopened.conn.execute("SELECT status FROM executions WHERE id=?", (execution_id,)).fetchone()[0] == "FAILED"
    assert reopened.conn.execute("SELECT COUNT(*) FROM claims WHERE active=1").fetchone()[0] == 0
    assert reopened.conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE event_type='recovery.stale_claim_released'"
    ).fetchone()[0] == 1


def test_recover_stale_never_releases_live_process(tmp_path: Path) -> None:
    project, store, _ = _with_baseline(tmp_path)
    proc = subprocess.Popen(SLEEPER)
    try:
        execution_id = _running_execution(store, proc)
        (action,) = recover_stale(store, TASK)
        assert action.action == "SKIPPED_LIVE"
        assert store.conn.execute("SELECT status FROM executions WHERE id=?", (execution_id,)).fetchone()[0] == "RUNNING"
        assert store.get_task(TASK)["status"] == TaskStatus.CLAIMED
    finally:
        proc.kill()
        proc.wait()


def test_recover_stale_skips_unknown_identity_and_rejects_missing_task(tmp_path: Path) -> None:
    project, store, _ = _with_baseline(tmp_path)
    _running_execution(store, None)  # no pid recorded: liveness cannot be proven dead
    (action,) = recover_stale(store, TASK)
    assert action.action == "SKIPPED_UNKNOWN"
    with pytest.raises(OperatorActionError):
        recover_stale(store, "nope")


# --- adopt-candidate ---------------------------------------------------------------------------


def test_adopt_candidate_registers_audits_and_validates_to_review(tmp_path: Path) -> None:
    project, store, base = _with_baseline(tmp_path)
    sha = _commit(project, {"docs/a.md": "adopted\n"})
    store.close()

    code, out = _cli(project, "adopt-candidate", "--task", TASK, "--sha", sha[:10], "--validate", "--json")

    assert code == 0, out
    report = json.loads(out)
    assert report["candidate_sha"] == sha and report["baseline_sha"] == base
    assert report["validation"] == "PASSED"
    assert (report["stage"], report["status"]) == (Stage.REVIEW, TaskStatus.OPEN)
    from stagemesh.persistence import Store

    reopened = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    candidate = reopened.latest_candidate(TASK)
    assert candidate["sha"] == sha and candidate["durable_handoff"] == 1
    assert candidate["produced_by"] == "manual-reconciliation"
    assert reopened.conn.execute("SELECT COUNT(*) FROM audit_events WHERE event_type='candidate.adopted'").fetchone()[0] == 1


def test_adopt_candidate_without_validate_stops_at_validate_stage(tmp_path: Path) -> None:
    project, store, _ = _with_baseline(tmp_path)
    sha = _commit(project, {"docs/a.md": "adopted\n"})
    report = adopt_candidate(store, project, TASK, sha, "manual-reconciliation")
    assert report["validation"] is None and report["stage"] == Stage.VALIDATE


def test_adopt_candidate_rejects_unknown_sha_unrelated_history_and_baseline(tmp_path: Path) -> None:
    project, store, base = _with_baseline(tmp_path)
    with pytest.raises(OperatorActionError, match="does not exist"):
        adopt_candidate(store, project, TASK, "0" * 40, "manual-reconciliation")
    with pytest.raises(OperatorActionError, match="equals the task baseline"):
        adopt_candidate(store, project, TASK, base, "manual-reconciliation")
    git = GitWorkspace(project)
    git.run("checkout", "--orphan", "unrelated")
    git.run("rm", "-rf", "--cached", ".", "--quiet")
    other = _commit(project, {"docs/x.md": "x\n"}, "unrelated root")
    with pytest.raises(OperatorActionError, match="not based on task baseline"):
        adopt_candidate(store, project, TASK, other, "manual-reconciliation")
    assert store.latest_candidate(TASK) is None


def test_adopt_candidate_rejects_cache_noise(tmp_path: Path) -> None:
    project, store, _ = _with_baseline(tmp_path)
    sha = _commit(
        project,
        {"docs/a.md": "ok\n", "apps/web-v2/.npm-cache/_update-notifier-last-checked": ""},
    )
    with pytest.raises(OperatorActionError, match=r"\.npm-cache"):
        adopt_candidate(store, project, TASK, sha, "manual-reconciliation")
    assert store.latest_candidate(TASK) is None


# --- candidate hygiene -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "noisy"),
    [
        ("apps/web-v2/.npm-cache/_update-notifier-last-checked", True),
        (".npm-cache/x/y", True),
        ("pkg/node_modules/a/index.js", True),
        ("src/__pycache__/m.cpython-312.pyc", True),
        ("src/m.pyc", True),
        ("docs/.DS_Store", True),
        ("docs/a.md", False),
        ("src/cache.py", False),
        ("docs/npm-cache.md", False),
    ],
)
def test_noise_path_classification(path: str, noisy: bool) -> None:
    assert is_noise_path(path) is noisy


def test_validation_fails_candidate_with_noise_file_even_if_in_scope(tmp_path: Path) -> None:
    from stagemesh.validation import Validator

    project, store = _setup(
        tmp_path,
        {
            "objective": "docs",
            "allowed_files": ["docs/**", "**/.npm-cache/**"],
            "required_tests": [{"name": "docs-static", "command": [sys.executable, "-c", "pass"]}],
        },
    )
    store.set_task_baseline(TASK, GitWorkspace(project).head())
    sha = _commit(project, {"docs/a.md": "ok\n", "apps/w/.npm-cache/_update-notifier-last-checked": ""})
    store.add_candidate(TASK, sha, "codex", True)

    assert Validator().validate(store, TASK, sha, project) == EvidenceStatus.FAILED
    payload = json.loads(store.conn.execute("SELECT payload FROM evidence WHERE kind='VALIDATION'").fetchone()[0])
    assert any(f["code"] == "candidate_noise_file" for f in payload["findings"])


# --- health semantics --------------------------------------------------------------------------


def test_health_ignores_superseded_historical_failures_but_labels_them(tmp_path: Path) -> None:
    project, store, _ = _with_baseline(tmp_path)
    failed = store.start_execution(task_id=TASK, claim_id=None, kind=ExecutionKind.VALIDATION)
    store.finish_execution(failed, ExecutionStatus.FAILED)
    current = health(store)
    assert current.ok is False and current.current_failed_execution_count == 1
    assert "current_failed_executions" in current.current_problems

    later = store.start_execution(task_id=TASK, claim_id=None, kind=ExecutionKind.VALIDATION)
    store.finish_execution(later, ExecutionStatus.SUCCEEDED)
    report = health(store)
    assert report.ok is True
    assert report.failed_execution_count == 1 and report.historical_failed_execution_count == 1
    assert report.current_failed_execution_count == 0

    store.close()
    code, out = _cli(project, "health", "--json")
    data = json.loads(out)
    assert code == 0 and data["ok"] is True and data["ok_scope"] == "current"
    assert data["historical_failed_execution_count"] == 1 and data["current_problems"] == []


def test_health_flags_stale_running_execution(tmp_path: Path) -> None:
    project, store, _ = _with_baseline(tmp_path)
    proc = subprocess.Popen(SLEEPER)
    _running_execution(store, proc)
    proc.kill()
    proc.wait()
    report = health(store)
    assert report.ok is False and report.stale_execution_count == 1
    assert "stale_running_executions" in report.current_problems
