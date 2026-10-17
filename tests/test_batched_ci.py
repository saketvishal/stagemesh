"""Tests for batched development and CI workflow improvements.

Validates:
1. Support grouping compatible, independently implemented tasks into a bounded integration batch.
2. Run focused, affected-component tests during development.
3. Preserve task-specific validation and independent review evidence.
4. Combine completed candidates on a protected integration branch, with conflict detection.
5. Run full required integration checks against the exact combined candidate.
6. Avoid pushing incomplete intermediate integration states that trigger redundant CI.
7. Reuse test evidence only when candidate identity, dependencies, and affected components permit it.
8. Serialize final integration and prevent workspace conflicts.
9. Preserve branch protections, required hosted checks, and auditability.
10. Report time spent waiting for CI, duplicate runs avoided, and batch-level results.
"""

from __future__ import annotations

import json
from pathlib import Path

from test_survivor_wave import Rig, make_store

import stagemesh.cli as cli_module
import stagemesh.run_window as rw
from stagemesh.concurrency import contract_conflict
from stagemesh.domain import EvidenceKind
from stagemesh.runtime_freshness import RuntimeFreshness
from stagemesh.survivor_wave import RepoState, format_report, load_wave_contract, run_wave

FRESH = RuntimeFreshness(running_from="src", running_from_source=True, installed_cli="x", source_cli="y")
IN_SYNC = RepoState(main_ref="main", main_sha="a" * 40, origin_sha="a" * 40, relation="in_sync")
OK_PROVIDERS = lambda store, config, strict_live: rw.Check("providers", True, "fake providers")


class MockClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_batched_wave_avoids_intermediate_prs_and_records_metrics(tmp_path: Path) -> None:
    """Requirement 1, 3, 4, 6, 10: Tasks in a wave are combined into a batch; only the terminal candidate is published."""
    store, project = make_store(tmp_path, ["1", "2", "3"])
    rig = Rig(project)
    hooks = rig.hooks()
    hooks.is_ancestor = lambda older, newer: older < newer
    clock = MockClock()

    report = run_wave(
        store,
        ["1", "2", "3"],
        hooks,
        concurrency=3,
        ordered_publication=True,
        batch_ci=True,
        clock=clock,
    )

    assert report.ok
    assert len(report.tasks) == 3
    # Exactly 1 PR was opened instead of 3
    assert len(rig.prs) == 1
    assert rig.prs == ["3"]

    # Intermediate records avoided redundant hosted CI runs
    t1 = next(t for t in report.tasks if t.task_id == "1")
    t2 = next(t for t in report.tasks if t.task_id == "2")
    t3 = next(t for t in report.tasks if t.task_id == "3")

    assert t1.landing == "batched_intermediate"
    assert t2.landing == "batched_intermediate"
    assert t3.landing == "pr_opened"
    assert t1.pr_url == t3.pr_url
    assert t2.pr_url == t3.pr_url

    # Duplicate runs avoided reported accurately (3 tasks -> 2 intermediate runs avoided)
    assert report.duplicate_runs_avoided == 2
    assert len(report.batch_results) == 1
    batch = report.batch_results[0]
    assert batch["task_ids"] == ["1", "2", "3"]
    assert batch["combined_sha"] == t3.integrated_candidate
    assert batch["duplicate_runs_avoided"] == 2
    assert batch["status"] == "batched_published"

    # Task-specific validation and independent review evidence are preserved for every candidate
    for tid in ("1", "2", "3"):
        task = next(t for t in report.tasks if t.task_id == tid)
        sha = task.integrated_candidate
        assert sha is not None
        assert store.has_evidence(tid, sha, EvidenceKind.VALIDATION)
        assert store.has_evidence(tid, sha, EvidenceKind.REVIEW)

    # Format report displays batched CI line
    text = format_report(report)
    assert "batched CI: 2 duplicate run(s) avoided" in text


def test_unbatched_mode_publishes_all_prs_when_batch_ci_disabled(tmp_path: Path) -> None:
    """Requirement 6: When batch_ci is disabled, each task opens its own PR individually."""
    store, project = make_store(tmp_path, ["1", "2"])
    rig = Rig(project)
    hooks = rig.hooks()
    hooks.is_ancestor = lambda older, newer: older < newer

    report = run_wave(
        store,
        ["1", "2"],
        hooks,
        concurrency=2,
        ordered_publication=True,
        batch_ci=False,
    )

    assert report.ok
    # Both PRs opened individually
    assert len(rig.prs) == 2
    assert rig.prs == ["1", "2"]
    assert report.duplicate_runs_avoided == 0
    assert len(report.batch_results) == 0


