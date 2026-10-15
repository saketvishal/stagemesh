"""Exercise publication boundaries with real Git objects and hostile candidates."""
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("check_attribution", ROOT / "scripts/check_attribution.py")
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)
HOOK_SPEC = importlib.util.spec_from_file_location("install_attribution_hooks", ROOT / "scripts/install_attribution_hooks.py")
hooks = importlib.util.module_from_spec(HOOK_SPEC)
HOOK_SPEC.loader.exec_module(hooks)


def git(repo, *args, env=None):
    return subprocess.check_output(["git", *args], cwd=repo, env=env, text=True).strip()


def commit(repo, message="change", author="owner@example.org", committer="owner@example.org"):
    env = dict(os.environ, GIT_AUTHOR_NAME="Owner", GIT_AUTHOR_EMAIL=author,
               GIT_COMMITTER_NAME="Owner", GIT_COMMITTER_EMAIL=committer)
    git(repo, "-c", "core.hooksPath=", "commit", "--allow-empty", "-m", message, env=env)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.name", "Owner")
    git(tmp_path, "config", "user.email", "owner@example.org")
    commit(tmp_path, "historical\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    return tmp_path


@pytest.mark.parametrize("author,committer,message", [
    ("noreply@anthropic.com", "owner@example.org", "change"),
    ("owner@example.org", "stagemesh@example.invalid", "change"),
    ("12345678+saketvishal@users.noreply.github.com", "owner@example.org", "change"),
    ("owner@example.org", "owner@example.org", "change\n\nCo-Authored-By: Claude <noreply@anthropic.com>"),
    ("owner@example.org", "owner@example.org", "change\n\nco-authored-by: worker <codex+local-worker@stagemesh.invalid>"),
])
def test_rejects_incoming_bad_identity(repo, author, committer, message):
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo, message, author, committer)
    assert guard.main(["--repo", str(repo), "--base", base, "--head", head]) == 1


def test_human_contribution_passes_without_rejecting_existing_history(repo):
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, "change\n\nCo-Authored-By: Teammate <teammate@example.org>")
    head = commit(repo, "second")
    assert guard.main(["--repo", str(repo), "--base", base, "--head", head]) == 0


def test_checks_entire_range_and_merge_side_branch(repo):
    base = git(repo, "rev-parse", "HEAD")
    bad = commit(repo, "bad\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    git(repo, "checkout", "--detach", base)
    commit(repo, "clean side")
    git(repo, "-c", "core.hooksPath=", "merge", "--no-ff", bad, "-m", "merge")
    assert guard.check_commits(repo, [f"{base}..HEAD"])


def test_missing_objects_fail_closed(repo):
    assert guard.main(["--repo", str(repo), "--base", "a" * 40, "--head", "b" * 40]) == 2
    assert guard.main(["--repo", str(repo), "--base", "HEAD", "--head", "HEAD"]) == 2


def test_message_hook_checks_actual_identity_and_trailer(repo, monkeypatch):
    message = repo / "message.txt"
    message.write_text("change\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    assert guard.check_message(repo, message)
    message.write_text("clean change")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "noreply@anthropic.com")
    assert guard.check_message(repo, message)


def test_pre_push_checks_new_and_existing_branches_and_deletion(repo, monkeypatch):
    import io
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/remotes/origin/main", base)
    head = commit(repo, "bad\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    for old in (base, "0" * 40):
        monkeypatch.setattr("sys.stdin", io.StringIO(f"refs/heads/task {head} refs/heads/task {old}\n"))
        assert guard.main(["--repo", str(repo), "--pre-push", "origin"]) == 1
    monkeypatch.setattr("sys.stdin", io.StringIO(f"(delete) {'0' * 40} refs/heads/task {head}\n"))
    assert guard.main(["--repo", str(repo), "--pre-push", "origin"]) == 0


def test_hook_install_is_idempotent_and_preserves_existing_hooks(repo):
    hook_dir = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-path", "hooks"))
    existing = hook_dir / "pre-push"
    existing.write_text("existing hook")
    with pytest.raises(ValueError, match="preserved"):
        hooks.install(repo)
    assert existing.read_text() == "existing hook"
    assert not (hook_dir / "commit-msg").exists()
    existing.unlink()
    hooks.install(repo)
    hooks.install(repo)
    assert "--message-file" in (hook_dir / "commit-msg").read_text()
    assert "--pre-push" in existing.read_text()
    git(repo, "config", "core.hooksPath", "custom-hooks")
    with pytest.raises(ValueError, match="already configured"):
        hooks.install(repo)
