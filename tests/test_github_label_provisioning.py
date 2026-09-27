"""Regression coverage for stagemesh:* GitHub label auto-provisioning (SM-008).

Verifies:
1. A brand-new repo with zero StageMesh labels gets `stagemesh:done` created
   on first sync, with no operator setup required.
2. Label provisioning is idempotent: it is not re-attempted once confirmed.
3. Label provisioning only runs when the GitHub adapter is enabled (repo set).
4. Permission/network/API failures during provisioning are recorded as
   durable outbound-sync evidence and never raised -- local task state is
   unaffected.
5. DONE synchronization is not permanently stuck merely because
   stagemesh:done was initially absent: once provisioned, the label is
   applied and the issue is closed with no manual intervention.
"""

from __future__ import annotations

import subprocess
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.events import record_event
from build_coordinator.models import TASK_STATES, BuildTask, BuildTaskEvent
from build_coordinator.runner.models import RunnerConfig
from build_coordinator.runner.orchestrator import BuildRunner
from build_coordinator.task_source.github import GitHubTaskSource
from build_coordinator.types import EventInput


EXPECTED_LIFECYCLE_LABELS = [f"stagemesh:{state.lower()}" for state in TASK_STATES]


class LabelAwareMockClient:
    """Mock GitHub client that models real label-existence semantics."""

    def __init__(self, issues: list[dict] | None = None, existing_labels: list[str] | None = None, fail_on: str | None = None):
        self.issues = list(issues or [])
        self.known_labels: set[str] = set(existing_labels or [])
        self.created_labels: list[str] = []
        self.comments: list[dict] = []
        self.closed: list[str] = []
        self.labels: list[dict] = []
        self.removed_labels: list[dict] = []
        self.fail_on = fail_on

    def list_issues(self, repo: str, labels: tuple[str, ...]):
        return self.issues

    def list_labels(self, repo: str):
        if self.fail_on == "list_labels":
            raise RuntimeError("permission denied: cannot list labels")
        if self.fail_on == "rate_limit_labels":
            raise RuntimeError("secondary rate limit exceeded while listing labels")
        return sorted(self.known_labels)

    def create_label(self, repo: str, name: str):
        if self.fail_on == "create_label":
            raise RuntimeError("permission denied: cannot create labels")
        self.created_labels.append(name)
        self.known_labels.add(name)

    def add_comment(self, repo: str, number: str, body: str):
        self.comments.append({"repo": repo, "number": str(number), "body": body})

    def add_label(self, repo: str, number: str, label: str):
        # Mirrors real GitHub: applying a label that doesn't exist fails.
        if label not in self.known_labels:
            raise RuntimeError(f"label '{label}' not found in repository")
        self.labels.append({"repo": repo, "number": str(number), "label": label})
        for issue in self.issues:
            if str(issue.get("number")) == str(number):
                issue.setdefault("labels", []).append({"name": label})

    def remove_label(self, repo: str, number: str, label: str):
        for issue in self.issues:
            if str(issue.get("number")) == str(number):
                issue["labels"] = [
                    existing
                    for existing in issue.get("labels", [])
                    if (existing.get("name") if isinstance(existing, dict) else str(existing)) != label
                ]
                break
        self.removed_labels.append({"repo": repo, "number": str(number), "label": label})

    def close_issue(self, repo: str, number: str):
        self.closed.append(str(number))


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(bind=engine)
    initialize_schema()
    yield


