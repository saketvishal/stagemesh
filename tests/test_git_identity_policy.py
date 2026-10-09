"""Git identity policy: StageMesh resolves the owner's identity, never writes one, and never fabricates contributors."""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import pytest
from test_run_ready import _project

from stagemesh.attribution import attribution_for_worker
from stagemesh.autonomy.gitfacts import GitFacts
from stagemesh.git import GitWorkspace
from stagemesh.git_identity import (
    GitIdentityError,
    configured_identity,
    is_non_human_email,
    project_identity,
    sanitize_commit_message,
)
from stagemesh.workspaces import prepare_task_workspace

OWNER = ("Repo Owner", "42+repoowner@users.noreply.github.com")
HUMAN = ("Ada Lovelace", "ada@example.org")
SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture(autouse=True)
def isolated_git_config(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No test here may read or write the real machine's git identity."""
    home = tmp_path_factory.mktemp("gitglobal")
    global_config = home / "gitconfig"
    global_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(key, raising=False)
    return global_config


def _raw(path: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=path, text=True, capture_output=True, check=True).stdout.strip()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ident(path: Path, rev: str = "HEAD") -> tuple[str, str]:
    author, committer = _raw(path, "log", "-1", "--format=%an <%ae>%n%cn <%ce>", rev).splitlines()
    return author, committer


def _owner_repo(tmp_path: Path) -> Path:
    project = _project(tmp_path, ["T-1"])
    _raw(project, "config", "user.name", OWNER[0])  # the owner's own configuration, set before StageMesh ever runs
    _raw(project, "config", "user.email", OWNER[1])
    return project


# ---- StageMesh never writes or changes an identity -------------------------------------------------------------------------------


def test_source_never_writes_git_identity_config() -> None:
    pattern = re.compile(r"""config["',\s]+(--\w+["',\s]+)*["']?user\.(name|email)|["']user\.(name|email)=""")
    offenders = [
        f"{path.relative_to(SRC)}:{number}"
        for path in SRC.rglob("*.py")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line) and "git_identity.py" not in path.name and "startswith(" not in line  # git.py only *detects* an inline -c override
    ]
    assert offenders == []


def test_task_worktree_creation_and_commits_leave_the_owners_git_configuration_untouched(tmp_path: Path, isolated_git_config: Path) -> None:
    project = _owner_repo(tmp_path)
    local_config, before_global = project / ".git" / "config", _digest(isolated_git_config)
    before_local = _digest(local_config)

    worktree = prepare_task_workspace(project, "T-1")
    (worktree / "change.txt").write_text("x\n", encoding="utf-8")
    GitWorkspace(worktree).commit_all("task change")

    assert _digest(local_config) == before_local and _digest(isolated_git_config) == before_global
    assert (_raw(project, "config", "user.name"), _raw(project, "config", "user.email")) == OWNER
    assert (_raw(worktree, "config", "user.name"), _raw(worktree, "config", "user.email")) == OWNER  # the shared config, still the owner's


def test_init_if_needed_does_not_write_an_identity_and_commits_use_the_synthetic_fallback_per_command(tmp_path: Path) -> None:
    repo = tmp_path / "fresh"
    git = GitWorkspace(repo)
    git.init_if_needed()
    assert configured_identity(repo) is None
    assert "[user]" not in (repo / ".git" / "config").read_text(encoding="utf-8")

    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    git.commit_all("first")

    assert _ident(repo) == ("StageMesh <stagemesh@stagemesh.invalid>", "StageMesh <stagemesh@stagemesh.invalid>")
    assert "[user]" not in (repo / ".git" / "config").read_text(encoding="utf-8")  # supplied for that one command only


def test_the_users_environment_identity_is_respected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _owner_repo(tmp_path)
    monkeypatch.setenv("GIT_AUTHOR_NAME", HUMAN[0])
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", HUMAN[1])
    (repo / "e.txt").write_text("e\n", encoding="utf-8")
    GitWorkspace(repo).commit_all("env identity")
    author, committer = _ident(repo)
    assert author == f"{HUMAN[0]} <{HUMAN[1]}>" and committer == f"{OWNER[0]} <{OWNER[1]}>"


# ---- provider-created commits ------------------------------------------------------------------------------------------------------


def test_provider_commits_carry_the_project_identity_and_no_fabricated_contributor(tmp_path: Path) -> None:
    project = _owner_repo(tmp_path)
    worktree = prepare_task_workspace(project, "T-1")
    (worktree / "p.txt").write_text("provider change\n", encoding="utf-8")

    GitWorkspace(worktree).commit_all(
        "provider change\n\nCo-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>",
        attribution=attribution_for_worker("local-worker", "codex"),
    )

    author, committer = _ident(worktree)
    assert author == committer == f"{OWNER[0]} <{OWNER[1]}>"
    body = _raw(worktree, "log", "-1", "--format=%B")
    assert "co-authored-by" not in body.lower() and "anthropic" not in body.lower()
    assert "local-worker" not in _raw(worktree, "log", "--all", "--format=%an %ae %cn %ce")


