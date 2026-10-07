"""Fail-closed isolation: no runtime path of this checkout may resolve into another StageMesh checkout."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from autonomy_support import commit, git, init_repo

from stagemesh.autonomy.cli import EXIT_NOT_ISOLATED
from stagemesh.autonomy.isolation import (
    FORBIDDEN_ENV,
    IsolationViolation,
    check_isolation,
    require_isolation,
)
from stagemesh.cli import main


def _checkout(path: Path, *, stagemesh_shaped: bool = True) -> Path:
    repo = init_repo(path)
    files = {"README.md": "x\n"}
    if stagemesh_shaped:
        files["pyproject.toml"] = '[project]\nname = "stagemesh"\n'
        files["src/stagemesh/__init__.py"] = ""
    commit(repo, files, "init")
    if stagemesh_shaped:
        (repo / ".stagemesh").mkdir()
    return repo


def _codes(report) -> set[str]:
    return {f.code for f in report.findings}


def _link_dir(link: Path, target: Path) -> None:
    """A directory junction on Windows (no privilege needed), a symlink elsewhere."""
    link.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
        if result.returncode != 0:
            pytest.skip(f"cannot create a junction: {result.stderr}")
    else:
        os.symlink(target, link, target_is_directory=True)


def test_an_independent_checkout_is_isolated(tmp_path: Path) -> None:
    mine = _checkout(tmp_path / "mine")
    other = _checkout(tmp_path / "other")
    report = check_isolation(mine, forbidden_checkouts=[other])
    assert report.isolated, report.findings
    assert set(report.paths) >= {"runtime", "sqlite", "worktrees", "logs", "tmp", "agents"}
    assert all(Path(p).resolve().is_relative_to((mine / ".stagemesh").resolve()) for p in report.paths.values())
    assert require_isolation(mine, forbidden_checkouts=[other]) is not None


def test_worktree_root_configured_into_another_checkout_fails_closed(tmp_path: Path) -> None:
    mine = _checkout(tmp_path / "mine")
    other = _checkout(tmp_path / "other")
    (mine / ".stagemesh" / "config.json").write_text(
        json.dumps({"runtime": {"worktree_root": str(other / ".stagemesh" / "worktrees")}}), encoding="utf-8"
    )

    # detected both by explicit declaration and heuristically (it looks like a StageMesh checkout), so an undeclared sibling is caught too
    for declared in ([other], []):
        report = check_isolation(mine, forbidden_checkouts=declared)
        assert not report.isolated
        assert "PATH_RESOLVES_INTO_OTHER_CHECKOUT" in _codes(report)
        assert any(str(other.resolve()).casefold() in f.message.casefold() for f in report.findings if f.code == "PATH_RESOLVES_INTO_OTHER_CHECKOUT")
    with pytest.raises(IsolationViolation) as raised:
        require_isolation(mine)
    assert "worktrees" in str(raised.value)


def test_a_junction_or_symlink_into_another_checkout_is_followed_not_trusted(tmp_path: Path) -> None:
    mine = _checkout(tmp_path / "mine")
    other = _checkout(tmp_path / "other")
    (other / ".stagemesh" / "worktrees").mkdir()
    _link_dir(mine / ".stagemesh" / "worktrees", other / ".stagemesh" / "worktrees")

    report = check_isolation(mine, forbidden_checkouts=[other])

    assert not report.isolated
    assert "PATH_RESOLVES_INTO_OTHER_CHECKOUT" in _codes(report)
    resolved = [f for f in report.findings if f.code == "PATH_RESOLVES_INTO_OTHER_CHECKOUT"]
    assert resolved and all(str(other.resolve()).casefold() in f.path.casefold() for f in resolved)


def test_state_directory_junctioned_into_another_checkout_fails_closed(tmp_path: Path) -> None:
    mine = _checkout(tmp_path / "mine")
    other = _checkout(tmp_path / "other")
    (mine / ".stagemesh").rmdir()
    _link_dir(mine / ".stagemesh", other / ".stagemesh")  # the entire runtime (SQLite, logs, tmp, agents) is the other checkout's
    report = check_isolation(mine, forbidden_checkouts=[other])
    assert not report.isolated and "PATH_RESOLVES_INTO_OTHER_CHECKOUT" in _codes(report)


def test_forbidden_checkouts_can_come_from_the_environment_and_a_local_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mine = _checkout(tmp_path / "mine")
    elsewhere = _checkout(tmp_path / "elsewhere", stagemesh_shaped=False)  # not StageMesh-shaped: only declarations can catch it
    (mine / ".stagemesh" / "config.json").write_text(json.dumps({"runtime": {"worktree_root": str(elsewhere / "wt")}}), encoding="utf-8")
    assert "PATH_RESOLVES_INTO_OTHER_CHECKOUT" not in _codes(check_isolation(mine))  # undeclared and unrecognizable

    monkeypatch.setenv(FORBIDDEN_ENV, str(elsewhere))
    assert "PATH_RESOLVES_INTO_OTHER_CHECKOUT" in _codes(check_isolation(mine))
    monkeypatch.delenv(FORBIDDEN_ENV)

    (mine / ".stagemesh" / "isolation.json").write_text(json.dumps({"forbidden_checkouts": [str(elsewhere)]}), encoding="utf-8")
    assert "PATH_RESOLVES_INTO_OTHER_CHECKOUT" in _codes(check_isolation(mine))


def test_project_nested_inside_or_containing_a_forbidden_checkout_fails_closed(tmp_path: Path) -> None:
    other = _checkout(tmp_path / "other")
    inside = _checkout(other / "nested")
    assert "PROJECT_OVERLAPS_FORBIDDEN_CHECKOUT" in _codes(check_isolation(inside, forbidden_checkouts=[other]))
    mine = _checkout(tmp_path / "mine")
    assert "PROJECT_OVERLAPS_FORBIDDEN_CHECKOUT" in _codes(check_isolation(other, forbidden_checkouts=[other / "nested"]))
    assert mine.exists()


def test_a_linked_worktree_of_another_repository_shares_its_git_store(tmp_path: Path) -> None:
    other = _checkout(tmp_path / "other")
    linked = tmp_path / "linked"
    git(other, "worktree", "add", "--detach", str(linked))
    report = check_isolation(linked)
    assert "SHARED_GIT_STORE" in _codes(report) and not report.isolated


def test_git_worktrees_registered_outside_the_checkout_are_foreign(tmp_path: Path) -> None:
    mine = _checkout(tmp_path / "mine")
    git(mine, "worktree", "add", "--detach", str(tmp_path / "stray"))
    assert "FOREIGN_WORKTREE" in _codes(check_isolation(mine))


def test_stagemesh_temp_candidate_worktrees_are_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    mine = _checkout(tmp_path / "mine")
    candidate = tmp_path / "stagemesh-candidate-owned" / "checkout"

    class FakeGitWorkspace:
        def __init__(self, project: Path) -> None:
            self.project = project

        def run(self, *args: str, **_: object):
            stdout_by_command = {
                ("rev-parse", "--show-toplevel"): f"{mine}\n",
                ("rev-parse", "--path-format=absolute", "--git-common-dir"): f"{mine / '.git'}\n",
                ("worktree", "prune"): "",
                ("worktree", "list", "--porcelain"): f"worktree {mine}\n\nworktree {candidate}\n",
            }
            return type("GitResult", (), {"stdout": stdout_by_command[args]})()

    monkeypatch.setattr("stagemesh.autonomy.isolation.GitWorkspace", FakeGitWorkspace)

    report = check_isolation(mine)
    assert "FOREIGN_WORKTREE" not in _codes(report)


def test_project_must_be_its_own_repository_root(tmp_path: Path) -> None:
    outer = _checkout(tmp_path / "outer", stagemesh_shaped=False)
    inner = outer / "sub"
    inner.mkdir()
    assert "PROJECT_NOT_REPOSITORY_ROOT" in _codes(check_isolation(inner))


def test_shared_database_and_global_registry_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mine = _checkout(tmp_path / "mine")
    assert "SHARED_DATABASE" in _codes(check_isolation(mine, database_url="postgresql://db.example/stagemesh"))
    assert "SHARED_DATABASE" not in _codes(check_isolation(mine, database_url=f"sqlite:///{mine / '.stagemesh' / 'stagemesh.sqlite3'}"))
    assert "SHARED_DATABASE" in _codes(check_isolation(mine, database_url=f"sqlite:///{tmp_path / 'other.sqlite3'}"))
    monkeypatch.setenv("STAGEMESH_DATABASE_URL", "postgresql://shared/db")
    assert "SHARED_DATABASE" in _codes(check_isolation(mine))
    monkeypatch.delenv("STAGEMESH_DATABASE_URL")
    assert "SHARED_REGISTRY" in _codes(check_isolation(mine, registry_path=Path.home() / ".stagemesh" / "registry.json"))
    assert "SHARED_REGISTRY" not in _codes(check_isolation(mine, registry_path=mine / ".stagemesh" / "registry.json"))


def test_runtime_paths_outside_the_runtime_directory_are_flagged_unless_explicitly_allowed(tmp_path: Path) -> None:
    mine = _checkout(tmp_path / "mine")
    (mine / ".stagemesh" / "config.json").write_text(json.dumps({"runtime": {"worktree_root": str(tmp_path / "scratch")}}), encoding="utf-8")
    assert "PATH_OUTSIDE_RUNTIME" in _codes(check_isolation(mine))
    assert "PATH_OUTSIDE_RUNTIME" not in _codes(check_isolation(mine, allow_external_runtime_paths=True))


def test_invalid_runtime_config_fails_closed(tmp_path: Path) -> None:
    mine = _checkout(tmp_path / "mine")
    (mine / ".stagemesh" / "config.json").write_text(json.dumps({"runtime": {"worktree_root": str(mine)}}), encoding="utf-8")
    assert "UNSAFE_RUNTIME_CONFIG" in _codes(check_isolation(mine))


def test_cli_isolation_command_exits_nonzero_when_not_isolated(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    mine = _checkout(tmp_path / "mine", stagemesh_shaped=False)  # an ordinary project: any installed StageMesh is fine
    (mine / ".stagemesh").mkdir(exist_ok=True)
    other = _checkout(tmp_path / "other")
    assert main(["--project", str(mine), "autonomy", "isolation", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["isolated"] is True
    (mine / ".stagemesh" / "config.json").write_text(json.dumps({"runtime": {"worktree_root": str(other / "wt")}}), encoding="utf-8")
    assert main(["--project", str(mine), "autonomy", "isolation", "--forbid", str(other)]) == EXIT_NOT_ISOLATED
    assert "failing closed" in capsys.readouterr().out


def test_coordinator_setup_refuses_to_run_supervised_in_a_non_isolated_checkout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from stagemesh.cli import _build_coordinator, _SetupError
    from stagemesh.config import load_config
    from stagemesh.persistence import Store

    mine = _checkout(tmp_path / "mine", stagemesh_shaped=False)
    other = _checkout(tmp_path / "other")
    (mine / ".stagemesh").mkdir(exist_ok=True)
    (mine / ".stagemesh" / "autonomy.json").write_text(json.dumps({"enabled": True}), encoding="utf-8")
    store = Store(mine / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    args = type("Args", (), {"dry_run": True, "provider": None})()

    _build_coordinator(args, mine, load_config(mine), store, None)  # isolated: builds fine

    (mine / ".stagemesh" / "config.json").write_text(json.dumps({"runtime": {"worktree_root": str(other / ".stagemesh" / "worktrees")}}), encoding="utf-8")
    with pytest.raises(_SetupError):
        _build_coordinator(args, mine, load_config(mine), store, None)
    err = capsys.readouterr().err
    assert "isolation violation [PATH_RESOLVES_INTO_OTHER_CHECKOUT]" in err and str(other.resolve()).casefold() in err.casefold()


def test_invalid_autonomy_settings_fail_closed_instead_of_running_unsupervised(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from stagemesh.cli import _build_coordinator, _SetupError
    from stagemesh.config import load_config
    from stagemesh.persistence import Store

    mine = _checkout(tmp_path / "mine", stagemesh_shaped=False)
    (mine / ".stagemesh").mkdir(exist_ok=True)
    (mine / ".stagemesh" / "autonomy.json").write_text("{not json", encoding="utf-8")
    store = Store(mine / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    with pytest.raises(_SetupError):
        _build_coordinator(type("A", (), {"dry_run": True, "provider": None})(), mine, load_config(mine), store, None)
    assert "autonomy config error" in capsys.readouterr().err


def test_running_stagemesh_code_from_another_checkout_is_refused(tmp_path: Path) -> None:
    """The real hazard: a global editable install makes `import stagemesh` resolve to a different checkout."""
    import stagemesh

    mine = _checkout(tmp_path / "mine")  # StageMesh-shaped: it has its own src/stagemesh, but the imported package is not it
    report = check_isolation(mine, check_running_code=True)
    assert "RUNNING_CODE_FROM_OTHER_CHECKOUT" in _codes(report) and not report.isolated
    finding = next(f for f in report.findings if f.code == "RUNNING_CODE_FROM_OTHER_CHECKOUT")
    assert str(Path(stagemesh.__file__).parent.resolve()) in finding.message and "PYTHONPATH=src" in finding.message
    assert "RUNNING_CODE_FROM_OTHER_CHECKOUT" not in _codes(check_isolation(mine))  # opt-in check

    repo_root = Path(stagemesh.__file__).resolve().parents[2]  # this checkout, whose src/stagemesh is the code under test
    assert "RUNNING_CODE_FROM_OTHER_CHECKOUT" not in _codes(check_isolation(repo_root, check_running_code=True))


def test_ordinary_projects_may_use_any_installed_stagemesh(tmp_path: Path) -> None:
    ordinary = _checkout(tmp_path / "ordinary", stagemesh_shaped=False)
    (ordinary / ".stagemesh").mkdir(exist_ok=True)
    assert "RUNNING_CODE_FROM_OTHER_CHECKOUT" not in _codes(check_isolation(ordinary, check_running_code=True))


def test_a_declared_tool_checkout_must_be_the_one_whose_code_is_running(tmp_path: Path) -> None:
    """StageMesh developing StageMesh: the project is a StageMesh checkout, the tool code comes from a named, different checkout."""
    import stagemesh

    tool_checkout = Path(stagemesh.__file__).resolve().parents[2]  # this checkout: its src/stagemesh is what is imported
    target = _checkout(tmp_path / "target")  # StageMesh-shaped, but not where the running code lives
    assert "RUNNING_CODE_FROM_OTHER_CHECKOUT" in _codes(check_isolation(target, check_running_code=True))  # default: wrong
    declared = check_isolation(target, check_running_code=True, expected_code_checkout=tool_checkout)
    assert "RUNNING_CODE_FROM_OTHER_CHECKOUT" not in _codes(declared)  # explicitly declared tool checkout is accepted
    wrong = check_isolation(target, expected_code_checkout=tmp_path / "somewhere-else")
    assert "RUNNING_CODE_FROM_OTHER_CHECKOUT" in _codes(wrong)  # ...and only that one


def test_autonomy_settings_accept_a_code_checkout(tmp_path: Path) -> None:
    from stagemesh.autonomy.wiring import load_settings

    runtime = tmp_path / ".stagemesh"
    runtime.mkdir()
    (runtime / "autonomy.json").write_text(json.dumps({"enabled": True, "code_checkout": str(tmp_path / "tool")}), encoding="utf-8")
    assert load_settings(runtime).code_checkout == str(tmp_path / "tool")
