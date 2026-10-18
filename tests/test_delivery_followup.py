"""Automatic delivery follow-through, end to end through the real `run_window` and the real follow-up worker.

Real: run_window, run_wave, the ledger/store (reopened across "restarts"), delivery state, the follow-up registry, the file lock, the
retry registry, the Windows Task Scheduler adapter (with a recording command runner) and the CLI parser for the resume command.
Fake: the implementation, the PR opener, hosted checks / merge (an audit record like the landing guard writes), hosting reads, the
OS scheduler (an in-memory registry) and the clock.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from test_run_window import FRESH, IN_SYNC, OK_PROVIDERS, Clock
from test_survivor_wave import Rig, make_store

import stagemesh.cli as cli_module
import stagemesh.run_window as rw
from stagemesh import readiness
from stagemesh.concurrency import IntegrationLock
from stagemesh.delivery_followup import (
    MAX_TRANSIENT_FAILURES,
    HostingError,
    PullRequestSnapshot,
    cancel_follow_up,
    generation,
    rearm_follow_up,
    run_follow_up,
)
from stagemesh.delivery_state import clear_external_block, externally_blocked, task_delivery
from stagemesh.domain import Stage
from stagemesh.follow_through import (
    active_follow_ups,
    follow_up_active,
    follow_up_outcome,
    follow_up_record,
    schedule_handle,
)
from stagemesh.followup_scheduler import SchedulerError, WindowsTaskScheduler
from stagemesh.persistence import Store
from stagemesh.survivor_wave import RunResult

REPO = "acme/app"
BASE = "refs/heads/main"


@pytest.fixture(autouse=True)
def _no_real_runtime_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(readiness, "check_runtime", lambda project: FRESH)
    monkeypatch.setattr(readiness, "active_cooldowns", lambda store, seconds: [])


class FakeScheduler:
    """The OS schedule: an in-memory registry that records every call, so cleanup is observable rather than assumed."""

    name = "fake-scheduler"

    def __init__(self) -> None:
        self.live: dict[str, tuple[list[str], int]] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_removals = 0

    def ensure(self, key, argv, interval_seconds):
        handle = f"fake/{key}"
        self.calls.append(("ensure", handle))
        self.live[handle] = (list(argv), interval_seconds)
        return handle

    def remove(self, handle):
        self.calls.append(("remove", handle))
        if self.fail_removals:
            self.fail_removals -= 1
            raise SchedulerError("access denied")
        self.live.pop(handle, None)

    def exists(self, handle):
        return handle in self.live


class FakeHosting:
    def __init__(self) -> None:
        self.head: dict[str, str] = {}
        self.state = "OPEN"
        self.error: HostingError | None = None
        self.reads = 0

    def pr_snapshot(self, pr_url):
        self.reads += 1
        if self.error:
            raise self.error
        return PullRequestSnapshot(self.state, self.head.get(pr_url, ""), "main")


class Harness:
    """One project, restartable: `restart()` reopens the ledger from disk exactly as a new process would."""

    def __init__(self, tmp_path: Path, task_ids: list[str] | None = None):
        self.ids = task_ids or ["1"]
        self.tmp = tmp_path
        self.store, self.project = make_store(tmp_path, self.ids)
        self.clock = Clock()
        self.scheduler = FakeScheduler()
        self.hosting = FakeHosting()
        self.notes: list[tuple[str, str]] = []
        self.implementations = 0
        self.resume_windows = 0
        self.pr_attempts: list[str] = []
        self.pr_error: str | None = None
        self.merge_script: list[dict] = []
        self.landing_calls = 0
        self.rig = Rig(self.project, {})

    # -- the project's collaborators --------------------------------------------------------------------------------------------

    def restart(self) -> None:
        self.store.close()
        self.store = Store(self.project / ".stagemesh" / "stagemesh.sqlite3")
        self.store.migrate()

    def notify(self, key, message):
        self.notes.append((key, message))

    def _implement(self, expire: float | None):
        def work(rig, task_id):
            task = self.store_for_worker(rig).get_task(task_id)
            if task["stage"] == Stage.INTEGRATE:  # resume: delivery only, never another implementation
                worker = rig.worker_store()
                try:
                    worker.advance_task(task_id, Stage.DONE)
                finally:
                    worker.close()
                if expire is not None:
                    self.clock.now += expire
                return RunResult("DONE")
            self.implementations += 1
            result = rig.finish_done(task_id)
            if expire is not None:
                self.clock.now += expire
            return result

        return work

    @staticmethod
    def store_for_worker(rig):
        return rig.worker_store()

    def open_pr(self, record):
        self.pr_attempts.append(record.task_id)
        if self.pr_error:
            raise RuntimeError(self.pr_error)
        return f"https://github.com/{REPO}/pull/{100 + int(record.task_id)}"

    def merge(self, record, deadline):
        self.landing_calls += 1
        step = self.merge_script.pop(0) if self.merge_script else {"merged": False, "reason": "hosted CI deadline reached; PR retained for retry"}
        if step.get("merged"):
            self.store.add_audit_event("window.fast_forward_merged", {"task_id": record.task_id, "candidate_sha": record.integrated_candidate, "pr": 1})
        else:
            self.store.add_audit_event("window.landing_status", {
                "task_id": record.task_id, "candidate_sha": record.integrated_candidate, "pr_url": record.pr_url, "state": "waiting",
                "reason": step["reason"],
            })
        return step

    # -- windows ------------------------------------------------------------------------------------------------------------------

    def window(self, *, tasks=None, duration=10.0, expire=None, follow_up=True, merge=False, open_prs=True, scheduler=True):
        options = rw.WindowOptions(
            duration_seconds=duration, concurrency=1, max_tasks=5, tasks=tasks, open_prs=open_prs, follow_up=follow_up,
            auto_merge_ff=merge, required_checks=("web",) if merge else (), merge_policy="hosted",
            report_path=self.tmp / "out" / "window.json",
        )
        rig = Rig(self.project, {task_id: self._implement(expire) for task_id in self.ids})
        hooks = rig.hooks()
        hooks.repo_state = lambda: IN_SYNC
        hooks.open_pr = self.open_pr
        self.rig = rig
        config = cli_module.load_config(self.project)
        self._prs = []
        return rw.run_window(
            self.store, self.project, config, options, hooks, base_ref=BASE, staging_ref="refs/heads/wave/staging",
            runtime=FRESH, list_prs=self._list_prs, provider_check=OK_PROVIDERS, clock=self.clock,
            merge_pr=self.merge if merge else None, follow_up_scheduler=self.scheduler if scheduler else None, repository=REPO,
        )

    def _list_prs(self, project):
        found = []
        for task_id in self.ids:
            delivery = task_delivery(self.store, task_id, REPO)
            if delivery.pr_url and delivery.state in {"PUBLISHED", "AWAITING_CHECKS"}:
                found.append({"url": delivery.pr_url, "headRefName": f"wave/task-{task_id}"})
        return found

    def resume(self, record):
        """What the scheduled command does: the authorizing window's own narrowed options, for one task."""
        self.resume_windows += 1
        scope = record["scope"]
        report = self.window(tasks=[record["task_id"]], duration=60.0, follow_up=False, merge="merge" in scope, open_prs="publish" in scope)
        return report.exit_code

    def wake(self, key, **overrides):
        kwargs = dict(scheduler=self.scheduler, hosting=self.hosting, window_runner=self.resume, notify=self.notify, repository=REPO, clock=self.clock)
        kwargs.update(overrides)
        return run_follow_up(self.store, self.project, key, **kwargs)

    def only_key(self) -> str:
        active = active_follow_ups(self.store)
        assert len(active) == 1, active
        return active[0]["key"]