def _force_capacity_cooldown_expired(source: GitHubTaskSource, session, *, surface: str = "labels") -> None:
    """Simulate a capacity cooldown that has genuinely elapsed, without
    sleeping in real time.

    `ensure_labels()`'s cooldown check consults both the source's in-memory
    `_capacity_backoff_until_by_surface` dict *and*, once that in-memory
    value has expired, the most recently persisted
    `task_source.capacity_wait` event for the same surface/repo (so a fresh
    process instance honors a cooldown recorded before a restart). To make
    a test's next call see a genuinely-cleared cooldown, both must be
    rolled back into the past.

    Also clears the shared `__rest_primary__` pseudo-surface: since
    GitHubTaskSource._is_primary_capacity_message() now defaults an
    unverified/unknown-scope capacity message to *shared* (see that
    method's docstring -- message wording like "secondary rate limit"
    alone is never sufficient to prove narrow scope), a plain rate-limit
    failure recorded by these fixtures also sets the shared backoff, and
    that must be rolled back too or a later "intended success" call would
    still be suppressed by the shared gate even after "labels"'s own entry
    is cleared.
    """
    source._capacity_backoff_until_by_surface[surface] = 0.0
    source._capacity_backoff_until_by_surface[source._SHARED_PRIMARY_SURFACE] = 0.0
    events = session.scalars(
        select(BuildTaskEvent)
        .where(BuildTaskEvent.event_type == "task_source.capacity_wait")
        .order_by(BuildTaskEvent.created_at.desc())
    ).all()
    for event in events:
        data = dict(event.event_data or {})
        if data.get("repo") != source.repo:
            continue
        if data.get("surface") != surface and not data.get("primary"):
            continue
        data["backoff_until"] = time.time() - 1
        event.event_data = data
        session.add(event)
    session.commit()


def test_new_repo_with_zero_labels_gets_stagemesh_done_provisioned():
    """1. Brand-new repo, no StageMesh labels -> provisioned automatically on first sync."""
    client = LabelAwareMockClient(issues=[], existing_labels=[])
    source = GitHubTaskSource(repo="example/new-repo", client=client)

    with SessionLocal() as session:
        source.discover_tasks(session)
        session.commit()

    assert client.created_labels == EXPECTED_LIFECYCLE_LABELS
    assert set(EXPECTED_LIFECYCLE_LABELS).issubset(client.known_labels)


def test_provisioning_is_idempotent_across_cycles():
    """2. Label creation only happens once even across repeated discover cycles."""
    client = LabelAwareMockClient(issues=[], existing_labels=[])
    source = GitHubTaskSource(repo="example/new-repo", client=client)

    with SessionLocal() as session:
        source.discover_tasks(session)
        source.discover_tasks(session)
        source.discover_tasks(session)
        session.commit()

    assert client.created_labels == EXPECTED_LIFECYCLE_LABELS


def test_provisioning_skips_labels_that_already_exist():
    """Idempotent w.r.t. pre-existing repo state: no duplicate creation attempted."""
    client = LabelAwareMockClient(issues=[], existing_labels=EXPECTED_LIFECYCLE_LABELS)
    source = GitHubTaskSource(repo="example/repo", client=client)

    with SessionLocal() as session:
        source.discover_tasks(session)
        session.commit()

    assert client.created_labels == []


def test_provisioning_never_runs_when_adapter_disabled():
    """3. No repo configured -> adapter disabled -> no provisioning attempted."""
    client = LabelAwareMockClient(issues=[])
    source = GitHubTaskSource(repo=None, client=client)

    with SessionLocal() as session:
        results = source.discover_tasks(session)
        session.commit()

    assert results == []
    assert client.created_labels == []


def test_provisioning_failure_recorded_durably_and_does_not_raise():
    """4. Permission/API failure during provisioning is recorded, not raised."""
    client = LabelAwareMockClient(issues=[], existing_labels=[], fail_on="list_labels")
    source = GitHubTaskSource(repo="example/repo", client=client)

    with SessionLocal() as session:
        # Must not raise.
        source.discover_tasks(session)
        session.commit()

    assert client.created_labels == []

    with SessionLocal() as session:
        events = session.scalars(
            select(BuildTaskEvent).where(
                BuildTaskEvent.event_type == "task_source.label_provisioning_failed",
            )
        ).all()
        assert len(events) == 1
        assert "permission denied" in events[0].event_data.get("error", "")


