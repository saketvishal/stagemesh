from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

import stagemesh.auto_plan as auto_plan_module
import stagemesh.cli as cli_module
from stagemesh.contracts import canonical_contract_json, parse_contract
from stagemesh.domain import EvidenceKind, EvidenceStatus
from stagemesh.git import GitWorkspace
from stagemesh.persistence import Store

GATE = {"name": "project-acceptance-fake", "command": [sys.executable, "-c", "pass"], "timeout_seconds": 60}


def _project(tmp_path: Path, description: str | None = "Add the widget endpoint.") -> Path:
    project = tmp_path / "repo"
    project.mkdir()
    git = GitWorkspace(project)
    git.init_if_needed()
    git.run("config", "user.email", "t@example.invalid")
    git.run("config", "user.name", "T")
    (project / "README.md").write_text("base\n", encoding="utf-8")
    git.commit_all("base")
    runtime = project / ".stagemesh"
    (runtime / "contracts").mkdir(parents=True)
    task = {"id": "T-1", "title": "Widget endpoint", "eligible": True, "state": "OPEN"}
    if description is not None:
        task["description"] = description
    (runtime / "backlog.json").write_text(json.dumps({"tasks": [task]}), encoding="utf-8")
    return project


def _contract(project: Path) -> Path:
    return project / ".stagemesh" / "contracts" / "T-1.json"


def _continue(project: Path, *argv: str) -> tuple[int, dict]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = cli_module.main(["--project", str(project), "continue", "--dry-run", "--json", "--task", "T-1", *argv])
    return code, json.loads(out.getvalue())


@pytest.fixture
def fake_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auto_plan_module, "detect_gates", lambda project: [dict(GATE)])


def test_missing_contract_is_generated_and_the_task_continues(tmp_path: Path, fake_gates: None) -> None:
    project = _project(tmp_path)

    code, result = _continue(project)

    assert code == 0 and result["stop_reason"] == "DONE", result
    plan = result["auto_plan"]
    assert plan["occurred"] is True and plan["reused_existing"] is False
    assert plan["path"] == str(_contract(project)) and plan["gates"] == ["project-acceptance-fake"]
    assert [e.split(": ", 1)[1].split(" (")[0].split(" at ")[0] for e in plan["events"]] == [
        "missing change contract detected",
        "auto-planning started",
        "contract created",
        "continuing to implementation",
    ]
    contract = json.loads(_contract(project).read_text(encoding="utf-8"))
    assert contract["generated_by"] == "stagemesh-auto-plan" and contract["explicit"] is True
    assert contract["objective"].startswith("Widget endpoint") and "Add the widget endpoint." in contract["objective"]
    assert ".stagemesh/**" in contract["forbidden_files"] and contract["required_tests"][0]["name"] == "project-acceptance-fake"


def test_existing_contract_is_reused_untouched(tmp_path: Path, fake_gates: None) -> None:
    project = _project(tmp_path)
    manual = {
        "objective": "manual",
        "allowed_files": ["stagemesh-task-*.txt"],
        "required_tests": [dict(GATE, name="smoke")],
    }
    _contract(project).write_text(json.dumps(manual), encoding="utf-8")
    before = _contract(project).read_text(encoding="utf-8")

    code, result = _continue(project)

    assert code == 0 and result["stop_reason"] == "DONE", result
    assert result["auto_plan"]["occurred"] is False and result["auto_plan"]["reused_existing"] is True
    assert _contract(project).read_text(encoding="utf-8") == before


def _stagemesh_project(tmp_path: Path) -> Path:
    project = _project(tmp_path)
    (project / "pyproject.toml").write_text(
        "[project]\nname = \"stagemesh\"\n\n[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n",
        encoding="utf-8",
    )
    tests = project / "tests"
    tests.mkdir()
    for name in ("test_run_ready.py", "test_provider_pool.py", "test_canary_regression.py"):
        (tests / name).write_text("def test_placeholder():\n    pass\n", encoding="utf-8")
    GitWorkspace(project).commit_all("add stagemesh smoke harness")
    return project