def test_state_transplant_commits_are_committed_as_the_project_identity(tmp_path: Path) -> None:
    project = _owner_repo(tmp_path)
    head = _raw(project, "rev-parse", "HEAD")
    tree = _raw(project, "rev-parse", "HEAD^{tree}")
    sha = GitFacts(project).commit_tree(tree, head, "rebuilt\n\nCo-authored-by: Codex <codex@openai.com>")
    assert _ident(project, sha) == (f"{OWNER[0]} <{OWNER[1]}>", f"{OWNER[0]} <{OWNER[1]}>")
    assert "co-authored-by" not in _raw(project, "log", "-1", "--format=%B", sha).lower()


# ---- rebases and integrations -------------------------------------------------------------------------------------------------------


def test_rebase_preserves_a_genuine_contributors_authorship_and_commits_as_the_owner(tmp_path: Path) -> None:
    project = _owner_repo(tmp_path)
    base = _raw(project, "rev-parse", "HEAD")
    branch = _raw(project, "rev-parse", "--abbrev-ref", "HEAD")
    worktree = prepare_task_workspace(project, "T-1")

    (worktree / "ada.txt").write_text("by a human contributor\n", encoding="utf-8")
    _raw(worktree, "add", "-A")
    subprocess.run(
        ["git", "-c", f"user.name={HUMAN[0]}", "-c", f"user.email={HUMAN[1]}", "commit", "-q", "-m", "human work\n\nCo-authored-by: Grace Hopper <grace@example.org>"],
        cwd=worktree, check=True,
    )
    (project / "main.txt").write_text("main advanced\n", encoding="utf-8")
    GitWorkspace(project).commit_all("main advanced")  # the integration ref moves

    result = GitWorkspace(worktree).run("rebase", branch)  # StageMesh's own rebase path (SerializedIntegrator uses git.run)

    assert result.returncode == 0 and _raw(worktree, "merge-base", "HEAD", base) == base
    author, committer = _ident(worktree)
    assert author == f"{HUMAN[0]} <{HUMAN[1]}>"  # the human keeps authorship
    assert committer == f"{OWNER[0]} <{OWNER[1]}>"  # the rebase is committed by the owner, not by a synthetic StageMesh identity
    assert "Co-authored-by: Grace Hopper <grace@example.org>" in _raw(worktree, "log", "-1", "--format=%B")  # a human trailer is untouched


# ---- refusing wrong identities --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "email",
    [
        "12345678+repoowner@users.noreply.github.com",  # an invented id belongs to a different GitHub account
        "Repo Owner 42+repoowner@users.noreply.github.com",  # name glued into the address
        "owner@@example.org",
        "",
    ],
)
def test_placeholder_or_malformed_identities_are_refused_before_any_commit(tmp_path: Path, email: str) -> None:
    project = _owner_repo(tmp_path)
    _raw(project, "config", "user.email", email)
    before = _raw(project, "rev-parse", "HEAD")
    (project / "x.txt").write_text("x\n", encoding="utf-8")

    if email == "":
        # an empty email is simply "not configured": the synthetic fallback applies
        GitWorkspace(project).commit_all("fallback")
        assert _ident(project)[0] == "StageMesh <stagemesh@stagemesh.invalid>"
        return
    with pytest.raises(GitIdentityError):
        GitWorkspace(project).commit_all("must not be created")
    assert _raw(project, "rev-parse", "HEAD") == before


def test_a_placeholder_identity_in_the_environment_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _owner_repo(tmp_path)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Repo Owner")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "12345678+repoowner@users.noreply.github.com")
    (project / "x.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(GitIdentityError):
        GitWorkspace(project).commit_all("must not be created")


def test_project_identity_prefers_the_owners_configuration_to_the_synthetic_one(tmp_path: Path) -> None:
    project = _owner_repo(tmp_path)
    identity = project_identity(project)
    assert (identity.name, identity.email, identity.source) == (*OWNER, "configured")


# ---- trailers -------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>",
        "Co-authored-by: StageMesh <stagemesh@example.invalid>",
        "Co-authored-by: StageMesh codex worker local-worker <codex+local-worker@stagemesh.invalid>",
        "Co-authored-by: Codex <codex@openai.com>",
        "Co-authored-by: Vishal Singh <12345678+saketvishal@users.noreply.github.com>",
        "Co-authored-by: Vishal Singh <Vishal Singh 12345678+saketvishal@users.noreply.github.com>",
        "Co-authored-by: no email at all",
    ],
)
def test_non_human_and_placeholder_coauthor_trailers_are_stripped(line: str) -> None:
    cleaned = sanitize_commit_message(f"Fix a thing\n\nBody text.\n\n{line}\n")
    assert "co-authored-by" not in cleaned.lower()
    assert cleaned.startswith("Fix a thing\n\nBody text.")


def test_genuine_human_coauthors_and_message_bodies_are_preserved() -> None:
    message = "Subject\n\nBody mentions Co-authored-by: in prose.\n\nCo-authored-by: Ada Lovelace <ada@example.org>\n"
    assert sanitize_commit_message(message) == message
    assert not is_non_human_email("ada@example.org") and is_non_human_email("noreply@anthropic.com")
