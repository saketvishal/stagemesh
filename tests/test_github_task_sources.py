import json

from stagemesh.config import load_config
from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage, TaskStatus
from stagemesh.operator_actions import task_details
from stagemesh.persistence import Store
from stagemesh.scheduling import Scheduler
from stagemesh.task_sources import (
    ConfiguredGitHubTaskSource,
    DiscoveredTask,
    GitHubApiIssueSource,
    GitHubIssueSource,
    sync_source,
    task_sources_from_config,
)


def test_configured_github_task_source_is_loaded_from_config(tmp_path):
    config_dir = tmp_path / ".stagemesh"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "github": {"owner": "example", "repo": "repo"},
                "task_sources": [
                    {
                        "name": "github-ready",
                        "type": "github",
                        "labels": ["status:QUEUED"],
                        "excluded_labels": ["stagemesh:blocked"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    config = load_config(tmp_path)
    sources = task_sources_from_config(config)

    assert len(sources) == 1
    assert isinstance(sources[0], ConfiguredGitHubTaskSource)
    assert sources[0].name == "github-ready"
    assert sources[0].labels == ("status:QUEUED",)
    assert sources[0].excluded_labels == ("stagemesh:blocked",)


def test_github_task_source_filters_to_required_labels():
    source = ConfiguredGitHubTaskSource(
        "example",
        "repo",
        None,
        labels=("status:QUEUED",),
        excluded_labels=("stagemesh:blocked", "status:REMEDIATING"),
    )
    source.source = _FakeIssueSource(
        [
            DiscoveredTask("github", "1", "ready", labels=("status:QUEUED",)),
            DiscoveredTask("github", "2", "other", labels=("bug",)),
            DiscoveredTask("github", "3", "blocked", labels=("status:QUEUED", "stagemesh:blocked")),
            DiscoveredTask("github", "4", "remediating", labels=("status:QUEUED", "status:REMEDIATING")),
        ]
    )

    tasks = source.discover()

    assert [(task.source_id, task.eligible) for task in tasks] == [
        ("1", True),
        ("2", False),
        ("3", False),
        ("4", False),
    ]


def test_github_issue_source_blocks_deferred_and_blocked_labels():
    tasks, status = GitHubIssueSource(
        [
            {"number": 1, "title": "ready", "labels": [{"name": "stagemesh:ready"}]},
            {"number": 2, "title": "blocked", "labels": [{"name": "stagemesh:blocked"}]},
            {"number": 3, "title": "deferred", "labels": [{"name": "stagemesh:deferred"}]},
            {"number": 4, "title": "status blocked", "labels": [{"name": "status:BLOCKED"}]},
            {"number": 5, "title": "remediating", "labels": [{"name": "status:REMEDIATING"}]},
        ]
    ).discover()

    assert status == "OK"
    assert [(task.source_id, task.eligible, task.labels) for task in tasks] == [
        ("1", True, ("stagemesh:ready",)),
        ("2", False, ("stagemesh:blocked",)),
        ("3", False, ("stagemesh:deferred",)),
        ("4", False, ("status:BLOCKED",)),
        ("5", False, ("status:REMEDIATING",)),
    ]


class _FakeIssueSource:
    def __init__(self, tasks):
        self.tasks = tasks

    def discover(self):
        return self.tasks, "OK", None


def test_unavailable_github_source_warns_instead_of_looking_empty(capsys):
    source = ConfiguredGitHubTaskSource("example", "repo", None, labels=("stagemesh:ready",))
    source.source.discover = lambda: ([], "STALE", None)

    assert source.discover() == []
    assert "unavailable (STALE)" in capsys.readouterr().err


def test_github_api_issue_source_paginates_all_issue_states(monkeypatch):
    calls = []
    page_one = [
        {"number": i, "title": f"closed {i}", "state": "closed", "labels": []}
        for i in range(1, 101)
    ]
    page_two = [
        {
            "number": 148,
            "title": "desired ready",
            "state": "open",
            "labels": [{"name": "stagemesh:ready"}],
        }
    ]

    def fake_urlopen(request, timeout):
        assert timeout == 20
        calls.append(request.full_url)
        page = page_one if request.full_url.endswith("page=1") else page_two
        return _JsonResponse(page)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    tasks, status, retry_after = GitHubApiIssueSource("example", "repo").discover()

    assert status == "OK"
    assert retry_after is None
    assert [task.source_id for task in tasks][-1] == "148"
    assert len(calls) == 2


def test_syncing_closed_github_issue_retires_local_task_from_auto_selection(tmp_path):
    store = _store(tmp_path)
    try:
        task_id = store.upsert_task("ready", source="github", source_id="1")
        store.advance_task(task_id, Stage.IMPLEMENT)
        claim_id = store.acquire_claim(task_id, "worker-a")
        assert claim_id is not None
        store.add_candidate(task_id, "abc123", "fake", True)
        store.add_evidence(task_id, "abc123", EvidenceKind.VALIDATION, EvidenceStatus.PASSED)

        ids = sync_source(
            store,
            [DiscoveredTask("github", "1", "ready", eligible=False, state="CLOSED")],
        )

        task = store.get_task(task_id)
        assert ids == []
        assert task["stage"] == Stage.IMPLEMENT
        assert task["status"] == TaskStatus.BLOCKED
        assert Scheduler(store).decision(task_id).reason == "source closed"
        assert store.latest_candidate(task_id)["sha"] == "abc123"
        assert store.has_evidence(task_id, "abc123", EvidenceKind.VALIDATION)
        active_claim = store.conn.execute(
            "SELECT 1 FROM claims WHERE task_id=? AND active=1",
            (task_id,),
        ).fetchone()
        assert active_claim is None
        assert task_details(store, task_id)["source_reason"] == "source closed"
    finally:
        store.close()


def test_syncing_github_issue_without_required_label_retires_local_task(tmp_path):
    store = _store(tmp_path)
    try:
        task_id = store.upsert_task("ready", source="github", source_id="1")

        ids = sync_source(
            store,
            [DiscoveredTask("github", "1", "ready", eligible=False, labels=())],
        )

        assert ids == []
        assert store.get_task(task_id)["status"] == TaskStatus.BLOCKED
        assert Scheduler(store).decision(task_id).reason == "source no longer eligible"
        assert task_details(store, task_id)["source_reason"] == "source no longer eligible"
    finally:
        store.close()


def test_closed_github_sync_preserves_done_task(tmp_path):
    store = _store(tmp_path)
    try:
        task_id = store.upsert_task("done", source="github", source_id="1")
        store.advance_task(task_id, Stage.DONE)

        sync_source(
            store,
            [DiscoveredTask("github", "1", "done", eligible=False, state="CLOSED")],
        )

        task = store.get_task(task_id)
        assert task["stage"] == Stage.DONE
        assert task["status"] == TaskStatus.DONE
        assert Scheduler(store).decision(task_id).reason == "done"
    finally:
        store.close()


def test_reopened_github_issue_restores_source_retired_task(tmp_path):
    store = _store(tmp_path)
    try:
        task_id = store.upsert_task("ready", source="github", source_id="1")
        sync_source(
            store,
            [DiscoveredTask("github", "1", "ready", eligible=False, state="CLOSED")],
        )

        ids = sync_source(
            store,
            [DiscoveredTask("github", "1", "ready", labels=("stagemesh:ready",))],
        )

        assert ids == [task_id]
        assert store.get_task(task_id)["status"] == TaskStatus.OPEN
        assert Scheduler(store).decision(task_id).reason == "eligible"
        assert task_details(store, task_id)["source_reason"] is None
    finally:
        store.close()


def test_relabelled_github_issue_restores_source_retired_task(tmp_path):
    store = _store(tmp_path)
    try:
        task_id = store.upsert_task("ready", source="github", source_id="1")
        sync_source(
            store,
            [DiscoveredTask("github", "1", "ready", eligible=False, labels=())],
        )

        ids = sync_source(
            store,
            [DiscoveredTask("github", "1", "ready", labels=("stagemesh:ready",))],
        )

        assert ids == [task_id]
        assert store.get_task(task_id)["status"] == TaskStatus.OPEN
        assert Scheduler(store).decision(task_id).reason == "eligible"
        assert task_details(store, task_id)["source_reason"] is None
    finally:
        store.close()


def test_ready_label_drift_is_recorded_and_reconciled(tmp_path):
    store = _store(tmp_path)
    try:
        ready = ("stagemesh:ready",)
        task_id = store.upsert_task("ready", source="github", source_id="1")
        sync_source(store, [DiscoveredTask("github", "1", "ready", labels=ready)])
        sync_source(store, [DiscoveredTask("github", "1", "ready", eligible=False, labels=())])

        assert store.get_task(task_id)["status"] == TaskStatus.BLOCKED
        ids = sync_source(store, [DiscoveredTask("github", "1", "ready", labels=ready)])

        assert ids == [task_id]
        assert store.get_task(task_id)["status"] == TaskStatus.OPEN
        statuses = [
            row["status"]
            for row in store.conn.execute(
                "SELECT status FROM source_events WHERE source='github' AND source_id='1' ORDER BY rowid"
            )
        ]
        assert [s for s in statuses if s.startswith("READY_")] == ["READY_REMOVED", "READY_RESTORED"]
    finally:
        store.close()


def test_task_source_label_filter_change_retires_previously_matching_issue(tmp_path):
    store = _store(tmp_path)
    try:
        task_id = store.upsert_task("old ready", source="github", source_id="1")
        source = ConfiguredGitHubTaskSource(
            "example",
            "repo",
            None,
            labels=("stagemesh:ready",),
        )
        source.source = _FakeIssueSource(
            [DiscoveredTask("github", "1", "old ready", labels=("bug",))]
        )

        ids = sync_source(store, source.discover())

        assert ids == []
        assert store.get_task(task_id)["status"] == TaskStatus.BLOCKED
        assert Scheduler(store).decision(task_id).reason == "source no longer eligible"
    finally:
        store.close()


def _store(tmp_path) -> Store:
    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    return store


class _JsonResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")
