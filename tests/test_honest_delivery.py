"""Scenarios 7-10: honest completion, resumable delivery, one permission blocker, one completion notice."""

from __future__ import annotations

import io
import json
from pathlib import Path

from stagemesh.delivery_state import (
    clear_external_block,
    externally_blocked,
    is_permission_denial,
    record_external_block,
    task_delivery,
)
from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage
from stagemesh.follow_through import (
    complete_follow_up,
    consolidate_blockers,
    follow_up_active,
    notify_once,
    register_follow_up,
)
from stagemesh.outbound_lifecycle import sync_outbound_outcomes
from stagemesh.progress import ProgressReporter, with_progress
from stagemesh.survivor_wave import RunResult, format_report, run_wave, select_eligible

from test_survivor_wave import Rig, make_store

PR_MODE_REF = "refs/heads/wave/staging"


def _finish_in_pr_mode(rig: Rig, task_id: str) -> RunResult:
    """Like Rig.finish_done, but the integration evidence names the wave staging ref, as PR mode records it."""
    store = rig.worker_store()
    try:
        sha = (task_id.replace("-", "") + "1" * 40)[:40]
        store.add_candidate(task_id, sha, "codex", True)
        store.add_evidence(task_id, sha, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)
        store.add_evidence(task_id, sha, EvidenceKind.REVIEW, EvidenceStatus.PASSED)
        store.add_evidence(task_id, sha, EvidenceKind.INTEGRATION, EvidenceStatus.PASSED, {"integration_ref": PR_MODE_REF})
        store.advance_task(task_id, Stage.DONE)
    finally:
        store.close()
    return RunResult("DONE")


def _events(store, event_type: str) -> list[dict]:
    return [json.loads(r[0]) for r in store.conn.execute("SELECT payload FROM audit_events WHERE event_type=? ORDER BY created_at, rowid", (event_type,))]


def test_ledger_done_in_pr_mode_is_staged_not_delivered(tmp_path: Path) -> None:
    store, project = make_store(tmp_path, ["1"])
    run_wave(store, ["1"], Rig(project, {"1": _finish_in_pr_mode}, ).hooks(), open_prs=False)

    delivery = task_delivery(store, "1")

    assert store.get_task("1")["stage"] == Stage.DONE
    assert delivery.state == "STAGED" and delivery.complete is False
    assert "no PR published and nothing merged" in delivery.label()


def test_progress_never_prints_unqualified_done_when_publication_is_still_required(tmp_path: Path) -> None:
    store, project = make_store(tmp_path, ["1"])
    stream = io.StringIO()
    hooks = with_progress(Rig(project, {"1": _finish_in_pr_mode}).hooks(), project / ".stagemesh" / "stagemesh.sqlite3", ProgressReporter(stream))

    run_wave(store, ["1"], hooks)

    text = stream.getvalue()
    assert "task 1: finished: DONE (ledger complete; delivery pending: PR not yet published or merged)" in text
    assert "PR opened https://example.invalid/pr/1 (awaiting checks; not merged)" in text


def test_deadline_before_publication_preserves_the_candidate_and_reports_delivery_pending(tmp_path: Path) -> None:
    store, project = make_store(tmp_path, ["1"])
    now = [0.0]

    def work(rig, task_id):
        result = _finish_in_pr_mode(rig, task_id)
        now[0] = 11.0  # the window deadline passes while the task finishes
        return result

    rig = Rig(project, {"1": work})
    report = run_wave(store, ["1"], rig.hooks(), deadline=10.0, clock=lambda: now[0])

    record = report.tasks[0]
    assert rig.prs == [] and record.landing == "staged_not_published" and record.resumable
    assert record.stop_reason == "PR_DELIVERY_PENDING" and record.outcome == "open"  # not "done"
    delivery = task_delivery(store, "1")
    assert delivery.state == "DELIVERY_PENDING" and delivery.complete is False
    assert "window deadline reached before PR publication" in delivery.label()
    assert store.get_task("1")["stage"] == Stage.INTEGRATE  # resumable at the delivery step
    assert store.conn.execute("SELECT COUNT(*) FROM candidates WHERE task_id='1'").fetchone()[0] == 1  # the candidate is preserved
    assert "resumable" in format_report(report)


def test_restart_resumes_delivery_once_without_replaying_implementation_or_losing_evidence(tmp_path: Path) -> None:
    store, project = make_store(tmp_path, ["1"])
    now = [0.0]

    def work(rig, task_id):
        result = _finish_in_pr_mode(rig, task_id)
        now[0] = 11.0
        return result

    first = Rig(project, {"1": work})
    run_wave(store, ["1"], first.hooks(), deadline=10.0, clock=lambda: now[0])
    candidates = store.conn.execute("SELECT sha FROM candidates WHERE task_id='1'").fetchall()
    evidence_before = store.conn.execute("SELECT COUNT(*) FROM evidence WHERE task_id='1'").fetchone()[0]
    store.close()

    # A restart: new process, new connection, same ledger. The resumed run only completes the INTEGRATE step (evidence exists).
    from stagemesh.persistence import Store

    restarted = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    restarted.migrate()
    implementations: list[str] = []

    def resume(rig, task_id):
        implementations.append(task_id)
        worker = rig.worker_store()
        try:
            worker.advance_task(task_id, Stage.DONE)  # no new candidate, no new evidence: nothing is replayed
        finally:
            worker.close()
        return RunResult("DONE")

    second = Rig(project, {"1": resume})
    report = run_wave(restarted, ["1"], second.hooks())

    assert second.prs == ["1"] and first.prs == []  # exactly one PR across both runs
    assert report.tasks[0].landing == "pr_opened" and report.tasks[0].pr_url
    assert [row["sha"] for row in restarted.conn.execute("SELECT sha FROM candidates WHERE task_id='1'")] == [r["sha"] for r in candidates]
    assert restarted.conn.execute("SELECT COUNT(*) FROM evidence WHERE task_id='1'").fetchone()[0] == evidence_before
    published, = _events(restarted, "delivery.pr_published")
    assert published["pr_url"] == "https://example.invalid/pr/1" and published["candidate_sha"] == candidates[0]["sha"]
    assert task_delivery(restarted, "1").state == "PUBLISHED"
    # a further window finds nothing to do: no duplicate PR
    third = Rig(project, {})
    run_wave(restarted, ["1"], third.hooks())
    assert third.prs == [] and third.runs == []


