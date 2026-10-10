"""Legacy candidates authored as a StageMesh worker are re-attributed to the owner instead of blocking integration forever."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from test_provider_pool import TASK, Rig

import stagemesh.git as git_module
from stagemesh.concurrency import IntegrationLock
from stagemesh.domain import Stage
from stagemesh.git_identity import attribution_offences
from stagemesh.provider_pool import IMPLEMENT, REVIEW
from stagemesh.reattribution import reattribute_range
from stagemesh.serialized_integration import SerializedIntegrator

OWNER = ("Repo Owner", "42+repoowner@users.noreply.github.com")
WORKER = ("StageMesh claude worker local-worker", "claude+local-worker@stagemesh.invalid")
HUMAN = ("Ada Lovelace", "ada@example.org")


@pytest.fixture(autouse=True)
def isolated_git_config(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path_factory.mktemp("gitglobal") / "gitconfig"
    config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(key, raising=False)


def _git(path: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(["git", *args], cwd=path, text=True, capture_output=True, check=True, env=env).stdout.strip()


def _commit(path: Path, name: str, content: str, who: tuple[str, str], message: str, date: str = "1700000000 +0000") -> str:
    (path / name).write_text(content, encoding="utf-8")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": who[0], "GIT_AUTHOR_EMAIL": who[1], "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_NAME": who[0], "GIT_COMMITTER_EMAIL": who[1], "GIT_COMMITTER_DATE": date,
    }
    _git(path, "add", "-A", env=env)
    _git(path, "commit", "-q", "-m", message, env=env)
    return _git(path, "rev-parse", "HEAD")


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", OWNER[0])
    _git(repo, "config", "user.email", OWNER[1])
    return repo


def _log(path: Path, rev_range: str) -> list[str]:
    return _git(path, "log", "--reverse", "--format=%H|%an <%ae>|%cn <%ce>|%ad|%s", "--date=raw", rev_range).splitlines()


def test_only_offending_commits_and_their_descendants_are_recreated_with_trees_authors_and_dates_kept(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = _commit(repo, "a.txt", "base", OWNER, "base")
    clean = _commit(repo, "b.txt", "clean", OWNER, "clean first commit")
    legacy = _commit(repo, "c.txt", "legacy", WORKER, "worker change\n\nCo-authored-by: Claude <noreply@anthropic.com>", "1700000100 +0000")
    human = _commit(repo, "d.txt", "human", HUMAN, "genuine contributor", "1700000200 +0000")

    new_head = reattribute_range(repo, base, human)

    assert new_head is not None and new_head != human
    rows = [row.split("|") for row in _log(repo, f"{base}..{new_head}")]
    assert rows[0][0] == clean  # untouched history keeps its exact SHA
    assert rows[1][1] == f"{OWNER[0]} <{OWNER[1]}>" and rows[1][3] == "1700000100 +0000" and rows[1][4] == "worker change"
    assert rows[2][1] == f"{HUMAN[0]} <{HUMAN[1]}>" and rows[2][3] == "1700000200 +0000"  # genuine authorship survives the rewrite
    assert all(row[2] == f"{OWNER[0]} <{OWNER[1]}>" for row in rows[1:])
    assert "anthropic" not in _git(repo, "log", "-3", "--format=%B", new_head).lower()
    assert _git(repo, "rev-parse", f"{new_head}^{{tree}}") == _git(repo, "rev-parse", f"{legacy[:0] or human}^{{tree}}")
    messages = _git(repo, "log", "--format=%H%x1f%an <%ae>%x1f%cn <%ce>%x1f%B%x1e", f"{base}..{new_head}")
    commits = [tuple(r.strip().split("\x1f", 3)) for r in messages.split("\x1e") if r.strip()]
    assert attribution_offences(list(commits)) == []


def test_nothing_to_fix_merges_and_unapproved_owner_are_left_alone(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = _commit(repo, "a.txt", "base", OWNER, "base")
    clean = _commit(repo, "b.txt", "clean", OWNER, "clean")
    assert reattribute_range(repo, base, clean) is None  # no offence, no rewrite

    _git(repo, "checkout", "-q", "-b", "side", base)
    side = _commit(repo, "s.txt", "side", WORKER, "worker side")
    _git(repo, "checkout", "-q", "-")
    _git(repo, "merge", "--no-ff", "-q", "-m", "merge", side)
    assert reattribute_range(repo, base, _git(repo, "rev-parse", "HEAD")) is None  # a merge is never auto-rewritten

    _git(repo, "config", "user.email", "noreply@anthropic.com")  # an unapproved owner identity cannot be used to re-attribute
    assert reattribute_range(repo, base, side) is None


def test_a_legacy_worker_authored_candidate_is_reattributed_and_integrates_after_fresh_validation_and_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path, {"provider-a": "ok", "provider-b": "ok"}, pools={IMPLEMENT: ("provider-a",), REVIEW: ("provider-b",)})
    # production wiring: `continue` and `queue-run` always integrate through the SerializedIntegrator
    rig.coordinator.integrator = SerializedIntegrator(
        "refs/heads/integration", True, IntegrationLock(rig.project / ".stagemesh" / "integration.lock"), max_rebases=2
    )
    real_identity_env = git_module.identity_env_for
    legacy = {
        "GIT_AUTHOR_NAME": WORKER[0], "GIT_AUTHOR_EMAIL": WORKER[1],
        "GIT_COMMITTER_NAME": "StageMesh", "GIT_COMMITTER_EMAIL": "stagemesh@stagemesh.invalid",
    }
    rig.tick(1)  # PLAN
    monkeypatch.setattr(git_module, "identity_env_for", lambda path, existing=None: dict(legacy))  # how a pre-policy runtime committed
    rig.tick(1)  # IMPLEMENT
    monkeypatch.setattr(git_module, "identity_env_for", real_identity_env)

    first = rig.store.latest_candidate(TASK)["sha"]
    assert "claude+local-worker@stagemesh.invalid" in rig.git.run("log", "-1", "--format=%ae", first).stdout

    rig.tick(12)  # VALIDATE, REVIEW, INTEGRATE (re-attributed, back to VALIDATE), VALIDATE, REVIEW, INTEGRATE

    assert rig.stage == Stage.DONE
    final = rig.store.latest_candidate(TASK)["sha"]
    assert final != first
    assert rig.git.run("rev-parse", f"{final}^{{tree}}").stdout == rig.git.run("rev-parse", f"{first}^{{tree}}").stdout
    authors = rig.git.run("log", "--format=%an <%ae>|%cn <%ce>", f"{rig.base}..integration").stdout.splitlines()
    assert authors and all("local-worker" not in line for line in authors)
    assert rig.ref("integration") == final
    events = [r["payload"] for r in rig.store.conn.execute("SELECT payload FROM audit_events WHERE event_type='integration.reattributed'")]
    assert len(events) == 1 and first in events[0] and final in events[0]
