import json

from stagemesh.config import load_config
from stagemesh.task_sources import ConfiguredGitHubTaskSource, DiscoveredTask, GitHubIssueSource, task_sources_from_config


def test_configured_github_task_source_is_loaded_from_config(tmp_path):
    config_dir = tmp_path / ".stagemesh"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "github": {"owner": "example", "repo": "repo"},
                "task_sources": [{"name": "github-ready", "type": "github", "labels": ["stagemesh:ready"]}],
            }
        ),
        encoding="utf-8",
    )

    config = load_config(tmp_path)
    sources = task_sources_from_config(config)

    assert len(sources) == 1
    assert isinstance(sources[0], ConfiguredGitHubTaskSource)
    assert sources[0].name == "github-ready"
    assert sources[0].labels == ("stagemesh:ready",)


def test_github_task_source_filters_to_required_labels():
    source = ConfiguredGitHubTaskSource("example", "repo", None, labels=("stagemesh:ready",))
    source.source = _FakeIssueSource(
        [
            DiscoveredTask("github", "1", "ready", labels=("stagemesh:ready",)),
            DiscoveredTask("github", "2", "other", labels=("bug",)),
        ]
    )

    assert [task.source_id for task in source.discover()] == ["1"]


def test_github_issue_source_blocks_deferred_and_blocked_labels():
    tasks, status = GitHubIssueSource(
        [
            {"number": 1, "title": "ready", "labels": [{"name": "stagemesh:ready"}]},
            {"number": 2, "title": "blocked", "labels": [{"name": "stagemesh:blocked"}]},
            {"number": 3, "title": "deferred", "labels": [{"name": "stagemesh:deferred"}]},
        ]
    ).discover()

    assert status == "OK"
    assert [(task.source_id, task.eligible, task.labels) for task in tasks] == [
        ("1", True, ("stagemesh:ready",)),
        ("2", False, ("stagemesh:blocked",)),
        ("3", False, ("stagemesh:deferred",)),
    ]


class _FakeIssueSource:
    def __init__(self, tasks):
        self.tasks = tasks

    def discover(self):
        return self.tasks, "OK", None
