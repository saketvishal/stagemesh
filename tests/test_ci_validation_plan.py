from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from stagemesh.ci_validation_plan import (
    CAPABILITY_DEPENDENT_TESTS,
    CAPABILITY_DIRECT_TESTS,
    CI_VALIDATION_CONTRACT,
    COMPONENT,
    DOCS,
    FULL,
    MANDATORY_CHECKS,
    OPERATOR,
    OPERATOR_DEPENDENT_TESTS,
    OPERATOR_DIRECT_TESTS,
    SMOKE,
    TASK_SOURCE_DEPENDENT_TESTS,
    TASK_SOURCE_DIRECT_TESTS,
    derive_ci_validation_plan,
)

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "ci_validation_plan_script", ROOT / "scripts" / "ci_validation_plan.py"
)
script = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = script
SPEC.loader.exec_module(script)
RANGE_SPEC = importlib.util.spec_from_file_location(
    "check_ci_range_script", ROOT / "scripts" / "check_ci_range.py"
)
range_script = importlib.util.module_from_spec(RANGE_SPEC)
sys.modules[RANGE_SPEC.name] = range_script
RANGE_SPEC.loader.exec_module(range_script)


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=repo, env=env, text=True, encoding="utf-8"
    ).strip()


def commit(repo: Path, message: str) -> str:
    environment = dict(
        os.environ,
        GIT_AUTHOR_NAME="Owner",
        GIT_AUTHOR_EMAIL="owner@example.org",
        GIT_COMMITTER_NAME="Owner",
        GIT_COMMITTER_EMAIL="owner@example.org",
    )
    git(repo, "-c", "core.hooksPath=", "add", ".", env=environment)
    git(repo, "-c", "core.hooksPath=", "commit", "-m", message, env=environment)
    return git(repo, "rev-parse", "HEAD")


def test_exact_operator_roots_are_normalized_and_deterministic() -> None:
    paths = (
        r"SRC\StageMesh\UI\APP.JS",
        "./src/stagemesh/activity.py",
        "tests/test_ui_browser_behavior.py",
        "docs/operator-ui-contract.md",
        "src/stagemesh/activity.py",
    )
    plan = derive_ci_validation_plan(reversed(paths))

    assert plan.contract == CI_VALIDATION_CONTRACT
    assert plan.mode == OPERATOR
    expected_paths = {
        path.casefold().replace("\\", "/").removeprefix("./") for path in paths
    }
    assert plan.changed_paths == tuple(sorted(expected_paths))
    assert plan.components == ("activity_read_model", "packaged_operator_ui")
    assert plan.direct_test_targets == OPERATOR_DIRECT_TESTS
    assert plan.dependent_test_targets == OPERATOR_DEPENDENT_TESTS
    assert plan.mandatory_checks == MANDATORY_CHECKS
    assert plan.required_os == ("windows", "linux")
    assert plan.browser_required and plan.windows_installer_required and plan.runtime_coverage


def test_exact_capability_roots_select_bounded_cross_platform_component_tests() -> None:
    plan = derive_ci_validation_plan(
        (
            "src/stagemesh/agent_skills.py",
            r"SRC\StageMesh\CAPABILITIES.PY",
            "tests/test_agent_skills.py",
            "docs/agent-skills.md",
        )
    )

    assert plan.mode == COMPONENT
    assert plan.components == ("bundled_agent_skills", "capability_registry")
    assert plan.direct_test_targets == tuple(sorted(CAPABILITY_DIRECT_TESTS))
    assert plan.dependent_test_targets == tuple(sorted(CAPABILITY_DEPENDENT_TESTS))
    assert plan.required_os == ("windows", "linux")
    assert plan.runtime_coverage is True
    assert plan.browser_required is False
    assert plan.windows_installer_required is False


def test_exact_task_source_root_selects_progress_and_admission_contracts() -> None:
    plan = derive_ci_validation_plan(
        ("src/stagemesh/task_sources.py", "tests/test_github_task_sources.py")
    )

    assert plan.mode == COMPONENT
    assert plan.components == ("task_source_sync",)
    assert plan.direct_test_targets == tuple(sorted(TASK_SOURCE_DIRECT_TESTS))
    assert plan.dependent_test_targets == tuple(sorted(TASK_SOURCE_DEPENDENT_TESTS))
    assert plan.required_os == ("windows", "linux")