def _deadline_before_publication(h: Harness):
    return h.window(expire=100.0)


# -------------------------------------------------------------------------------------------------------------------------------
# registration from the real window path


def test_a_window_that_misses_its_deadline_registers_one_exactly_identified_scheduled_follow_up(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    report = _deadline_before_publication(h)

    assert report.time_limit_reached and h.pr_attempts == []  # nothing published yet
    assert task_delivery(h.store, "1", REPO).state == "DELIVERY_PENDING"
    entry, = report.follow_ups
    assert entry["state"] == "registered" and entry["scheduled"] is True and entry["scope"] == ["publish"]
    key = entry["key"]
    record = follow_up_record(h.store, key)
    sha = h.store.latest_candidate("1")["sha"]
    assert (record["repository"], record["task_id"], record["candidate_sha"]) == (REPO, "1", sha)
    assert record["scope"] == ["publish"] and record["merge_policy"] is None  # a pr-only window never authorizes a merge
    assert record["authorized_by"] == "run-window --follow-up" and record["window_id"] == report.window_id
    assert h.scheduler.calls == [("ensure", f"fake/{key}")] and schedule_handle(h.store, key) == f"fake/{key}"
    assert "scheduled" in rw.format_window_report(report)

    # The scheduled command is a real, narrowed run-window: one task, same authority, no recursion.
    argv = record["resume_argv"]
    assert argv[1:3] == ["-m", "stagemesh.cli"]
    parsed = cli_module.build_parser().parse_args(argv[3:])
    assert (parsed.command if hasattr(parsed, "command") else None) in (None, "run-window") and parsed.tasks == "1"
    assert parsed.concurrency == 1 and parsed.max_tasks == 1 and parsed.pr_only is True and not parsed.follow_up
    command = h.scheduler.live[f"fake/{key}"][0]
    assert command[-2:] == ["--key", key] and "follow-up" in command


def test_the_same_pending_delivery_never_gets_a_second_follow_up_or_schedule(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    first = _deadline_before_publication(h).follow_ups[0]["key"]
    h.restart()
    again = h.window(tasks=["1"], expire=100.0, follow_up=True)  # a later window over the same pending delivery (same candidate)
    assert [e["key"] for e in again.follow_ups] == [first] and again.follow_ups[0]["state"] == "existing"
    assert len(active_follow_ups(h.store)) == 1 and h.scheduler.calls.count(("ensure", f"fake/{first}")) == 1  # not re-created: it is alive


def test_without_the_explicit_flag_nothing_is_scheduled(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    report = h.window(expire=100.0, follow_up=False)
    assert report.follow_ups == [] and h.scheduler.calls == [] and active_follow_ups(h.store) == []


def test_merge_is_never_inferred_a_pr_only_window_publishes_but_does_not_follow_to_a_merge(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    report = h.window()  # publishes before the deadline; checks/completion exist but nobody authorized a merge
    assert h.pr_attempts == ["1"] and task_delivery(h.store, "1", REPO).state == "PUBLISHED"
    entry, = report.follow_ups
    assert entry["state"] == "not_followed" and "did not authorize merge" in entry["reason"]
    assert active_follow_ups(h.store) == [] and h.scheduler.calls == []


def test_a_window_without_a_github_origin_cannot_bind_a_follow_up(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    store = h.store
    options = rw.WindowOptions(duration_seconds=10, concurrency=1, tasks=["1"], follow_up=True, report_path=tmp_path / "o.json")
    rig = Rig(h.project, {"1": h._implement(100.0)})
    hooks = rig.hooks()
    hooks.repo_state = lambda: IN_SYNC
    report = rw.run_window(store, h.project, cli_module.load_config(h.project), options, hooks, base_ref=BASE, staging_ref="refs/heads/wave/staging",
                           runtime=FRESH, list_prs=lambda p: [], provider_check=OK_PROVIDERS, clock=h.clock, follow_up_scheduler=h.scheduler,
                           repository=None)
    assert report.follow_ups[0]["state"] == "not_followed" or report.follow_ups[0]["state"] == "error" or "origin" in report.follow_ups[0].get("reason", "")


# -------------------------------------------------------------------------------------------------------------------------------
# restart -> delivery, completion cleanup


def test_restart_resumes_publication_without_replaying_implementation_then_ends_and_removes_its_schedule(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    key = _deadline_before_publication(h).follow_ups[0]["key"]
    candidates = [r["sha"] for r in h.store.conn.execute("SELECT sha FROM candidates")]
    h.restart()  # the process died; the schedule is what wakes the follow-up

    outcome = h.wake(key)

    assert h.implementations == 1 and [r["sha"] for r in h.store.conn.execute("SELECT sha FROM candidates")] == candidates
    assert h.pr_attempts == ["1"]  # exactly one PR, for the exact reviewed head
    published, = [dict(r) for r in h.store.conn.execute("SELECT payload FROM audit_events WHERE event_type='delivery.pr_published'")]
    assert candidates[0] in published["payload"] and REPO in published["payload"]
    assert outcome.state == "published_merge_not_authorized" and outcome.terminal and outcome.notified
    assert not follow_up_active(h.store, key) and schedule_handle(h.store, key) is None
    assert h.scheduler.live == {} and ("remove", f"fake/{key}") in h.scheduler.calls  # the schedule is really gone, not just marked
    assert len(h.notes) == 1 and "merging was not authorized" in h.notes[0][1]


def test_duplicate_and_late_wakeups_after_completion_do_nothing_and_say_nothing(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    key = _deadline_before_publication(h).follow_ups[0]["key"]
    h.wake(key)
    windows, attempts, calls = h.resume_windows, list(h.pr_attempts), list(h.scheduler.calls)

    for _ in range(4):
        outcome = h.wake(key)
        assert outcome.state == "inactive" and not outcome.notified

    assert (h.resume_windows, h.pr_attempts) == (windows, attempts) and len(h.notes) == 1
    assert [c for c in h.scheduler.calls if c[0] == "ensure"] == [c for c in calls if c[0] == "ensure"]


def test_concurrent_wakeups_run_exactly_one_worker(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    key = _deadline_before_publication(h).follow_ups[0]["key"]
    entered, release = threading.Event(), threading.Event()
    first_runs, outcomes = [], []

    def slow_resume(record):
        first_runs.append(record["task_id"])
        entered.set()
        assert release.wait(10)  # a long-running resume window in the first worker
        return 0

    def first():
        other = Store(h.project / ".stagemesh" / "stagemesh.sqlite3")  # a second process has its own connection
        outcomes.append(run_follow_up(other, h.project, key, scheduler=h.scheduler, hosting=h.hosting, window_runner=slow_resume,
                                      notify=h.notify, repository=REPO, clock=h.clock))
        other.close()

    thread = threading.Thread(target=first)
    thread.start()
    assert entered.wait(10)
    duplicates = [h.wake(key) for _ in range(3)]  # more wakeups arrive while the first worker is still working
    release.set()
    thread.join(10)

    assert [d.state for d in duplicates] == ["duplicate_worker"] * 3 and not any(d.terminal or d.notified for d in duplicates)
    assert first_runs == ["1"] and h.resume_windows == 0 and h.pr_attempts == [] and h.notes == []  # one worker, one window, no noise
    assert outcomes[0].state == "retry"  # the first worker's window delivered nothing: one bounded transient failure, no blocker
    assert h.wake(key, clock=_later(h)).state == "published_merge_not_authorized"  # the lock was released; the next wakeup delivers
    assert h.pr_attempts == ["1"] and h.resume_windows == 1 and len(h.notes) == 1


def _later(h: Harness):
    h.clock.now += 400  # past the retry backoff the first (unproductive) worker recorded
    return h.clock


def test_a_held_lock_blocks_a_worker_in_another_process(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    key = _deadline_before_publication(h).follow_ups[0]["key"]
    with IntegrationLock(h.project / ".stagemesh" / "followups" / f"{key}.lock", timeout_seconds=1).hold("other process"):
        assert h.wake(key).state == "duplicate_worker"
    assert h.resume_windows == 0 and h.pr_attempts == []


# -------------------------------------------------------------------------------------------------------------------------------
# pending CI, landing and the exact-head guard


def test_pending_ci_keeps_one_quiet_follow_up_until_the_exact_head_merges(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    first = h.window(merge=True)  # publishes, the guarded landing finds hosted checks still pending
    assert h.pr_attempts == ["1"] and h.landing_calls == 1 and first.merged_prs == []
    assert task_delivery(h.store, "1", REPO).state == "AWAITING_CHECKS"
    entry, = first.follow_ups
    key = entry["key"]
    assert entry["scope"] == ["publish", "merge"] and follow_up_record(h.store, key)["merge_policy"] == "hosted"
    assert follow_up_record(h.store, key)["required_checks"] == ["web"] and follow_up_record(h.store, key)["pr_url"].endswith("/pull/101")
    sha = h.store.latest_candidate("1")["sha"]
    h.hosting.head["https://github.com/acme/app/pull/101"] = sha

    waits = [h.wake(key) for _ in range(3)]  # CI stays pending across three wakeups

    assert [w.state for w in waits] == ["waiting"] * 3 and not any(w.terminal for w in waits)
    assert [w.notified for w in waits] == [True, False, False]  # the unchanged state is announced once, not three times
    assert h.pr_attempts == ["1"] and h.landing_calls == 4  # one landing attempt per wakeup, never another publication
    assert follow_up_active(h.store, key) and list(h.scheduler.live) == [f"fake/{key}"]  # still exactly one schedule

    h.merge_script.append({"merged": True, "candidate_sha": sha})  # checks went green
    done = h.wake(key)

    assert done.state == "merged" and done.notified and task_delivery(h.store, "1", REPO).state == "MERGED"
    assert h.scheduler.live == {} and not follow_up_active(h.store, key)
    assert len(h.notes) == 2  # "waiting" once, "merged" once
    assert [h.wake(key).state for _ in range(3)] == ["inactive"] * 3 and len(h.notes) == 2 and h.landing_calls == 5


def test_a_pr_whose_head_is_not_the_reviewed_candidate_is_never_merged(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    key = h.window(merge=True).follow_ups[0]["key"]
    h.hosting.head["https://github.com/acme/app/pull/101"] = "f" * 40  # someone pushed to the PR branch

    outcome = h.wake(key)

    assert outcome.state == "stale_pr" and outcome.terminal and h.landing_calls == 1  # no further landing attempt
    assert h.scheduler.live == {} and len(h.notes) == 1 and "refusing to merge" in h.notes[0][1]


def test_a_closed_or_other_repository_pr_is_stale(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    key = h.window(merge=True).follow_ups[0]["key"]
    h.hosting.state = "CLOSED"
    assert h.wake(key).state == "stale_pr" and h.landing_calls == 1


def test_a_changed_candidate_is_stale_and_nothing_runs(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    key = _deadline_before_publication(h).follow_ups[0]["key"]
    h.store.add_candidate("1", "c" * 40, "codex", True)  # the task moved on to a different candidate

    outcome = h.wake(key)

    assert outcome.state == "stale_candidate" and h.resume_windows == 0 and h.pr_attempts == []
    assert h.scheduler.live == {} and len(h.notes) == 1


def test_a_different_repository_origin_is_rejected(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    key = _deadline_before_publication(h).follow_ups[0]["key"]
    outcome = h.wake(key, repository="someone/else")
    assert outcome.state == "repository_mismatch" and h.resume_windows == 0 and h.pr_attempts == []


def test_deadline_expiry_ends_with_one_actionable_blocker(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    key = _deadline_before_publication(h).follow_ups[0]["key"]
    h.clock.now += 30 * 3600  # past the 24h ttl

    outcome = h.wake(key)

    assert outcome.state == "expired" and h.resume_windows == 0 and h.pr_attempts == []
    assert "follow-up --rearm" in h.notes[0][1] and len(h.notes) == 1 and h.scheduler.live == {}
    assert h.wake(key).state == "inactive" and len(h.notes) == 1


# -------------------------------------------------------------------------------------------------------------------------------
# permission denial, transient failures


def test_permission_denial_is_recorded_once_ends_the_follow_up_and_tries_no_other_route(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    key = _deadline_before_publication(h).follow_ups[0]["key"]
    h.pr_error = "remote: Permission to acme/app.git denied to the bot. fatal: unable to access (403)"

    outcome = h.wake(key)

    assert outcome.state == "externally_blocked" and outcome.terminal
    assert h.pr_attempts == ["1"]  # one attempt at the prohibited action; no alternative branch, credential or retry
    assert externally_blocked(h.store, "1") is not None and task_delivery(h.store, "1", REPO).state == "EXTERNALLY_BLOCKED"
    assert h.scheduler.live == {} and len(h.notes) == 1 and "delivery-unblock" in h.notes[0][1]
    assert [h.wake(key).state for _ in range(3)] == ["inactive"] * 3 and h.pr_attempts == ["1"]

    # Later windows do not pick the task up again either, with or without --follow-up.
    again = h.window(tasks=["1"], follow_up=True)
    assert h.pr_attempts == ["1"] and again.follow_ups == [] and active_follow_ups(h.store) == []

    # Access granted and explicitly cleared: a fresh, separately keyed follow-up may be armed. The old one stays ended.
    clear_external_block(h.store, "1", "owner", "token now has write access")
    h.pr_error = None
    assert generation(h.store, "1") == 1
    rearmed = h.window(tasks=["1"], expire=100.0, follow_up=True)  # unblocked, but again out of time before publishing
    fresh, = rearmed.follow_ups
    assert fresh["state"] == "registered" and fresh["key"] != key and fresh["scheduled"] is True
    assert follow_up_outcome(h.store, key).startswith("externally_blocked")  # the ended one stays ended
    assert [r["key"] for r in active_follow_ups(h.store)] == [fresh["key"]] and h.pr_attempts == ["1"]


def test_a_permission_denial_while_merging_blocks_the_merge_path_once(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    key = h.window(merge=True).follow_ups[0]["key"]
    sha = h.store.latest_candidate("1")["sha"]
    h.hosting.head["https://github.com/acme/app/pull/101"] = sha
    h.hosting.error = HostingError("GraphQL: Resource not accessible by integration (HTTP 403)")

    outcome = h.wake(key)

    assert outcome.state == "externally_blocked" and h.landing_calls == 1  # the merge was not retried through the window either
    assert externally_blocked(h.store, "1") is not None and h.scheduler.live == {}
    assert h.wake(key).state == "inactive" and h.landing_calls == 1 and len(h.notes) == 1


def test_transient_failures_back_off_are_bounded_and_end_with_one_blocker(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    key = _deadline_before_publication(h).follow_ups[0]["key"]
    h.pr_error = "connection reset by peer"

    states = []
    for _ in range(MAX_TRANSIENT_FAILURES + 3):
        h.clock.now += 400  # past the retry backoff each time
        states.append(h.wake(key).state)
        if states[-1] == "transient_exhausted":
            break

    assert states[:-1] == ["retry"] * (MAX_TRANSIENT_FAILURES - 1) and states[-1] == "transient_exhausted"
    assert h.pr_attempts == ["1"] * MAX_TRANSIENT_FAILURES  # bounded, never an open-ended loop
    assert len(h.notes) == 1 and h.scheduler.live == {}
    assert h.wake(key).state == "inactive" and len(h.pr_attempts) == MAX_TRANSIENT_FAILURES


def test_a_wakeup_inside_the_backoff_window_does_nothing(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    key = _deadline_before_publication(h).follow_ups[0]["key"]
    h.pr_error = "connection reset by peer"
    assert h.wake(key).state == "retry"
    attempts = len(h.pr_attempts)
    assert h.wake(key).state == "not_due" and len(h.pr_attempts) == attempts and h.notes == []


# -------------------------------------------------------------------------------------------------------------------------------
# crash boundaries


def test_a_failed_schedule_removal_is_retried_by_the_next_wakeup_and_announced_once(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    key = _deadline_before_publication(h).follow_ups[0]["key"]
    h.scheduler.fail_removals = 1

    outcome = h.wake(key)  # delivery finishes, but the OS refuses to remove the schedule

    assert outcome.terminal and not follow_up_active(h.store, key) and key and h.scheduler.live  # complete, still scheduled
    assert schedule_handle(h.store, key) == f"fake/{key}" and "could not be removed" in h.notes[0][1]
    again = h.wake(key)  # the schedule fires once more; it only cleans up
    assert again.state == "inactive" and h.scheduler.live == {} and schedule_handle(h.store, key) is None
    assert h.pr_attempts == ["1"] and len(h.notes) == 1 and h.resume_windows == 1


def test_a_crash_between_registration_and_scheduling_is_repaired_by_the_next_window(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    report = h.window(expire=100.0, scheduler=False)  # registered, but no scheduler was reachable
    key = report.follow_ups[0]["key"]
    assert report.follow_ups[0]["scheduled"] is False and h.scheduler.calls == [] and follow_up_active(h.store, key)
    assert "stagemesh follow-up --key" in report.follow_ups[0]["reason"]  # the operator is told the one command

    h.restart()
    repaired = h.window(tasks=["1"], expire=100.0)

    assert repaired.follow_ups[0]["state"] == "existing" and repaired.follow_ups[0]["scheduled"] is True
    assert h.scheduler.calls == [("ensure", f"fake/{key}")] and len(active_follow_ups(h.store)) == 1


def test_a_lost_schedule_is_recreated_not_duplicated(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    key = _deadline_before_publication(h).follow_ups[0]["key"]
    h.scheduler.live.clear()  # someone deleted the OS task out from under us
    h.window(tasks=["1"], expire=100.0)
    assert list(h.scheduler.live) == [f"fake/{key}"] and [c for c in h.scheduler.calls if c[0] == "ensure"] == [("ensure", f"fake/{key}")] * 2


def test_cancel_removes_the_schedule_and_is_idempotent(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    key = _deadline_before_publication(h).follow_ups[0]["key"]

    first = cancel_follow_up(h.store, key, h.scheduler, h.notify, "operator cancelled")
    second = cancel_follow_up(h.store, key, h.scheduler, h.notify, "operator cancelled")

    assert first.state == "cancelled" and first.notified and not second.notified
    assert h.scheduler.live == {} and not follow_up_active(h.store, key) and len(h.notes) == 1
    assert h.wake(key).state == "inactive" and h.pr_attempts == []
    assert cancel_follow_up(h.store, "delivery-unknown", h.scheduler, h.notify, "x").state == "unknown_follow_up"


def test_rearm_lets_the_next_window_arm_a_fresh_follow_up_for_the_same_task(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    key = _deadline_before_publication(h).follow_ups[0]["key"]
    cancel_follow_up(h.store, key, h.scheduler, h.notify, "pause")
    ended = h.window(tasks=["1"], expire=100.0)
    assert ended.follow_ups[0]["state"] == "ended"

    assert rearm_follow_up(h.store, key) == "1"
    fresh = h.window(tasks=["1"], expire=100.0)

    assert fresh.follow_ups[0]["state"] == "registered" and fresh.follow_ups[0]["key"] != key and len(active_follow_ups(h.store)) == 1


# -------------------------------------------------------------------------------------------------------------------------------
# the actual scheduler adapter and the CLI entry points


class Recorder:
    """Stands in for `schtasks.exe`: a task exists between a successful /Create and a successful /Delete."""

    def __init__(self) -> None:
        self.tasks: set[str] = set()
        self.commands: list[list[str]] = []
        self.refuse_delete = False

    def __call__(self, argv):
        import subprocess

        self.commands.append(list(argv))
        action, name = argv[1], argv[argv.index("/TN") + 1]
        if action == "/Create":
            self.tasks.add(name)
            return subprocess.CompletedProcess(argv, 0, "SUCCESS", "")
        if action == "/Delete":
            if self.refuse_delete:
                return subprocess.CompletedProcess(argv, 1, "", "ERROR: Access is denied.")
            if name not in self.tasks:
                return subprocess.CompletedProcess(argv, 1, "", "ERROR: The system cannot find the file specified.")
            self.tasks.discard(name)
            return subprocess.CompletedProcess(argv, 0, "SUCCESS", "")
        return subprocess.CompletedProcess(argv, 0 if name in self.tasks else 1, "", "")


def test_completion_cleans_up_through_the_actual_windows_adapter(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    recorder = Recorder()
    h.scheduler = WindowsTaskScheduler(h.project, runner=recorder)
    key = _deadline_before_publication(h).follow_ups[0]["key"]
    handle = schedule_handle(h.store, key)
    create, = [c for c in recorder.commands if c[1] == "/Create"]
    launcher = Path(create[create.index("/TR") + 1].strip('"'))
    assert handle == f"StageMesh\\followup-{key}" and recorder.tasks == {handle}
    assert create[create.index("/SC") + 1] == "MINUTE" and create[create.index("/MO") + 1] == "10" and "/F" in create
    assert launcher.is_file() and f"--key {key}" in launcher.read_text(encoding="utf-8") and "follow-up" in launcher.read_text(encoding="utf-8")

    outcome = h.wake(key)

    assert outcome.state == "published_merge_not_authorized"
    assert recorder.tasks == set() and not launcher.exists()  # the OS task and its launcher are really gone
    assert [c[1] for c in recorder.commands if c[1] in ("/Delete", "/Query")] == ["/Delete", "/Query"]  # removed, then verified gone
    assert schedule_handle(h.store, key) is None


def test_a_refused_deletion_is_reported_not_swallowed(tmp_path: Path) -> None:
    recorder = Recorder()
    scheduler = WindowsTaskScheduler(tmp_path, runner=recorder)
    handle = scheduler.ensure("k", ["python", "-m", "stagemesh.cli"], 600)
    recorder.refuse_delete = True
    with pytest.raises(SchedulerError, match="could not remove"):
        scheduler.remove(handle)
    assert scheduler.exists(handle)
    recorder.refuse_delete = False
    scheduler.remove(handle)
    assert not scheduler.exists(handle)
    scheduler.remove(handle)  # already gone: success


def test_cli_follow_up_entry_points_have_runtime_callers(tmp_path: Path, capsys, monkeypatch) -> None:
    h = Harness(tmp_path)
    key = _deadline_before_publication(h).follow_ups[0]["key"]
    h.store.close()
    scheduler = h.scheduler
    monkeypatch.setattr("stagemesh.followup_scheduler.default_scheduler", lambda project: scheduler)

    assert cli_module.main(["--project", str(h.project), "follow-up", "--list"]) == 0
    assert key in capsys.readouterr().out
    assert cli_module.main(["--project", str(h.project), "follow-up", "--key", "delivery-nope"]) == 2
    monkeypatch.setenv("STAGEMESH_PROVIDER_PROCESS", "1")
    assert cli_module.main(["--project", str(h.project), "follow-up", "--key", key, "--cancel"]) == 2  # a provider cannot cancel or re-arm
    monkeypatch.delenv("STAGEMESH_PROVIDER_PROCESS")
    assert cli_module.main(["--project", str(h.project), "follow-up", "--key", key, "--cancel", "--reason", "test"]) == 0
    assert scheduler.live == {}
    parsed = cli_module.build_parser().parse_args(["run-window", "--follow-up", "--follow-up-interval", "5m", "--follow-up-ttl", "2h"])
    assert parsed.follow_up and parsed.follow_up_interval == "5m" and parsed.follow_up_ttl == "2h"


def test_wiring_is_reachable_from_the_real_entry_points() -> None:
    """The helpers are not dead code: run_window arms follow-ups, the CLI runs the worker, completion goes through the scheduler."""
    import inspect

    import stagemesh.delivery_followup as df

    assert "register_pending_follow_ups" in inspect.getsource(rw.run_window)
    assert "run_follow_up" in inspect.getsource(cli_module.command_follow_up) and "default_scheduler" in inspect.getsource(cli_module.command_run_window)
    assert "scheduler.remove" in inspect.getsource(df._deschedule) and "scheduler.exists" in inspect.getsource(df._deschedule)


# -------------------------------------------------------------------------------------------------------------------------------
# landing outcomes: a terminal answer is not a reason to keep asking


def test_a_merge_permission_denial_inside_a_window_is_recorded_once_and_never_retried(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.merge_script = [{"merged": False, "reason": "GraphQL: Resource not accessible by integration (HTTP 403)"}]

    first = h.window(merge=True)

    assert h.landing_calls == 1 and externally_blocked(h.store, "1") is not None
    assert task_delivery(h.store, "1", REPO).state == "EXTERNALLY_BLOCKED" and "delivery-unblock" in first.stop_reason
    assert first.follow_ups == [] and h.scheduler.calls == []  # nothing is scheduled to knock on a closed door
    url = task_delivery(h.store, "1", REPO).pr_url
    h._list_prs = lambda project: [{"url": url, "headRefName": "wave/task-1"}]  # like `gh`: the open PR is listed whatever its task state
    second = h.window(tasks=["1"], merge=True)  # the pending PR is found again, but the recorded denial stops the attempt before the guard
    assert h.landing_calls == 1 and "delivery-unblock" in (second.stop_reason or "")


def test_a_terminal_landing_reason_is_not_followed(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.merge_script = [{"merged": False, "reason": "hosted CI failed; candidate not merged"}]

    report = h.window(merge=True)

    entry, = report.follow_ups
    assert entry["state"] == "not_followed" and "hosted CI failed" in entry["reason"]
    assert active_follow_ups(h.store) == [] and h.scheduler.calls == [] and h.landing_calls == 1


def test_a_terminal_reason_found_by_the_worker_ends_it_with_one_blocker(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    key = h.window(merge=True).follow_ups[0]["key"]  # CI pending: armed
    h.hosting.head["https://github.com/acme/app/pull/101"] = h.store.latest_candidate("1")["sha"]
    h.merge_script.append({"merged": False, "reason": "PR closed without merging"})

    outcome = h.wake(key)

    assert outcome.state == "landing_blocked" and outcome.terminal and h.landing_calls == 2
    assert "needs a decision" in h.notes[0][1] and len(h.notes) == 1 and h.scheduler.live == {}
    assert [h.wake(key).state for _ in range(3)] == ["inactive"] * 3 and h.landing_calls == 2


def test_landing_reasons_are_classified_conservatively() -> None:
    from stagemesh.delivery_state import landing_retryable

    for reason in ("hosted CI deadline reached; PR retained for retry", "GitHub mergeability remained unknown for 60s; no merge attempted",
                   "merge_deadline_reached", "hosted checks missing after 60s: web", "queue admission paused"):
        assert landing_retryable(reason), reason
    for reason in ("hosted CI failed; candidate not merged", "PR closed without merging", "PR head no longer matches the staged candidate",
                   "PR conflicts with current base", "server-required checks are missing, pending or red", "unexpected PR URL",
                   "GraphQL: Resource not accessible by integration (HTTP 403)", "something nobody has seen before"):
        assert not landing_retryable(reason), reason  # unknown means ask a person, not loop


# -------------------------------------------------------------------------------------------------------------------------------
# hardening


def test_the_worker_only_runs_the_narrowed_command_it_registered(tmp_path: Path) -> None:
    from stagemesh.delivery_followup import UnsafeResumeCommand, validated_resume_argv

    h = Harness(tmp_path)
    key = _deadline_before_publication(h).follow_ups[0]["key"]
    record = follow_up_record(h.store, key)

    assert validated_resume_argv(record, h.project)[5] == "run-window"
    for tampered in (
        {**record, "resume_argv": ["cmd.exe", "/c", "echo", "owned"]},
        {**record, "resume_argv": record["resume_argv"][:5] + ["queue", "reconcile"]},
        {**record, "resume_argv": [*record["resume_argv"][:4], str(tmp_path / "elsewhere"), *record["resume_argv"][5:]]},
        {**record, "resume_argv": [*record["resume_argv"], "--follow-up"]},
        {**record, "task_id": "another-task"},
        {**record, "resume_argv": []},
    ):
        with pytest.raises(UnsafeResumeCommand):
            validated_resume_argv(tampered, h.project)


def test_hosting_read_failures_never_crash_a_wakeup(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    key = h.window(merge=True).follow_ups[0]["key"]

    class Exploding:
        def pr_snapshot(self, url):
            raise TimeoutError("gh took too long")

    outcome = h.wake(key, hosting=Exploding())

    assert outcome.state == "waiting" and h.landing_calls == 2 and follow_up_active(h.store, key)  # the resume window re-read it itself


def test_the_gh_adapter_turns_every_failure_into_a_classified_error(tmp_path: Path, monkeypatch) -> None:
    import subprocess

    from stagemesh.delivery_followup import GhHosting

    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired("gh", 30)

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(HostingError) as slow:
        GhHosting(tmp_path).pr_snapshot("https://github.com/acme/app/pull/1")
    assert slow.value.permission is False

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "HTTP 403: Resource not accessible by integration"))
    with pytest.raises(HostingError) as denied:
        GhHosting(tmp_path).pr_snapshot("https://github.com/acme/app/pull/1")
    assert denied.value.permission is True


# -------------------------------------------------------------------------------------------------------------------------------
# independent-review fixes


def test_rate_limiting_is_transient_not_a_permission_denial() -> None:
    from stagemesh.delivery_state import is_permission_denial

    assert not is_permission_denial("HTTP 403: API rate limit exceeded for user")
    assert not is_permission_denial("error 403 secondary rate limit")
    assert is_permission_denial("HTTP 403: Resource not accessible by integration")


def test_a_stale_landing_result_does_not_veto_arming_a_later_window(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.merge_script = [{"merged": False, "reason": "hosted CI failed; candidate not merged"}]
    assert h.window(merge=True).follow_ups[0]["state"] == "not_followed"
    time.sleep(0.05)
    h.merge_script = [{"merged": False, "reason": "hosted CI deadline reached; PR retained for retry"}]  # CI re-run on the same head
    again = h.window(tasks=["1"], merge=True)
    assert again.follow_ups and again.follow_ups[0]["state"] == "registered"


def test_rearm_and_unblock_never_create_a_second_worker_for_an_active_delivery(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    key = _deadline_before_publication(h).follow_ups[0]["key"]
    assert rearm_follow_up(h.store, key) is None  # still active: refused
    clear_external_block(h.store, "1", "owner", "x")  # an unblock bumps the generation, but the active follow-up is reused
    again = h.window(tasks=["1"], expire=100.0)
    assert [e["key"] for e in again.follow_ups] == [key] and len(active_follow_ups(h.store)) == 1


def test_a_schedule_created_before_a_crash_is_still_removed_on_completion(tmp_path: Path) -> None:
    recorder = Recorder()
    h = Harness(tmp_path)
    h.scheduler = WindowsTaskScheduler(h.project, runner=recorder)
    key = _deadline_before_publication(h).follow_ups[0]["key"]
    h.store.conn.execute("DELETE FROM audit_events WHERE event_type='followup.scheduled'")  # the handle was never recorded
    h.store.conn.commit()
    assert schedule_handle(h.store, key) is None and recorder.tasks

    h.wake(key)

    assert recorder.tasks == set()


def test_budget_never_blocks_ticks_that_call_no_provider(tmp_path: Path) -> None:
    from stagemesh.efficiency import BudgetPolicy, record_provider_call

    rig = _LoopRigForStage(tmp_path)
    for i in range(5):
        record_provider_call(rig.store, task_id="TASK-1", purpose="review", provider="claude", outcome="failed", reason="same", duration_seconds=1.0)
    rig.coordinator.budget_policy = BudgetPolicy(max_equivalent_failures=2)
    rig.store.advance_task("TASK-1", Stage.INTEGRATE)
    from stagemesh.run_ready import RunSummary, drive_task

    summary = RunSummary(True, "UNSET", task_id="TASK-1")
    drive_task(rig.store, rig.project, rig.coordinator, "TASK-1", summary, max_steps=1, global_health=False)
    assert summary.steps  # the integrate tick ran despite the spent provider budget


class _LoopRigForStage:
    def __init__(self, tmp_path: Path):
        from test_completion_integrity import FakeReviewAdapter
        from test_completion_integrity import Rig as CompletionRig

        rig = CompletionRig(tmp_path, FakeReviewAdapter("claude"), integration_ref="refs/heads/integration")
        self.store, self.project, self.coordinator = rig.store, rig.project, rig.coordinator
