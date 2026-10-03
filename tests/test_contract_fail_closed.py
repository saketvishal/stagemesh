from __future__ import annotations

import json
import sys
from pathlib import Path

from stagemesh.contract_binding import bind_task_contract, contract_for_candidate
from stagemesh.contracts import ChangeContract
from stagemesh.coordinator import Coordinator
from stagemesh.domain import EvidenceKind, EvidenceStatus, ExecutionKind, Stage
from stagemesh.execution import SubprocessExecutor
from stagemesh.git import GitWorkspace
from stagemesh.integration import Integrator
from stagemesh.persistence import Store
from stagemesh.review import Reviewer
from stagemesh.validation import Validator
from stagemesh.validation_plan import (
    CORE_LIFECYCLE_OR_SCHEMA_SECURITY,
    DOCS_ONLY,
    derive_validation_plan,
)

TASK = "TASK-1"
PASS_CMD = [sys.executable, "-c", "pass"]


def _repo(tmp_path: Path, files: dict[str, str]) -> tuple[Path, GitWorkspace]:
    project = tmp_path / "repo"
    project.mkdir()
    workspace = GitWorkspace(project)
    workspace.init_if_needed()
    workspace.run("config", "user.email", "test@example.invalid")
    workspace.run("config", "user.name", "StageMesh Test")
    for name, text in files.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    workspace.commit_all("base")
    return project, workspace


def _write_contract(project: Path, contract: dict[str, object]) -> None:
    (project / ".stagemesh" / "contracts").mkdir(parents=True, exist_ok=True)
    (project / ".stagemesh" / "contracts" / f"{TASK}.json").write_text(json.dumps(contract), encoding="utf-8")


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    store.upsert_task("fail closed", source_id=TASK)
    return store


def _validate(tmp_path: Path, project: Path, workspace: GitWorkspace, contract: dict[str, object], path: str):
    _write_contract(project, contract)
    store = _store(tmp_path)
    base = workspace.head()
    (project / path).write_text("changed\n", encoding="utf-8")
    sha = workspace.commit_all("candidate")
    store.set_task_baseline(TASK, base)
    store.add_candidate(TASK, sha, "codex", True)
    status = Validator().validate(store, TASK, sha, project)
    row = store.conn.execute(
        "SELECT payload FROM evidence WHERE candidate_sha=? AND kind=?", (sha, EvidenceKind.VALIDATION)
    ).fetchone()
    return store, sha, status, json.loads(row["payload"])


def _codes(payload: dict[str, object]) -> set[str]:
    return {item["code"] for item in payload["findings"]}


def test_missing_contract_blocks_provider_before_implementation(tmp_path: Path) -> None:
    project, _ = _repo(tmp_path, {"src/app.py": "X = 1\n"})
    store = _store(tmp_path)
    store.advance_task(TASK, Stage.IMPLEMENT)
    marker = tmp_path / "invoked.marker"
    script = f"from pathlib import Path\nPath({str(marker)!r}).write_text('x')\n"
    executor = SubprocessExecutor([sys.executable, "-c", script], name="codex")

    assert Coordinator(store, project, executor=executor).tick() == 0

    assert not marker.exists()
    assert store.latest_candidate(TASK) is None
    assert store.get_task(TASK)["stage"] == Stage.IMPLEMENT
    assert store.task_contract(TASK) is None
    assert not store.conn.execute(
        "SELECT 1 FROM executions WHERE kind=?", (ExecutionKind.IMPLEMENTATION,)
    ).fetchall()
    event = json.loads(store.audit_events()[0]["payload"])
    assert event["reason"].startswith("missing_explicit_contract")


def test_implicit_or_malformed_contract_blocks_provider(tmp_path: Path) -> None:
    project, _ = _repo(tmp_path, {"src/app.py": "X = 1\n"})
    store = _store(tmp_path)
    executor = SubprocessExecutor([sys.executable, "-c", "raise SystemExit(5)"], name="codex")
    for contract, reason in (
        ({"objective": "x", "explicit": False}, "missing_explicit_contract"),
        ({"allowed_files": ["**"]}, "invalid_contract"),
    ):
        _write_contract(project, contract)
        result = executor.run(store, TASK, None, project)
        assert result.failure_reason is not None and result.failure_reason.startswith(reason)
        assert store.task_contract(TASK) is None


