from __future__ import annotations

from pathlib import Path

from stagemesh.ci_validation_plan import OPERATOR_DEPENDENT_TESTS, OPERATOR_DIRECT_TESTS

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
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
        assert "--plan-file .stagemesh-ci-plan.json" in plan
        assert "STAGEMESH_CI_FORCE_FULL:" in plan


def test_mandatory_steps_never_depend_on_focused_mode() -> None:
    jobs = _jobs(WORKFLOW.read_text(encoding="utf-8"))
    for job in jobs.values():
        for name in MANDATORY_STEPS:
            assert "if:" not in _step(job, name), name
        assert "python scripts/check_ci_range.py" in _step(
            job, "Attribution and static range policy"
        )
        assert "tests/test_docs_links.py" in _step(job, "Focused validation policy")


def test_component_mode_uses_validated_plan_file_on_each_required_os() -> None:
    jobs = _jobs(WORKFLOW.read_text(encoding="utf-8"))
    condition = "if: ${{ steps.validation-plan.outputs.mode == 'component' }}"
    for os_name, job in jobs.items():
        component = _step(job, "Audited component tests")
        assert condition in component
        assert "scripts/run_ci_component_tests.py" in component
        assert "--plan .stagemesh-ci-plan.json" in component
        assert f"--os {os_name}" in component


def test_full_mode_has_one_authoritative_regression_and_published_evidence() -> None:
    jobs = _jobs(WORKFLOW.read_text(encoding="utf-8"))
    full_condition = "if: ${{ steps.validation-plan.outputs.mode == 'full' }}"
    for os_name, job in jobs.items():
        complete = _step(job, "Authoritative complete regression")
        assert full_condition in complete
        assert "scripts/run_regression.py" in complete
        assert f"--jobs {4 if os_name == 'windows' else 6}" in complete
        assert f"{os_name}-regression-journal.jsonl" in complete
        assert f"{os_name}-regression-report.json" in complete
        assert "STAGEMESH_CI_HEAD_SHA:" in complete

        publish = _step(job, "Publish complete regression evidence")
        assert "always()" in publish
        assert "actions/upload-artifact@v4" in publish
        assert f"regression-{os_name}-${{{{ github.run_attempt }}}}" in publish
        assert f"{os_name}-regression-report.json" in publish
        assert f"{os_name}-regression-journal.jsonl" in publish

        operator_condition = "if: ${{ steps.validation-plan.outputs.mode == 'operator' }}"
        assert operator_condition in _step(job, "Stabilization matrix")
        operator = _step(job, "Operator activity contract and read-only API")
        assert operator_condition in operator
        for target in (*OPERATOR_DIRECT_TESTS, *OPERATOR_DEPENDENT_TESTS):
            assert target in operator, target

        assert job.count("scripts/run_regression.py") == 1

    text = WORKFLOW.read_text(encoding="utf-8")
    for removed_group in (
        "Delivery and window regressions",
        "Isolated validation temporary directories",
        "Reliable execution, validation and delivery",
        "Research, planning and adverse evidence pipeline",
    ):
        assert removed_group not in text


def test_main_push_is_smoke_while_schedule_and_manual_runs_are_full() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    pre_jobs = text[: text.index("\njobs:\n")]
    assert "  push:\n    branches: [main]" in pre_jobs
    assert "  schedule:\n    - cron:" in pre_jobs
    assert "  workflow_dispatch:\n    inputs:\n      mode:" in pre_jobs
    assert "default: full" in pre_jobs and "options: [full]" in pre_jobs
    force = (
        "github.event_name == 'schedule' || "
        "github.event_name == 'workflow_dispatch'"
    )
    assert text.count(force) == 2
    assert "github.event_name == 'push' ||" not in text
