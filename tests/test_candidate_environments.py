from __future__ import annotations

import json
import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from stagemesh.candidate_environments import _relative, prepare_candidate_environments


def test_absent_config_does_not_create_environment(tmp_path):
    prepare_candidate_environments(tmp_path, tmp_path / "checkout")
    assert not (tmp_path / "checkout").exists()


@pytest.mark.parametrize(
    "path",
    [
        "../.venv",
        "/tmp/.venv",
        "C:/.venv",
        ".git/.venv",
        ".stagemesh/.venv",
        "app",
        "x\\.venv",
        "x\n/.venv",
    ],
)
def test_paths_cannot_escape_or_inject(path):
    with pytest.raises(ValueError):
        _relative(path)


def _fixture(tmp_path):
    project, checkout = tmp_path / "operator", tmp_path / "checkout"
    (project / ".stagemesh").mkdir(parents=True)
    for root in (project, checkout):
        (root / "backend/app").mkdir(parents=True)
        (root / "backend/pyproject.toml").write_text("[project]\nname='example'\n")
        (root / "backend/app/__init__.py").write_text("VALUE = " + repr(root.name) + "\n")
    source = project / "backend/.venv"
    venv.EnvBuilder(with_pip=False).create(source)
    packages = source / (
        "Lib/site-packages"
        if os.name == "nt"
        else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    )
    (packages / "dependency.py").write_text("VALUE = 'installed dependency'\n")
    # Reusing this .pth would import the wrong app; it must never be processed.
    (packages / "editable.pth").write_text(str(project / "backend") + "\n")
    (packages / "executable.pth").write_text(
        "import pathlib; pathlib.Path(" + repr(str(project / "pth-ran")) + ").touch()\n"
    )
    config = {
        "schema_version": 1,
        "python": {".venv": "backend/.venv", "backend/.venv": "backend/.venv"},
    }
    (project / ".stagemesh/validation-environments.json").write_text(json.dumps(config))
    return project, checkout, packages


def test_dependencies_available_but_application_stays_in_candidate(tmp_path):
    project, checkout, packages = _fixture(tmp_path)
    prepare_candidate_environments(project, checkout)
    for rel in (".venv", "backend/.venv"):
        python = checkout / rel / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        result = subprocess.run(
            [str(python), "-c", "import app,dependency; print(app.VALUE, dependency.VALUE)"],
            cwd=checkout / "backend",
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == "checkout installed dependency"
    assert not (project / "pth-ran").exists()
    assert (packages / "editable.pth").is_file()


def test_changed_manifest_refuses_before_creating_any_environment(tmp_path):
    project, checkout, _ = _fixture(tmp_path)
    (checkout / "backend/pyproject.toml").write_text("changed dependency declaration")
    with pytest.raises(ValueError, match="dependencies differ"):
        prepare_candidate_environments(project, checkout)
    assert not (checkout / ".venv").exists()


def test_existing_candidate_environment_is_not_overwritten(tmp_path):
    project, checkout, _ = _fixture(tmp_path)
    target = checkout / ".venv"
    target.mkdir()
    sentinel = target / "preserve"
    sentinel.write_text("existing")
    with pytest.raises(ValueError, match="existing"):
        prepare_candidate_environments(project, checkout)
    assert sentinel.read_text() == "existing"


def test_symlink_escape_is_rejected(tmp_path, monkeypatch):
    project, checkout, _ = _fixture(tmp_path)
    original = Path.resolve

    def resolve(path, *args, **kwargs):
        if path == checkout / ".venv":
            return tmp_path / "outside"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(ValueError, match="escapes"):
        prepare_candidate_environments(project, checkout)


def test_crlf_checkout_preserves_the_same_dependency_declaration(tmp_path):
    project, checkout, _ = _fixture(tmp_path)
    manifest = project / "backend/pyproject.toml"
    manifest.write_bytes(manifest.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    prepare_candidate_environments(project, checkout)
    assert (checkout / ".venv").is_dir()


def test_tracked_lockfile_cannot_disappear_but_untracked_operator_lock_can(tmp_path):
    project, checkout, _ = _fixture(tmp_path)
    subprocess.run(["git", "init", str(project)], check=True, capture_output=True)
    lock = project / "backend/uv.lock"
    lock.write_text("operator-only lock")
    prepare_candidate_environments(project, checkout)
    second = tmp_path / "second"
    (second / "backend").mkdir(parents=True)
    (second / "backend/pyproject.toml").write_text((project / "backend/pyproject.toml").read_text())
    subprocess.run(["git", "add", "backend/uv.lock"], cwd=project, check=True)
    with pytest.raises(ValueError, match="dependencies differ"):
        prepare_candidate_environments(project, second)
    assert not (second / ".venv").exists()


def test_malformed_configuration_is_refused(tmp_path):
    config = tmp_path / ".stagemesh/validation-environments.json"
    config.parent.mkdir()
    config.write_text('{"schema_version": true, "python": {}}')
    with pytest.raises(ValueError, match="schema version"):
        prepare_candidate_environments(tmp_path, tmp_path / "checkout")


def test_package_versions_agree():
    import ast
    import tomllib

    import stagemesh

    root = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((root / "pyproject.toml").read_text())
    tree = ast.parse((root / "build_backend.py").read_text())
    backend_version = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets)
    )
    assert metadata["project"]["version"] == stagemesh.__version__ == backend_version


def test_invalid_environment_config_fails_validation_without_running_gate(tmp_path):
    from test_change_contracts import _repo

    from stagemesh.contracts import ChangeContract, GateCommand, evaluate_contract

    repo = tmp_path / "repo"
    workspace = _repo(repo)
    config = repo / ".stagemesh/validation-environments.json"
    config.parent.mkdir()
    config.write_text('{"schema_version":true,"python":{}}')
    contract = ChangeContract(
        objective="Validate exact candidate",
        required_tests=(
            GateCommand("must-not-run", [sys.executable, "-c", "raise SystemExit(0)"]),
        ),
    )
    result = evaluate_contract(repo, workspace.head(), contract, baseline_sha=workspace.head())
    assert not result.passed
    assert any(f["code"] == "candidate_workspace_unavailable" for f in result.findings)
    assert result.gates == ()
    worktrees = workspace.run("worktree", "list", "--porcelain").stdout
    assert worktrees.count("worktree ") == 1
