"""`stagemesh init` gives a project without a profile a safe default one, and never touches an existing profile or state."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
from test_survivor_wave import _git, _with_origin

from stagemesh.cli import main
from stagemesh.profile import load_profile, profile_path


def _init(project: Path, capsys, *, strict: bool = False) -> tuple[int, str, str]:
    argv = ["--project", str(project), "init"]
    if not strict:
        argv.append("--allow-incomplete")
    code = main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def _state(project: Path) -> dict[str, str]:
    return {
        p.relative_to(project).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((project / ".stagemesh").rglob("*"))
        if p.is_file() and p.name != "stagemesh.sqlite3"
    }


def _ready_repository(root: Path) -> Path:
    project = root / "project"
    project.mkdir()
    _git(project, "init", "-q")
    _git(project, "config", "user.email", "test@example.invalid")
    _git(project, "config", "user.name", "StageMesh Test")
    (project / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(project, "add", "seed.txt")
    _git(project, "commit", "-q", "-m", "seed")
    _with_origin(root, project)
    return project


def test_fresh_project_gets_default_profile(tmp_path: Path, capsys) -> None:
    code, out, _ = _init(tmp_path, capsys)
    assert code == 0 and "created default profile" in out
    profile = load_profile(tmp_path)
    assert profile is not None and profile.default_type == "default"
    assert [g["command"] for g in profile.gates.values()] == [["git", "diff", "--check"]]  # nothing conventional to detect
    assert ".stagemesh/**" in profile.forbidden_files


def test_default_profile_uses_detected_root_gates(tmp_path: Path, capsys) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    _init(tmp_path, capsys)
    profile = load_profile(tmp_path)
    assert any(g["command"][:3] == ["python", "-m", "pytest"] for g in profile.gates.values())


def test_existing_project_without_profile_gets_one_and_keeps_state(tmp_path: Path, capsys) -> None:
    _init(tmp_path, capsys)
    profile_path(tmp_path).unlink()
    (tmp_path / ".stagemesh" / "config.json").write_text('{"providers": {"codex": {"command": "codex exec"}}}\n', encoding="utf-8")
    (tmp_path / ".stagemesh" / "contracts").mkdir()
    (tmp_path / ".stagemesh" / "contracts" / "7.json").write_text('{"objective": "x"}\n', encoding="utf-8")
    assert main(["--project", str(tmp_path), "init", "--task", "7", "--allow-incomplete"]) == 0
    capsys.readouterr()
    profile_path(tmp_path).unlink()
    before = {k: v for k, v in _state(tmp_path).items() if k != ".stagemesh/profile.json"}
    code, out, _ = _init(tmp_path, capsys)
    assert code == 0 and "created default profile" in out and profile_path(tmp_path).is_file()
    assert {k: v for k, v in _state(tmp_path).items() if k != ".stagemesh/profile.json"} == before
    from stagemesh.cli import db_path
    from stagemesh.persistence import Store

    store = Store(db_path(tmp_path))
    assert [r["title"] for r in store.conn.execute("SELECT title FROM tasks")] == ["7"]  # task history survives
    store.close()


def test_existing_profile_is_preserved_unchanged(tmp_path: Path, capsys) -> None:
    _init(tmp_path, capsys)
    custom = json.loads(profile_path(tmp_path).read_text(encoding="utf-8"))
    custom["name"] = "hand-written"
    text = json.dumps(custom, indent=4) + "\n"
    profile_path(tmp_path).write_text(text, encoding="utf-8")
    code, out, _ = _init(tmp_path, capsys)
    assert code == 0 and "created default profile" not in out
    assert profile_path(tmp_path).read_text(encoding="utf-8") == text


def test_init_is_idempotent(tmp_path: Path, capsys) -> None:
    _init(tmp_path, capsys)
    first = _state(tmp_path)
    for _ in range(2):
        code, out, _ = _init(tmp_path, capsys)
        assert code == 0 and "created default profile" not in out
    assert _state(tmp_path) == first
    assert list((tmp_path / ".stagemesh").glob(".profile.json.*")) == []  # no temp leftovers


def test_project_smoke_passes_after_init(tmp_path: Path, capsys) -> None:
    _init(tmp_path, capsys)
    code = main(["--project", str(tmp_path), "project-smoke", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert code == 0 and report["ok"], report["checks"]


@pytest.mark.parametrize("content", ["{not json", '{"schema_version": 2}'])
def test_malformed_profile_is_not_overwritten(tmp_path: Path, capsys, content: str) -> None:
    (tmp_path / ".stagemesh").mkdir()
    profile_path(tmp_path).write_text(content, encoding="utf-8")
    code, _, err = _init(tmp_path, capsys)
    assert code == 0 and "left unchanged" in err
    assert profile_path(tmp_path).read_text(encoding="utf-8") == content
    assert main(["--project", str(tmp_path), "init", "--json", "--allow-incomplete"]) == 0
    assert json.loads(capsys.readouterr().out)["profile"] == "invalid"


@pytest.mark.parametrize(
    "files",
    [
        {"package.json": '{"scripts": {"test": "jest"}}'},
        {"Cargo.toml": "[package]\n"},
        {"go.mod": "module x\n"},
        {"pytest.ini": "[pytest]\n"},
        {},
    ],
)
def test_project_smoke_passes_for_every_detected_gate_kind(tmp_path: Path, capsys, files: dict[str, str]) -> None:
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    _init(tmp_path, capsys)
    code = main(["--project", str(tmp_path), "project-smoke", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert code == 0 and report["ok"], report["checks"]
    assert all(g["command"][0] not in ("cmd", "sh") for g in load_profile(tmp_path).gates.values())


def test_placeholder_gate_is_called_out(tmp_path: Path, capsys) -> None:
    _, out, _ = _init(tmp_path, capsys)
    assert "validation_gates: no project test configuration was detected" in out
    main(["--project", str(tmp_path), "project-smoke", "--json"])
    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)["checks"]}
    assert checks["validation gates are real"]["status"] == "warn"


def test_init_does_not_claim_success_when_readiness_blockers_remain(tmp_path: Path, capsys) -> None:
    code, out, _ = _init(tmp_path, capsys, strict=True)

    assert code == 2
    assert "StageMesh setup needs attention" in out
    assert "StageMesh initialization is incomplete" in out
    assert "StageMesh is ready" not in out

    assert main(["--project", str(tmp_path), "init", "--json"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["readiness"]["status"] == "needs_attention"
    assert {item["name"] for item in report["readiness"]["blockers"]} >= {
        "validation_gates",
        "provider_authorization",
    }


def test_init_claims_ready_only_with_real_gates_and_provider_opt_in(tmp_path: Path, capsys) -> None:
    project = _ready_repository(tmp_path)
    (project / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    runtime = project / ".stagemesh"
    runtime.mkdir()
    (runtime / "config.json").write_text(
        json.dumps(
            {
                "providers": {"local-agent": {"command": f'"{sys.executable}"', "capabilities": ["IMPLEMENT", "REVIEW"]}},
                "authorization": {"unattended_providers": ["local-agent"]},
                "routing": {"require_independent_review": False},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    code, out, err = _init(project, capsys, strict=True)

    assert code == 0, out + err
    assert "StageMesh is ready" in out
    assert "readiness blockers" not in out


def test_init_refuses_ready_when_default_pr_window_would_refuse_repository(tmp_path: Path, capsys) -> None:
    project = _ready_repository(tmp_path)
    (project / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    runtime = project / ".stagemesh"
    runtime.mkdir()
    (runtime / "config.json").write_text(
        json.dumps(
            {
                "providers": {"local-agent": {"command": f'"{sys.executable}"', "capabilities": ["IMPLEMENT", "REVIEW"]}},
                "authorization": {"unattended_providers": ["local-agent"]},
                "routing": {"require_independent_review": False},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (project / "local-only.txt").write_text("local only\n", encoding="utf-8")
    _git(project, "add", "local-only.txt")
    _git(project, "commit", "-q", "-m", "local only")

    code, out, _ = _init(project, capsys, strict=True)

    assert code == 2
    assert "repo: local" in out and "is ahead origin" in out
    assert "run-window` will refuse" in out
    assert "StageMesh is ready" not in out

    assert main(["--project", str(project), "init", "--json"]) == 2
    report = json.loads(capsys.readouterr().out)
    repo = next(item for item in report["readiness"]["blockers"] if item["name"] == "repo")
    assert "git pull cannot remove local-only commits" in repo["detail"]


def test_tests_directory_without_safe_test_configuration_is_reported_during_init(tmp_path: Path, capsys) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_database_pipeline.py").write_text("raise SystemExit('requires a live database')\n", encoding="utf-8")

    code, _, _ = _init(tmp_path, capsys, strict=True)
    assert code == 2

    assert main(["--project", str(tmp_path), "init", "--json"]) == 2
    report = json.loads(capsys.readouterr().out)
    validation = next(item for item in report["readiness"]["blockers"] if item["name"] == "validation_gates")
    assert "no project test configuration was detected" in validation["detail"]
    assert "test_database_pipeline.py" not in str(report)  # init never guesses or runs an unsafe standalone test


def test_init_accepts_distinct_available_implementation_and_review_providers(tmp_path: Path, capsys) -> None:
    project = _ready_repository(tmp_path)
    (project / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    runtime = project / ".stagemesh"
    runtime.mkdir()
    executable = f'"{sys.executable}"'
    (runtime / "config.json").write_text(
        json.dumps(
            {
                "providers": {
                    "implementer": {"command": executable, "capabilities": ["IMPLEMENT"]},
                    "reviewer": {"command": executable, "capabilities": ["REVIEW"]},
                },
                "authorization": {"unattended_providers": ["implementer", "reviewer"]},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    code, out, err = _init(project, capsys, strict=True)

    assert code == 0, out + err
    assert "StageMesh is ready" in out


def test_default_profile_keeps_planner_forbidden_set(tmp_path: Path, capsys) -> None:
    from stagemesh.auto_plan import FORBIDDEN_FILES

    _init(tmp_path, capsys)
    assert set(FORBIDDEN_FILES) <= set(load_profile(tmp_path).forbidden_files)


def test_stagemesh_shaped_project_gets_a_valid_profile(tmp_path: Path, capsys) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "stagemesh"\n', encoding="utf-8")
    (tmp_path / "tests").mkdir()
    for name in ("test_run_ready.py", "test_provider_pool.py", "test_canary_regression.py", "test_zero_config_github.py"):
        (tmp_path / "tests" / name).write_text("", encoding="utf-8")
    code, out, _ = _init(tmp_path, capsys)
    assert code == 0 and "created default profile" in out
    names = [g["name"] for g in load_profile(tmp_path).gates.values()]
    assert "stagemesh-lifecycle-smoke" in names and any("acceptance" in n for n in names)