def test_conflict_detection_prevents_overlapping_tasks_in_batch(tmp_path: Path) -> None:
    """Requirement 1, 4: Compatible tasks are grouped; conflicting tasks cannot run concurrently."""
    store, project = make_store(tmp_path, ["A", "B"])

    # Overwrite contract of B to overlap with A
    path_a = project / ".stagemesh/contracts/A.json"
    path_b = project / ".stagemesh/contracts/B.json"
    data_a = json.loads(path_a.read_text(encoding="utf-8"))
    data_b = json.loads(path_b.read_text(encoding="utf-8"))

    data_a["protected_files"] = ["shared/module.py"]
    data_b["allowed_files"] = ["shared/module.py"]
    path_a.write_text(json.dumps(data_a), encoding="utf-8")
    path_b.write_text(json.dumps(data_b), encoding="utf-8")

    contract_a = load_wave_contract(store, project, "A")
    contract_b = load_wave_contract(store, project, "B")

    # Contract conflict is detected
    conflict = contract_conflict(contract_a, contract_b)
    assert conflict is not None
    assert "shared/module.py" in str(conflict)


def test_run_window_batched_auto_merge_lands_combined_candidate(tmp_path: Path) -> None:
    """Requirement 5, 8, 9, 10: Fast-forward auto-merge validates exact combined candidate and merges batch."""
    store, project = make_store(tmp_path, ["10", "20"])
    rig = Rig(project)
    hooks = rig.hooks()
    hooks.is_ancestor = lambda older, newer: older < newer
    hooks.repo_state = lambda: IN_SYNC
    clock = MockClock()

    merged_log: list[str] = []

    def mock_merge(record, deadline):
        clock.advance(15.0)  # simulate 15s CI wait
        merged_log.append(record.task_id)
        return {
            "merged": True,
            "remote_landed": True,
            "local_synced": True,
            "pr": 100,
            "candidate_sha": record.integrated_candidate,
            "ci_wait_seconds": 15.0,
        }

    options = rw.WindowOptions(
        auto_merge_ff=True,
        required_checks=("web",),
        batch_ci=True,
        report_path=tmp_path / "report.json",
        tasks=["10", "20"],
    )
    config = cli_module.load_config(project)

    report = rw.run_window(
        store,
        project,
        config,
        options,
        hooks,
        base_ref="refs/heads/main",
        staging_ref="refs/heads/wave/staging",
        merge_pr=mock_merge,
        runtime=FRESH,
        list_prs=lambda p: [],
        provider_check=OK_PROVIDERS,
        clock=clock,
    )

    assert report.ok
    # Only 1 merge call executed for the combined candidate "20"
    assert len(merged_log) == 1
    assert merged_log == ["20"]

    # Both tasks are merged in the report
    merged_ids = [m.get("task_id") for m in report.merged_prs]
    assert "10" in merged_ids
    assert "20" in merged_ids

    # Duplicate runs avoided: 1 intermediate run avoided
    assert report.duplicate_runs_avoided == 1
    assert report.ci_wait_seconds >= 15.0
    assert len(report.batch_results) == 1
    assert report.batch_results[0]["status"] == "merged"
    assert report.batch_results[0]["task_ids"] == ["10", "20"]

    # Audit events record the batched landing
    events = [
        json.loads(row["payload"])
        for row in store.conn.execute(
            "SELECT payload FROM audit_events WHERE event_type=?", ("batch.integrated",)
        ).fetchall()
    ]
    assert len(events) == 1
    assert events[0]["task_ids"] == ["10", "20"]

    # Report text includes batched CI metrics
    text = rw.format_window_report(report)
    assert "batched CI: 1 duplicate run(s) avoided" in text
    assert "15.0s spent waiting for CI" in text
    assert "ci-mode batch" in text


def test_ci_mode_immediate_preserves_individual_task_publishing(tmp_path: Path) -> None:
    """Acceptance: --ci-mode immediate preserves current individual PR and CI behavior."""
    store, project = make_store(tmp_path, ["A1", "A2"])
    rig = Rig(project)
    hooks = rig.hooks()
    hooks.is_ancestor = lambda older, newer: older < newer
    hooks.repo_state = lambda: IN_SYNC

    options = rw.WindowOptions(
        ci_mode="immediate",
        tasks=["A1", "A2"],
        report_path=tmp_path / "report.json",
    )
    config = cli_module.load_config(project)

    report = rw.run_window(
        store,
        project,
        config,
        options,
        hooks,
        base_ref="refs/heads/main",
        staging_ref="refs/heads/wave/staging",
        runtime=FRESH,
        list_prs=lambda p: [],
        provider_check=OK_PROVIDERS,
    )

    assert report.ok
    assert report.options.ci_mode == "immediate"
    assert report.options.batch_ci is False
    # Both PRs opened individually
    assert len(rig.prs) == 2
    assert rig.prs == ["A1", "A2"]
    text = rw.format_window_report(report)
    assert "ci-mode immediate" in text