def test_multiple_audited_families_union_targets_deterministically() -> None:
    paths = (
        "src/stagemesh/task_sources.py",
        "src/stagemesh/provider_events.py",
        "tests/test_provider_events.py",
        "tests/test_source_sync_preserves_progress.py",
    )
    plan = derive_ci_validation_plan(reversed(paths))

    assert plan.mode == COMPONENT
    assert plan.components == ("capability_evidence", "task_source_sync")
    assert plan.direct_test_targets == tuple(
        sorted({*CAPABILITY_DIRECT_TESTS, *TASK_SOURCE_DIRECT_TESTS})
    )
    assert plan.dependent_test_targets == tuple(
        sorted({*CAPABILITY_DEPENDENT_TESTS, *TASK_SOURCE_DEPENDENT_TESTS})
    )
    assert plan.to_dict() == derive_ci_validation_plan(paths).to_dict()


@pytest.mark.parametrize(
    "paths",
    [
        ("tests/test_capability_registry.py",),
        ("src/stagemesh/capabilities.py", "tests/test_unknown_capability.py"),
        ("src/stagemesh/task_sources.py", "src/stagemesh/persistence.py"),
        ("src/stagemesh/agent_skills.py", "src/stagemesh/providers.py"),
        ("src/stagemesh/activity.py", "src/stagemesh/capabilities.py"),
        ("src/stagemesh/mcp_proxy.py",),
        ("src/stagemesh/github.py",),
    ],
)
def test_component_support_only_unknown_and_shared_boundary_scopes_fail_safe(
    paths: tuple[str, ...],
) -> None:
    plan = derive_ci_validation_plan(paths)

    assert plan.mode == FULL
    assert plan.required_os == ("windows", "linux")


@pytest.mark.parametrize(
    "paths,reason",
    [
        ((), "empty"),
        (("src/stagemesh/activity.py", "src/stagemesh/persistence.py"), "full-regression paths"),
        (("src/stagemesh/activity.py", "tests/test_unknown_operator_case.py"), "unmapped paths"),
        (("tests/test_activity.py",), "no exact audited component source root"),
        (("src/stagemesh/not_a_real_boundary.py",), "unmapped paths"),
    ],
)
def test_empty_mixed_support_only_and_unmapped_scopes_fail_safe(
    paths: tuple[str, ...], reason: str
) -> None:
    plan = derive_ci_validation_plan(paths)
    assert plan.mode == FULL
    assert plan.required_os == ("windows", "linux")
    assert any(reason in item for item in plan.reasons)


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        "AGENTS.md",
        "build_backend.py",
        "pyproject.toml",
        "requirements-ci.txt",
        "docs/profiles/stagemesh.profile.json",
        "docs/focused-validation.md",
        "docs/validation-components.md",
        "src/stagemesh/ci_validation_plan.py",
        "src/stagemesh/validation_plan.py",
        "src/stagemesh/coordinator.py",
        "src/stagemesh/persistence.py",
        "src/stagemesh/security.py",
        "src/stagemesh/authorization.py",
        "src/stagemesh/provider_pool.py",
        "src/stagemesh/redaction.py",
        "src/stagemesh/validation_services.py",
        "src/stagemesh/public/schema/model.py",
        "scripts/ci_validation_plan.py",
        "scripts/run_ci_component_tests.py",
        "tests/test_ci_component_runner.py",
        "tests/test_ci_validation_plan.py",
    ],
)
def test_policy_dependency_lifecycle_security_and_selector_paths_force_full(path: str) -> None:
    plan = derive_ci_validation_plan((path,))
    assert plan.mode == FULL, path
    assert "boundary changed" in plan.reasons[0]


def test_documentation_only_plan_is_portable_and_claims_no_runtime_coverage() -> None:
    plan = derive_ci_validation_plan(("README.md", "docs/operator-guide.md"))
    assert plan.mode == DOCS
    assert plan.required_os == ("portable",)
    assert not plan.browser_required
    assert not plan.windows_installer_required
    assert not plan.runtime_coverage
    assert plan.direct_test_targets == plan.dependent_test_targets == ()


@pytest.mark.parametrize("path", ["", "../outside.py", "/absolute.py", r"C:\outside.py"])
def test_invalid_paths_fail_safe_without_escaping_repository(path: str) -> None:
    plan = derive_ci_validation_plan((path,))
    assert plan.mode == FULL
    assert "invalid changed path" in plan.reasons[0]