def test_rate_limited_label_provisioning_warning_is_suppressed_within_run(caplog):
    """The first rate-limited label check records one durable failure event
    and logs one warning. While the recorded "labels" cooldown is still
    active, further discover_tasks() calls must suppress the actual repeated
    GitHub request entirely -- not just the warning -- so no new event and
    no new warning are recorded for those calls. Only once the cooldown has
    genuinely elapsed does a real request resume, and a still-failing
    request then records a second event and a second warning."""
    client = LabelAwareMockClient(issues=[], existing_labels=[], fail_on="rate_limit_labels")
    source = GitHubTaskSource(repo="example/repo", client=client)

    with caplog.at_level("WARNING", logger="build_coordinator.task_source.github"):
        with SessionLocal() as session:
            source.discover_tasks(session)
            # The "labels" cooldown recorded by the first failure is still
            # active: these must be no-ops -- no new real GitHub request,
            # no new event, no new warning.
            source.discover_tasks(session)
            source.discover_tasks(session)
            session.commit()

        warnings = [
            record
            for record in caplog.records
            if "Failed to provision GitHub lifecycle labels" in record.getMessage()
        ]
        assert len(warnings) == 1

        with SessionLocal() as session:
            events = session.scalars(
                select(BuildTaskEvent).where(
                    BuildTaskEvent.event_type == "task_source.label_provisioning_failed",
                )
            ).all()
            assert len(events) == 1
            assert events[0].event_data["capacity"]["reason"] == "RATE_LIMITED"

            # Once the cooldown has genuinely elapsed, provisioning is
            # retried for real and, still failing, records a second event --
            # a second *warning* log line is not expected here since these
            # are direct discover_tasks() calls with no runner-cycle
            # boundary (only begin_sync_cycle(), called once per runner
            # cycle, or a successful attempt resets the per-run warning
            # flag; see test_label_provisioning_warning_resets_between_runner_cycles
            # for the cycle-boundary case).
            _force_capacity_cooldown_expired(source, session)
            source.discover_tasks(session)
            session.commit()

        warnings = [
            record
            for record in caplog.records
            if "Failed to provision GitHub lifecycle labels" in record.getMessage()
        ]
        assert len(warnings) == 1

        with SessionLocal() as session:
            events = session.scalars(
                select(BuildTaskEvent).where(
                    BuildTaskEvent.event_type == "task_source.label_provisioning_failed",
                )
            ).all()
            assert len(events) == 2


def test_label_provisioning_warning_resets_between_runner_cycles(caplog):
    """A long-lived runner keeps surfacing persistent label-provisioning
    failures -- but only once the recorded "labels" cooldown from the
    previous cycle has genuinely elapsed; a second cycle that lands while
    the cooldown is still active must not make a real GitHub request or log
    a duplicate warning, no matter that begin_sync_cycle() resets the
    per-cycle warning flag."""
    client = LabelAwareMockClient(issues=[], existing_labels=[], fail_on="rate_limit_labels")
    source = GitHubTaskSource(repo="example/repo", client=client)
    runner = BuildRunner(SessionLocal, RunnerConfig.default(), task_source=source)

    with caplog.at_level("WARNING", logger="build_coordinator.task_source.github"):
        runner.run_once()

        with SessionLocal() as session:
            events = session.scalars(
                select(BuildTaskEvent).where(
                    BuildTaskEvent.event_type == "task_source.label_provisioning_failed",
                )
            ).all()
            assert len(events) == 1

        # A cycle landing while the cooldown is still active is a genuine
        # no-op: no real request, no new event, no new warning.
        runner.run_once()
        warnings = [
            record
            for record in caplog.records
            if "Failed to provision GitHub lifecycle labels" in record.getMessage()
        ]
        assert len(warnings) == 1
        with SessionLocal() as session:
            events = session.scalars(
                select(BuildTaskEvent).where(
                    BuildTaskEvent.event_type == "task_source.label_provisioning_failed",
                )
            ).all()
            assert len(events) == 1

        # Once the cooldown has genuinely elapsed, the next cycle resumes a
        # real attempt; still failing, it logs a second warning (the
        # per-cycle flag was already reset by this cycle's begin_sync_cycle()).
        with SessionLocal() as session:
            _force_capacity_cooldown_expired(source, session)
        runner.run_once()

    warnings = [
        record
        for record in caplog.records
        if "Failed to provision GitHub lifecycle labels" in record.getMessage()
    ]
    assert len(warnings) == 2


