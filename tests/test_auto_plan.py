from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

import stagemesh.auto_plan as auto_plan_module
import stagemesh.cli as cli_module
from stagemesh.git import GitWorkspace

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

    assert code == 0 and result["stop_reason"] == "DONE"
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

    assert code == 0 and result["stop_reason"] == "DONE"
    assert result["auto_plan"]["occurred"] is False and result["auto_plan"]["reused_existing"] is True
    assert _contract(project).read_text(encoding="utf-8") == before


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
