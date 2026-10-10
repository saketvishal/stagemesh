"""Forward attribution guards: a provider, StageMesh worker or placeholder account can never become a repository contributor."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
from pathlib import Path

import pytest
from test_provider_pool import Rig

import stagemesh.cli as cli_module
import stagemesh.providers as providers_module
from stagemesh.config import load_config
from stagemesh.domain import EvidenceKind, EvidenceStatus
from stagemesh.git import GitWorkspace
from stagemesh.git_identity import (
    GitIdentityError,
    attribution_offences,
    identity_report,
    provider_environment,
    validate_identity,
)
from stagemesh.integration import _attribution_findings
from stagemesh.provider_pool import IMPLEMENT, REVIEW
from stagemesh.queue_run import preflight

OWNER = ("Repo Owner", "42+repoowner@users.noreply.github.com")
TOOL_EMAILS = ["noreply@anthropic.com", "codex+local-worker@stagemesh.invalid", "bot@openai.com", "grok@x.ai"]


@pytest.fixture(autouse=True)
def isolated_git_config(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path_factory.mktemp("gitglobal")
    global_config = home / "gitconfig"
    global_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(key, raising=False)
    return global_config


def _raw(path: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(["git", *args], cwd=path, text=True, capture_output=True, check=True, env=env).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _raw(repo, "init", "-q")
    _raw(repo, "config", "user.name", OWNER[0])
    _raw(repo, "config", "user.email", OWNER[1])
    return repo


def _commit(repo: Path, message: str, name: str, email: str) -> str:
    (repo / "f.txt").write_text(message, encoding="utf-8")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
    }
    _raw(repo, "add", "-A", env=env)
    _raw(repo, "commit", "-q", "-m", message, env=env)
    return _raw(repo, "rev-parse", "HEAD")


def test_provider_environment_pins_the_owner_identity_over_any_other_git_configuration(tmp_path: Path, isolated_git_config: Path) -> None:
    isolated_git_config.write_text("[user]\n\tname = claude\n\temail = noreply@anthropic.com\n", encoding="utf-8")  # the tool's own config
    repo = _repo(tmp_path)

    env = provider_environment(repo)

    assert env["GIT_AUTHOR_EMAIL"] == env["GIT_COMMITTER_EMAIL"] == OWNER[1]
    (repo / "x.txt").write_text("x", encoding="utf-8")
    _raw(repo, "add", "-A", env=env)
    _raw(repo, "commit", "-q", "-m", "made by a provider", env=env)
    assert _raw(repo, "log", "-1", "--format=%an <%ae>|%cn <%ce>") == f"{OWNER[0]} <{OWNER[1]}>|{OWNER[0]} <{OWNER[1]}>"


def test_implementation_and_review_providers_are_launched_with_the_project_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, {"provider-a": "ok", "provider-b": "ok"}, pools={IMPLEMENT: ("provider-a",), REVIEW: ("provider-b",)})
    launched: list[dict[str, str]] = []
    real_popen = providers_module.subprocess.Popen

    def recording_popen(*args, **kwargs):
        if "provider.py" in " ".join(map(str, args[0])):  # the global Popen also serves git and process helpers
            launched.append(dict(kwargs.get("env") or {}))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(providers_module.subprocess, "Popen", recording_popen)
    rig.tick(4)  # PLAN, IMPLEMENT, VALIDATE, REVIEW

    assert len(launched) >= 2, "expected an implementation and a review launch"
    for env in launched:
        assert env["GIT_COMMITTER_EMAIL"] == env["GIT_AUTHOR_EMAIL"] == "t@example.invalid"
        assert env["GIT_COMMITTER_NAME"] == env["GIT_AUTHOR_NAME"] == "T"


@pytest.mark.parametrize("email", TOOL_EMAILS)
def test_tool_and_worker_identities_are_refused_before_any_commit(tmp_path: Path, email: str) -> None:
    with pytest.raises(GitIdentityError):
        validate_identity("Some Tool", email)
    repo = _repo(tmp_path)
    _raw(repo, "config", "user.email", email)
    (repo / "a.txt").write_text("a", encoding="utf-8")
    with pytest.raises(GitIdentityError):
        GitWorkspace(repo).commit_all("must not be created")
    assert subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=repo, capture_output=True).returncode != 0  # no commit exists


def test_attribution_offences_name_every_leak_kind() -> None:
    commits = [
        ("a" * 40, "Vishal <42+owner@users.noreply.github.com>", "Vishal <42+owner@users.noreply.github.com>", "clean\n"),
        ("b" * 40, "codex <codex+local-worker@stagemesh.invalid>", "Owner <o@example.org>", "x\n"),
        ("c" * 40, "Owner <o@example.org>", "Owner <o@example.org>", "x\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n"),
        ("d" * 40, "Owner <o@example.org>", "Owner <o@example.org>", "x\n\nCo-authored-by: Real Person <person@example.org>\n"),
        ("e" * 40, "Owner <12345678+owner@users.noreply.github.com>", "Owner <o@example.org>", "x\n"),
    ]
    text = " | ".join(attribution_offences(commits))
    assert "bbbbbbbbbb author" in text and "cccccccccc trailer" in text and "eeeeeeeeee author" in text
    assert "aaaaaaaaaa" not in text and "dddddddddd" not in text  # owner and genuine human co-authors are fine


def test_integration_gate_flags_only_the_candidates_own_leaking_commits(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _commit(repo, "history already published", "Old", "claude@anthropic.com")  # existing history never blocks new work
    base = _raw(repo, "rev-parse", "HEAD")
    clean = _commit(repo, "clean change", *OWNER)
    assert _attribution_findings(repo, base, clean) == []

    leaked = _commit(repo, "leaked\n\nCo-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>", *OWNER)
    findings = _attribution_findings(repo, base, leaked)

    assert [f["code"] for f in findings] == ["attribution_violation"]
    assert leaked[:10] in findings[0]["message"] and clean[:10] not in findings[0]["message"]


def test_a_leaking_candidate_is_not_integrated_through_the_coordinator_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, {"provider-a": "ok", "provider-b": "ok"}, pools={IMPLEMENT: ("provider-a",), REVIEW: ("provider-b",)})
    seen: list[str] = []

    def leaking(project, baseline, candidate):
        seen.append(candidate)
        return [{"severity": "error", "code": "attribution_violation", "message": "forced leak"}]

    monkeypatch.setattr("stagemesh.integration._attribution_findings", leaking)
    before = rig.ref("integration")
    rig.tick(6)

    assert seen, "integration never consulted the attribution gate"
    row = rig.store.conn.execute(
        "SELECT status, payload FROM evidence WHERE kind=? ORDER BY created_at DESC", (EvidenceKind.INTEGRATION,)
    ).fetchone()
    assert row["status"] == EvidenceStatus.FAILED
    assert "attribution_violation" in json.dumps(json.loads(row["payload"])["findings"])
    assert rig.ref("integration") == before


def test_identity_report_distinguishes_ok_synthetic_and_unapproved(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert identity_report(repo)["status"] == "ok"

    _raw(repo, "config", "user.email", "claude+local-worker@stagemesh.invalid")
    report = identity_report(repo)
    assert report["status"] == "unapproved" and "AI provider or StageMesh worker" in str(report["problem"])

    _raw(repo, "config", "--unset", "user.email")
    _raw(repo, "config", "--unset", "user.name")
    assert identity_report(repo)["status"] == "synthetic"


def test_doctor_and_preflight_surface_an_unapproved_identity(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert cli_module.main(["--project", str(repo), "doctor", "--json"]) == 0
    assert json.loads(out.getvalue())["git_identity"]["status"] == "ok"
    assert preflight(repo, load_config(repo), require_ref=False)["ok"] is True

    _raw(repo, "config", "user.email", "noreply@anthropic.com")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli_module.main(["--project", str(repo), "doctor"])
    assert "WARNING git identity" in out.getvalue() and "[unapproved]" in out.getvalue()
    result = preflight(repo, load_config(repo), require_ref=False)
    assert result["ok"] is False
    assert any(c["name"] == "git identity is approved" and not c["ok"] for c in result["checks"])
