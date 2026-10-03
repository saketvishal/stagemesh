from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import stagemesh.cli as cli_module
from stagemesh.config import load_config
from stagemesh.profile_smoke import forbidden_pattern_problems, gate_safety_problems, run_smoke

from test_run_ready import _project

CAVENTRA = Path(__file__).resolve().parents[1] / "docs" / "profiles" / "caventra.profile.json"
PY = sys.executable


def profile(**overrides) -> dict:
    base = {
        "schema_version": 1,
        "name": "demo",
        "gates": {
            "docs-static": {"command": [PY, "-c", "pass"], "timeout_seconds": 60},
            "unit": {"command": [PY, "-c", "pass"]},
            "acceptance-all": {"command": [PY, "-c", "pass"]},
        },
        "forbidden_files": [".env", "secrets/**"],
        "defaults": {"max_changed_files": 10, "max_diff_lines": 500},
        "task_types": {
            "docs": {"validation_level": "light", "allowed_files": ["docs/**"], "gates": ["docs-static"], "labels": ["docs"], "keywords": ["readme"]},
            "code": {"validation_level": "standard", "allowed_files": ["src/**"], "gates": ["unit"], "labels": ["code"], "keywords": ["bug"]},
            "full": {"validation_level": "full", "allowed_files": ["**"], "gates": ["docs-static", "unit", "acceptance-all"], "labels": ["risk:high"]},
        },
        "type_selection": {"default_type": "full", "escalation_type": "full"},
        "smoke": {"tasks": [{"title": "Fix the readme", "expect_type": "docs"}, {"title": "Fix a bug", "labels": ["code"], "expect_type": "code"}]},
    }
    base.update(overrides)
    return base


def project_with(tmp_path: Path, raw: dict | None, tasks: list[str] | None = None) -> Path:
    project = _project(tmp_path, tasks or [], contracts=[])
    if raw is not None:
        (project / ".stagemesh" / "profile.json").write_text(json.dumps(raw), encoding="utf-8")
    return project


def smoke(project: Path, **kwargs):
    config = load_config(project)
    return run_smoke(project, config, sync=lambda store, targeted: cli_module._sync_all_sources(store, project, config, targeted), **kwargs)


def status(report, name: str) -> str:
    return next(c.status for c in report.checks if c.name == name)


def test_generic_profile_passes_every_check(tmp_path: Path) -> None:
    report = smoke(project_with(tmp_path, profile()))
    assert report.ok, report.to_dict()
    assert report.profile == "demo" and set(report.task_types) == {"docs", "code", "full"}
    assert [p["ok"] for p in report.probes] == [True, True]
    assert status(report, "task selection") == "skip"


def test_missing_or_broken_profile_fails(tmp_path: Path) -> None:
    assert not smoke(project_with(tmp_path / "a", None)).ok
    report = smoke(project_with(tmp_path / "b", profile(task_types={})))
    assert not report.ok and "unusable" in report.checks[0].detail


def test_shell_wrapped_and_malformed_gates_are_rejected(tmp_path: Path) -> None:
    assert gate_safety_problems({"command": ["bash", "-c", "pytest"], "timeout_seconds": 60})[0]
    assert gate_safety_problems({"command": ["C:\\Windows\\System32\\cmd.exe", "/c", "x"], "timeout_seconds": 60})[0]
    assert gate_safety_problems({"command": ["pytest -q && rm -rf ."], "timeout_seconds": 60})[0]
    assert gate_safety_problems({"command": [PY], "timeout_seconds": 60, "cwd": "../outside"})[0]
    assert gate_safety_problems({"command": [PY], "timeout_seconds": 60, "cwd": "C:/abs"})[0]
    assert gate_safety_problems({"command": [PY], "timeout_seconds": 99999})[0]
    assert gate_safety_problems({"command": [PY, "-c", "pass"], "timeout_seconds": 60}) == ([], [])
    assert gate_safety_problems({"command": ["no-such-tool-xyz"], "timeout_seconds": 60})[1]  # warning only
    raw = profile()
    raw["gates"]["unit"] = {"command": ["sh", "-c", "echo hi"]}
    report = smoke(project_with(tmp_path, raw))
    assert status(report, "validation gates are safe command lists") == "fail" and not report.ok


