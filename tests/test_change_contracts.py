from __future__ import annotations

import json
import sys
from pathlib import Path

from stagemesh.contract_binding import contract_for_candidate
from stagemesh.contracts import ChangeContract, GateCommand, evaluate_contract, parse_contract
from stagemesh.coordinator import Coordinator
from stagemesh.domain import EvidenceKind, EvidenceStatus, ExecutionStatus, Stage, TaskStatus
from stagemesh.execution import ExecutionResult, Executor, SubprocessExecutor
from stagemesh.git import GitWorkspace
from stagemesh.integration import Integrator
from stagemesh.persistence import Store
from stagemesh.remediation import RemediationPolicy
from stagemesh.review import Reviewer
from stagemesh.validation import Validator
from stagemesh.workspaces import task_workspace


def _repo(path: Path) -> GitWorkspace:
    path.mkdir()
    workspace = GitWorkspace(path)
    workspace.init_if_needed()
    workspace.run("config", "user.email", "test@example.invalid")
    workspace.run("config", "user.name", "StageMesh Test")
    (path / "src").mkdir()
    (path / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    workspace.commit_all("initial")
    return workspace


def _store(path: Path) -> Store:
    store = Store(path / "state.sqlite3")
    store.migrate()
    return store


class SequencedExecutor(Executor):
    name = "implementer"

    def __init__(self, project: Path, values: list[str]):
        self.project = project
        self.values = values
        self.calls = 0

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        self.calls += 1
        execution_id = store.start_execution(task_id=task_id, claim_id=claim_id, kind="IMPLEMENTATION")
        value = self.values[min(self.calls - 1, len(self.values) - 1)]
        (self.project / "src" / "app.py").write_text(value, encoding="utf-8")
        sha = GitWorkspace(self.project).commit_all(f"candidate {self.calls}")
        store.add_candidate(task_id, sha, self.name, True)
        store.finish_execution(execution_id, ExecutionStatus.SUCCEEDED, sha)
        return ExecutionResult(ExecutionStatus.SUCCEEDED, sha, durable_handoff=True)


class RecordingReviewAdapter:
    def __init__(self, name: str = "reviewer", response: str = '{"decision":"PASS"}') -> None:
        self.name = name
        self.response = response
        self.calls = 0
        self.prompts: list[str] = []

    def review(self, prompt: str) -> str:
        self.calls += 1
        self.prompts.append(prompt)
        return self.response


def test_contract_rejects_forbidden_and_out_of_scope_files(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    (project / "README.md").write_text("changed\n", encoding="utf-8")
    sha = workspace.commit_all("candidate")

    contract = ChangeContract(
        objective="Only change application code",
        allowed_files=("src/**",),
        forbidden_files=("README.md",),
    )
    result = evaluate_contract(project, sha, contract, run_gates=False)

    assert result.status == "FAILED"
    assert "README.md" in result.changed_files
    assert {finding["code"] for finding in result.findings} >= {
        "outside_allowed_files",
        "forbidden_file_changed",
    }


def test_contract_runs_required_gates_and_blocks_failures(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "src" / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
    sha = workspace.commit_all("candidate")

    contract = ChangeContract(
        objective="Run deterministic tests",
        allowed_files=("src/**",),
        required_tests=(GateCommand("failing-test", [sys.executable, "-c", "import sys; sys.exit(7)"]),),
    )
    result = evaluate_contract(project, sha, contract, run_gates=True)

    assert result.status == "FAILED"
    assert result.gates[0].name == "failing-test"
    assert result.gates[0].returncode == 7
    assert any(finding["code"] == "gate_failed" for finding in result.findings)


def test_contract_gates_run_from_exact_candidate_workspace(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "src" / "app.py").write_text("VALUE = 'candidate'\n", encoding="utf-8")
    sha = workspace.commit_all("candidate")
    (project / "src" / "app.py").write_text("VALUE = 'shared checkout only'\n", encoding="utf-8")

    contract = ChangeContract(
        objective="Validate candidate contents",
        allowed_files=("src/**",),
        required_tests=(
            GateCommand(
                "candidate-workspace",
                [
                    sys.executable,
                    "-c",
                    "import pathlib, sys; sys.exit(0 if 'candidate' in pathlib.Path('src/app.py').read_text() else 9)",
                ],
            ),
        ),
    )

    result = evaluate_contract(project, sha, contract, run_gates=True)

    assert result.status == "PASSED"
    assert result.gates[0].status == "PASSED"


def test_validator_records_failed_contract_evidence_for_canary_violation(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / ".stagemesh" / "contracts").mkdir(parents=True)
    (project / ".stagemesh" / "contracts" / "TASK-1.json").write_text(
        json.dumps(
            {
                "objective": "Keep documentation untouched",
                "allowed_files": ["src/**"],
                "forbidden_files": ["README.md"],
            }
        ),
        encoding="utf-8",
    )
    (project / "README.md").write_text("canary violation\n", encoding="utf-8")
    sha = workspace.commit_all("bad candidate")

    store = _store(tmp_path)
    task_id = store.upsert_task("contract task", source_id="TASK-1")
    store.add_candidate(task_id, sha, "test", True)

    status = Validator().validate(store, task_id, sha, project)

    assert status is EvidenceStatus.FAILED
    assert store.has_evidence(task_id, sha, EvidenceKind.VALIDATION, EvidenceStatus.FAILED)
    assert not store.has_evidence(task_id, sha, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)
    store.close()


def test_reviewer_independently_creates_structured_findings(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "stagemesh.contract.json").write_text(
        json.dumps(
            {
                "objective": "Source only",
                "allowed_files": ["src/**"],
                "forbidden_files": ["README.md"],
            }
        ),
        encoding="utf-8",
    )
    (project / "README.md").write_text("review canary\n", encoding="utf-8")
    sha = workspace.commit_all("review candidate")

    store = _store(tmp_path)
    task_id = store.upsert_task("review task")
    store.add_candidate(task_id, sha, "test", True)

    status = Reviewer().review(store, task_id, sha, project)

    assert status is EvidenceStatus.FAILED
    findings = store.open_findings_for_candidate(task_id, sha)
    assert findings
    assert any("forbidden" in finding["message"] for finding in findings)
    store.close()


def test_bound_contract_survives_filesystem_mutation_after_validation(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    store = _store(tmp_path)
    task_id = store.upsert_task("mutation task")
    contract_path = project / "stagemesh.contract.json"
    contract_path.write_text(
        json.dumps(
            {
                "objective": "Source only",
                "allowed_files": ["src/**", "stagemesh.contract.json"],
                "forbidden_files": ["README.md"],
                "required_tests": [{"name": "smoke", "command": [sys.executable, "-c", "pass"]}],
            }
        ),
        encoding="utf-8",
    )
    (project / "src" / "app.py").write_text("VALUE = 7\n", encoding="utf-8")
    sha = workspace.commit_all("source candidate")

    store.add_candidate(task_id, sha, "implementer", True)

    assert Validator().validate(store, task_id, sha, project) is EvidenceStatus.PASSED
    binding = store.contract_binding(task_id, sha)
    assert binding is not None
    bound_hash = binding["digest"]

    contract_path.write_text(
        json.dumps({"objective": "Mutated later", "allowed_files": ["docs/**"], "forbidden_files": ["src/**"]}),
        encoding="utf-8",
    )

    assert Reviewer(provider_name="reviewer").review(store, task_id, sha, project) is EvidenceStatus.PASSED
    row = store.conn.execute(
        "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? AND status=?",
        (task_id, sha, EvidenceKind.REVIEW, EvidenceStatus.PASSED),
    ).fetchone()
    payload = json.loads(row["payload"])
    assert payload["contract_hash"] == bound_hash
    assert payload["objective"] == "Source only"
    assert payload["independent_reviewer"] is False
    assert payload["review_execution_invoked"] is False
    store.close()


def test_reviewer_provider_identity_must_be_distinct_from_implementer(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "src" / "app.py").write_text("VALUE = 9\n", encoding="utf-8")
    sha = workspace.commit_all("candidate")

    store = _store(tmp_path)
    task_id = store.upsert_task("review identity")
    store.add_candidate(task_id, sha, "implementer", True)

    assert Reviewer(provider_name="implementer").review(store, task_id, sha, project) is EvidenceStatus.FAILED
    assert store.open_findings_for_candidate(task_id, sha)

    assert Reviewer(provider_name="reviewer").review(store, task_id, sha, project) is EvidenceStatus.PASSED
    row = store.conn.execute(
        "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? AND status=?",
        (task_id, sha, EvidenceKind.REVIEW, EvidenceStatus.PASSED),
    ).fetchone()
    payload = json.loads(row["payload"])
    assert payload["review_provider"] == "reviewer"
    assert payload["implementer_provider"] == "implementer"
    assert payload["independent_reviewer"] is False
    assert payload["review_execution_invoked"] is False
    assert payload["deterministic_contract_gate"] is True
    store.close()


def test_distinct_reviewer_adapter_executes_independent_review(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "src" / "app.py").write_text("VALUE = 10\n", encoding="utf-8")
    sha = workspace.commit_all("candidate")

    store = _store(tmp_path)
    task_id = store.upsert_task("review execution")
    store.add_candidate(task_id, sha, "implementer", True)
    adapter = RecordingReviewAdapter(name="reviewer")

    assert Reviewer(adapter=adapter).review(store, task_id, sha, project) is EvidenceStatus.PASSED
    row = store.conn.execute(
        "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? AND status=?",
        (task_id, sha, EvidenceKind.REVIEW, EvidenceStatus.PASSED),
    ).fetchone()
    payload = json.loads(row["payload"])
    assert adapter.calls == 1
    assert payload["review_provider"] == "reviewer"
    assert payload["review_execution_provider"] == "reviewer"
    assert payload["independent_reviewer"] is True
    assert payload["review_execution_invoked"] is True
    assert payload["deterministic_contract_gate"] is True
    store.close()


def test_reviewer_adapter_matching_implementer_is_rejected(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "src" / "app.py").write_text("VALUE = 11\n", encoding="utf-8")
    sha = workspace.commit_all("candidate")

    store = _store(tmp_path)
    task_id = store.upsert_task("review same provider")
    store.add_candidate(task_id, sha, "implementer", True)
    adapter = RecordingReviewAdapter(name="implementer")

    assert Reviewer(adapter=adapter).review(store, task_id, sha, project) is EvidenceStatus.FAILED
    assert adapter.calls == 0
    assert store.open_findings_for_candidate(task_id, sha)
    store.close()


def test_malformed_independent_review_output_fails_closed(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "src" / "app.py").write_text("VALUE = 12\n", encoding="utf-8")
    sha = workspace.commit_all("candidate")

    store = _store(tmp_path)
    task_id = store.upsert_task("malformed review")
    store.add_candidate(task_id, sha, "implementer", True)
    adapter = RecordingReviewAdapter(name="reviewer", response="PASS")

    assert Reviewer(adapter=adapter).review(store, task_id, sha, project) is EvidenceStatus.FAILED
    findings = store.open_findings_for_candidate(task_id, sha)
    assert any("JSON with decision PASS or FAIL" in row["message"] for row in findings)
    store.close()


def test_command_review_adapter_rejects_workspace_mutation(tmp_path: Path) -> None:
    from stagemesh.providers import RuntimeCommandAdapter

    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "src" / "app.py").write_text("VALUE = 13\n", encoding="utf-8")
    sha = workspace.commit_all("candidate")
    script = tmp_path / "mutating_review.py"
    script.write_text(
        "from pathlib import Path\n"
        "Path('src/app.py').write_text('VALUE = 99\\n', encoding='utf-8')\n"
        "print('{\"decision\":\"PASS\"}')\n",
        encoding="utf-8",
    )

    store = _store(tmp_path)
    task_id = store.upsert_task("mutating review")
    store.add_candidate(task_id, sha, "implementer", True)
    adapter = RuntimeCommandAdapter(name="reviewer", command=(sys.executable, str(script)))

    assert Reviewer(adapter=adapter).review(store, task_id, sha, project) is EvidenceStatus.FAILED
    payload = json.loads(
        store.conn.execute(
            "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? AND status=?",
            (task_id, sha, EvidenceKind.REVIEW, EvidenceStatus.FAILED),
        ).fetchone()["payload"]
    )
    assert "mutated candidate workspace" in payload["review_response"]
    assert workspace.head() == sha
    assert not (project / "src" / "app.py").read_text(encoding="utf-8").endswith("99\n")
    store.close()


def test_contract_parser_accepts_named_command_objects() -> None:
    contract = parse_contract(
        {
            "objective": "Ship safely",
            "acceptance_criteria": ["tests pass"],
            "allowed_files": ["src/**"],
            "required_tests": [{"name": "unit", "command": [sys.executable, "--version"], "timeout_seconds": 5}],
        }
    )

    assert contract.objective == "Ship safely"
    assert contract.acceptance_criteria == ("tests pass",)
    assert contract.required_tests[0].name == "unit"


def test_dependency_manifest_change_requires_dependency_gate(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "pyproject.toml").write_text("[project]\nname = 'changed'\n", encoding="utf-8")
    sha = workspace.commit_all("change manifest")

    result = evaluate_contract(
        project,
        sha,
        ChangeContract(objective="Do not smuggle dependency changes", allowed_files=("**",)),
        run_gates=False,
    )

    assert result.status == "FAILED"
    assert any(finding["code"] == "dependency_manifest_changed_without_gate" for finding in result.findings)


def test_dependency_manifest_detection_is_case_insensitive(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "PyProject.TOML").write_text("[project]\nname = 'changed'\n", encoding="utf-8")
    sha = workspace.commit_all("case variant manifest")

    result = evaluate_contract(
        project,
        sha,
        ChangeContract(objective="Catch Windows manifest variants", allowed_files=("**",)),
        run_gates=False,
    )

    assert result.status == "FAILED"
    assert any(finding["code"] == "dependency_manifest_changed_without_gate" for finding in result.findings)


def test_rename_source_and_target_are_checked_against_scope(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "secrets.txt").write_text("secret\n", encoding="utf-8")
    workspace.commit_all("add protected file")
    workspace.run("mv", "secrets.txt", "src/secrets.py")
    sha = workspace.commit_all("rename protected file into scope")

    result = evaluate_contract(
        project,
        sha,
        ChangeContract(
            objective="Do not touch secrets",
            allowed_files=("src/**",),
            forbidden_files=("secrets.txt",),
        ),
        run_gates=False,
    )

    assert "secrets.txt" in result.changed_files
    assert "src/secrets.py" in result.changed_files
    assert result.status == "FAILED"
    assert any(finding["code"] == "forbidden_file_changed" for finding in result.findings)


def test_change_size_limits_reject_unrelated_refactor(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "src" / "app.py").write_text("VALUE = 2\nOTHER = 3\n", encoding="utf-8")
    (project / "src" / "extra.py").write_text("EXTRA = 1\n", encoding="utf-8")
    sha = workspace.commit_all("too broad")

    result = evaluate_contract(
        project,
        sha,
        ChangeContract(
            objective="One small edit",
            allowed_files=("src/**",),
            max_changed_files=1,
            max_diff_lines=1,
        ),
        run_gates=False,
    )

    codes = {finding["code"] for finding in result.findings}
    assert {"change_size_files_exceeded", "change_size_lines_exceeded"} <= codes


def test_integration_requires_validation_and_review_for_exact_sha(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "src" / "app.py").write_text("VALUE = 42\n", encoding="utf-8")
    sha = workspace.commit_all("candidate")

    store = _store(tmp_path)
    task_id = store.upsert_task("integration task")
    store.add_candidate(task_id, sha, "test", True)

    assert Integrator().integrate(store, task_id, sha, project) is EvidenceStatus.FAILED
    assert not store.has_evidence(task_id, sha, EvidenceKind.INTEGRATION, EvidenceStatus.PASSED)

    store.add_evidence(task_id, sha, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)
    store.add_evidence(task_id, sha, EvidenceKind.REVIEW, EvidenceStatus.PASSED)
    assert Integrator().integrate(store, task_id, sha, project) is EvidenceStatus.FAILED

    assert Validator().validate(store, task_id, sha, project) is EvidenceStatus.PASSED
    assert Reviewer(adapter=RecordingReviewAdapter()).review(store, task_id, sha, project) is EvidenceStatus.PASSED
    assert Integrator().integrate(store, task_id, sha, project) is EvidenceStatus.PASSED
    store.close()


def test_integration_rejects_contract_hash_mismatch(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "src" / "app.py").write_text("VALUE = 43\n", encoding="utf-8")
    sha = workspace.commit_all("candidate")

    store = _store(tmp_path)
    task_id = store.upsert_task("integration mismatch")
    store.add_candidate(task_id, sha, "test", True)
    bound = contract_for_candidate(store, task_id, sha, project)
    mismatched = {**bound.evidence_payload(), "contract_hash": "0" * 64}
    store.add_evidence(task_id, sha, EvidenceKind.VALIDATION, EvidenceStatus.PASSED, mismatched)
    store.add_evidence(task_id, sha, EvidenceKind.REVIEW, EvidenceStatus.PASSED, bound.evidence_payload())

    assert Integrator().integrate(store, task_id, sha, project) is EvidenceStatus.FAILED
    store.close()


def test_provider_prompt_includes_change_contract(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    store = _store(tmp_path)
    task_id = store.upsert_task("prompt task")
    (project / ".stagemesh" / "contracts").mkdir(parents=True)
    (project / ".stagemesh" / "contracts" / f"{task_id}.json").write_text(
        json.dumps(
            {
                "objective": "Only touch source",
                "allowed_files": ["src/**"],
                "forbidden_files": ["README.md"],
            }
        ),
        encoding="utf-8",
    )
    store.advance_task(task_id, "IMPLEMENT")
    script = (
        "import pathlib, sys\n"
        "pathlib.Path('captured_prompt.txt').write_text(sys.stdin.read(), encoding='utf-8')\n"
    )

    Coordinator(store, project, executor=SubprocessExecutor([sys.executable, "-c", script], name="prompt-capture")).tick()

    prompt = (task_workspace(project, task_id) / "captured_prompt.txt").read_text(encoding="utf-8")
    assert "Change contract:" in prompt
    assert "Only touch source" in prompt
    assert "Forbidden files: README.md" in prompt
    assert not (project / "captured_prompt.txt").exists()
    store.close()


def test_implementation_runs_in_isolated_worktree_not_shared_project(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    store = _store(tmp_path)
    task_id = store.upsert_task("isolated task")
    coord = Coordinator(store, project, remediation_policy=RemediationPolicy(max_attempts=1))
    assert coord.tick() == 1
    assert coord.tick() == 1

    assert not (project / f"stagemesh-task-{task_id}.txt").exists()
    assert (task_workspace(project, task_id) / f"stagemesh-task-{task_id}.txt").exists()
    assert store.latest_candidate(task_id) is not None
    store.close()


def test_remediation_produces_new_candidate_and_does_not_reuse_stale_evidence(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "stagemesh.contract.json").write_text(
        json.dumps(
            {
                "objective": "Source value must be remediated",
                "allowed_files": ["src/**"],
                "required_tests": [
                    {
                        "name": "value-remediated",
                        "command": [
                            sys.executable,
                            "-c",
                            "from pathlib import Path; assert 'VALUE = 11' in Path('src/app.py').read_text()",
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    store = _store(tmp_path)
    task_id = store.upsert_task("remediate task")
    coord = Coordinator(
        store,
        project,
        executor=SequencedExecutor(project, ["VALUE = 10\n", "VALUE = 11\n"]),
        reviewer=Reviewer(adapter=RecordingReviewAdapter()),
    )

    assert coord.tick() == 1  # PLAN -> IMPLEMENT
    assert coord.tick() == 1  # candidate 1
    first_sha = store.latest_candidate(task_id)["sha"]
    assert coord.tick() == 1
    assert store.get_task(task_id)["stage"] == Stage.IMPLEMENT
    assert coord.tick() == 1
    second_sha = store.latest_candidate(task_id)["sha"]

    assert second_sha != first_sha
    assert store.has_evidence(task_id, first_sha, EvidenceKind.VALIDATION, EvidenceStatus.FAILED)
    assert store.open_findings_for_candidate(task_id, first_sha)
    assert not store.has_evidence(task_id, second_sha, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)
    assert coord.tick() == 1
    assert store.has_evidence(task_id, second_sha, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)
    store.close()


def test_remediation_exhaustion_blocks_task(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    store = _store(tmp_path)
    task_id = store.upsert_task("exhaust task")
    (project / "stagemesh.contract.json").write_text(
        json.dumps({"objective": "Source only", "allowed_files": ["src/**"], "forbidden_files": ["README.md"]}),
        encoding="utf-8",
    )
    (project / "README.md").write_text("break contract\n", encoding="utf-8")
    sha = workspace.commit_all("failing candidate")
    store.add_candidate(task_id, sha, "implementer", True)
    store.advance_task(task_id, Stage.VALIDATE)
    assert Validator().validate(store, task_id, sha, project) is EvidenceStatus.FAILED
    store.add_task_remediation(task_id, "VALIDATE", sha)  # the one allowed remediation is already spent

    coord = Coordinator(store, project, remediation_policy=RemediationPolicy(max_attempts=1))
    assert coord.tick() == 1

    task = store.get_task(task_id)
    assert task["stage"] == Stage.VALIDATE
    assert task["status"] == TaskStatus.BLOCKED
    store.close()