def test_placeholder_plan_without_executable_gate_fails(tmp_path: Path) -> None:
    project, workspace = _repo(tmp_path, {"src/app.py": "X = 1\n"})
    store, _, status, payload = _validate(
        tmp_path, project, workspace, {"objective": "localized", "allowed_files": ["src/**"]}, "src/app.py"
    )

    assert status is EvidenceStatus.FAILED
    assert payload["gates"] == []
    assert payload["validation_checks"]["planned"] == ["focused-tests"]
    assert payload["validation_checks"]["executed"] == []
    assert payload["validation_checks"]["missing"] == ["focused-tests"]
    assert "planned_check_missing_command" in _codes(payload)
    store.close()


def test_acceptance_criteria_without_executable_validation_fails(tmp_path: Path) -> None:
    project, workspace = _repo(tmp_path, {"src/app.py": "X = 1\n"})
    store, _, status, payload = _validate(
        tmp_path,
        project,
        workspace,
        {"objective": "localized", "allowed_files": ["src/**"], "acceptance_criteria": ["X is 2"]},
        "src/app.py",
    )

    assert status is EvidenceStatus.FAILED
    assert "acceptance_criteria_without_executable_gate" in _codes(payload)
    store.close()


def test_declared_docs_only_cannot_hide_core_change() -> None:
    contract = ChangeContract(
        objective="docs", explicit=True, validation_classification=DOCS_ONLY, allowed_files=("**",)
    )

    plan = derive_validation_plan(contract, ("src/stagemesh/coordinator.py",))

    assert plan.classification == CORE_LIFECYCLE_OR_SCHEMA_SECURITY
    assert plan.risk_level == "HIGH"
    assert any("raised to CORE_LIFECYCLE_OR_SCHEMA_SECURITY" in reason for reason in plan.escalation_reasons)


def test_valid_localized_contract_executes_focused_gates_and_passes(tmp_path: Path) -> None:
    project, workspace = _repo(tmp_path, {"src/widgets/card.py": "X = 1\n"})
    store, _, status, payload = _validate(
        tmp_path,
        project,
        workspace,
        {
            "objective": "localized",
            "allowed_files": ["src/widgets/**", ".stagemesh/**"],
            "acceptance_criteria": ["card changes"],
            "required_tests": [{"name": "focused-card-tests", "command": PASS_CMD}],
            "lint": [{"name": "focused-lint", "command": PASS_CMD}],
        },
        "src/widgets/card.py",
    )

    assert status is EvidenceStatus.PASSED
    assert payload["validation_checks"]["missing"] == []
    assert payload["validation_checks"]["executed"] == ["focused-card-tests", "focused-lint"]
    assert [gate["status"] for gate in payload["gates"]] == ["PASSED", "PASSED"]
    store.close()


def test_bound_contract_cannot_drift_after_implementation_starts(tmp_path: Path) -> None:
    project, workspace = _repo(tmp_path, {"src/app.py": "X = 1\n"})
    original = {
        "objective": "original",
        "allowed_files": ["src/**", ".stagemesh/**"],
        "required_tests": [{"name": "focused-tests", "command": PASS_CMD}],
    }
    _write_contract(project, original)
    store = _store(tmp_path)
    base = workspace.head()
    store.set_task_baseline(TASK, base)
    bound = bind_task_contract(store, project, TASK, base)

    _write_contract(project, {"objective": "mutated", "allowed_files": ["docs/**"], "forbidden_files": ["src/**"]})
    (project / "src" / "app.py").write_text("X = 2\n", encoding="utf-8")
    sha = workspace.commit_all("candidate")
    store.add_candidate(TASK, sha, "codex", True)

    assert contract_for_candidate(store, TASK, sha, project).digest == bound.digest
    assert Validator().validate(store, TASK, sha, project) is EvidenceStatus.PASSED
    assert Reviewer(provider_name="reviewer").review(store, TASK, sha, project) is EvidenceStatus.PASSED
    assert Integrator().integrate(store, TASK, sha, project) is EvidenceStatus.PASSED
    rows = store.conn.execute("SELECT payload FROM evidence WHERE candidate_sha=?", (sha,)).fetchall()
    payloads = [json.loads(row["payload"]) for row in rows]
    assert {payload["contract_hash"] for payload in payloads} == {bound.digest}
    assert {payload["baseline_sha"] for payload in payloads} == {base}
    store.close()
