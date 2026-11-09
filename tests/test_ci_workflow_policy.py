from __future__ import annotations

from pathlib import Path

from stagemesh.ci_validation_plan import OPERATOR_DEPENDENT_TESTS, OPERATOR_DIRECT_TESTS

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
MANDATORY_STEPS = (
    "Compute audited validation plan",
    "Attribution and static range policy",
    "Fail-closed repository quality gate",
    "Focused validation policy",
    "Declarative and executable contract invariants",
)


def _jobs(text: str) -> dict[str, str]:
    windows = text.index("\n  windows:\n")
    linux = text.index("\n  linux:\n")
    return {"windows": text[windows:linux], "linux": text[linux:]}


def _attribution_job(text: str) -> str:
    start = text.index("\n  attribution:\n")
    end = text.index("\n  windows:\n")
    return text[start:end]


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
    text = WORKFLOW.read_text(encoding="utf-8")
    jobs = _jobs(text)
    attribution = _attribution_job(text)
    attribution_step = _step(attribution, "Attribution and static range policy")
    assert "if:" not in attribution_step
    assert "python scripts/check_ci_range.py" in attribution_step
    for job in jobs.values():
        assert "needs: attribution" in job
        for name in MANDATORY_STEPS:
            if name == "Attribution and static range policy":
                continue
            assert "if:" not in _step(job, name), name
        quality = _step(job, "Fail-closed repository quality gate")
        assert "scripts/quality_gate.py --mode ci" in quality.replace("\\", "/")
        assert "STAGEMESH_CI_BASE_SHA:" in quality
        assert "STAGEMESH_CI_HEAD_SHA:" in quality
        assert "ci-quality-report.json" in quality
        assert "tests/test_docs_links.py" in _step(job, "Focused validation policy")


def test_quality_control_files_have_code_owner_review_boundaries() -> None:
    owners = (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
    for path in (
        "/.github/",
        "/.gitignore",
        "/.stagemesh/quality-policy.json",
        "/pyproject.toml",
        "/build_backend.py",
        "/scripts/quality_gate.py",
        "/src/stagemesh/quality_policy.py",
    ):
        assert f"{path} @saketvishal" in owners


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
    windows = _step(jobs["windows"], "Authoritative Windows platform regression")
    assert full_condition in windows
    assert "scripts/run_regression.py" in windows
    assert "--profile windows-essential --jobs 1" in windows
    assert "windows-regression-journal.jsonl" in windows
    assert "windows-regression-report.json" in windows
    assert "STAGEMESH_CI_HEAD_SHA:" in windows
    assert "Authoritative complete regression" not in jobs["windows"]

    linux = _step(jobs["linux"], "Authoritative complete regression")
    assert full_condition in linux
    assert "scripts/run_regression.py" in linux
    assert "--jobs 6" in linux and "--profile" not in linux
    assert "linux-regression-journal.jsonl" in linux
    assert "linux-regression-report.json" in linux
    assert "STAGEMESH_CI_HEAD_SHA:" in linux

    publish_names = {
        "windows": "Publish Windows platform evidence",
        "linux": "Publish complete regression evidence",
    }
    for os_name, job in jobs.items():
        publish = _step(job, publish_names[os_name])
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