def test_cli_emits_versioned_json_and_github_outputs(tmp_path: Path, capsys) -> None:
    output = tmp_path / "github-output"
    plan_file = tmp_path / "plan.json"
    assert script.main(
        [
            "--path",
            "src/stagemesh/api_server.py",
            "--path",
            "tests/test_api_server.py",
            "--github-output",
            str(output),
            "--plan-file",
            str(plan_file),
        ]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["contract"] == CI_VALIDATION_CONTRACT
    assert payload["mode"] == OPERATOR
    assert payload["required_os"] == ["windows", "linux"]
    assert json.loads(plan_file.read_text(encoding="utf-8")) == payload
    assert output.read_text(encoding="utf-8").splitlines() == [
        "mode=operator",
        "run_full=false",
        "run_operator=true",
        "run_component=false",
        "run_docs=false",
        "run_smoke=false",
        "required_os=windows,linux",
    ]


def test_cli_emits_explicit_component_output_without_shell_targets(
    tmp_path: Path, capsys
) -> None:
    output = tmp_path / "github-output"
    plan_file = tmp_path / "plan.json"

    assert script.main(
        [
            "--path",
            "src/stagemesh/capabilities.py",
            "--github-output",
            str(output),
            "--plan-file",
            str(plan_file),
        ]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    lines = output.read_text(encoding="utf-8").splitlines()
    assert payload["mode"] == COMPONENT
    assert "run_component=true" in lines
    assert not any("test_capability_registry" in line for line in lines)
    assert json.loads(plan_file.read_text(encoding="utf-8")) == payload


def test_cli_derives_exact_changed_paths_from_git_range(tmp_path: Path) -> None:
    git(tmp_path, "init")
    (tmp_path / "src" / "stagemesh").mkdir(parents=True)
    source = tmp_path / "src" / "stagemesh" / "activity.py"
    source.write_text("before\n", encoding="utf-8")
    base = commit(tmp_path, "base")
    source.write_text("after\n", encoding="utf-8")
    head = commit(tmp_path, "operator change")

    plan = script.plan_from_inputs(repo=tmp_path, base=base, head=head, event="pull_request")
    assert plan.mode == OPERATOR
    assert plan.changed_paths == ("src/stagemesh/activity.py",)


def test_missing_or_invalid_git_identity_falls_back_to_explicit_full_plan() -> None:
    plan = script.plan_from_inputs(
        repo=ROOT,
        base="not-a-sha",
        head="also-not-a-sha",
        event="pull_request",
    )
    assert plan.mode == FULL
    assert "unavailable" in plan.reasons[0]
    assert plan.reasons[1] == "diff planning error: ValueError"


@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch"])
def test_authoritative_events_force_full_without_reading_a_diff(event: str) -> None:
    plan = script.plan_from_inputs(
        repo=ROOT,
        paths=("src/stagemesh/activity.py",),
        event=event,
    )
    assert plan.mode == FULL
    assert plan.reasons == (f"{event} event requires full regression",)


def test_main_push_uses_bounded_smoke_for_ordinary_changes() -> None:
    plan = script.plan_from_inputs(
        repo=ROOT,
        paths=("src/stagemesh/feature.py", "tests/test_feature.py"),
        event="push",
    )
    assert plan.mode == SMOKE
    assert plan.required_os == ("windows", "linux")
    assert plan.mandatory_checks == MANDATORY_CHECKS
    assert not plan.browser_required
    assert not plan.windows_installer_required
    assert not plan.runtime_coverage


def test_main_push_derives_smoke_from_exact_git_range(tmp_path: Path) -> None:
    git(tmp_path, "init")
    source = tmp_path / "src" / "stagemesh" / "feature.py"
    source.parent.mkdir(parents=True)
    source.write_text("before\n", encoding="utf-8")
    base = commit(tmp_path, "base")
    source.write_text("after\n", encoding="utf-8")
    head = commit(tmp_path, "ordinary change")

    plan = script.plan_from_inputs(
        repo=tmp_path,
        base=base,
        head=head,
        event="push",
    )

    assert plan.mode == SMOKE
    assert plan.changed_paths == ("src/stagemesh/feature.py",)


def test_main_push_emits_explicit_smoke_output(tmp_path: Path, capsys) -> None:
    output = tmp_path / "github-output"
    assert script.main(
        [
            "--event",
            "push",
            "--path",
            "src/stagemesh/feature.py",
            "--github-output",
            str(output),
        ]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == SMOKE
    assert "run_smoke=true" in output.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        "pyproject.toml",
        "src/stagemesh/security.py",
        "schema/model.sql",
    ],
)
def test_main_push_keeps_full_regression_for_ci_sensitive_boundaries(path: str) -> None:
    plan = script.plan_from_inputs(repo=ROOT, paths=(path,), event="push")
    assert plan.mode == FULL
    assert "CI-sensitive boundary changed" in plan.reasons[0]


def test_ci_sensitive_protected_merge_push_uses_smoke_after_full_pr_validation(
    tmp_path: Path,
) -> None:
    git(tmp_path, "init")
    git(tmp_path, "config", "user.name", "Owner")
    git(tmp_path, "config", "user.email", "owner@example.org")
    main_branch = git(tmp_path, "branch", "--show-current")
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    base = commit(tmp_path, "base")
    git(tmp_path, "checkout", "-b", "feature")
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: CI\n", encoding="utf-8")
    commit(tmp_path, "ci coverage")
    git(tmp_path, "checkout", main_branch)
    git(tmp_path, "merge", "--no-ff", "feature", "-m", "Merge pull request")
    head = git(tmp_path, "rev-parse", "HEAD")

    plan = script.plan_from_inputs(repo=tmp_path, base=base, head=head, event="push")

    assert plan.mode == SMOKE
    assert plan.components == ("protected_merge_smoke",)
    assert plan.changed_paths == (".github/workflows/ci.yml",)
    assert "two-parent protected merge topology" in plan.reasons[0]


def test_ci_sensitive_direct_push_with_one_parent_remains_full(tmp_path: Path) -> None:
    git(tmp_path, "init")
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: before\n", encoding="utf-8")
    base = commit(tmp_path, "base")
    workflow.write_text("name: after\n", encoding="utf-8")
    head = commit(tmp_path, "direct ci change")

    plan = script.plan_from_inputs(repo=tmp_path, base=base, head=head, event="push")

    assert plan.mode == FULL
    assert "CI-sensitive boundary changed" in plan.reasons[0]


def test_main_push_with_unavailable_range_fails_safe_to_full() -> None:
    plan = script.plan_from_inputs(
        repo=ROOT,
        base="not-a-sha",
        head="also-not-a-sha",
        event="push",
    )
    assert plan.mode == FULL
    assert "post-merge base/head diff is unavailable" in plan.reasons[0]


def test_candidate_range_policy_runs_static_and_attribution_checks(monkeypatch) -> None:
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(range_script.subprocess, "run", run)
    monkeypatch.setenv("STAGEMESH_CI_EVENT", "pull_request")
    monkeypatch.setenv("STAGEMESH_CI_BASE_SHA", "a" * 40)
    monkeypatch.setenv("STAGEMESH_CI_HEAD_SHA", "b" * 40)

    assert range_script.main() == 0
    assert [
        "git",
        "-c",
        "core.whitespace=cr-at-eol",
        "diff",
        "--check",
        f"{'a' * 40}...{'b' * 40}",
        "--",
    ] in calls
    assert any("scripts/check_attribution.py" in command for command in calls)


def test_main_push_range_does_not_recheck_github_merge_commit_attribution(monkeypatch) -> None:
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(range_script.subprocess, "run", run)
    monkeypatch.setenv("STAGEMESH_CI_EVENT", "push")
    monkeypatch.setenv("STAGEMESH_CI_BASE_SHA", "c" * 40)
    monkeypatch.setenv("STAGEMESH_CI_HEAD_SHA", "d" * 40)

    assert range_script.main() == 0
    assert any(
        command[:6]
        == ["git", "-c", "core.whitespace=cr-at-eol", "diff", "--check", "c" * 40 + "..." + "d" * 40]
        for command in calls
    )
    assert not any("scripts/check_attribution.py" in command for command in calls)


def test_non_candidate_full_event_has_no_range_policy_command(monkeypatch) -> None:
    monkeypatch.setenv("STAGEMESH_CI_EVENT", "schedule")
    monkeypatch.delenv("STAGEMESH_CI_BASE_SHA", raising=False)
    monkeypatch.delenv("STAGEMESH_CI_HEAD_SHA", raising=False)
    monkeypatch.setattr(
        range_script.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("schedule should not inspect a candidate range"),
    )
    assert range_script.main() == 0