def test_forbidden_patterns_must_exist_and_be_sane(tmp_path: Path) -> None:
    assert forbidden_pattern_problems(("**",)) and forbidden_pattern_problems(("../x",)) and forbidden_pattern_problems(("/etc/**",))
    assert not forbidden_pattern_problems((".env", "secrets/**"))
    report = smoke(project_with(tmp_path / "a", profile(forbidden_files=[])))
    assert status(report, "forbidden-file patterns exist") == "fail"
    raw = profile()
    raw["task_types"]["docs"]["forbidden_files"] = ["docs/**"]  # also allowed
    assert status(smoke(project_with(tmp_path / "b", raw)), "forbidden-file patterns exist") == "fail"


def test_wrong_probe_expectation_fails_and_unreachable_type_warns(tmp_path: Path) -> None:
    raw = profile(smoke={"tasks": [{"title": "Fix the readme", "expect_type": "code"}]})
    report = smoke(project_with(tmp_path / "a", raw))
    assert status(report, "smoke probes select the expected types") == "fail" and report.probes[0]["selected"] == "docs"
    raw = profile()
    raw["task_types"]["code"]["labels"] = []
    raw["task_types"]["code"]["keywords"] = []
    raw["smoke"] = {"tasks": [{"title": "Fix the readme", "expect_type": "docs"}]}
    report = smoke(project_with(tmp_path / "b", raw))
    assert status(report, "every task type is reachable") == "warn" and report.ok


def test_task_check_shows_type_and_contract(tmp_path: Path) -> None:
    project = project_with(tmp_path, profile(), ["7"])
    backlog = json.loads((project / ".stagemesh" / "backlog.json").read_text(encoding="utf-8"))
    backlog["tasks"][0].update(title="Fix a bug in the parser", labels=["code"])
    (project / ".stagemesh" / "backlog.json").write_text(json.dumps(backlog), encoding="utf-8")
    report = smoke(project, task_ids=["7"])
    assert report.ok, report.to_dict()
    task = report.tasks[0]
    assert task["task_type"] == "code" and task["selection"]["source"] == "label" and task["gates"] == ["unit"]
    assert task["allowed_files"] == ["src/**"] and ".env" in task["forbidden_files"]
    assert not smoke(project, task_ids=["nope"]).ok
    assert not (project / ".stagemesh" / "contracts" / "7.json").exists()  # nothing was written


def test_dry_run_selection_plans_without_writing(tmp_path: Path) -> None:
    project = project_with(tmp_path, profile(), ["1", "2"])
    before = sorted(p.relative_to(project).as_posix() for p in (project / ".stagemesh").rglob("*"))
    report = smoke(project, dry_run_selection=True)
    assert report.ok, report.to_dict()
    assert {c["task_id"] for c in report.selection["eligible"]} == {"1", "2"}
    assert all(c["contract"] == "needs_auto_plan" for c in report.selection["eligible"])
    assert sorted(p.relative_to(project).as_posix() for p in (project / ".stagemesh").rglob("*")) == before
    assert not (project / ".stagemesh" / "stagemesh.sqlite3").exists()


def test_cli_json_and_exit_codes(tmp_path: Path) -> None:
    project = project_with(tmp_path / "ok", profile(), ["1"])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli_module.main(["--project", str(project), "project-smoke", "--json", "--task", "1", "--dry-run-selection"])
    data = json.loads(out.getvalue())
    assert code == 0 and data["ok"] is True and data["summary"]["fail"] == 0 and data["tasks"][0]["task_id"] == "1"
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli_module.main(["--project", str(project_with(tmp_path / "bad", None)), "project-smoke"])
    assert code == 1 and "[FAIL] profile loads" in out.getvalue() and "Smoke FAILED" in out.getvalue()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli_module.main(["--project", str(project), "project-smoke"])
    assert "[PASS] profile loads" in out.getvalue() and "Smoke passed" in out.getvalue()


def test_shipped_caventra_profile_passes_the_generic_smoke(tmp_path: Path) -> None:
    project = project_with(tmp_path, json.loads(CAVENTRA.read_text(encoding="utf-8")))
    report = smoke(project)
    failed = [c.to_dict() for c in report.checks if c.status == "fail"]
    assert not failed, failed
    assert len(report.probes) >= 5 and all(p["ok"] for p in report.probes)
