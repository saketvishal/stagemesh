from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import stagemesh.cli as cli_module
import stagemesh.github as github_module
from stagemesh.github import detect_github_repository
from stagemesh.persistence import Store
from stagemesh.task_sources import DiscoveredTask, GitHubApiIssueSource


def _git(project: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={project.resolve().as_posix()}", *args],
        cwd=project,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _github_project(tmp_path: Path, remote: str = "git@github.com:acme/widgets.git") -> Path:
    project = tmp_path / "repo"
    project.mkdir()
    _git(project, "init", "-q", "-b", "main")
    _git(project, "remote", "add", "origin", remote)
    return project


def _prepared_contract(project: Path, task: str) -> None:
    from test_run_ready import FAKE_CONTRACT
    directory = project / ".stagemesh/contracts"
    directory.mkdir(exist_ok=True)
    (directory / f"{task}.json").write_text(json.dumps(FAKE_CONTRACT), encoding="utf-8")


def _prepare_pr_repository(project: Path) -> None:
    """Give read-only PR-plan fixtures matching local/origin branch tips without network access."""
    (project / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(project, "add", "seed.txt")
    _git(
        project,
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "user.name=StageMesh Test",
        "commit",
        "-q",
        "-m",
        "seed",
    )
    _git(project, "update-ref", "refs/remotes/origin/main", _git(project, "rev-parse", "HEAD"))


def _disable_independent_review_for_plan(project: Path) -> None:
    """Keep read-only plan tests scoped to planning instead of host provider inventory."""
    config_path = project / ".stagemesh" / "config.json"
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["providers"] = {
        "local-agent": {
            "command": f'"{sys.executable}"',
            "capabilities": ["IMPLEMENT", "REVIEW"],
        }
    }
    data["authorization"] = {"unattended_providers": ["local-agent"]}
    data["routing"] = {
        **data.get("routing", {}),
        "require_independent_review": False,
    }
    config_path.write_text(json.dumps(data), encoding="utf-8")


def test_repository_detection_reuses_unchanged_local_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _github_project(tmp_path)
    github_module._detect_github_repository_cached.cache_clear()
    real_run = github_module.subprocess.run
    calls: list[list[str]] = []

    def counted(*args, **kwargs):
        calls.append(args[0])
        return real_run(*args, **kwargs)

    monkeypatch.setattr(github_module.subprocess, "run", counted)
    found = detect_github_repository(project)
    assert found is not None and (found.owner, found.repo) == ("acme", "widgets")
    found = detect_github_repository(project)
    assert found is not None and (found.owner, found.repo) == ("acme", "widgets")
    assert len(calls) == 1


def test_repository_detection_invalidates_when_remote_changes(tmp_path: Path) -> None:
    project = _github_project(tmp_path)
    github_module._detect_github_repository_cached.cache_clear()
    found = detect_github_repository(project)
    assert found is not None and (found.owner, found.repo) == ("acme", "widgets")
    _git(project, "remote", "set-url", "origin", "https://github.com/acme/changed.git")
    found = detect_github_repository(project)
    assert found is not None and (found.owner, found.repo) == ("acme", "changed")


def test_github_cli_token_is_bounded_captured_and_never_printed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, object] = {}

    def run(argv, **kwargs):
        seen.update({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(argv, 0, stdout="TEST_ONLY_TOKEN\n", stderr="")

    monkeypatch.setattr(github_module.subprocess, "run", run)

    assert github_module.github_cli_token() == "TEST_ONLY_TOKEN"
    assert seen["argv"] == ["gh", "auth", "token"]
    assert seen["capture_output"] is True and seen["check"] is False and seen["timeout"] == 5.0
    assert "TEST_ONLY_TOKEN" not in capsys.readouterr().out


def test_init_persists_detected_github_origin_as_the_ready_issue_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _github_project(tmp_path)
    monkeypatch.setattr(GitHubApiIssueSource, "discover", lambda self: ([], "OK", None))

    assert cli_module.main(["--project", str(project), "init", "--json", "--allow-incomplete"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["task_source_sync"] == {"status": "synced"}
    data = json.loads((project / ".stagemesh" / "config.json").read_text(encoding="utf-8"))
    assert data["github"] == {
        "owner": "acme",
        "repo": "widgets",
        "auto_label_unlabeled": True,
    }
    assert data["task_sources"] == [
        {
            "name": "github",
            "type": "github",
            "labels": ["stagemesh:ready"],
            "excluded_labels": [
                "stagemesh:blocked",
                "stagemesh:deferred",
                "status:BLOCKED",
                "status:REMEDIATING",
            ],
        }
    ]


def test_init_classifies_unclassified_issues_preserves_team_labels_and_resyncs_ready_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from stagemesh.github import GitHubClient, GitHubResult

    project = _github_project(tmp_path)
    (project / ".stagemesh").mkdir()
    (project / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    contracts = project / ".stagemesh/contracts"
    contracts.mkdir()
    (contracts / "19.json").write_text("{}\n", encoding="utf-8")
    labels: dict[str, tuple[str, ...]] = {
        "17": (),
        "18": ("team:owned",),
        "19": (),
        "20": ("stagemesh:deferred",),
        "21": ("caventra:objective",),
    }

    def discover(self: GitHubApiIssueSource):
        return (
            [
                DiscoveredTask("github", issue, f"Issue {issue}", labels=issue_labels)
                for issue, issue_labels in labels.items()
            ],
            "OK",
            None,
        )

    writes: list[tuple[str, str]] = []

    def ensure_label(self: GitHubClient, name: str, color: str, description: str) -> GitHubResult:
        assert color and description
        return GitHubResult("OK", {"name": name})

    def add_issue_labels(self: GitHubClient, number: str, added: tuple[str, ...]) -> GitHubResult:
        labels[number] = (*labels[number], *added)
        writes.append((number, added[0]))
        return GitHubResult("OK", {})

    monkeypatch.setattr(GitHubApiIssueSource, "discover", discover)
    monkeypatch.setattr(GitHubClient, "ensure_label", ensure_label)
    monkeypatch.setattr(GitHubClient, "add_issue_labels", add_issue_labels)
    monkeypatch.setenv("STAGEMESH_GITHUB_TOKEN", "TEST_ONLY_NOT_A_CREDENTIAL")

    assert cli_module.main(["--project", str(project), "init", "--json", "--allow-incomplete"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["github_issue_classification"] == {
        "status": "labeled",
        "scanned": 3,
        "ready_labeled": 2,
        "triage_labeled": 1,
        "failed": 0,
    }
    assert writes == [
        ("17", "stagemesh:ready"),
        ("18", "stagemesh:ready"),
        ("19", "stagemesh:needs-triage"),
    ]
    assert labels["18"] == ("team:owned", "stagemesh:ready")
    assert labels["20"] == ("stagemesh:deferred",)
    assert labels["21"] == ("caventra:objective",)
    store = Store(project / ".stagemesh/stagemesh.sqlite3")
    assert store.source_state("github", "17")["eligible"] is True
    assert store.source_state("github", "18")["eligible"] is True
    assert store.source_state("github", "19")["eligible"] is False
    events = [json.loads(row["payload"]) for row in store.audit_events() if row["event_type"] == "github.init_issue_classified"]
    assert {(event["source_id"], event["outcome"]) for event in events} == {
        ("17", "LABELED"),
        ("18", "LABELED"),
        ("19", "LABELED"),
    }
    store.close()


def test_interactive_init_uses_authenticated_gh_cli_without_persisting_its_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from stagemesh.github import GitHubClient, GitHubResult

    project = _github_project(tmp_path)
    (project / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    labels = ["team:owned"]

    def discover(self: GitHubApiIssueSource):
        return ([DiscoveredTask("github", "31", "Team-labeled issue", labels=tuple(labels))], "OK", None)

    monkeypatch.delenv("STAGEMESH_GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(cli_module, "discovered_provider_entries", dict)
    monkeypatch.setattr(cli_module.sys, "stdin", type("TTY", (), {"isatty": lambda self: True})())
    monkeypatch.setattr(cli_module, "github_cli_token", lambda: "TEST_ONLY_GH_TOKEN")
    monkeypatch.setattr(GitHubApiIssueSource, "discover", discover)
    monkeypatch.setattr(
        GitHubClient,
        "ensure_label",
        lambda self, name, color, description: GitHubResult("OK", {"name": name}),
    )

    def add_issue_labels(self: GitHubClient, number: str, added: tuple[str, ...]) -> GitHubResult:
        assert number == "31" and added == ("stagemesh:ready",)
        labels.extend(added)
        return GitHubResult("OK", {})

    monkeypatch.setattr(GitHubClient, "add_issue_labels", add_issue_labels)

    assert cli_module.main(["--project", str(project), "init", "--allow-incomplete"]) == 0

    output = capsys.readouterr().out
    assert "used the authenticated GitHub CLI" in output
    assert "classified GitHub issues (ready: 1; needs triage: 0; failed: 0)" in output
    assert labels == ["team:owned", "stagemesh:ready"]
    config = json.loads((project / ".stagemesh/config.json").read_text(encoding="utf-8"))
    assert "token" not in config["github"]


def test_noninteractive_init_can_explicitly_use_gh_cli_without_persisting_its_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _github_project(tmp_path)
    seen: list[str] = []

    monkeypatch.delenv("STAGEMESH_GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(cli_module.sys, "stdin", type("Pipe", (), {"isatty": lambda self: False})())
    monkeypatch.setattr(cli_module, "github_cli_token", lambda: seen.append("gh") or "TEST_ONLY_GH_TOKEN")
    monkeypatch.setattr(GitHubApiIssueSource, "discover", lambda self: ([], "OK", None))

    assert (
        cli_module.main(
            ["--project", str(project), "init", "--json", "--allow-incomplete", "--use-gh-auth"]
        )
        == 0
    )

    report = json.loads(capsys.readouterr().out)
    assert seen == ["gh"]
    assert report["github_auth_source"] == "gh_cli"
    config = json.loads((project / ".stagemesh/config.json").read_text(encoding="utf-8"))
    assert "token" not in config["github"]


def test_init_defers_unlabeled_issue_writes_without_github_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _github_project(tmp_path)
    monkeypatch.delenv("STAGEMESH_GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(
        GitHubApiIssueSource,
        "discover",
        lambda self: ([DiscoveredTask("github", "21", "Unlabeled issue")], "OK", None),
    )

    assert cli_module.main(["--project", str(project), "init", "--json", "--allow-incomplete"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["github_issue_classification"] == {
        "status": "write_authorization_required",
        "scanned": 1,
        "ready_labeled": 0,
        "triage_labeled": 0,
        "failed": 0,
        "reason": "use an interactive terminal with an authenticated GitHub CLI, or set STAGEMESH_GITHUB_TOKEN with Issues write permission",
    }
    assert "github_issue_classification" in {item["name"] for item in report["readiness"]["blockers"]}


def test_config_rejects_non_boolean_unlabeled_issue_policy(tmp_path: Path) -> None:
    project = _github_project(tmp_path)
    path = project / ".stagemesh/config.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"github": {"auto_label_unlabeled": "yes"}}), encoding="utf-8")

    assert cli_module.main(["--project", str(project), "init"]) == 2


def test_init_reports_deferred_source_sync_without_leaking_exception_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _github_project(tmp_path)

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("provider response included SECRET_VALUE")

    monkeypatch.setattr(cli_module, "_sync_all_sources", unavailable)

    assert cli_module.main(["--project", str(project), "init", "--json", "--allow-incomplete"]) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["task_source_sync"] == {"status": "deferred", "error_type": "RuntimeError"}
    assert "task_source_sync" in {item["name"] for item in payload["readiness"]["blockers"]}
    assert "task-source sync deferred (RuntimeError)" in captured.err
    assert "SECRET_VALUE" not in captured.out + captured.err

    text_root = tmp_path / "text-mode"
    text_root.mkdir()
    text_project = _github_project(text_root)
    assert cli_module.main(["--project", str(text_project), "init", "--allow-incomplete"]) == 0
    captured = capsys.readouterr()
    assert "StageMesh setup needs attention" in captured.out
    assert "task-source sync deferred (RuntimeError)" in captured.err
    assert "SECRET_VALUE" not in captured.out + captured.err


def test_run_window_plan_previews_ready_issues_without_persisting_them(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("STAGEMESH_CODEX_CMD", sys.executable)
    project = _github_project(tmp_path)
    assert cli_module.main(["--project", str(project), "init", "--allow-incomplete"]) == 0
    _disable_independent_review_for_plan(project)
    _prepare_pr_repository(project)
    _prepared_contract(project, "17")
    capsys.readouterr()

    def discover(self: GitHubApiIssueSource):
        return (
            [
                DiscoveredTask(
                    "github",
                    "17",
                    "Ship the widget",
                    labels=("stagemesh:ready",),
                )
            ],
            "OK",
            None,
        )

    monkeypatch.setattr(GitHubApiIssueSource, "discover", discover)

    code = cli_module.main(
        [
            "--project",
            str(project),
            "run-window",
            "--plan",
            "--json",
            "--report",
            str(tmp_path / "plan.json"),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["would_run"] == ["17"]
    from stagemesh.persistence import Store
    store = Store(project / ".stagemesh/stagemesh.sqlite3")
    assert store.get_task("17") is None
    store.close()


def test_init_migrates_existing_install_before_read_only_plan(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("STAGEMESH_CODEX_CMD", sys.executable)
    project = _github_project(tmp_path)
    (project / ".stagemesh").mkdir()
    config_path = project / ".stagemesh" / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "github": {"owner": "acme", "repo": "widgets"},
                "providers": {
                    "local-agent": {
                        "command": f'"{sys.executable}"',
                        "capabilities": ["IMPLEMENT", "REVIEW"],
                    }
                },
                "authorization": {"unattended_providers": ["local-agent"]},
                "routing": {"require_independent_review": False},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(GitHubApiIssueSource, "discover", lambda self: ([DiscoveredTask("github", "3", "T", labels=("stagemesh:ready",))], "OK", None))
    assert cli_module.main(["--project", str(project), "init", "--allow-incomplete"]) == 0
    capsys.readouterr()
    before = config_path.read_bytes()
    _prepare_pr_repository(project)
    _prepared_contract(project, "3")

    code = cli_module.main(["--project", str(project), "run-window", "--plan", "--json", "--report", str(tmp_path / "plan.json")])

    assert code == 0
    assert json.loads(capsys.readouterr().out)["would_run"] == ["3"]
    migrated = json.loads(config_path.read_text(encoding="utf-8"))
    assert migrated["task_sources"][0]["labels"] == ["stagemesh:ready"]
    assert migrated["github"]["auto_label_unlabeled"] is True
    assert config_path.read_bytes() == before


def test_run_window_plan_preserves_explicit_legacy_source_policy(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("STAGEMESH_CODEX_CMD", sys.executable)
    project = _github_project(tmp_path)
    (project / ".stagemesh").mkdir()
    config_path = project / ".stagemesh" / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "github": {"owner": "acme", "repo": "widgets"},
                "task_sources": [{"name": "github", "type": "github", "labels": ["status:QUEUED"]}],
                "providers": {
                    "local-agent": {
                        "command": f'"{sys.executable}"',
                        "capabilities": ["IMPLEMENT", "REVIEW"],
                    }
                },
                "authorization": {"unattended_providers": ["local-agent"]},
                "routing": {"require_independent_review": False},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        GitHubApiIssueSource,
        "discover",
        lambda self: ([DiscoveredTask("github", "4", "T", labels=("stagemesh:ready",))], "OK", None),
    )
    _prepare_pr_repository(project)

    code = cli_module.main(["--project", str(project), "run-window", "--plan", "--json", "--report", str(tmp_path / "plan.json")])

    assert code == 0
    assert json.loads(capsys.readouterr().out)["would_run"] == []
    assert json.loads(config_path.read_text(encoding="utf-8"))["task_sources"][0]["labels"] == ["status:QUEUED"]


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (["status:QUEUED"], ["status:QUEUED"]),
        (["stagemesh:ready"], ["stagemesh:ready"]),
        (["status:QUEUED", "area:x"], ["status:QUEUED", "area:x"]),
        (["custom"], ["custom"]),
    ],
)
def test_init_preserves_explicit_github_source_policy(
    tmp_path: Path, labels: list[str], expected: list[str]
) -> None:
    project = _github_project(tmp_path)
    (project / ".stagemesh").mkdir()
    config_path = project / ".stagemesh" / "config.json"
    source = {"name": "github", "type": "github", "labels": list(labels), "excluded_labels": ["x"]}
    config_path.write_text(json.dumps({"github": {"owner": "acme", "repo": "widgets"}, "task_sources": [source]}), encoding="utf-8")

    assert cli_module.main(["--project", str(project), "init", "--allow-incomplete"]) == 0

    source["labels"] = expected
    migrated = json.loads(config_path.read_text(encoding="utf-8"))
    assert migrated["task_sources"] == [source]
    assert migrated["github"]["auto_label_unlabeled"] is True


def test_init_migrates_existing_github_admission_and_refuses_ready_without_write_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _github_project(tmp_path)
    (project / ".stagemesh").mkdir()
    (project / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    config_path = project / ".stagemesh/config.json"
    source = {
        "name": "github",
        "type": "github",
        "labels": ["stagemesh:ready"],
        "excluded_labels": ["stagemesh:blocked", "stagemesh:deferred"],
    }
    config_path.write_text(
        json.dumps({"github": {"owner": "acme", "repo": "widgets"}, "task_sources": [source]}),
        encoding="utf-8",
    )
    monkeypatch.delenv("STAGEMESH_GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(
        GitHubApiIssueSource,
        "discover",
        lambda self: ([DiscoveredTask("github", "9", "Scoped task", labels=("autonomous-task",))], "OK", None),
    )

    assert cli_module.main(["--project", str(project), "init", "--json", "--allow-incomplete"]) == 0

    report = json.loads(capsys.readouterr().out)
    migrated = json.loads(config_path.read_text(encoding="utf-8"))
    assert migrated["task_sources"] == [source]
    assert migrated["github"]["auto_label_unlabeled"] is True
    assert report["github_issue_classification"]["status"] == "write_authorization_required"
    assert "github_issue_classification" in {item["name"] for item in report["readiness"]["blockers"]}


def test_init_preserves_explicit_github_classification_opt_out(tmp_path: Path) -> None:
    project = _github_project(tmp_path)
    config_path = project / ".stagemesh/config.json"
    config_path.parent.mkdir()
    config_path.write_text(
        json.dumps(
            {
                "github": {"owner": "acme", "repo": "widgets", "auto_label_unlabeled": False},
                "task_sources": [],
            }
        ),
        encoding="utf-8",
    )

    assert cli_module.main(["--project", str(project), "init", "--allow-incomplete"]) == 0

    migrated = json.loads(config_path.read_text(encoding="utf-8"))
    assert migrated["github"]["auto_label_unlabeled"] is False
    assert migrated["task_sources"] == []


def test_run_window_unmigratable_missing_source_refuses_with_one_fix_and_writes_report(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    project = _github_project(tmp_path)
    (project / ".stagemesh").mkdir()
    (project / ".stagemesh" / "config.json").write_text(
        json.dumps({"github": {"owner": "acme", "repo": "widgets"}}), encoding="utf-8"
    )
    monkeypatch.setattr(cli_module, "_ensure_default_github_task_source", lambda project: False)
    report = tmp_path / "plan.json"

    code = cli_module.main(["--project", str(project), "run-window", "--plan", "--json", "--report", str(report)])

    payload = json.loads(capsys.readouterr().out)
    failed = [check for check in payload["preflight"] if not check["ok"] and check["fatal"]]
    assert code == 2
    assert [check["fix"] for check in failed] == ["stagemesh init"]
    assert json.loads(report.read_text(encoding="utf-8")) == payload


@pytest.mark.parametrize("sources", [[], [{"name": "local", "type": "json", "path": ".stagemesh/backlog.json"}]])
def test_init_preserves_explicit_disabled_or_local_sources(tmp_path: Path, sources) -> None:
    project = _github_project(tmp_path)
    path = project / ".stagemesh/config.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"task_sources": sources}), encoding="utf-8")
    assert cli_module.main(["--project", str(project), "init", "--allow-incomplete"]) == 0
    initialized = json.loads(path.read_text(encoding="utf-8"))
    assert initialized["task_sources"] == sources
    assert "providers" in initialized  # provider initialization is independent of task-source policy


@pytest.mark.parametrize("value", [False, 0, [], ""])
def test_init_rejects_invalid_github_fields_before_writing(tmp_path: Path, value) -> None:
    project = _github_project(tmp_path)
    path = project / ".stagemesh/config.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"github": {"owner": value}}), encoding="utf-8")
    before = path.read_bytes()
    assert cli_module.main(["--project", str(project), "init"]) == 2
    assert path.read_bytes() == before
    assert not (path.parent / "stagemesh.sqlite3").exists()


def test_plan_missing_source_does_not_create_an_implicit_report(tmp_path: Path, capsys) -> None:
    project = _github_project(tmp_path)
    path = project / ".stagemesh/config.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"github": {"owner": "acme", "repo": "widgets"}}), encoding="utf-8")
    assert cli_module.main(["--project", str(project), "run-window", "--plan", "--json"]) == 2
    assert not (path.parent / "reports").exists()
    assert not (path.parent / "stagemesh.sqlite3").exists()