def _legacy_generated_contract(project: Path) -> dict:
    payload = {
        "objective": "Widget endpoint",
        "explicit": True,
        "validation_classification": "CORE_LIFECYCLE_OR_SCHEMA_SECURITY",
        "validation_escalation_reasons": ["auto-generated contract has no file scope; full project validation required"],
        "acceptance_criteria": ["The change fulfils the task objective and nothing else."],
        "allowed_files": ["**"],
        "forbidden_files": [".stagemesh/**"],
        "required_tests": [
            {"name": "project-acceptance-pytest", "command": ["python", "-m", "pytest", "-q"], "timeout_seconds": 1800}
        ],
        "max_changed_files": 60,
        "max_diff_lines": 6000,
        "generated_by": "stagemesh-auto-plan",
        "source_task": "T-1",
    }
    _contract(project).write_text(json.dumps(payload), encoding="utf-8")
    return payload


def test_existing_legacy_generated_stagemesh_contract_refreshes_to_bounded_smoke(tmp_path: Path) -> None:
    project = _stagemesh_project(tmp_path)
    _legacy_generated_contract(project)

    code, result = _continue(project)

    assert code == 0 and result["stop_reason"] == "DONE", result
    plan = result["auto_plan"]
    assert plan["occurred"] is True and plan["refreshed_existing"] is True
    assert plan["gates"] == ["stagemesh-lifecycle-smoke"]
    refreshed = json.loads(_contract(project).read_text(encoding="utf-8"))
    assert refreshed["generated_by"] == "stagemesh-auto-plan"
    assert refreshed["required_tests"][0]["name"] == "stagemesh-lifecycle-smoke"
    assert refreshed["required_tests"][0]["command"] == [
        "python",
        "-m",
        "pytest",
        "-q",
        "tests/test_run_ready.py",
        "tests/test_provider_pool.py",
        "tests/test_canary_regression.py",
    ]


def test_frozen_legacy_generated_contract_refreshes_when_no_evidence_exists(tmp_path: Path) -> None:
    project = _stagemesh_project(tmp_path)
    legacy = _legacy_generated_contract(project)
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    store.upsert_task("Widget endpoint", source="local-backlog", source_id="T-1")
    store.cache_source("local-backlog", "T-1", {"objective": "Add the widget endpoint."}, "OPEN")
    baseline = GitWorkspace(project).head()
    store.bind_task_contract("T-1", baseline, 1, "0" * 64, canonical_contract_json(parse_contract(legacy)))
    store.add_candidate("T-1", "abc123", "codex", True)

    refreshed = auto_plan_module.refresh_stale_generated_contract(store, project, "T-1")

    assert refreshed is not None and refreshed.gates == ("stagemesh-lifecycle-smoke",)
    frozen = json.loads(store.task_contract("T-1")["canonical_json"])
    bound = json.loads(store.contract_binding("T-1", "abc123")["canonical_json"])
    assert frozen["required_tests"][0]["name"] == "stagemesh-lifecycle-smoke"
    assert bound["required_tests"][0]["name"] == "stagemesh-lifecycle-smoke"


def test_frozen_legacy_generated_contract_with_evidence_preserves_history(tmp_path: Path) -> None:
    project = _stagemesh_project(tmp_path)
    legacy = _legacy_generated_contract(project)
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    store.upsert_task("Widget endpoint", source="local-backlog", source_id="T-1")
    store.cache_source("local-backlog", "T-1", {"objective": "Add the widget endpoint."}, "OPEN")
    baseline = GitWorkspace(project).head()
    store.bind_task_contract("T-1", baseline, 1, "0" * 64, canonical_contract_json(parse_contract(legacy)))
    store.add_candidate("T-1", "abc123", "codex", True)
    store.add_evidence("T-1", "abc123", EvidenceKind.VALIDATION, EvidenceStatus.PASSED, {"contract_hash": "0" * 64})

    assert auto_plan_module.refresh_stale_generated_contract(store, project, "T-1") is None
    assert json.loads(store.task_contract("T-1")["canonical_json"])["required_tests"][0]["name"] == "project-acceptance-pytest"


