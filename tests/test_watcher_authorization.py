"""Repository authorization tests (SDD-001 section 9.2)."""



from __future__ import annotations



import json

import subprocess

from pathlib import Path



import pytest



from build_coordinator.watcher import authorization as auth





def _init_repo(path) -> None:

    path.mkdir(parents=True, exist_ok=True)

    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)





def _write_config(tmp_path, entries) -> str:

    config_path = tmp_path / "build-coordinator.json"

    config_path.write_text(json.dumps({"authorized_repositories": entries}), encoding="utf-8")

    return str(config_path)





def test_authorize_passes_for_matching_slug_and_remote(tmp_path, monkeypatch):

    repo_root = tmp_path / "orchestrator"

    _init_repo(repo_root)

    subprocess.run(

        ["git", "-C", str(repo_root), "remote", "add", "origin", "https://github.com/saketvishal/stagemesh-orchestrator.git"],

        check=True,

        capture_output=True,

    )

    config_path = _write_config(

        tmp_path,

        [{"slug": "stagemesh-orchestrator", "control_repo_root": str(repo_root)}],

    )

    monkeypatch.setenv("BUILD_COORDINATOR_CONFIG", config_path)



    repo = auth.authorize("stagemesh-orchestrator")

    assert repo.slug == "saketvishal/stagemesh-orchestrator"

    assert repo.control_repo_root == repo_root





def test_git_remote_probe_is_scoped_to_configured_safe_directory(monkeypatch):

    calls = []



    def fake_run(args, **kwargs):

        calls.append(args)

        return subprocess.CompletedProcess(args, 0, stdout="https://github.com/saketvishal/stagemesh-orchestrator.git\n")



    monkeypatch.setattr(auth.subprocess, "run", fake_run)



    repo_root = Path("C:/stagemesh-orchestrator")

    remote = auth._git_remote_url(repo_root, "origin")



    assert remote == "https://github.com/saketvishal/stagemesh-orchestrator.git"

    assert calls == [

        [

            "git",

            "-c",

            f"safe.directory={repo_root.resolve().as_posix()}",

            "-C",

            str(repo_root),

            "remote",

            "get-url",

            "origin",

        ]

    ]





def test_authorize_fails_closed_on_remote_mismatch(tmp_path, monkeypatch):

    repo_root = tmp_path / "orchestrator"

    _init_repo(repo_root)

    subprocess.run(

        ["git", "-C", str(repo_root), "remote", "add", "origin", "https://github.com/saketvishal/stagemesh.git"],

        check=True,

        capture_output=True,

    )

    config_path = _write_config(

        tmp_path,

        [{"slug": "stagemesh-orchestrator", "control_repo_root": str(repo_root)}],

    )

    monkeypatch.setenv("BUILD_COORDINATOR_CONFIG", config_path)



    with pytest.raises(auth.RepositoryAuthorizationError, match="does not resolve"):

        auth.authorize("stagemesh-orchestrator")





def test_authorize_fails_closed_on_non_git_path(tmp_path, monkeypatch):

    not_a_repo = tmp_path / "not-a-repo"

    not_a_repo.mkdir()

    config_path = _write_config(

        tmp_path,

        [{"slug": "stagemesh-orchestrator", "control_repo_root": str(not_a_repo)}],

    )

    monkeypatch.setenv("BUILD_COORDINATOR_CONFIG", config_path)



    with pytest.raises(auth.RepositoryAuthorizationError, match="not a git worktree"):

        auth.authorize("stagemesh-orchestrator")





def test_authorize_fails_closed_on_missing_path(tmp_path, monkeypatch):

    config_path = _write_config(

        tmp_path,

        [{"slug": "stagemesh-orchestrator", "control_repo_root": str(tmp_path / "does-not-exist")}],

    )

    monkeypatch.setenv("BUILD_COORDINATOR_CONFIG", config_path)



    with pytest.raises(auth.RepositoryAuthorizationError, match="does not exist"):

        auth.authorize("stagemesh-orchestrator")





def test_authorize_fails_closed_when_repo_not_configured(tmp_path, monkeypatch):

    config_path = _write_config(tmp_path, [])

    monkeypatch.setenv("BUILD_COORDINATOR_CONFIG", config_path)



    with pytest.raises(auth.RepositoryAuthorizationError, match="not in the operator"):

        auth.authorize("stagemesh-orchestrator")





def test_authorize_rejects_unauthorized_repo_name(tmp_path, monkeypatch):

    config_path = _write_config(

        tmp_path,

        [{"slug": "not-a-real-repo", "control_repo_root": str(tmp_path)}],

    )

    monkeypatch.setenv("BUILD_COORDINATOR_CONFIG", config_path)



    with pytest.raises(auth.RepositoryAuthorizationError):

        auth.authorize("not-a-real-repo")





def test_explicit_relative_config_path_fails_closed(monkeypatch):

    # coordinator_config._explicit_config_path fails closed on a relative

    # BUILD_COORDINATOR_CONFIG -- authorization must surface that, not swallow it.

    from build_coordinator.coordinator_config import CoordinatorConfigError



    monkeypatch.setenv("BUILD_COORDINATOR_CONFIG", "relative/path.json")

    with pytest.raises(CoordinatorConfigError):

        auth.authorize("stagemesh-orchestrator")

