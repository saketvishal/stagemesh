"""`stagemesh report latest`: the structured run report derived from the store and git."""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest
from test_bounded_execution import TASK, _setup

import stagemesh.cli as cli_module
from stagemesh.contract_binding import bind_task_contract
from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage
from stagemesh.git import GitWorkspace
from stagemesh.run_report import _branch, build_run_report, format_run_report, latest_task_id
from stagemesh.validation import Validator

PY = sys.executable
OK_GATE = {"name": "docs-static", "command": [PY, "-c", "pass"]}
CONTRACT = {"objective": "docs only", "allowed_files": ["docs/**"], "required_tests": [OK_GATE]}


def cli(project: Path, *argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_module.main(["--project", str(project), *argv])
    return code, out.getvalue(), err.getvalue()


def validated_task(tmp_path: Path, contract: dict | None = None, path: str = "docs/a.md"):
    project, store = _setup(tmp_path, contract or CONTRACT)
    exclude = project / ".git" / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_text(".stagemesh/\n", encoding="utf-8")
    base = store.set_task_baseline(TASK, GitWorkspace(project).head())
    bind_task_contract(store, project, TASK, base)
    (project / path).write_text("candidate\n", encoding="utf-8")
    sha = GitWorkspace(project).commit_all("candidate")
    store.add_candidate(TASK, sha, "provider", durable_handoff=True)
    store.advance_task(TASK, Stage.VALIDATE)
    Validator().validate(store, TASK, sha, project)
    return project, store, sha


def test_report_has_every_requested_field_for_a_passing_candidate(tmp_path: Path) -> None:
    project, store, sha = validated_task(tmp_path)

    report = build_run_report(store, project)

    assert report["task_id"] == TASK and report["commit_sha"] == sha
    assert report["branch"] in {"main", "master"}
    assert report["files_changed"] == ["docs/a.md"]
    assert report["tests_run"] == [{"name": "docs-static", "command": [PY, "-c", "pass"], "status": "PASSED", "returncode": 0}]
    assert report["pass_fail_counts"] == {"unit": "validation gates", "total": 1, "passed": 1, "failed": 0}
    assert report["evidence"]["latest_validation"] == "PASSED"
    assert report["verdict"] == "IN_PROGRESS"  # validated, not yet reviewed/integrated
    assert report["review_findings"] == [] and report["known_baseline_failures"] == [] and report["pr_url"] is None
    assert report["next_recommended_action"]["command"] == "stagemesh continue"


def test_failed_validation_reports_failing_gate_findings_and_verdict(tmp_path: Path) -> None:
    # only planned checks execute; a docs-only change plans "docs-static"
    contract = {**CONTRACT, "required_tests": [{"name": "docs-static", "command": [PY, "-c", "raise SystemExit(1)"]}]}
    project, store, _ = validated_task(tmp_path, contract)

    report = build_run_report(store, project)

    assert report["verdict"] == "FAILED"
    assert report["pass_fail_counts"] == {"unit": "validation gates", "total": 1, "passed": 0, "failed": 1}
    assert report["review_findings"] and all(f["status"] == "OPEN" for f in report["review_findings"])


def test_scope_violation_is_listed_in_files_changed(tmp_path: Path) -> None:
    project, store, _ = validated_task(tmp_path, path="src/app.py")

    report = build_run_report(store, project)

    assert report["files_changed"] == ["src/app.py"] and report["verdict"] == "FAILED"


def test_known_failures_policy_gate_is_listed_as_a_baseline_failure_signal(tmp_path: Path) -> None:
    project, store, sha = validated_task(tmp_path)
    gates = [{"name": "unit", "status": "PASSED"}, {"name": "backend-engineering-known-failures", "status": "PASSED"}]
    store.add_evidence(TASK, sha, EvidenceKind.VALIDATION, EvidenceStatus.PASSED, {"gates": gates})

    report = build_run_report(store, project)

    assert report["known_baseline_failures"] == [{"gate": "backend-engineering-known-failures", "status": "PASSED"}]
    assert report["pass_fail_counts"]["total"] == 2


def test_pr_url_comes_from_external_evidence_for_the_candidate_only(tmp_path: Path) -> None:
    project, store, sha = validated_task(tmp_path)
    store.add_external_evidence("pull-request", "PASS", "https://github.com/o/r/pull/7", "0" * 40)
    assert build_run_report(store, project)["pr_url"] is None
    store.add_external_evidence("pull-request", "PASS", "https://github.com/o/r/pull/9", sha)
    assert build_run_report(store, project)["pr_url"] == "https://github.com/o/r/pull/9"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/o/r/pull/12/",
        "https://github.com/o/r/pull/12?w=1",
        "https://github.com/o/r/pull/12/files",
        "https://github.com/o/r/pull/12/files?w=1#diff-abc",
        "https://github.com/o/r/pull/12.patch",
        "https://github.com/o/r/pull/12.diff",
    ],
)
def test_suffixed_pr_url_in_external_evidence_is_reported_canonically(tmp_path: Path, url: str) -> None:
    project, store, sha = validated_task(tmp_path)
    store.add_external_evidence("pull-request", "PASS", url, sha)
    assert build_run_report(store, project)["pr_url"] == "https://github.com/o/r/pull/12"


@pytest.mark.parametrize("url", ["https://github.com/o/r/issues/12", "https://github.com/o/r/pull/12abc", "https://github.com/o/r/pull/"])
def test_non_pr_external_evidence_url_is_ignored(tmp_path: Path, url: str) -> None:
    project, store, sha = validated_task(tmp_path)
    store.add_external_evidence("pull-request", "PASS", url, sha)
    assert build_run_report(store, project)["pr_url"] is None