def test_label_provisioning_warning_resets_after_success(caplog):
    """A long-lived adapter logs a later provisioning failure after recovery."""
    client = LabelAwareMockClient(issues=[], existing_labels=[], fail_on="rate_limit_labels")
    source = GitHubTaskSource(repo="example/repo", client=client)

    with caplog.at_level("WARNING", logger="build_coordinator.task_source.github"):
        with SessionLocal() as session:
            source.discover_tasks(session)

            # The "labels" cooldown recorded by the rate-limited failure
            # above must have genuinely elapsed before the next call can
            # make a real request at all -- otherwise this "intended
            # success" call would be silently suppressed by the cooldown
            # (correctly so) and never actually succeed.
            _force_capacity_cooldown_expired(source, session)
            client.fail_on = None
            source.discover_tasks(session)
            assert client.created_labels == EXPECTED_LIFECYCLE_LABELS

            source._labels_ensured = False
            client.fail_on = "list_labels"
            source.discover_tasks(session)
            session.commit()

    warnings = [
        record
        for record in caplog.records
        if "Failed to provision GitHub lifecycle labels" in record.getMessage()
    ]
    assert len(warnings) == 2
    assert "rate limit" in warnings[0].getMessage().lower()
    assert "permission denied" in warnings[1].getMessage().lower()


def test_provisioning_retries_on_next_cycle_after_failure():
    """Failed provisioning is retried on a later sync, not abandoned forever."""
    client = LabelAwareMockClient(issues=[], existing_labels=[], fail_on="list_labels")
    source = GitHubTaskSource(repo="example/repo", client=client)

    with SessionLocal() as session:
        source.discover_tasks(session)
        session.commit()
    assert client.created_labels == []

    client.fail_on = None
    with SessionLocal() as session:
        source.discover_tasks(session)
        session.commit()
    assert client.created_labels == EXPECTED_LIFECYCLE_LABELS


def test_open_issue_reopens_done_task_when_label_initially_absent():
    """5. Regression: open source issue reopens local DONE work before outbound sync.

    No manual intervention required: the same runner cycle that discovers the
    open issue also provisions lifecycle labels before applying READY during
    outbound sync.
    """
    client = LabelAwareMockClient(
        issues=[
            {
                "number": 200,
                "title": "Fix flaky retry logic",
                "body": "Stabilize the retry backoff.",
                "labels": [],
                "url": "https://github.com/example/repo/issues/200",
            }
        ],
        existing_labels=[],  # stagemesh:done does not exist yet
    )
    source = GitHubTaskSource(repo="example/repo", client=client)

    with SessionLocal() as session:
        source.discover_tasks(session)
        task = session.get(BuildTask, "GH-200")
        task.state = "DONE"
        session.commit()

    config = RunnerConfig.default()
    runner = BuildRunner(SessionLocal, config, task_source=source)
    cycle = runner.run_once()

    assert "stagemesh:ready" in client.created_labels
    assert "GH-200" in cycle.outbound_synced
    assert client.labels and client.labels[0]["label"] == "stagemesh:ready"
    assert client.closed == []

    with SessionLocal() as session:
        task = session.get(BuildTask, "GH-200")
        assert task.state == "READY"


def test_done_sync_provisions_label_without_fresh_discovery_pass():
    """A restart with local DONE work can provision labels during outbound sync."""
    client = LabelAwareMockClient(issues=[], existing_labels=[])
    source = GitHubTaskSource(repo="example/repo", client=client)

    with SessionLocal() as session:
        session.add(
            BuildTask(
                task_id="GH-201",
                title="Already completed imported task",
                description="Completed before this process started.",
                acceptance_criteria=["Done"],
                state="DONE",
            )
        )
        record_event(
            session,
            EventInput(
                task_id="GH-201",
                event_type="task.synced_from_source",
                actor="github-sync",
                event_data={"source": "https://github.com/example/repo/issues/201"},
            ),
        )
        session.commit()

    config = RunnerConfig.default()
    runner = BuildRunner(SessionLocal, config, task_source=source)
    cycle = runner.run_once()

    assert client.created_labels == EXPECTED_LIFECYCLE_LABELS
    assert "GH-201" in cycle.outbound_synced
    assert client.labels == [{"repo": "example/repo", "number": "201", "label": "stagemesh:done"}]
    assert client.closed == ["201"]


