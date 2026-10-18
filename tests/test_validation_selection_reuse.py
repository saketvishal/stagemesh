"""Scenario 3 and the evidence-reuse rules: authoritative baseline, no silent broadening, complete-input equivalence."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from stagemesh.contracts import GateCommand
from stagemesh.domain import EvidenceKind, EvidenceStatus
from stagemesh.git import GitWorkspace
from stagemesh.validation import Validator
from stagemesh.validation_reuse import declared_gate_bases, select_files

from test_bounded_execution import TASK, _setup

PY = sys.executable


def _contract(counter: Path, extra_gate: bool = False) -> dict:
    # Appends a character per execution so the number of real runs is observable.
    script = f"import pathlib; p = pathlib.Path({str(counter)!r}); p.write_text(p.read_text() + 'x' if p.exists() else 'x')"
    gates = [{"name": "unit", "command": [PY, "-c", script]}]
    if extra_gate:
        gates.append({"name": "unit-again", "command": [PY, "-c", script]})  # same command under another name
    return {"objective": "docs only", "allowed_files": ["docs/**"], "required_tests": gates}


def _candidate_after_unrelated_ui_work(project: Path, store) -> tuple[str, str, str]:
    """baseline B; other work lands a UI file on the integration branch; the candidate (docs only) is built on that tip."""
    git = GitWorkspace(project)
    _ignore_runtime_files(project)
    baseline = git.head()
    store.set_task_baseline(TASK, baseline)
    (project / "web").mkdir()
    (project / "web" / "app.tsx").write_text("export const App = () => null\n", encoding="utf-8")
    (project / "package.json").write_text(json.dumps({"scripts": {"test:e2e": "playwright test"}}), encoding="utf-8")
    tip = git.commit_all("unrelated ui work landed by another task")
    git.run("checkout", "-q", "-b", "candidate")
    (project / "docs" / "a.md").write_text("changed by this task\n", encoding="utf-8")
    sha = git.commit_all("candidate")
    git.run("checkout", "-q", "-")
    store.add_candidate(TASK, sha, "p", durable_handoff=True)
    return baseline, tip, sha


def _ignore_runtime_files(project: Path) -> None:
    exclude = project / ".git" / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_text(".stagemesh/\n", encoding="utf-8")


def _single_candidate(project: Path, store) -> str:
    git = GitWorkspace(project)
    _ignore_runtime_files(project)
    store.set_task_baseline(TASK, git.head())
    (project / "docs" / "a.md").write_text("changed\n", encoding="utf-8")
    sha = git.commit_all("candidate")
    store.add_candidate(TASK, sha, "p", durable_handoff=True)
    return sha


def test_unrelated_historical_ui_work_does_not_broaden_automatic_selection(tmp_path: Path) -> None:
    counter = tmp_path / "runs.txt"
    project, store = _setup(tmp_path, _contract(counter))
    baseline, tip, sha = _candidate_after_unrelated_ui_work(project, store)

    status = Validator().validate(store, TASK, sha, project)

    payload = json.loads(store.conn.execute("SELECT payload FROM evidence WHERE kind='VALIDATION'").fetchone()[0])
    selection = payload["selection"]
    assert payload["baseline_sha"] == baseline == selection["baseline_sha"]  # the bound baseline stays authoritative
    assert selection["baseline_source"] == "bound-task-baseline"
    assert set(selection["excluded_from_selection"]) == {"web/app.tsx", "package.json"}
    assert selection["selection_files"] == ["docs/a.md"]
    assert selection["proposed_baseline_sha"] == tip and "kept out of automatic validation selection" in selection["reason"]
    assert [gate["name"] for gate in payload["gates"]] == ["unit"]  # no browser end-to-end gate was pulled in
    assert payload["validation_plan"]["classification"] != "CORE_LIFECYCLE_OR_SCHEMA_SECURITY"
    # the scope check still sees the full diff against the bound baseline
    assert {item["path"] for item in payload["findings"] if item["code"] == "outside_allowed_files"} == {"web/app.tsx", "package.json"}
    assert status is EvidenceStatus.FAILED


def test_nothing_is_excluded_without_proof_the_baseline_is_stale() -> None:
    inspected = ("docs/a.md", "web/app.tsx")
    report = select_files(Path("."), "0" * 40, None, inspected, None)
    assert report.selection_files == inspected and not report.excluded_files and report.baseline_source == "candidate-parent"
    unresolved = select_files(Path("."), "0" * 40, "1" * 40, inspected, "refs/heads/main")
    assert unresolved.selection_files == inspected and "could not be established" in unresolved.reason


def test_a_frozen_gate_with_a_broader_base_is_reported_and_preserved() -> None:
    gates = (
        GateCommand("diff-check", ["node", "tools/check.js", "--base", "origin/main"]),
        GateCommand("same", ["node", "tools/check.js", "--base=" + "a" * 40]),
        GateCommand("plain", ["pytest", "-q"]),
    )
    found = declared_gate_bases(gates, "b" * 40)
    assert {(item["gate"], item["declared_base"]) for item in found} == {("diff-check", "origin/main"), ("same", "a" * 40)}
    assert all(item["preserved"] and item["bound_baseline"] == "b" * 40 for item in found)
    assert {item["gate"] for item in declared_gate_bases(gates, "a" * 40)} == {"diff-check"}


def test_duplicate_commands_in_one_attempt_run_once(tmp_path: Path) -> None:
    counter = tmp_path / "runs.txt"
    project, store = _setup(tmp_path, _contract(counter, extra_gate=True))
    sha = _single_candidate(project, store)

    assert Validator().validate(store, TASK, sha, project) is EvidenceStatus.PASSED

    assert counter.read_text() == "x"  # one real execution
    payload = json.loads(store.conn.execute("SELECT payload FROM evidence WHERE kind='VALIDATION'").fetchone()[0])
    assert payload["validation_reuse"]["ran"] == ["unit"]
    assert payload["validation_reuse"]["reused"] == [{"gate": "unit-again", "from": "attempt:unit"}]


def test_prior_pass_is_reused_only_for_identical_complete_inputs(tmp_path: Path) -> None:
    counter = tmp_path / "runs.txt"
    project, store = _setup(tmp_path, _contract(counter))
    first = _single_candidate(project, store)
    git = GitWorkspace(project)
    assert Validator().validate(store, TASK, first, project) is EvidenceStatus.PASSED
    assert counter.read_text() == "x"

    # Same tree under a new commit (message-only change): identical inputs, so the passed gate is reused and the reuse is reported.
    git.run("commit", "-q", "--amend", "-m", "reworded candidate")
    second = git.head()
    assert second != first
    store.add_candidate(TASK, second, "p", durable_handoff=True)
    assert Validator().validate(store, TASK, second, project) is EvidenceStatus.PASSED
    assert counter.read_text() == "x"
    rows = [json.loads(r[0]) for r in store.conn.execute("SELECT payload FROM evidence WHERE kind='VALIDATION' ORDER BY created_at, rowid")]
    reused = rows[-1]["validation_reuse"]["reused"]
    assert reused and reused[0]["gate"] == "unit" and reused[0]["from"].startswith("evidence:")
    assert rows[-1]["validation_reuse"]["ran"] == []

    # Changed content invalidates it.
    (project / "docs" / "a.md").write_text("changed again\n", encoding="utf-8")
    third = git.commit_all("content changed")
    store.add_candidate(TASK, third, "p", durable_handoff=True)
    assert Validator().validate(store, TASK, third, project) is EvidenceStatus.PASSED
    assert counter.read_text() == "xx"


def test_changed_contract_environment_or_command_invalidates_reuse() -> None:
    from stagemesh.validation_reuse import gate_input_fingerprint

    gate = GateCommand("unit", [PY, "-c", "pass"], env=(("DB", "a"),))
    base = gate_input_fingerprint("t" * 40, "c" * 64, gate)
    assert base == gate_input_fingerprint("t" * 40, "c" * 64, GateCommand("renamed", [PY, "-c", "pass"], env=(("DB", "a"),)))
    assert base != gate_input_fingerprint("u" * 40, "c" * 64, gate)  # content / dependencies
    assert base != gate_input_fingerprint("t" * 40, "d" * 64, gate)  # contract
    assert base != gate_input_fingerprint("t" * 40, "c" * 64, GateCommand("unit", [PY, "-c", "pass"], env=(("DB", "b"),)))  # environment
    assert base != gate_input_fingerprint("t" * 40, "c" * 64, GateCommand("unit", [PY, "-c", "pass", "-x"], env=(("DB", "a"),)))  # command
    assert base != gate_input_fingerprint("t" * 40, "c" * 64, GateCommand("unit", [PY, "-c", "pass"], timeout_seconds=9, env=(("DB", "a"),)))


def test_failed_results_are_never_reused(tmp_path: Path) -> None:
    from stagemesh.validation_reuse import load_prior_passes

    project, store = _setup(tmp_path, _contract(tmp_path / "runs.txt"))
    store.add_candidate(TASK, "a" * 40, "p", durable_handoff=True)
    store.add_evidence(TASK, "a" * 40, EvidenceKind.VALIDATION, EvidenceStatus.FAILED, {
        "gates": [{"name": "unit", "status": "FAILED", "input_fingerprint": "f1"}, {"name": "lint", "status": "PASSED", "input_fingerprint": "f2"}],
    })
    assert set(load_prior_passes(store, TASK)) == {"f2"}


def test_reuse_is_all_or_nothing_so_gates_that_depend_on_each_others_side_effects_still_run_in_order(tmp_path: Path) -> None:
    flag = tmp_path / "fixed.flag"
    log = tmp_path / "order.txt"
    build = f"import pathlib; p = pathlib.Path({str(log)!r}); p.write_text((p.read_text() if p.exists() else '') + 'build;')"
    test = (
        f"import pathlib, sys; p = pathlib.Path({str(log)!r}); p.write_text((p.read_text() if p.exists() else '') + 'test;'); "
        f"sys.exit(0 if pathlib.Path({str(flag)!r}).exists() else 1)"
    )
    contract = {"objective": "docs only", "allowed_files": ["docs/**"], "required_tests": [
        {"name": "build", "command": [PY, "-c", build]}, {"name": "test", "command": [PY, "-c", test]},
    ]}
    project, store = _setup(tmp_path, contract)
    sha = _single_candidate(project, store)
    assert Validator().validate(store, TASK, sha, project) is EvidenceStatus.FAILED  # build passed, test failed
    assert log.read_text() == "build;test;"

    flag.write_text("ok", encoding="utf-8")  # the environment problem is fixed; the tree is unchanged
    git = GitWorkspace(project)
    git.run("commit", "-q", "--amend", "-m", "same tree, new commit")
    second = git.head()
    store.add_candidate(TASK, second, "p", durable_handoff=True)
    assert Validator().validate(store, TASK, second, project) is EvidenceStatus.PASSED

    assert log.read_text() == "build;test;build;test;"  # `build` was not skipped just because it passed before
    payload = [json.loads(r[0]) for r in store.conn.execute("SELECT payload FROM evidence WHERE kind='VALIDATION' ORDER BY created_at, rowid")][-1]
    assert payload["validation_reuse"]["reused"] == [] and payload["validation_reuse"]["ran"] == ["build", "test"]
