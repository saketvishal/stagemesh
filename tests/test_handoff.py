"""`stagemesh handoff export`: schema, read-only behaviour, output safety and redaction.

The package is meant to be pasted into a review, so the tests plant recognisable secrets in every place the package reads from (task
titles, findings, gate commands and environments, audit events, the git remote, the process environment) and assert that none of them
survives, and that exporting never changes task, queue or git state.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest
from test_bounded_execution import TASK
from test_bounded_execution import _setup as _base_setup

import stagemesh.cli as cli_module
from stagemesh.contract_binding import bind_task_contract
from stagemesh.domain import EvidenceKind, EvidenceStatus, ExecutionKind, Stage, TaskStatus
from stagemesh.git import GitWorkspace
from stagemesh.handoff import (
    HANDOFF_SCHEMA,
    MAX_TEXT,
    TOP_LEVEL_KEYS,
    HandoffError,
    build_handoff,
    sanitize,
    validate_handoff,
    write_handoff,
)
from stagemesh.persistence import Store

PY = sys.executable

GH_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
PASSWORD = "hunter2-correct-horse"
ENV_SECRET = "env-secret-value-93f1c2"
URL_CREDENTIAL = "s3cr3t-url-pass"
GATE_ENV_VALUE = "gate-env-value-77aa"

CONTRACT = {
    "objective": "docs only",
    "allowed_files": ["docs/**"],
    "forbidden_files": ["src/secret/**"],
    "protected_files": ["README.md"],
    "acceptance_criteria": ["docs updated"],
    "max_changed_files": 3,
    "required_tests": [
        {
            "name": "smoke",
            "command": [PY, "-c", "pass", "--api-key", GH_TOKEN],
            "env": {"API_TOKEN": GATE_ENV_VALUE, "DB_URL": f"postgres://u:{URL_CREDENTIAL}@db/x"},
        }
    ],
}


def _setup(tmp_path: Path):
    project, store = _base_setup(tmp_path, CONTRACT)
    exclude = project / ".git" / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_text(".stagemesh/\n", encoding="utf-8")
    return project, store


def with_candidate(tmp_path: Path):
    """A task with a frozen contract, a committed candidate and recorded (failed) validation evidence."""
    project, store = _setup(tmp_path)
    git = GitWorkspace(project)
    base = store.set_task_baseline(TASK, git.head())
    bind_task_contract(store, project, TASK, base)
    (project / "docs" / "a.md").write_text("candidate\n", encoding="utf-8")
    sha = git.commit_all("candidate")
    store.add_candidate(TASK, sha, "provider", durable_handoff=True)
    store.advance_task(TASK, Stage.VALIDATE)
    store.add_evidence(
        TASK,
        sha,
        EvidenceKind.VALIDATION,
        EvidenceStatus.FAILED,
        {
            "contract_hash": "abc123",
            "changed_files": ["docs/a.md"],
            "validation_checks": {"planned": ["smoke"], "executed": ["smoke"], "missing": []},
            "gates": [{"name": "smoke", "status": "FAILED", "command": [PY, "-c", "pass", "--token", PASSWORD], "returncode": 1, "stdout": "RAW-GATE-OUTPUT"}],
            "findings": [{"code": "gate_failed", "severity": "error", "message": f"smoke failed with password={PASSWORD}", "path": "docs/a.md"}],
        },
    )
    store.upsert_finding("f1", TASK, sha, "error", f"smoke failed with password={PASSWORD}")
    return project, store, sha


def run_cli(project: Path, *argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_module.main(["--project", str(project), *argv])
    return code, out.getvalue(), err.getvalue()


def snapshot(project: Path, store: Store) -> dict[str, object]:
    tables = ("tasks", "claims", "executions", "candidates", "evidence", "findings", "audit_events", "work_packets", "retry_state", "task_contracts")
    return {
        "rows": {t: [tuple(r) for r in store.conn.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in tables},
        "head": GitWorkspace(project).head(),
        "status": GitWorkspace(project).run("status", "--porcelain").stdout,
    }


# --- schema --------------------------------------------------------------------------------------------------------------


def test_document_matches_the_schema_and_carries_every_requested_section(tmp_path: Path) -> None:
    project, _store, sha = with_candidate(tmp_path)
    doc = build_handoff(project, now=1_700_000_000.0)

    assert validate_handoff(doc) == []
    assert set(doc) == set(TOP_LEVEL_KEYS)
    assert doc["schema"] == HANDOFF_SCHEMA
    assert doc["generated_at"] == "2023-11-14T22:13:20+00:00"
    assert doc["git"]["sha"] == sha and doc["git"]["branch"] and doc["git"]["is_repository"] is True
    task = doc["tasks"][0]
    assert (task["id"], task["stage"], task["status"]) == (TASK, "VALIDATE", "OPEN")
    assert task["latest_candidate"]["sha"] == sha and task["latest_validation"]["status"] == "FAILED"
    assert task["open_findings"][0]["severity"] == "error"
    assert doc["queue_control"]["backlog_state"] == "ACTIVE" and doc["queue_control"]["ready_task_ids"] == [TASK]
    assert doc["tests"][0]["status"] == "FAILED" and doc["tests"][0]["gates"][0]["returncode"] == 1
    assert doc["tests"][0]["checks"]["executed"] == ["smoke"]
    assert doc["changed_files"]["by_task"] == {TASK: ["docs/a.md"]}
    scope = doc["contract_scope"][0]
    assert scope["allowed_files"] == ["docs/**"] and scope["forbidden_files"] == ["src/secret/**"] and scope["max_changed_files"] == 3
    assert scope["source"] == "frozen" and scope["digest"]
    assert doc["next_action"]["command"] == "stagemesh continue" and doc["next_action"]["advisory"] is True
    assert json.loads(json.dumps(doc)) == doc  # plain JSON all the way down


def test_project_without_a_store_or_git_still_exports_a_valid_document(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    doc = build_handoff(bare)

    assert validate_handoff(doc) == []
    assert doc["git"]["is_repository"] is False and doc["git"]["sha"] is None
    assert doc["tasks"] == [] and doc["queue_control"]["backlog_state"] == "UNKNOWN"
    assert any("no StageMesh store" in w for w in doc["warnings"]) and any("not a git repository" in w for w in doc["warnings"])
    assert not (bare / ".stagemesh").exists()  # an export must not create runtime state


def test_validator_names_every_structural_problem() -> None:
    assert validate_handoff([]) == ["handoff must be a JSON object"]
    problems = validate_handoff({"schema": "other/9", "tasks": {}, "git": []})
    text = "\n".join(problems)
    assert "top-level keys differ" in text and "schema must be" in text and "tasks must be a list" in text and "git must be an object" in text


def test_validator_flags_a_task_entry_missing_fields(tmp_path: Path) -> None:
    project, _store, _sha = with_candidate(tmp_path)
    doc = build_handoff(project)
    del doc["tasks"][0]["latest_candidate"]
    assert validate_handoff(doc) == ["tasks[0] is missing ['latest_candidate']"]


def test_next_action_prefers_recovery_then_blocked_then_ready(tmp_path: Path) -> None:
    project, store, _sha = with_candidate(tmp_path)
    store.block_task(TASK)
    action = build_handoff(project)["next_action"]
    assert action["command"] == f"stagemesh task-doctor --task {TASK}"

    claim = store.acquire_claim(TASK, "worker-1")
    store.start_execution(task_id=TASK, claim_id=claim, kind=ExecutionKind.IMPLEMENTATION, pid=2_000_000_000, process_create_time=1.0, boot_id="boot", executable="python")
    doc = build_handoff(project)
    assert [e["process_state"] for e in doc["active_executions"]] == ["DEAD"]
    assert doc["next_action"]["command"] == f"stagemesh recover-stale --task {TASK}"


def test_done_project_suggests_review_not_more_work(tmp_path: Path) -> None:
    project, store, _sha = with_candidate(tmp_path)
    store.conn.execute("UPDATE tasks SET status=?, stage=?", (TaskStatus.DONE, Stage.DONE))
    store.conn.commit()
    action = build_handoff(project)["next_action"]
    assert action["command"] is None and "review the branch diff" in action["reason"]


# --- read-only -----------------------------------------------------------------------------------------------------------


def test_export_changes_no_task_queue_or_git_state(tmp_path: Path) -> None:
    project, store, _sha = with_candidate(tmp_path)
    store.acquire_claim(TASK, "worker-1")
    store.enqueue_work(TASK, "VALIDATE", None, None, {"note": "x"})
    store.upsert_retry_state("k", 1, 5.0, "provider capacity")
    before = snapshot(project, store)

    code, out, _err = run_cli(project, "handoff", "export", "--json")

    assert code == 0 and json.loads(out)["path"].startswith(".stagemesh/handoff/")
    assert snapshot(project, store) == before
    assert not list(project.glob("**/*.tmp"))


def test_queue_control_reports_claims_packets_retries_and_dirty_paths(tmp_path: Path) -> None:
    project, store, _sha = with_candidate(tmp_path)
    store.acquire_claim(TASK, "worker-1")
    store.enqueue_work(TASK, "VALIDATE", None, None, {})
    store.upsert_retry_state("provider:codex", 2, 4_000_000_000.0, "capacity")
    (project / "docs" / "dirty.md").write_text("wip\n", encoding="utf-8")

    doc = build_handoff(project)
    queue = doc["queue_control"]

    assert queue["claimed_task_ids"] == [TASK] and queue["ready_task_ids"] == []
    assert sum(queue["work_packets"]["by_status"].values()) == 1
    assert queue["retry_state"][0]["key"] == "provider:codex" and queue["retry_state"][0]["attempts"] == 2
    assert queue["dirty_working_tree"] == ["docs/dirty.md"] and doc["changed_files"]["uncommitted"] == ["docs/dirty.md"]
    assert doc["git"]["dirty"] is True


# --- output safety -------------------------------------------------------------------------------------------------------


def test_default_output_is_a_timestamped_file_under_the_runtime_dir(tmp_path: Path) -> None:
    project, _store, _sha = with_candidate(tmp_path)
    code, out, _err = run_cli(project, "handoff", "export")
    assert code == 0 and out.startswith("wrote handoff package to .stagemesh/handoff/")
    (written,) = (project / ".stagemesh" / "handoff").glob("*.json")
    assert validate_handoff(json.loads(written.read_text(encoding="utf-8"))) == []


def test_out_must_stay_inside_the_project_and_outside_git(tmp_path: Path) -> None:
    project, _store, _sha = with_candidate(tmp_path)
    outside = tmp_path / "elsewhere.json"
    for target in (outside, Path("..") / "escape.json", Path(".git") / "handoff.json"):
        code, _out, err = run_cli(project, "handoff", "export", "--out", str(target))
        assert code == 2 and "handoff error" in err
    assert not outside.exists() and not (tmp_path / "escape.json").exists() and not (project / ".git" / "handoff.json").exists()


def test_existing_output_is_never_overwritten_without_force(tmp_path: Path) -> None:
    project, _store, _sha = with_candidate(tmp_path)
    target = project / ".stagemesh" / "handoff" / "mine.json"
    target.parent.mkdir(parents=True)
    target.write_text("precious", encoding="utf-8")

    code, _out, err = run_cli(project, "handoff", "export", "--out", ".stagemesh/handoff/mine.json")
    assert code == 2 and "already exists" in err and target.read_text(encoding="utf-8") == "precious"

    code, _out, _err = run_cli(project, "handoff", "export", "--out", ".stagemesh/handoff/mine.json", "--force")
    assert code == 0 and json.loads(target.read_text(encoding="utf-8"))["schema"] == HANDOFF_SCHEMA


def test_writing_a_directory_target_is_refused(tmp_path: Path) -> None:
    project, _store, _sha = with_candidate(tmp_path)
    with pytest.raises(HandoffError, match="is a directory"):
        write_handoff(build_handoff(project), project, project / "docs")


# --- redaction -----------------------------------------------------------------------------------------------------------


def test_no_planted_secret_survives_anywhere_in_the_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEPLOY_API_TOKEN", ENV_SECRET)
    project, store, sha = with_candidate(tmp_path)
    store.upsert_task(f"rotate key {GH_TOKEN}", source_id="TASK-2")
    GitWorkspace(project).run("remote", "add", "origin", f"https://deploy:{URL_CREDENTIAL}@github.com/acme/repo.git")
    store.add_audit_event("task.implementation_unsuccessful", {"reason": f"provider said Authorization: Bearer {GH_TOKEN} and {ENV_SECRET}", "api_token": "raw-token-field"})
    store.add_external_evidence("CI", "PASSED", f"https://ci:{URL_CREDENTIAL}@ci.example/run/1", sha, "notes")

    path = write_handoff(build_handoff(project), project, None)
    raw = path.read_text(encoding="utf-8")

    for secret in (GH_TOKEN, PASSWORD, ENV_SECRET, URL_CREDENTIAL, GATE_ENV_VALUE, "raw-token-field", "RAW-GATE-OUTPUT"):
        assert secret not in raw, secret
    assert str(project) not in raw and project.as_posix() not in raw
    assert str(Path.home()) not in raw and Path.home().as_posix() not in raw
    doc = json.loads(raw)
    assert doc["git"]["remote_origin"] == "https://***REDACTED***@github.com/acme/repo.git"
    gate = doc["tests"][0]["gates"][0]
    assert gate["command"].endswith("--token '***REDACTED***'") and gate["returncode"] == 1
    scope_gate = doc["contract_scope"][0]["gates"][0]
    assert "***REDACTED***" in scope_gate["command"] and "env" not in scope_gate
    assert doc["tests"][0]["findings"][0]["message"] == "smoke failed with password=***REDACTED***"
    assert "rotate key ***REDACTED***" in [t["title"] for t in doc["tasks"]]


def test_package_states_what_it_leaves_out() -> None:
    doc = build_handoff(Path(__file__).parent)
    omitted = " ".join(doc["redaction"]["omitted"])
    for phrase in ("gate environment", "stdout and stderr", "prompts", "tokens", "absolute project path"):
        assert phrase in omitted
    assert doc["redaction"]["applied"] is True


@pytest.mark.parametrize(
    "text, leaked",
    [
        ("token=abc12345", "abc12345"),
        ("export GITHUB_TOKEN: abc12345xyz", "abc12345xyz"),
        ("password = 'p@ss word'", "p@ss word"),
        ("Authorization: Bearer abcdefghijklmno", "abcdefghijklmno"),
        ("clone https://user:pw12345@host/x.git", "pw12345"),
        ("key sk-abcdefghijklmnopqrstuvwx", "sk-abcdefghijklmnopqrstuvwx"),
        ("aws AKIAABCDEFGHIJKLMNOP", "AKIAABCDEFGHIJKLMNOP"),
        ("slack xoxb-1234567890-abcdef", "xoxb-1234567890-abcdef"),
        ("jwt eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4f", "SflKxwRJSMeKKF2QT4f"),
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJB\n-----END RSA PRIVATE KEY-----", "MIIBOgIBAAJB"),
    ],
)
def test_sanitize_redacts_secret_shapes_in_free_text(tmp_path: Path, text: str, leaked: str) -> None:
    cleaned = sanitize({"message": text}, tmp_path, secrets=[])
    assert leaked not in cleaned["message"] and "***REDACTED***" in cleaned["message"]


def test_sanitize_redacts_secret_keys_recursively_and_bounds_text(tmp_path: Path) -> None:
    cleaned = sanitize({"outer": [{"api_key": "k", "keep": "x" * (MAX_TEXT + 50)}], "Password": "p"}, tmp_path, secrets=["literal-secret"])
    assert cleaned["Password"] == "***REDACTED***" and cleaned["outer"][0]["api_key"] == "***REDACTED***"
    kept = cleaned["outer"][0]["keep"]
    assert kept.startswith("x" * MAX_TEXT) and kept.endswith("[truncated 50 chars]")
    assert sanitize("a literal-secret b", tmp_path, secrets=["literal-secret"]) == "a ***REDACTED*** b"


def test_sanitize_leaves_ordinary_text_alone(tmp_path: Path) -> None:
    ordinary = {"reason": "validation failed: docs/a.md is outside the allowed files", "n": 3, "ok": True, "none": None}
    assert sanitize(ordinary, tmp_path, secrets=[]) == ordinary