def test_non_done_lifecycle_sync_provisions_label_before_applying():
    """Any stagemesh:* task lifecycle label the adapter emits is provisioned first."""
    client = LabelAwareMockClient(issues=[], existing_labels=[])
    source = GitHubTaskSource(repo="example/repo", client=client)

    with SessionLocal() as session:
        session.add(
            BuildTask(
                task_id="GH-203",
                title="Review ready imported task",
                description="Ready for review.",
                acceptance_criteria=["Reviewed"],
                state="REVIEW_READY",
            )
        )
        record_event(
            session,
            EventInput(
                task_id="GH-203",
                event_type="task.synced_from_source",
                actor="github-sync",
                event_data={"source": "https://github.com/example/repo/issues/203"},
            ),
        )
        ok = source.sync_outbound(session, "GH-203", "REVIEW_READY")
        session.commit()

    assert ok is True
    assert client.created_labels == EXPECTED_LIFECYCLE_LABELS
    assert client.labels == [{"repo": "example/repo", "number": "203", "label": "stagemesh:review_ready"}]
    assert client.closed == []


def test_subprocess_non_done_lifecycle_records_state_for_idempotent_retry(monkeypatch):
    """Subprocess sync records the lifecycle state, not the label text."""
    source = GitHubTaskSource(repo="example/repo")
    called_cmds = []

    def fake_subprocess_run(cmd, **kwargs):
        called_cmds.append(cmd)
        if cmd[:3] == ["gh", "label", "list"]:
            return SimpleNamespace(returncode=0, stdout="[]", stderr="")
        if cmd[:3] == ["gh", "label", "create"]:
            return SimpleNamespace(returncode=0, stdout="created", stderr="")
        if cmd[:3] == ["gh", "issue", "view"]:
            return SimpleNamespace(returncode=0, stdout='{"labels":[]}', stderr="")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    with SessionLocal() as session:
        session.add(
            BuildTask(
                task_id="GH-205",
                title="Review ready via subprocess",
                description="Ready for review.",
                acceptance_criteria=["Reviewed"],
                state="REVIEW_READY",
            )
        )
        record_event(
            session,
            EventInput(
                task_id="GH-205",
                event_type="task.synced_from_source",
                actor="github-sync",
                event_data={"source": "https://github.com/example/repo/issues/205"},
            ),
        )
        ok = source.sync_outbound(session, "GH-205", "REVIEW_READY")
        assert ok is True
        session.commit()

    with SessionLocal() as session:
        events = session.scalars(
            select(BuildTaskEvent).where(
                BuildTaskEvent.task_id == "GH-205",
                BuildTaskEvent.event_type == "task.outbound_synced",
            )
        ).all()
        assert len(events) == 1
        assert events[0].event_data.get("state") == "REVIEW_READY"

        ok = source.sync_outbound(session, "GH-205", "REVIEW_READY")
        assert ok is True
        session.commit()

    edit_cmds = [cmd for cmd in called_cmds if cmd[:3] == ["gh", "issue", "edit"]]
    assert len(edit_cmds) == 1
    assert edit_cmds[0][-1] == "stagemesh:review_ready"


def test_reopened_issue_runner_cycle_replaces_stale_done_label_with_ready():
    """Normal discovery + runner path reconciles a reopened actionable issue."""
    client = LabelAwareMockClient(
        issues=[
            {
                "number": 205,
                "title": "Reopened after validation regression",
                "body": "Regression evidence means this is actionable again.",
                "labels": [{"name": "stagemesh:done"}, {"name": "priority:P0"}, {"name": "risk:high"}],
                "url": "https://github.com/example/repo/issues/205",
            }
        ],
        existing_labels=EXPECTED_LIFECYCLE_LABELS,
    )
    source = GitHubTaskSource(repo="example/repo", client=client)

    with SessionLocal() as session:
        session.add(
            BuildTask(
                task_id="GH-205",
                title="Previously completed issue",
                description="Closed before regression evidence arrived.",
                acceptance_criteria=["Done"],
                state="DONE",
                definition_metadata={
                    "source_type": "github",
                    "source_owner": "example/repo",
                    "source_ref": "205",
                    "source_url": "https://github.com/example/repo/issues/205",
                    "source_state": "CLOSED",
                    "source_issue_number": 205,
                },
            )
        )
        record_event(
            session,
            EventInput(
                task_id="GH-205",
                event_type="task.outbound_synced",
                actor="github-sync",
                event_data={"state": "DONE", "issue_number": 205},
            ),
        )
        session.commit()

    with SessionLocal() as session:
        results = source.discover_tasks(session)
        task = session.get(BuildTask, "GH-205")
        assert task.state == "READY"
        session.commit()

    assert [result.task_id for result in results] == ["GH-205"]

    config = RunnerConfig.default()
    runner = BuildRunner(SessionLocal, config, task_source=source)
    cycle = runner.run_once()

    assert "GH-205" in cycle.outbound_synced
    assert client.removed_labels == [{"repo": "example/repo", "number": "205", "label": "stagemesh:done"}]
    assert client.labels[-1] == {"repo": "example/repo", "number": "205", "label": "stagemesh:ready"}
    assert client.closed == []
    issue_labels = {label["name"] for label in client.issues[0]["labels"]}
    assert "stagemesh:done" not in issue_labels
    assert {"stagemesh:ready", "priority:P0", "risk:high"}.issubset(issue_labels)


