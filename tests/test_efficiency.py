"""Efficiency accounting and budgets, through the real adapters, drive loop and window.

Accounting: provider attempts, purposes, failure classes, provider-reported usage (unknown is never zero), stage transitions, checks
run/reused and validation time. Budgets: limits derived from the ledger, so they survive new windows and restarts, and enforced only
between stages.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from test_completion_integrity import TASK, FakeReviewAdapter
from test_completion_integrity import Rig as CompletionRig
from test_run_window import FRESH, IN_SYNC, OK_PROVIDERS, Clock
from test_survivor_wave import Rig as WaveRig
from test_survivor_wave import make_store

import stagemesh.cli as cli_module
import stagemesh.run_window as rw
from stagemesh import readiness
from stagemesh.config import ConfigValidationError, load_config
from stagemesh.diagnosis import DiagnosisPolicy
from stagemesh.domain import EvidenceKind, EvidenceStatus, TaskStatus
from stagemesh.efficiency import (
    PROVIDER_CALL,
    BudgetPolicy,
    Usage,
    check_task_budget,
    efficiency_report,
    equivalent_failure_streak,
    extract_usage,
    failure_class,
    provider_calls,
    record_provider_call,
    usage_totals,
)
from stagemesh.execution import SubprocessExecutor
from stagemesh.persistence import Store
from stagemesh.providers import RuntimeCommandAdapter
from stagemesh.run_ready import RunSummary, drive_task

PY = sys.executable


def _events(store: Store, event_type: str) -> list[dict]:
    return [json.loads(r[0]) for r in store.conn.execute("SELECT payload FROM audit_events WHERE event_type=? ORDER BY created_at, rowid", (event_type,))]


# -------------------------------------------------------------------------------------------------------------------------------
# usage honesty


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ('{"type":"result","usage":{"input_tokens":100,"output_tokens":20,"cache_read_input_tokens":60}}', Usage(100, 20, 60)),
        ('{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2}}\n{"type":"turn.completed","usage":{"input_tokens":5,"output_tokens":3}}', Usage(15, 5, None)),
        ('{"text":"ok","usage":{"prompt_tokens":7,"completion_tokens":4}}', Usage(7, 4, None)),
        ("tokens used: 12,345", Usage()),  # free text is not parsed: unknown, not guessed
        ('{"usage":{"input_tokens":1}}\n{"usage":{"input_tokens":2}}', Usage()),  # several unlabelled blocks: ambiguous, so unknown
        ('{"type":"result","usage":{"input_tokens":-5,"output_tokens":true}}', Usage()),  # invalid counts are not usage
        ("", Usage()),
    ],
)
def test_usage_is_extracted_only_when_unambiguous_and_unknown_is_never_zero(stdout: str, expected: Usage) -> None:
    usage = extract_usage(stdout)
    assert usage == expected
    if not expected.reported:
        assert usage.input is None and usage.output is None and usage.cached_input is None and not usage.reported


def test_totals_count_only_reported_calls_and_say_how_many_were_unknown() -> None:
    calls = [
        {"usage": Usage(100, 20, 60).to_dict()}, {"usage": Usage().to_dict()}, {"usage": Usage(50, None, None).to_dict()}, {},
    ]
    totals = usage_totals(calls)
    assert (totals["input"], totals["output"], totals["cached_input"]) == (150, 20, 60)
    assert totals["calls_reporting"] == 2 and totals["calls_unknown"] == 2
    assert usage_totals([{"usage": Usage().to_dict()}]) == {"input": None, "output": None, "cached_input": None, "unit": "tokens", "calls_reporting": 0, "calls_unknown": 1}


def test_recorded_usage_survives_the_audit_redactor_and_nothing_private_is_stored(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.sqlite3")
    store.migrate()
    store.upsert_task("t", source_id="T")
    record_provider_call(
        store, task_id="T", purpose="review", provider="claude", outcome="failed", duration_seconds=1.23456, candidate_sha="a" * 40,
        reason="timed out waiting for fixture db_engine at 10.0.0.7 after 600s sk-secret-0000", usage=Usage(120, 30, 80),
    )
    record_provider_call(store, task_id="T", purpose="implementation", provider="codex", outcome="succeeded", duration_seconds=2.0)
    first, second = _events(store, PROVIDER_CALL)

    assert first["usage"] == {"input": 120, "output": 30, "cached_input": 80, "unit": "tokens", "reported": True}  # not ***REDACTED***
    assert second["usage"] == {"input": None, "output": None, "cached_input": None, "unit": "tokens", "reported": False}
    assert first["duration_seconds"] == 1.235 and first["purpose"] == "review"
    assert first["failure_class"].startswith("timed out waiting for fixture db_engine at #.#.#.# after #s")
    assert len(first["failure_class"]) <= 80  # bounded: a class label, not a log
    assert set(first) == {"task_id", "candidate_sha", "provider", "purpose", "outcome", "failure_class", "duration_seconds", "usage"}
    assert second["failure_class"] is None


def test_equivalent_failures_compare_equal_across_candidates_and_numbers() -> None:
    assert failure_class("gate timed out after 600s on abcdef1234567") == failure_class("gate timed out after 900s on 0123456789abcd")
    calls = [{"outcome": "failed", "failure_class": "x"}, {"outcome": "succeeded"}, {"outcome": "failed", "failure_class": "y"},
             {"outcome": "timeout", "failure_class": "y"}, {"outcome": "failed", "failure_class": "y"}]
    assert equivalent_failure_streak(calls) == (3, "y")  # a success ends a streak; a different failure class starts a new one


# -------------------------------------------------------------------------------------------------------------------------------
# the real adapters record what they spend


def _repo_rig(tmp_path: Path, provider_body: str, reviewer=None) -> CompletionRig:
    rig = CompletionRig(tmp_path, reviewer or FakeReviewAdapter("claude"), integration_ref="refs/heads/integration")
    (tmp_path / "provider.py").write_text(provider_body, encoding="utf-8")
    rig.coordinator.executor = RuntimeCommandAdapter(name="codex", command=(PY, str(tmp_path / "provider.py")))
    return rig


REPORTING_PROVIDER = (
    "import json, pathlib\n"
    "pathlib.Path('docs/a.md').write_text('implemented')\n"
    "print(json.dumps({'type': 'result', 'usage': {'input_tokens': 900, 'output_tokens': 100, 'cache_read_input_tokens': 700}}))\n"
)


def test_the_real_implementation_adapter_records_one_call_with_reported_usage(tmp_path: Path) -> None:
    rig = _repo_rig(tmp_path, REPORTING_PROVIDER)

    rig.tick(1)

    call, = provider_calls(rig.store, TASK)
    assert call["purpose"] == "implementation" and call["provider"] == "codex" and call["outcome"] == "succeeded"
    assert call["candidate_sha"] == rig.store.latest_candidate(TASK)["sha"] and call["duration_seconds"] >= 0
    assert call["usage"] == {"input": 900, "output": 100, "cached_input": 700, "unit": "tokens", "reported": True}


def test_a_provider_that_reports_nothing_is_recorded_as_unknown_not_zero(tmp_path: Path) -> None:
    rig = _repo_rig(tmp_path, "import pathlib\npathlib.Path('docs/a.md').write_text('implemented')\n")

    rig.tick(1)

    call, = provider_calls(rig.store, TASK)
    assert call["usage"]["reported"] is False and call["usage"]["input"] is None and call["usage"]["output"] is None
    report = efficiency_report(rig.store)
    assert report["totals"]["usage"]["calls_unknown"] == 1 and report["totals"]["usage"]["input"] is None


def test_a_failed_implementation_is_recorded_with_its_failure_class_and_the_remediation_purpose(tmp_path: Path) -> None:
    rig = _repo_rig(tmp_path, "pass\n")  # exits 0, changes nothing

    rig.tick(1)
    rig.store.add_remediation_attempt  # noqa: B018 - the attribute exists on the real store (guards against a renamed API)
    first = provider_calls(rig.store, TASK)[-1]
    assert first["outcome"] == "failed" and first["failure_class"] == "no_implementation_change" and first["purpose"] == "implementation"


def test_review_attempts_are_recorded_per_call_with_usage_noted_by_the_adapter(tmp_path: Path) -> None:
    review = (
        "import json\nprint(json.dumps({'text': '{\"decision\":\"PASS\"}', 'usage': {'input_tokens': 300, 'output_tokens': 40}}))\n"
    )
    (tmp_path / "reviewer.py").write_text(review, encoding="utf-8")
    reviewer = RuntimeCommandAdapter(name="claude", command=(PY, str(tmp_path / "reviewer.py")))
    rig = _repo_rig(tmp_path, REPORTING_PROVIDER, reviewer=reviewer)

    rig.tick(3)  # implement, validate, review

    calls = provider_calls(rig.store, TASK)
    assert [c["purpose"] for c in calls] == ["implementation", "review"]
    assert calls[1]["provider"] == "claude" and calls[1]["outcome"] == "succeeded"
    assert calls[1]["usage"] == {"input": 300, "output": 40, "cached_input": None, "unit": "tokens", "reported": True}  # cached: unknown, not 0


def test_a_fake_reviewer_that_cannot_report_usage_is_still_counted_with_unknown_usage(tmp_path: Path) -> None:
    rig = _repo_rig(tmp_path, REPORTING_PROVIDER)
    rig.tick(3)
    review, = [c for c in provider_calls(rig.store, TASK) if c["purpose"] == "review"]
    assert review["usage"]["reported"] is False


# -------------------------------------------------------------------------------------------------------------------------------
# stage transitions, validation reuse and the report


def test_stage_transitions_and_check_reuse_are_reported_per_task(tmp_path: Path) -> None:
    rig = _repo_rig(tmp_path, REPORTING_PROVIDER)
    rig.tick(3)
    rig.store.add_evidence(TASK, "e" * 40, EvidenceKind.VALIDATION, EvidenceStatus.PASSED, {
        "validation_reuse": {"ran": ["unit"], "reused": [{"gate": "lint", "from": "x"}, {"gate": "types", "from": "x"}]}})

    task, = efficiency_report(rig.store)["tasks"]

    assert task["task_id"] == TASK and task["provider_attempts"] == 2 and task["attempts_by_purpose"] == {"implementation": 1, "review": 1}
    assert task["stage_transitions"] >= 3 and task["checks_reused"] >= 2 and task["validation_seconds"] >= 0
    assert {t["from"] for t in _events(rig.store, "stage.transition")} >= {"IMPLEMENT", "VALIDATE"}


def test_the_efficiency_command_prints_honest_totals(tmp_path: Path, capsys) -> None:
    rig = _repo_rig(tmp_path, REPORTING_PROVIDER)
    rig.tick(1)
    rig.store.close()

    assert cli_module.main(["--project", str(rig.project), "efficiency"]) == 0
    out = capsys.readouterr().out
    assert "provider attempts: 1" in out and "input 900, output 100, cached input 700" in out and "1 call(s) reported, 0 unknown" in out
    assert cli_module.main(["--project", str(rig.project), "efficiency", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["totals"]["provider_attempts"] == 1


# -------------------------------------------------------------------------------------------------------------------------------
# budgets: persisted, stage-boundary, no weakening of validation


class LoopRig(CompletionRig):
    """A provider that never changes anything: the same equivalent failure on every admission.

    The category-specific repeat diagnosis is switched off so the generic budget is what is under test; the default configuration
    has both, and whichever fires first bounds the loop (`test_the_default_configuration_is_bounded_by_the_earlier_guard`).
    """

    def __init__(self, tmp_path: Path, policy: BudgetPolicy, *, diagnosis: bool = False):
        super().__init__(tmp_path, FakeReviewAdapter("claude"), integration_ref="refs/heads/integration")
        if not diagnosis:
            self.coordinator.diagnosis_policy = DiagnosisPolicy(repeat_threshold=99, stop_on_repeat=False)
        (tmp_path / "noop.py").write_text("pass\n", encoding="utf-8")
        self.coordinator.executor = SubprocessExecutor([PY, str(tmp_path / "noop.py")], name="codex")
        self.coordinator.budget_policy = policy

    def window(self, steps: int = 6) -> RunSummary:
        """One run window's worth of work on the task, on a freshly opened ledger (a restart)."""
        self.store.close()
        self.store = Store(self.project / ".stagemesh" / "stagemesh.sqlite3")
        self.store.migrate()
        self.coordinator.store = self.store
        summary = RunSummary(True, "UNSET", task_id=TASK)
        drive_task(self.store, self.project, self.coordinator, TASK, summary, max_steps=steps, global_health=False)
        return summary

    @property
    def spent(self) -> int:
        return len(provider_calls(self.store, TASK))