def test_ci_mode_batch_with_concurrency_5(tmp_path: Path) -> None:
    """Acceptance: --ci-mode batch works with --concurrency 5, combines tasks and avoids intermediate CI runs."""
    ids = ["1", "2", "3", "4", "5"]
    store, project = make_store(tmp_path, ids)
    rig = Rig(project)
    hooks = rig.hooks()
    hooks.is_ancestor = lambda older, newer: older < newer
    hooks.repo_state = lambda: IN_SYNC

    options = rw.WindowOptions(
        ci_mode="batch",
        concurrency=5,
        tasks=ids,
        report_path=tmp_path / "report.json",
    )
    config = cli_module.load_config(project)

    report = rw.run_window(
        store,
        project,
        config,
        options,
        hooks,
        base_ref="refs/heads/main",
        staging_ref="refs/heads/wave/staging",
        runtime=FRESH,
        list_prs=lambda p: [],
        provider_check=OK_PROVIDERS,
    )

    assert report.ok
    assert report.options.ci_mode == "batch"
    assert report.options.batch_ci is True
    # Only 1 PR opened for all 5 tasks
    assert len(rig.prs) == 1
    assert rig.prs == ["5"]
    assert report.duplicate_runs_avoided == 4
    assert len(report.batch_results) == 1
    assert report.batch_results[0]["duplicate_runs_avoided"] == 4
    assert report.batch_results[0]["task_ids"] == ids
    text = rw.format_window_report(report)
    assert "ci-mode batch" in text
    assert "batched CI: 4 duplicate run(s) avoided" in text


def test_ci_mode_batch_failure_preserves_successful_tasks(tmp_path: Path) -> None:
    """Acceptance: A batch failure preserves successful candidates and supports safe recovery."""
    from stagemesh.survivor_wave import RunResult

    store, project = make_store(tmp_path, ["T1", "T2", "T3"])
    rig = Rig(project)
    hooks = rig.hooks()
    hooks.is_ancestor = lambda older, newer: older < newer

    original_run = hooks.run_task

    def failing_run(task_id: str, max_steps: int) -> RunResult:
        if task_id == "T2":
            # T2 fails validation/execution
            return RunResult("RUNNER_CRASH", "runner crashed on test failure", 1)
        return original_run(task_id, max_steps)

    hooks.run_task = failing_run

    report = run_wave(
        store,
        ["T1", "T2", "T3"],
        hooks,
        concurrency=3,
        ordered_publication=True,
        ci_mode="batch",
    )

    # T2 failed, but T1 and T3 succeeded
    t1 = next(t for t in report.tasks if t.task_id == "T1")
    t2 = next(t for t in report.tasks if t.task_id == "T2")
    t3 = next(t for t in report.tasks if t.task_id == "T3")

    assert t2.outcome == "infrastructure_failure"
    assert "runner crashed" in str(t2.reason)

    assert t1.outcome == "done"
    assert t3.outcome == "done"
    # Successful work is preserved: T1 and T3 form a successful batch
    assert len(rig.prs) == 1
    assert rig.prs == ["T3"]
    assert t1.landing == "batched_intermediate"
    assert t3.landing == "pr_opened"
    # Evidence for T1 and T3 is intact in the store
    assert store.has_evidence("T1", t1.integrated_candidate, EvidenceKind.VALIDATION)
    assert store.has_evidence("T3", t3.integrated_candidate, EvidenceKind.VALIDATION)


def test_ci_mode_defaults_and_cli_parsing(tmp_path: Path) -> None:
    """Acceptance: Omitted flag preserves existing behavior (defaults to immediate)."""
    options_default = rw.WindowOptions()
    assert options_default.ci_mode == "immediate"
    assert options_default.batch_ci is False

    options_batch = rw.WindowOptions(ci_mode="batch")
    assert options_batch.ci_mode == "batch"
    assert options_batch.batch_ci is True

    # Test CLI parser resolves --ci-mode
    parser = cli_module.build_parser()
    args_default = parser.parse_args(["run-window"])
    assert getattr(args_default, "ci_mode", None) == "immediate"

    args_batch = parser.parse_args(["run-window", "--ci-mode", "batch"])
    assert args_batch.ci_mode == "batch"

    args_immediate = parser.parse_args(["run-window", "--ci-mode", "immediate"])
    assert args_immediate.ci_mode == "immediate"