@pytest.mark.parametrize(
    "text, expected",
    [
        ("opened (https://github.com/o/r/pull/31).", "https://github.com/o/r/pull/31"),
        ("see https://github.com/o/r/pull/31, thanks", "https://github.com/o/r/pull/31"),
        ("https://github.com/o/r/pull/31/files and more", "https://github.com/o/r/pull/31"),
        ("https://github.com/o/r/pull/31.", "https://github.com/o/r/pull/31"),
        ("https://github.com/o/r/pull/31?w=1 trailing", "https://github.com/o/r/pull/31"),
    ],
)
def test_pr_url_in_source_event_payload_is_clean_and_canonical(tmp_path: Path, text: str, expected: str) -> None:
    project, store, _ = validated_task(tmp_path)
    store.add_source_event("local", TASK, "outbound", "OK", {"message": text})
    assert build_run_report(store, project)["pr_url"] == expected


def commit_on(project: Path, branch: str, name: str) -> str:
    git = GitWorkspace(project)
    git.run("checkout", "-q", "-b", branch)
    (project / name).write_text(name + "\n", encoding="utf-8")
    return git.commit_all(f"commit on {branch}")


def current(project: Path) -> str:
    return GitWorkspace(project).run("symbolic-ref", "--short", "HEAD").stdout.strip()


def test_branch_is_the_side_branch_not_the_unrelated_checkout(tmp_path: Path) -> None:
    project, store, _ = validated_task(tmp_path)
    main = current(project)
    side_sha = commit_on(project, "side", "docs/side.md")
    store.add_candidate(TASK, side_sha, "provider", durable_handoff=True)
    GitWorkspace(project).run("checkout", "-q", main)

    report = build_run_report(store, project)

    assert report["commit_sha"] == side_sha and current(project) == main
    assert report["branch"] == "side"


def test_branch_is_none_when_no_local_branch_contains_the_commit(tmp_path: Path) -> None:
    project, store, _ = validated_task(tmp_path)
    main = current(project)
    side_sha = commit_on(project, "side", "docs/side.md")
    GitWorkspace(project).run("checkout", "-q", main)
    GitWorkspace(project).run("branch", "-D", "side")  # the commit object survives, on no branch
    store.add_candidate(TASK, side_sha, "provider", durable_handoff=True)

    assert build_run_report(store, project)["branch"] is None


def test_branch_is_none_for_a_missing_commit_object(tmp_path: Path) -> None:
    project, _, _ = validated_task(tmp_path)
    assert _branch(project, "f" * 40) is None
    assert _branch(project, None) is None


def test_current_branch_wins_over_other_containing_branches(tmp_path: Path) -> None:
    project, _, sha = validated_task(tmp_path)
    GitWorkspace(project).run("branch", "aaa-also-contains", sha)  # sorts before the current branch
    assert _branch(project, sha) == current(project)


def test_detached_head_falls_back_to_a_containing_local_branch(tmp_path: Path) -> None:
    project, _, sha = validated_task(tmp_path)
    main = current(project)
    GitWorkspace(project).run("checkout", "-q", "--detach", sha)
    assert _branch(project, sha) == main


def test_latest_task_is_the_most_recently_active_one(tmp_path: Path) -> None:
    project, store, _ = validated_task(tmp_path)
    other = store.upsert_task("newer", source_id="TASK-2")
    store.conn.execute("UPDATE tasks SET updated_at = updated_at + 1000 WHERE id=?", (other,))
    store.conn.commit()
    assert latest_task_id(store) == other
    assert build_run_report(store, project, TASK)["task_id"] == TASK


def test_cli_report_latest_writes_json_and_text_artifacts(tmp_path: Path) -> None:
    project, store, sha = validated_task(tmp_path)
    store.close()

    code, out, _ = cli(project, "report", "latest", "--json", "--output", ".stagemesh/reports/run.json")
    assert code == 0
    assert json.loads(out)["commit_sha"] == sha
    assert json.loads((project / ".stagemesh" / "reports" / "run.json").read_text(encoding="utf-8"))["task_id"] == TASK

    code, out, _ = cli(project, "report", "latest", "--output", ".stagemesh/reports/run.md")
    assert code == 0 and "run report:" in out
    text = (project / ".stagemesh" / "reports" / "run.md").read_text(encoding="utf-8")
    assert f"commit SHA: {sha}" in text and "## Known baseline failures" in text and "docs/a.md" in text


def test_cli_report_latest_refuses_when_there_are_no_runs(tmp_path: Path) -> None:
    project = tmp_path / "empty"
    project.mkdir()
    code, _, err = cli(project, "report", "latest")
    assert code == 2 and "no_runs" in err

    code, out, _ = cli(project, "report", "latest", "--json")
    assert code == 2 and json.loads(out)["code"] == "no_runs"


def test_cli_report_latest_unknown_task_is_refused(tmp_path: Path) -> None:
    project, store, _ = validated_task(tmp_path)
    store.close()
    code, _, err = cli(project, "report", "latest", "--task", "nope")
    assert code == 2 and "unknown_task" in err


def test_plain_report_still_renders_the_final_report_and_rejects_task(tmp_path: Path) -> None:
    project, store, _ = validated_task(tmp_path)
    store.close()
    code, out, _ = cli(project, "report")
    assert code == 0 and "StageMesh vNext Final Report" in out
    code, _, err = cli(project, "report", "--task", TASK)
    assert code == 2 and "--task applies only" in err


def test_text_format_lists_none_for_empty_sections(tmp_path: Path) -> None:
    project, store, _ = validated_task(tmp_path)
    text = format_run_report(build_run_report(store, project))
    assert "- none recorded" in text and "PR URL: none" in text