def test_permission_denial_preserves_work_and_reports_one_blocker_without_bypass(tmp_path: Path) -> None:
    store, project = make_store(tmp_path, ["1", "2"])
    attempts: list[str] = []

    class Denied(Rig):
        def open_pr(self, record) -> str:
            attempts.append(record.task_id)
            raise RuntimeError("git push -u failed: remote: Permission to org/repo denied to bot (403)")

    rig = Denied(project, {"1": _finish_in_pr_mode, "2": _finish_in_pr_mode})
    report = run_wave(store, ["1", "2"], rig.hooks(), concurrency=1)

    first = report.tasks[0]
    assert first.landing == "externally_blocked" and "needs permission to push branches and open pull requests" in first.reason
    assert attempts == ["1"]  # one attempt; the rest of the window is not spent asking again
    assert len(_events(store, "delivery.externally_blocked")) == 1
    assert task_delivery(store, "1").state == "EXTERNALLY_BLOCKED"
    assert store.get_task("1")["stage"] == Stage.INTEGRATE  # work preserved, resumable
    assert store.conn.execute("SELECT COUNT(*) FROM candidates WHERE task_id='1'").fetchone()[0] == 1

    # A later window does not retry or re-report while the permission is still missing.
    eligible, skipped = select_eligible(store, ["1"])
    assert eligible == [] and "delivery externally blocked" in skipped["1"]
    assert record_external_block(store, "1", store.latest_candidate("1")["sha"], "same denial again", "permission") is False
    assert len(_events(store, "delivery.externally_blocked")) == 1
    assert attempts == ["1"]

    # Only an explicit, recorded unblock (access granted) allows a new attempt.
    clear_external_block(store, "1", "repo-owner", "write access granted to the token")
    assert externally_blocked(store, "1") is None
    eligible, _ = select_eligible(store, ["1"])
    assert eligible == ["1"]


def test_permission_denial_wording_is_recognised_but_ordinary_failures_are_not() -> None:
    assert is_permission_denial("remote: Permission to x denied (403)") and is_permission_denial("protected branch hook declined")
    assert not is_permission_denial("window deadline reached before PR publication command")
    assert not is_permission_denial("gh pr create failed: network timeout")


def test_an_upstream_issue_is_only_closed_after_the_exact_head_merged(tmp_path: Path) -> None:
    store, project = make_store(tmp_path, ["1"])
    run_wave(store, ["1"], Rig(project, {"1": _finish_in_pr_mode}).hooks(), open_prs=False)
    published: list[tuple[str, str]] = []

    class Sync:
        def publish_done(self, issue: str, subject: str) -> str:
            published.append((issue, subject))
            return "evt"

        def publish(self, *args, **kwargs) -> str:
            return "evt"

    task = store.get_task("1")
    store.conn.execute("UPDATE tasks SET source='github', source_id='101' WHERE id='1'")
    store.conn.commit()
    task = store.get_task("1")

    sync_outbound_outcomes(store, Sync(), frozenset({"github"}), [task], {"1": "DONE"})
    assert published == []  # ledger DONE on the staging ref closes nothing

    sha = str(store.latest_candidate("1")["sha"])
    store.add_audit_event("window.fast_forward_merged", {"task_id": "1", "candidate_sha": sha, "pr": 7})
    assert task_delivery(store, "1").state == "MERGED" and task_delivery(store, "1").complete

    sync_outbound_outcomes(store, Sync(), frozenset({"github"}), [task], {"1": "DONE"})
    assert published == [("101", sha)]


def test_completion_is_announced_once_and_the_follow_up_stops_scheduling_itself(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path, ["1"])
    key = "deliver:PR-401"

    assert register_follow_up(store, key, "merge when required checks pass") is True
    assert register_follow_up(store, key, "merge when required checks pass") is False  # never a second schedule
    assert follow_up_active(store, key)

    # The follow-up runs repeatedly; the first completion is announced, every later observation is silent.
    assert complete_follow_up(store, key, "merged at abcdef0") is True
    notices = [notify_once(store, f"{key}:complete", "PR 401 merged") for _ in range(5)]
    assert notices == [True, False, False, False, False]
    assert complete_follow_up(store, key, "merged at abcdef0") is False
    assert follow_up_active(store, key) is False  # whatever schedules it must now stop

    # persisted across a restart
    from stagemesh.persistence import Store

    reopened = Store(store.db_path) if hasattr(store, "db_path") else store
    assert notify_once(reopened, f"{key}:complete", "PR 401 merged") is False


def test_the_same_blocker_on_several_tasks_is_one_entry() -> None:
    entries = consolidate_blockers(
        [
            ("148", "delivery externally blocked (needs permission to push): remote: denied (403)"),
            ("149", "delivery externally blocked (needs permission to push): remote: denied (403)"),
            ("150", "contract_policy_conflict: Engineering Impact record must change docs/design/**"),
        ]
    )
    assert [(e["tasks"], e["count"]) for e in entries] == [(["148", "149"], 2), (["150"], 1)]