def test_equivalent_failures_stop_at_the_limit_across_windows_and_restarts(tmp_path: Path) -> None:
    rig = LoopRig(tmp_path, BudgetPolicy(max_equivalent_failures=3))

    outcomes = []
    for _ in range(5):  # five windows over the same failing task, each on a freshly opened ledger
        summary = rig.window()
        outcomes.append((summary.stop_reason, rig.spent))
        if rig.store.get_task(TASK)["status"] == TaskStatus.BLOCKED:
            break

    assert rig.spent == 3  # bounded: three identical failures, never a fourth blind attempt
    assert outcomes[-1][0] == "BLOCKED" and "budget_exhausted (equivalent_failures)" in summary.message
    assert "stagemesh retry-task" in summary.message and rig.store.get_task(TASK)["status"] == TaskStatus.BLOCKED
    blocker, = _events(rig.store, "task.budget_exhausted")
    assert blocker["observed"] == 3 and blocker["limit"] == 3
    classified = _events(rig.store, "task.block_classified")[-1]
    assert classified["failure_code"] == "budget_exhausted" and classified["kind"] == "BLOCKED_RECOVERABLE_OPERATIONAL"
    assert len(_events(rig.store, "task.budget_exhausted")) == 1  # one actionable blocker, not one per window


def test_the_budget_is_checked_only_between_stages_so_nothing_running_is_interrupted(tmp_path: Path) -> None:
    rig = LoopRig(tmp_path, BudgetPolicy(max_equivalent_failures=2))
    for _ in range(2):
        rig.window(steps=1)
    assert rig.spent == 2
    running_before = list(rig.store.running_executions())
    summary = rig.window(steps=1)

    assert summary.stop_reason == "BLOCKED" and summary.steps == []  # it never started another stage
    assert rig.spent == 2 and not running_before and not list(rig.store.running_executions())
    assert not rig.store.has_active_claim(TASK)


