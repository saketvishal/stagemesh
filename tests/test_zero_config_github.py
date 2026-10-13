from __future__ import annotations

import json
import subprocess
from pathlib import Path

import stagemesh.cli as cli_module
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


def test_init_persists_detected_github_origin_as_the_ready_issue_source(tmp_path: Path) -> None:
    project = _github_project(tmp_path)

    assert cli_module.main(["--project", str(project), "init", "--json"]) == 0

    data = json.loads((project / ".stagemesh" / "config.json").read_text(encoding="utf-8"))
    assert data["github"] == {"owner": "acme", "repo": "widgets"}
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


def test_run_window_plan_syncs_ready_github_issues_from_zero_config(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    project = _github_project(tmp_path)
    assert cli_module.main(["--project", str(project), "init"]) == 0
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
    assert code in {0, 2}
    assert payload["would_run"] == ["17"]


def test_run_window_missing_github_source_refuses_with_one_fix(tmp_path: Path, capsys) -> None:
    project = _github_project(tmp_path)
    (project / ".stagemesh").mkdir()
    (project / ".stagemesh" / "config.json").write_text(
        json.dumps({"github": {"owner": "acme", "repo": "widgets"}}),
        encoding="utf-8",
    )

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
    failed = [check for check in payload["preflight"] if not check["ok"] and check["fatal"]]
    assert code == 2
    assert failed == [
        {
            "name": "task_sources",
            "ok": False,
            "detail": "GitHub repository acme/widgets has no GitHub task source configured",
            "fatal": True,
            "fix": "stagemesh init",
        }
    ]


def test_bootstrap_migrates_existing_github_config_to_ready_issue_source(tmp_path: Path) -> None:
    from test_bootstrap_stagemesh import _load_bootstrap

    bootstrap = _load_bootstrap()
    bootstrap.PROJECT = tmp_path
    (tmp_path / ".stagemesh").mkdir()
    (tmp_path / ".stagemesh" / "config.json").write_text(
        json.dumps({"github": {"owner": "acme", "repo": "widgets"}}),
        encoding="utf-8",
    )

    bootstrap._configure_queue_source()

    data = json.loads((tmp_path / ".stagemesh" / "config.json").read_text(encoding="utf-8"))
    assert data["task_sources"][0]["type"] == "github"
    assert data["task_sources"][0]["labels"] == ["stagemesh:ready"]