def test_frozen_legacy_generated_contract_with_failed_evidence_refreshes(tmp_path: Path) -> None:
    project = _stagemesh_project(tmp_path)
    legacy = _legacy_generated_contract(project)
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    store.upsert_task("Widget endpoint", source="local-backlog", source_id="T-1")
    store.cache_source("local-backlog", "T-1", {"objective": "Add the widget endpoint."}, "OPEN")
    baseline = GitWorkspace(project).head()
    store.bind_task_contract("T-1", baseline, 1, "0" * 64, canonical_contract_json(parse_contract(legacy)))
    store.add_candidate("T-1", "abc123", "codex", True)
    store.add_evidence("T-1", "abc123", EvidenceKind.VALIDATION, EvidenceStatus.FAILED, {"contract_hash": "0" * 64})

    refreshed = auto_plan_module.refresh_stale_generated_contract(store, project, "T-1")

    assert refreshed is not None and refreshed.gates == ("stagemesh-lifecycle-smoke",)
    assert json.loads(store.task_contract("T-1")["canonical_json"])["required_tests"][0]["name"] == "stagemesh-lifecycle-smoke"


def test_no_auto_plan_keeps_the_missing_contract_refusal(tmp_path: Path, fake_gates: None) -> None:
    project = _project(tmp_path)

    code, result = _continue(project, "--no-auto-plan")

    assert code == 2 and result["stop_reason"] == "REFUSED:missing_contract"
    assert "--no-auto-plan" in result["message"] and "next_action" in result["detail"]
    assert result["auto_plan"]["occurred"] is False and not _contract(project).exists()


def test_generation_fails_closed_without_a_detectable_gate(tmp_path: Path) -> None:
    project = _project(tmp_path)  # bare repo: no package.json, pytest config, Cargo.toml or go.mod

    code, result = _continue(project)

    assert code == 2 and result["stop_reason"] == "REFUSED:auto_plan_failed"
    assert result["detail"]["auto_plan_reason"] == "no_validation_gates" and "by hand" in result["detail"]["next_action"]
    assert result["auto_plan"]["occurred"] is False and not _contract(project).exists()
    assert result["steps_run"] == 0


def test_generation_fails_closed_when_the_generated_contract_is_invalid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(tmp_path)
    monkeypatch.setattr(auto_plan_module, "detect_gates", lambda p: [{"name": "bad", "command": [], "timeout_seconds": 60}])

    code, result = _continue(project)

    assert code == 2 and result["stop_reason"] == "REFUSED:auto_plan_failed"
    assert result["detail"]["auto_plan_reason"] == "invalid_generated_contract"
    assert not _contract(project).exists()


def test_run_ready_without_json_flag_reports_progress_lines(tmp_path: Path, fake_gates: None, capsys: pytest.CaptureFixture[str]) -> None:
    project = _project(tmp_path)
    assert cli_module.main(["--project", str(project), "run-ready", "--dry-run", "--task", "T-1"]) == 0
    captured = capsys.readouterr()
    err = captured.out + captured.err
    for fragment in ("missing change contract detected", "auto-planning started", "contract created at", "continuing to implementation"):
        assert fragment in err


def test_stage_mesh_project_uses_bounded_lifecycle_smoke_instead_of_full_pytest(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = \"stagemesh\"\n\n[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n",
        encoding="utf-8",
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    for name in ("test_run_ready.py", "test_provider_pool.py", "test_canary_regression.py"):
        (tests / name).write_text("def test_placeholder():\n    pass\n", encoding="utf-8")

    gates = auto_plan_module.detect_gates(tmp_path)

    assert gates == [
        {
            "name": "stagemesh-lifecycle-smoke",
            "command": [
                "python",
                "-m",
                "pytest",
                "-q",
                "tests/test_run_ready.py",
                "tests/test_provider_pool.py",
                "tests/test_canary_regression.py",
            ],
            "timeout_seconds": 360,
        }
    ]

def test_detect_gates_reads_conventional_project_files(tmp_path: Path) -> None:
    assert auto_plan_module.detect_gates(tmp_path) == []
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}), encoding="utf-8")
    (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
    assert [g["name"] for g in auto_plan_module.detect_gates(tmp_path)] == ["project-acceptance-npm", "project-acceptance-go"]
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": 'echo "no test specified" && exit 1'}}), encoding="utf-8")
    assert [g["name"] for g in auto_plan_module.detect_gates(tmp_path)] == ["project-acceptance-go"]
