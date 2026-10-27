"""Failure-to-delivery, end to end with fakes: setup timeout -> diagnosis stop -> repair -> adverse review -> remediation ->
independent pass -> staged -> deadline -> delivery pending -> restart -> published -> merged -> issue closure.

Real: gate runner, validator, diagnosis, coordinator, reviewer gate, integrator, survivor wave and delivery state.
Fake: the implementation provider (a script), the review adapters, the PR opener and the merge (an audit record), and the clock.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from test_survivor_wave import Rig

from stagemesh.capacity import CapacityKind
from stagemesh.coordinator import Coordinator, TargetSelection
from stagemesh.delivery_state import task_delivery
from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage, TaskStatus
from stagemesh.execution import SubprocessExecutor
from stagemesh.git import GitWorkspace
from stagemesh.integration import Integrator
from stagemesh.outbound_lifecycle import sync_outbound_outcomes
from stagemesh.persistence import Store
from stagemesh.provider_pool import REVIEW, ProviderLog, ProviderPool
from stagemesh.review import Reviewer
from stagemesh.review_identity import command_snapshot, note_review_identity
from stagemesh.run_ready import RunSummary, drive_task, format_stop
from stagemesh.survivor_wave import RunResult, run_wave
from stagemesh.validation import Validator

TASK = "TASK-1"
PY = sys.executable
STAGING = "refs/heads/wave/staging"
FINDING = '{"decision":"FAIL_WITH_FINDINGS","findings":[{"severity":"error","message":"docs/a.md does not record the Engineering Impact","path":"docs/a.md"}]}'


class SequencedReviewer:
    """Answers from a script, one answer per call, and remembers how often it was asked."""

    def __init__(self, name: str, answers: list[str]):
        self.name, self.answers, self.calls = name, list(answers), 0
        self.command = ("controlled-review", name)
        self.capabilities = frozenset({"review"})

    def command_for(self, _capability):
        return self.command

    def all_commands(self):
        return (self.command,)

    def check_stage_capacity(self, _capability):
        return CapacityKind.AVAILABLE

    def review_candidate(self, prompt: str, project: Path, candidate_sha: str) -> str:
        self.calls += 1
        note_review_identity(command_snapshot(self.name, self.command))
        return self.answers.pop(0) if self.answers else '{"decision":"PASS"}'


class Flow(Rig):
    def __init__(self, tmp_path: Path, reviewers: list[SequencedReviewer]):
        project = tmp_path / "repo"
        project.mkdir()
        git = GitWorkspace(project)
        git.init_if_needed()
        git.run("config", "user.email", "test@example.invalid")
        git.run("config", "user.name", "StageMesh Test")
        (project / "docs").mkdir()
        (project / "docs" / "a.md").write_text("a\n", encoding="utf-8")
        git.commit_all("base")
        git.run("branch", "wave/staging")
        self.fixed = tmp_path / "environment-fixed.flag"
        self.counter = tmp_path / "implementations.txt"
        gate = (
            "import pathlib, sys, time\n"
            f"if pathlib.Path({str(self.fixed)!r}).exists(): sys.exit(0)\n"
            "print('collected 41 items\\nsession fixture db_engine: setting up', flush=True)\n"  # collection finished, then session setup never completes
            "time.sleep(30)\n"
        )
        contracts = project / ".stagemesh" / "contracts"
        contracts.mkdir(parents=True)
        (contracts / f"{TASK}.json").write_text(
            json.dumps({"objective": "docs only", "allowed_files": ["docs/**"],
                        "required_tests": [{"name": "pure-unit", "command": [PY, "-c", gate], "timeout_seconds": 2}]}),
            encoding="utf-8",
        )
        provider = tmp_path / "provider.py"
        provider.write_text(
            "import pathlib\n"
            f"c = pathlib.Path({str(self.counter)!r})\n"
            "n = int(c.read_text()) + 1 if c.exists() else 1\n"
            "c.write_text(str(n))\n"
            "pathlib.Path('docs/a.md').write_text('implementation %d\\n' % n)\n",
            encoding="utf-8",
        )
        self.provider = provider
        self.reviewers = reviewers
        self.normal_reviewer = SequencedReviewer("normal-review", [])
        super().__init__(project)
        self.prs: list[str] = []
        self.store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
        self.store.migrate()
        self.store.upsert_task("end to end", source_id=TASK)
        self.store.advance_task(TASK, Stage.IMPLEMENT)
        self.summaries: list[RunSummary] = []
        self.deadline_after_done: list[float] | None = None

    @property
    def implementations(self) -> int:
        return int(self.counter.read_text()) if self.counter.exists() else 0

    def run_task(self, task_id: str, max_steps: int) -> RunResult:
        action = self.behaviour.get(task_id)
        return action(self, task_id) if callable(action) else self.drive(task_id, max_steps)

    def drive(self, task_id: str, max_steps: int) -> RunResult:
        store = self.worker_store()
        try:
            reviewer = next((r for r in self.reviewers if r.answers or r is self.reviewers[-1]), self.reviewers[-1])
            pool = ProviderPool(
                [self.normal_reviewer, reviewer], {REVIEW: (self.normal_reviewer.name, reviewer.name)},
                require_independent=True, policy="priority",
                priorities={self.normal_reviewer.name: 0, reviewer.name: 1}, log=ProviderLog(echo=False),
            )
            coordinator = Coordinator(
                store, self.project,
                executor=SubprocessExecutor([PY, str(self.provider)], name="codex"),
                validator=Validator(),
                reviewer=Reviewer(review_pool=pool, require_independent=True, separate_roles=True),
                integrator=Integrator(integration_ref=STAGING, require_independent_review=True),
                require_independent_review=True,
                target=TargetSelection(task_id),
            )
            summary = RunSummary(True, "UNSET", task_id=task_id)
            drive_task(store, self.project, coordinator, task_id, summary, max_steps=max_steps, global_health=False)
            self.summaries.append(summary)
            return RunResult(summary.stop_reason, summary.message)
        finally:
            store.close()


def _events(store, event_type: str) -> list[dict]:
    return [json.loads(r[0]) for r in store.conn.execute("SELECT payload FROM audit_events WHERE event_type=? ORDER BY created_at, rowid", (event_type,))]


def test_failure_to_delivery_with_accurate_state_at_every_step(tmp_path: Path) -> None:
    claude = SequencedReviewer("claude", [FINDING])  # raises one adverse finding, then passes
    flow = Flow(tmp_path, [claude])
    store = flow.store
    clock = [0.0]

    # 1. Window 1: the gate stalls after collection. One diagnosis, no blind retry, nothing published, nothing called DONE.
    report = run_wave(store, [TASK], flow.hooks(), max_steps=6, advance_baselines=False, unblock_blocked=False, open_prs=True)
    assert flow.implementations == 1  # a single implementation attempt was spent
    assert report.tasks[0].outcome in {"blocked", "blocked_needs_decision"} and flow.prs == []
    assert store.get_task(TASK)["status"] == TaskStatus.BLOCKED
    assert "gate_timeout_diagnosis_required" in _events(store, "task.block_classified")[-1]["reason"]
    assert task_delivery(store, TASK).state == "IN_PROGRESS"
    assert "diagnosis" in format_stop(flow.summaries[-1]) or flow.summaries[-1].stop_reason == "BLOCKED"

    # 2. The operator fixes the environment and gives the task a fresh start (the supported retry); the gate now runs.
    flow.fixed.write_text("ok", encoding="utf-8")
    assert store.unblock_task(TASK)

    # 3. Window 2 (with a deadline that passes once the task is done and before publication): adverse review, remediation, the
    #    independent pass on the new candidate, integration on the staging ref. Delivery is pending, never "done".
    def window_two(rig, task_id):
        result = rig.drive(task_id, 12)
        clock[0] = 11.0
        return result

    flow.behaviour = {TASK: window_two}
    report = run_wave(store, [TASK], flow.hooks(), max_steps=12, deadline=10.0, clock=lambda: clock[0], advance_baselines=False, unblock_blocked=False)
    record = report.tasks[0]
    assert claude.calls >= 2  # FAIL_WITH_FINDINGS on the first candidate, PASS on the remediated one
    review_rows = [(row["candidate_sha"], row["status"], json.loads(row["payload"])["review_role"])
                   for row in store.conn.execute("SELECT candidate_sha,status,payload FROM evidence WHERE kind='REVIEW' ORDER BY created_at,rowid")]
    assert len(review_rows) == 4
    reviews = [(sha, status) for sha, status, role in review_rows if role == "adverse"]
    assert [status for _, status in reviews] == ["FAILED", "PASSED"] and reviews[0][0] != reviews[1][0]  # the adverse pass is for a new candidate
    normal = [(sha, status) for sha, status, role in review_rows if role == "normal"]
    assert [status for _, status in normal] == ["PASSED", "PASSED"] and [sha for sha, _ in normal] == [sha for sha, _ in reviews]
    assert flow.normal_reviewer.calls == 2  # both candidates earned their own normal role
    assert record.landing == "staged_not_published" and record.stop_reason == "PR_DELIVERY_PENDING" and record.resumable
    delivery = task_delivery(store, TASK)
    assert delivery.state == "DELIVERY_PENDING" and not delivery.complete and flow.prs == []
    candidates_before = [row["sha"] for row in store.conn.execute("SELECT sha FROM candidates WHERE task_id=? ORDER BY created_at, rowid", (TASK,))]
    implementations_before = flow.implementations

    # 4. Restart: a new window resumes delivery only. No implementation replays, one PR is opened for the exact reviewed head.
    flow.behaviour = {}
    report = run_wave(store, [TASK], flow.hooks(), max_steps=6, advance_baselines=False, unblock_blocked=False)
    assert flow.implementations == implementations_before  # nothing was re-implemented
    assert [row["sha"] for row in store.conn.execute("SELECT sha FROM candidates WHERE task_id=? ORDER BY created_at, rowid", (TASK,))] == candidates_before
    assert flow.prs == [TASK] and report.tasks[0].landing == "pr_opened"
    published, = _events(store, "delivery.pr_published")
    assert published["candidate_sha"] == candidates_before[-1]
    assert task_delivery(store, TASK).state == "PUBLISHED" and not task_delivery(store, TASK).complete

    # 5. The issue stays open while the PR is only published; it closes after the exact head merged, exactly once.
    closed: list[str] = []

    class Sync:
        def publish_done(self, issue, subject):
            closed.append(issue)
            return "evt"

        def publish(self, *args, **kwargs):
            return "evt"

    store.conn.execute("UPDATE tasks SET source='github', source_id='101' WHERE id=?", (TASK,))
    store.conn.commit()
    task = store.get_task(TASK)
    sync_outbound_outcomes(store, Sync(), frozenset({"github"}), [task], {TASK: "DONE"})
    assert closed == []
    store.add_audit_event("window.fast_forward_merged", {"task_id": TASK, "candidate_sha": candidates_before[-1], "pr": 9})
    assert task_delivery(store, TASK).state == "MERGED" and task_delivery(store, TASK).complete
    sync_outbound_outcomes(store, Sync(), frozenset({"github"}), [task], {TASK: "DONE"})
    assert closed == ["101"]
    assert store.get_task(TASK)["stage"] == Stage.DONE
    assert any(row["status"] == EvidenceStatus.PASSED for row in store.conn.execute("SELECT status FROM evidence WHERE kind=?", (EvidenceKind.INTEGRATION,)))