def test_client_lifecycle_replacement_skips_absent_stale_labels():
    """Client path is idempotent when most lifecycle labels are absent."""
    client = LabelAwareMockClient(
        issues=[
            {
                "number": 206,
                "title": "Ready issue without stale labels",
                "body": "No stale lifecycle label is currently applied.",
                "labels": [{"name": "priority:P1"}],
                "url": "https://github.com/example/repo/issues/206",
            }
        ],
        existing_labels=EXPECTED_LIFECYCLE_LABELS,
    )
    source = GitHubTaskSource(repo="example/repo", client=client)

    with SessionLocal() as session:
        source.discover_tasks(session)
        ok = source.sync_outbound(session, "GH-206", "READY")
        session.commit()

    assert ok is True
    assert client.removed_labels == []
    assert client.labels == [{"repo": "example/repo", "number": "206", "label": "stagemesh:ready"}]


def test_outbound_records_task_specific_failure_when_label_provisioning_fails():
    """Provisioning failures leave DONE intact and create retryable outbound evidence."""
    client = LabelAwareMockClient(issues=[], existing_labels=[], fail_on="create_label")
    source = GitHubTaskSource(repo="example/repo", client=client)

    with SessionLocal() as session:
        session.add(
            BuildTask(
                task_id="GH-202",
                title="Completed task awaiting GitHub sync",
                description="Done locally.",
                acceptance_criteria=["Done"],
                state="DONE",
            )
        )
        record_event(
            session,
            EventInput(
                task_id="GH-202",
                event_type="task.synced_from_source",
                actor="github-sync",
                event_data={"source": "https://github.com/example/repo/issues/202"},
            ),
        )
        session.commit()

    config = RunnerConfig.default()
    runner = BuildRunner(SessionLocal, config, task_source=source)
    cycle = runner.run_once()

    assert "GH-202" not in cycle.outbound_synced
    assert client.comments == []
    assert client.labels == []
    assert client.closed == []

    with SessionLocal() as session:
        task = session.get(BuildTask, "GH-202")
        assert task.state == "DONE"
        events = session.scalars(
            select(BuildTaskEvent).where(
                BuildTaskEvent.task_id == "GH-202",
                BuildTaskEvent.event_type == "task.outbound_sync_failed",
            )
        ).all()
        assert len(events) == 1
        assert events[0].event_data.get("action") == "label_provisioning"


def test_local_backlog_outbound_sync_never_depends_on_label_provisioning():
    """Local backlog tasks are not GitHub-originating and never need repo labels."""
    client = LabelAwareMockClient(issues=[], existing_labels=[], fail_on="list_labels")
    source = GitHubTaskSource(repo="example/repo", client=client)

    with SessionLocal() as session:
        session.add(
            BuildTask(
                task_id="LOCAL-204",
                title="Local backlog task",
                description="No GitHub source identity.",
                acceptance_criteria=["Done"],
                state="DONE",
            )
        )
        ok = source.sync_outbound(session, "LOCAL-204", "DONE")
        session.commit()

    assert ok is True
    assert client.created_labels == []
    assert client.comments == []
    assert client.labels == []
    assert client.closed == []

    with SessionLocal() as session:
        assert session.get(BuildTask, "LOCAL-204").state == "DONE"
        failures = session.scalars(
            select(BuildTaskEvent).where(
                BuildTaskEvent.event_type == "task_source.label_provisioning_failed",
            )
        ).all()
        assert failures == []