def test_a_fresh_start_by_the_operator_resets_the_task_counters_and_work_resumes(tmp_path: Path) -> None:
    rig = LoopRig(tmp_path, BudgetPolicy(max_equivalent_failures=2))
    for _ in range(4):
        rig.window(steps=1)
    assert rig.store.get_task(TASK)["status"] == TaskStatus.BLOCKED and rig.spent == 2

    assert rig.store.unblock_task(TASK)  # `stagemesh retry-task`
    assert check_task_budget(rig.store, TASK, rig.coordinator.budget_policy) is None
    rig.window(steps=1)

    assert rig.spent == 3  # exactly one more attempt was allowed, then the loop is bounded again


def test_the_per_task_call_budget_is_independent_of_the_failure_streak(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.sqlite3")
    store.migrate()
    store.upsert_task("t", source_id="T")
    for i in range(5):  # alternating outcomes: no streak, but a lot of spend
        record_provider_call(store, task_id="T", purpose="implementation", provider="codex", outcome="succeeded" if i % 2 else "failed",
                             reason=f"failure {i}x", duration_seconds=1.0, usage=Usage(100, 10, None))
    policy = BudgetPolicy(max_provider_calls_per_task=5, max_equivalent_failures=3)
    assert check_task_budget(store, "T", policy).exceeded == "provider_calls"
    assert check_task_budget(store, "T", BudgetPolicy(max_provider_calls_per_task=6, max_equivalent_failures=3)) is None
    usage_limit = check_task_budget(store, "T", BudgetPolicy(max_provider_calls_per_task=None, max_equivalent_failures=None, max_reported_usage_per_task=550))
    assert usage_limit.exceeded == "reported_usage" and "0 call(s) reported none" in usage_limit.detail
    store.close()


def test_unknown_usage_never_counts_toward_the_usage_budget(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.sqlite3")
    store.migrate()
    store.upsert_task("t", source_id="T")
    for _ in range(10):
        record_provider_call(store, task_id="T", purpose="implementation", provider="codex", outcome="succeeded", duration_seconds=1.0)
    assert check_task_budget(store, "T", BudgetPolicy(max_provider_calls_per_task=None, max_equivalent_failures=None, max_reported_usage_per_task=1)) is None
    store.close()


@pytest.fixture
def _quiet_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(readiness, "check_runtime", lambda project: FRESH)
    monkeypatch.setattr(readiness, "active_cooldowns", lambda store, seconds: [])


def test_the_window_budget_stops_admission_at_a_wave_boundary(tmp_path: Path, _quiet_runtime) -> None:
    store, project = make_store(tmp_path, ["1", "2"])
    for i in range(4):  # provider calls already spent in this window by earlier waves
        record_provider_call(store, task_id="1", purpose="implementation", provider="codex", outcome="failed", reason=f"r{i}", duration_seconds=1.0)
    rig = WaveRig(project, {})
    hooks = rig.hooks()
    hooks.repo_state = lambda: IN_SYNC
    config = load_config(project)
    config = type(config)(**{**config.__dict__, "budgets": type(config.budgets)(max_provider_calls_per_window=3)})
    options = rw.WindowOptions(report_path=tmp_path / "out.json", concurrency=1)
    report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", runtime=FRESH, list_prs=lambda p: [],
                           provider_check=OK_PROVIDERS, clock=Clock())
    # The calls above happened before this window started, so they are not this window's spend: the window runs normally.
    assert report.ok and rig.runs

    rig2 = WaveRig(project, {})
    hooks2 = rig2.hooks()
    hooks2.repo_state = lambda: IN_SYNC
    config2 = type(config)(**{**config.__dict__, "budgets": type(config.budgets)(max_provider_calls_per_window=1)})

    def spend(r, task_id):  # this window's own first wave spends past the limit (from the worker's own connection, like a child process)
        worker = r.worker_store()
        try:
            record_provider_call(worker, task_id=task_id, purpose="implementation", provider="codex", outcome="succeeded", duration_seconds=1.0)
        finally:
            worker.close()
        return r.finish_done(task_id)

    rig2.behaviour = {"2": spend, "1": spend}
    store.conn.execute("UPDATE tasks SET stage='PLAN', status='OPEN'")
    store.conn.commit()
    report2 = rw.run_window(store, project, config2, rw.WindowOptions(report_path=tmp_path / "out2.json", concurrency=1, max_tasks=5), hooks2,
                            base_ref="refs/heads/main", runtime=FRESH, list_prs=lambda p: [], provider_check=OK_PROVIDERS, clock=Clock())
    assert report2.stop_reason and "provider-call budget reached for this window" in report2.stop_reason
    assert sorted(rig2.runs) == ["1", "2"] and report2.counts() == {"done": 2}  # the wave in flight finished; nothing new was admitted


def test_budget_configuration_is_validated_and_defaults_are_bounded(tmp_path: Path) -> None:
    store, project = make_store(tmp_path, ["1"])
    config_path = project / ".stagemesh" / "config.json"
    base = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    assert load_config(project).budgets.max_equivalent_failures == 3 and load_config(project).budgets.max_provider_calls_per_task == 30
    for bad in ({"max_equivalent_failures": 1}, {"max_provider_calls_per_task": -1}, {"max_provider_calls_per_task": "many"}, {"nope": 1}):
        config_path.write_text(json.dumps({**base, "budgets": bad}), encoding="utf-8")
        with pytest.raises(ConfigValidationError):
            load_config(project)
    config_path.write_text(json.dumps({**base, "budgets": {"max_equivalent_failures": 0, "max_provider_calls_per_task": 9, "max_reported_usage_per_task": 1000}}), encoding="utf-8")
    loaded = load_config(project).budgets
    assert (loaded.max_equivalent_failures, loaded.max_provider_calls_per_task, loaded.max_reported_usage_per_task) == (None, 9, 1000)
    assert BudgetPolicy.from_config(loaded).max_provider_calls_per_task == 9
    store.close()


def test_the_default_configuration_is_bounded_by_the_earlier_guard(tmp_path: Path) -> None:
    rig = LoopRig(tmp_path, BudgetPolicy(), diagnosis=True)  # default budget (3) plus the existing repeat diagnosis (2)
    for _ in range(6):
        rig.window()
        if rig.store.get_task(TASK)["status"] == TaskStatus.BLOCKED:
            break
    assert rig.store.get_task(TASK)["status"] == TaskStatus.BLOCKED and rig.spent <= 3
