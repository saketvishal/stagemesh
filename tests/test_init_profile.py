"""`stagemesh init` gives a project without a profile a safe default one, and never touches an existing profile or state."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from stagemesh.cli import main
from stagemesh.profile import load_profile, profile_path


def _init(project: Path, capsys) -> tuple[int, str, str]:
    code = main(["--project", str(project), "init"])
    out = capsys.readouterr()
    return code, out.out, out.err


def _state(project: Path) -> dict[str, str]:
    return {
        p.relative_to(project).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((project / ".stagemesh").rglob("*"))
        if p.is_file() and p.name != "stagemesh.sqlite3"
    }


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
    assert main(["--project", str(tmp_path), "init", "--task", "7"]) == 0
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
    assert main(["--project", str(tmp_path), "init", "--json"]) == 0
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
    assert "no test gate was detected" in out
    main(["--project", str(tmp_path), "project-smoke", "--json"])
    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)["checks"]}
    assert checks["validation gates are real"]["status"] == "warn"


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
