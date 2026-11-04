from __future__ import annotations

from pathlib import Path

from stagemesh.ci_validation_plan import OPERATOR_DEPENDENT_TESTS, OPERATOR_DIRECT_TESTS

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
FULL_GROUPS = (
    "Delivery and window regressions",
    "Isolated validation temporary directories",
    "Missing provider tool host",
    "Existing queue stop compatibility",
    "Execution budgets and review ownership",
    "Autonomous recovery ledger",
    "Continuous driver control and recovery",
    "Validation environment and fallback progress",
    "Reliable execution, validation and delivery",
    "Provider context and edit scope",
    "Research, planning and adverse evidence pipeline",
    "Exact-label selector and scoped automatic recovery",
)
MANDATORY_STEPS = (
    "Compute audited validation plan",
    "Attribution and static range policy",
    "Focused validation policy",
    "Declarative and executable contract invariants",
)


def _jobs(text: str) -> dict[str, str]:
    windows = text.index("\n  windows:\n")
    linux = text.index("\n  linux:\n")
    return {"windows": text[windows:linux], "linux": text[linux:]}


def _step(job: str, name: str) -> str:
    marker = f"      - name: {name}\n"
    start = job.index(marker)
    next_step = job.find("\n      - ", start + len(marker))
    end = next_step if next_step >= 0 else len(job)
    return job[start:end]


def test_protected_jobs_compute_and_publish_exact_diff_plan_without_renaming() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    jobs = _jobs(text)
    assert set(jobs) == {"windows", "linux"}
    assert text.count("\n  windows:\n") == text.count("\n  linux:\n") == 1

    for job in jobs.values():
        assert "fetch-depth: 0" in job
        plan = _step(job, "Compute audited validation plan")
        assert "id: validation-plan" in plan
        assert "github.event.pull_request.base.sha" in plan
        assert "github.event.merge_group.base_sha" in plan
        assert "github.event.pull_request.head.sha" in plan
        assert "github.event.merge_group.head_sha" in plan
        assert "python scripts/ci_validation_plan.py" in plan
        assert "STAGEMESH_CI_FORCE_FULL:" in plan


def test_mandatory_steps_never_depend_on_focused_mode() -> None:
    jobs = _jobs(WORKFLOW.read_text(encoding="utf-8"))
    for job in jobs.values():
        for name in MANDATORY_STEPS:
            assert "if:" not in _step(job, name), name
        assert "python scripts/check_ci_range.py" in _step(
            job, "Attribution and static range policy"
        )


def test_complete_groups_are_retained_and_only_skip_for_audited_modes() -> None:
    jobs = _jobs(WORKFLOW.read_text(encoding="utf-8"))
    full_condition = "if: ${{ steps.validation-plan.outputs.mode == 'full' }}"
    focused_condition = (
        "if: ${{ steps.validation-plan.outputs.mode == 'full' || "
        "steps.validation-plan.outputs.mode == 'operator' }}"
    )
    for job in jobs.values():
        for name in FULL_GROUPS:
            block = _step(job, name)
            assert full_condition in block, name
            assert "run:" in block, name
        recovery = _step(job, "Autonomous recovery ledger")
        assert "tests/test_autonomous_recovery.py" in recovery
        assert "tests/test_recovery_ledger.py" in recovery
        delivery = _step(job, "Delivery and window regressions")
        assert "tests/test_autonomous_window.py" in delivery
        assert focused_condition in _step(job, "Stabilization matrix")
        operator = _step(job, "Operator activity contract and read-only API")
        assert focused_condition in operator
        for target in (*OPERATOR_DIRECT_TESTS, *OPERATOR_DEPENDENT_TESTS):
            assert target in operator, target


def test_main_schedule_and_manual_runs_are_authoritative_full_regressions() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    pre_jobs = text[: text.index("\njobs:\n")]
    assert "  push:\n    branches: [main]" in pre_jobs
    assert "  schedule:\n    - cron:" in pre_jobs
    assert "  workflow_dispatch:\n    inputs:\n      mode:" in pre_jobs
    assert "default: full" in pre_jobs and "options: [full]" in pre_jobs
    force = (
        "github.event_name == 'push' || github.event_name == 'schedule' || "
        "github.event_name == 'workflow_dispatch'"
    )
    assert text.count(force) == 2
