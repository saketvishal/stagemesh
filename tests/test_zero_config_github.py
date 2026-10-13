from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

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
    assert code == 0
    assert payload["would_run"] == ["17"]


def test_run_window_migrates_existing_install_in_place(tmp_path: Path, monkeypatch, capsys) -> None:
    project = _github_project(tmp_path)
    (project / ".stagemesh").mkdir()
    config_path = project / ".stagemesh" / "config.json"
    config_path.write_text(json.dumps({"github": {"owner": "acme", "repo": "widgets"}}), encoding="utf-8")
    monkeypatch.setattr(GitHubApiIssueSource, "discover", lambda self: ([DiscoveredTask("github", "3", "T", labels=("stagemesh:ready",))], "OK", None))

    code = cli_module.main(["--project", str(project), "run-window", "--plan", "--json", "--report", str(tmp_path / "plan.json")])

    assert code == 0
    assert json.loads(capsys.readouterr().out)["would_run"] == ["3"]
    assert json.loads(config_path.read_text(encoding="utf-8"))["task_sources"][0]["labels"] == ["stagemesh:ready"]


@pytest.mark.parametrize("labels", [["status:QUEUED"], ["stagemesh:ready"], ["status:QUEUED", "area:x"], ["custom"]])
def test_migration_keeps_existing_github_source_labels(tmp_path: Path, labels: list[str]) -> None:
    project = _github_project(tmp_path)
    (project / ".stagemesh").mkdir()
    config_path = project / ".stagemesh" / "config.json"
    source = {"name": "github", "type": "github", "labels": list(labels), "excluded_labels": ["x"]}
    config_path.write_text(json.dumps({"github": {"owner": "acme", "repo": "widgets"}, "task_sources": [source]}), encoding="utf-8")

    assert cli_module.main(["--project", str(project), "init"]) == 0

    assert json.loads(config_path.read_text(encoding="utf-8"))["task_sources"] == [source]


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
