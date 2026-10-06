"""Execution-owned workspaces and external-mutation detection.

Every scenario uses real git worktrees and real subprocess "agents". An "external" actor is the test itself (or a subprocess it starts)
changing a task worktree that StageMesh sealed, without holding the workspace lease.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from test_bounded_execution import TASK
from test_bounded_execution import _setup as _base_setup

from stagemesh.contracts import evaluate_contract, parse_contract
from stagemesh.coordinator import Coordinator, TargetSelection, TargetSelectionError
from stagemesh.domain import (
    EvidenceKind,
    EvidenceStatus,
    ExecutionKind,
    ExecutionStatus,
    Stage,
    TaskStatus,
)
from stagemesh.execution import EXTERNAL_WORKSPACE_MUTATION, FakeExecutor, SubprocessExecutor
from stagemesh.git import GitWorkspace
from stagemesh.integration import Integrator
from stagemesh.persistence import Store
from stagemesh.process_identity import popen_identity
from stagemesh.providers import RuntimeCommandAdapter
from stagemesh.review import Reviewer
from stagemesh.validation import Validator
from stagemesh.workspace_guard import (
    LEASE_RECOVERED,
    LEDGER_FILE,
    OWNER_FILE,
    WorkspaceMutation,
    acquire_workspace,
    observe,
    pin_candidate,
    verify_candidate_workspace,
)
from stagemesh.workspaces import prepare_task_workspace, task_workspace

PY = sys.executable
# a docs-only change plans a "docs-static" check, which must exist as a gate for validation to be able to pass
CONTRACT = {
    "objective": "docs only",
    "allowed_files": ["docs/**"],
    "required_tests": [{"name": "docs-static", "command": [sys.executable, "-c", "pass"]}],
}


def _setup(tmp_path: Path):
    project, store = _base_setup(tmp_path, CONTRACT)
    exclude = project / ".git" / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_text(".stagemesh/\n", encoding="utf-8")  # runtime state must never land in candidate commits
    return project, store

WRITE_DOC = "import pathlib\npathlib.Path('docs/a.md').write_text('agent change\\n', encoding='utf-8')\n"
COMMIT_DOC = (
    "import pathlib, subprocess\n"
    "pathlib.Path('docs/a.md').write_text('agent committed\\n', encoding='utf-8')\n"
    "subprocess.run(['git', 'add', '-A'], check=True)\n"
    "subprocess.run(['git', 'commit', '-q', '-m', 'agent commit'], check=True)\n"
)


def provider(tmp_path: Path, body: str, name: str = "codex") -> SubprocessExecutor:
    script = tmp_path / f"provider-{abs(hash(body))}.py"
    script.write_text(body, encoding="utf-8")
    return SubprocessExecutor([PY, str(script)], name=name)


def events(store: Store, event_type: str) -> list[dict]:
    rows = store.conn.execute("SELECT payload FROM audit_events WHERE event_type=? ORDER BY created_at, rowid", (event_type,)).fetchall()
    return [json.loads(r[0]) for r in rows]


def worktree(project: Path, task_id: str = TASK) -> Path:
    return task_workspace(project, task_id)


def ledger_of(project: Path, task_id: str = TASK) -> dict:
    gitdir = Path(GitWorkspace(worktree(project, task_id)).run("rev-parse", "--absolute-git-dir").stdout.strip())
    return json.loads((gitdir / LEDGER_FILE).read_text(encoding="utf-8"))


def gitdir_of(project: Path, task_id: str = TASK) -> Path:
    return Path(GitWorkspace(worktree(project, task_id)).run("rev-parse", "--absolute-git-dir").stdout.strip())


def run_implementation(store: Store, executor, project: Path, task_id: str = TASK):
    claim_id = store.acquire_claim(task_id, "worker-1")
    result = executor.run(store, task_id, claim_id, project)
    store.release_claim(claim_id)
    return result


def external_commit(project: Path, name: str = "external.txt") -> str:
    wt = worktree(project)
    (wt / name).write_text("written by someone else\n", encoding="utf-8")
    return GitWorkspace(wt).commit_all("external change")


class Counting(Validator):
    calls = 0

    def validate(self, store, task_id, candidate_sha, project):
        type(self).calls += 1
        return super().validate(store, task_id, candidate_sha, project)


class CountingReviewer(Reviewer):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def review(self, store, task_id, candidate_sha, project):
        self.calls += 1
        return super().review(store, task_id, candidate_sha, project)


class CountingIntegrator(Integrator):
    def __init__(self) -> None:
        super().__init__(require_independent_review=False)
        self.calls = 0

    def integrate(self, store, task_id, candidate_sha, project):
        self.calls += 1
        return super().integrate(store, task_id, candidate_sha, project)


def coordinator(project: Path, store: Store, executor, **kwargs) -> Coordinator:
    return Coordinator(store, project, executor=executor, target=TargetSelection(TASK), **kwargs)


def advance_to(stage: Stage, tmp_path: Path, **kwargs):
    """Drive the real coordinator with a real subprocess agent until the task sits at `stage` with an authorized candidate."""
    project, store = _setup(tmp_path)
    coord = coordinator(project, store, provider(tmp_path, WRITE_DOC), **kwargs)
    for _ in range(10):
        if Stage(store.get_task(TASK)["stage"]) is stage:
            break
        coord.tick()
    assert Stage(store.get_task(TASK)["stage"]) is stage
    return project, store, coord, str(store.latest_candidate(TASK)["sha"])


def mutation_events(store: Store) -> list[dict]:
    return events(store, EXTERNAL_WORKSPACE_MUTATION)


# --- 7. authorized agent changes still succeed --------------------------------------------------------------------------


def test_authorized_agent_edit_is_committed_and_sealed_as_the_candidate(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    result = run_implementation(store, provider(tmp_path, WRITE_DOC), project)

    assert result.status is ExecutionStatus.SUCCEEDED and result.candidate_sha
    assert GitWorkspace(worktree(project)).head() == result.candidate_sha
    ledger = ledger_of(project)
    assert ledger["head"] == result.candidate_sha and ledger["candidate"] == result.candidate_sha and ledger["dirty_count"] == 0
    assert not (gitdir_of(project) / OWNER_FILE).exists()  # the lease is released
    assert mutation_events(store) == []


def test_authorized_agent_commit_becomes_the_recorded_candidate(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    before = GitWorkspace(prepare_task_workspace(project, TASK)).head()

    result = run_implementation(store, provider(tmp_path, COMMIT_DOC), project)

    assert result.status is ExecutionStatus.SUCCEEDED
    assert result.candidate_sha != before and GitWorkspace(worktree(project)).head() == result.candidate_sha
    assert ledger_of(project)["candidate"] == result.candidate_sha
    assert mutation_events(store) == []


def test_whole_lifecycle_runs_normally_through_to_done(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    coord = coordinator(project, store, provider(tmp_path, WRITE_DOC))
    for _ in range(8):
        if store.get_task(TASK)["status"] == TaskStatus.DONE:
            break
        coord.tick()
    assert store.get_task(TASK)["status"] == TaskStatus.DONE
    assert mutation_events(store) == []


def test_remediation_attempt_reuses_the_sealed_worktree_without_a_false_alarm(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    executor = provider(tmp_path, "import pathlib, time\npathlib.Path('docs/a.md').write_text(str(time.time()), encoding='utf-8')\n")
    first = run_implementation(store, executor, project)
    second = run_implementation(store, executor, project)  # the next attempt starts from the sealed candidate
    assert first.candidate_sha != second.candidate_sha and second.status is ExecutionStatus.SUCCEEDED
    assert mutation_events(store) == []


def test_failed_attempt_leaves_a_sealed_state_the_next_attempt_accepts(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    failing = provider(tmp_path, "import pathlib, sys\npathlib.Path('docs/partial.md').write_text('half', encoding='utf-8')\nsys.exit(3)\n")
    assert run_implementation(store, failing, project).status is ExecutionStatus.FAILED
    assert (worktree(project) / "docs" / "partial.md").exists()  # partial edits stay, as before this feature

    assert run_implementation(store, provider(tmp_path, WRITE_DOC), project).status is ExecutionStatus.SUCCEEDED
    assert mutation_events(store) == []


# --- 9a. an external commit is detected ---------------------------------------------------------------------------------


def test_external_commit_between_executions_stops_the_next_agent_and_is_audited(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    first = run_implementation(store, provider(tmp_path, WRITE_DOC), project)
    foreign = external_commit(project)
    marker = tmp_path / "second-agent-ran.txt"
    second_agent = provider(tmp_path, f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\n")

    result = run_implementation(store, second_agent, project)

    assert result.status is ExecutionStatus.FAILED and result.failure_reason == EXTERNAL_WORKSPACE_MUTATION
    assert not marker.exists()  # the agent was never invoked
    assert GitWorkspace(worktree(project)).head() == foreign  # nothing was reset or adopted
    assert [r["sha"] for r in store.conn.execute("SELECT sha FROM candidates WHERE task_id=?", (TASK,))] == [first.candidate_sha]
    (event,) = mutation_events(store)
    assert event["task_id"] == TASK and event["reason"] == "head_changed" and event["stage"] == "IMPLEMENTATION:acquire"
    assert event["expected_sha"] == first.candidate_sha and event["observed_sha"] == foreign
    assert event["workspace"] == str(worktree(project)) and "remedy" in event
    assert not (gitdir_of(project) / OWNER_FILE).exists()


def test_audit_payload_has_a_fixed_deterministic_shape(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    run_implementation(store, provider(tmp_path, WRITE_DOC), project)
    external_commit(project)
    run_implementation(store, provider(tmp_path, WRITE_DOC), project)
    (event,) = mutation_events(store)
    assert set(event) == {
        "task_id", "execution_id", "claim_id", "workspace", "stage", "reason",
        "expected_sha", "observed_sha", "candidate_sha", "changed_paths", "detail", "remedy",
    }  # fmt: skip
    assert event["claim_id"]  # no execution row exists yet at acquisition, so the claim identifies the attempt


def test_agent_that_rewrites_history_during_its_run_is_detected(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    run_implementation(store, provider(tmp_path, WRITE_DOC), project)
    sealed = ledger_of(project)["head"]
    rewriting = provider(
        tmp_path,
        "import subprocess\n"
        "subprocess.run(['git', 'checkout', '-q', '--orphan', 'elsewhere'], check=True)\n"
        "subprocess.run(['git', 'commit', '-q', '--allow-empty', '-m', 'unrelated history'], check=True)\n",
    )

    result = run_implementation(store, rewriting, project)

    assert result.failure_reason == EXTERNAL_WORKSPACE_MUTATION and result.candidate_sha is None
    (event,) = mutation_events(store)
    assert event["reason"] == "head_not_descendant" and event["expected_sha"] == sealed and event["stage"] == "IMPLEMENTATION:after_agent"
    assert event["execution_id"]  # the running execution is named
    assert store.conn.execute("SELECT status FROM executions WHERE id=?", (event["execution_id"],)).fetchone()["status"] == ExecutionStatus.FAILED
    assert store.latest_candidate(TASK)["sha"] == sealed or store.latest_candidate(TASK) is not None  # no candidate was added for the rewrite
    assert len(list(store.conn.execute("SELECT 1 FROM candidates WHERE task_id=?", (TASK,)))) == 1


def test_ownership_stolen_during_the_run_is_detected(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    thief = provider(
        tmp_path,
        "import json, pathlib, subprocess\n"
        "gitdir = pathlib.Path(subprocess.run(['git', 'rev-parse', '--absolute-git-dir'], capture_output=True, text=True, check=True).stdout.strip())\n"
        f"owner = gitdir / {OWNER_FILE!r}\n"
        "data = json.loads(owner.read_text())\n"
        "data['token'] = 'someone-else'\n"
        "owner.write_text(json.dumps(data))\n"
        "pathlib.Path('docs/a.md').write_text('x')\n",
    )

    result = run_implementation(store, thief, project)

    assert result.failure_reason == EXTERNAL_WORKSPACE_MUTATION
    (event,) = mutation_events(store)
    assert event["reason"] == "workspace_ownership_lost" and event["stage"] == "IMPLEMENTATION:after_agent"
    assert store.latest_candidate(TASK) is None


# --- 9b. an external file modification is detected ----------------------------------------------------------------------


def test_external_tracked_file_edit_is_detected_before_the_next_agent_runs(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    first = run_implementation(store, provider(tmp_path, WRITE_DOC), project)
    (worktree(project) / "docs" / "a.md").write_text("tampered\n", encoding="utf-8")

    result = run_implementation(store, provider(tmp_path, WRITE_DOC), project)

    assert result.failure_reason == EXTERNAL_WORKSPACE_MUTATION
    (event,) = mutation_events(store)
    assert event["reason"] == "working_tree_modified" and event["changed_paths"] == ["docs/a.md"]
    assert event["expected_sha"] == event["observed_sha"] == first.candidate_sha  # same HEAD: only a file changed
    assert (worktree(project) / "docs" / "a.md").read_text(encoding="utf-8") == "tampered\n"  # not reset, not adopted


def test_external_untracked_file_is_detected(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    run_implementation(store, provider(tmp_path, WRITE_DOC), project)
    (worktree(project) / "dropped.txt").write_text("not ours\n", encoding="utf-8")
    assert run_implementation(store, provider(tmp_path, WRITE_DOC), project).failure_reason == EXTERNAL_WORKSPACE_MUTATION
    assert mutation_events(store)[0]["changed_paths"] == ["dropped.txt"]


def test_same_content_rewritten_is_not_a_mutation(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    run_implementation(store, provider(tmp_path, WRITE_DOC), project)
    path = worktree(project) / "docs" / "a.md"
    path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")  # touched, identical bytes
    assert run_implementation(store, provider(tmp_path, "import pathlib\npathlib.Path('docs/b.md').write_text('b')\n"), project).status is ExecutionStatus.SUCCEEDED
    assert mutation_events(store) == []


# --- 9c. another StageMesh execution cannot claim or write the same worktree --------------------------------------------


def test_second_execution_cannot_claim_a_leased_worktree(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    first = acquire_workspace(store, project, TASK, ExecutionKind.IMPLEMENTATION, claim_id="claim-1")
    marker = tmp_path / "must-not-run.txt"
    intruder = provider(tmp_path, f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\n")

    with pytest.raises(WorkspaceMutation) as raised:
        acquire_workspace(store, project, TASK, ExecutionKind.IMPLEMENTATION, claim_id="claim-2")
    assert raised.value.reason == "workspace_owned_by_another_execution"
    result = run_implementation(store, intruder, project)

    assert result.failure_reason == EXTERNAL_WORKSPACE_MUTATION and not marker.exists()
    assert all(e["reason"] == "workspace_owned_by_another_execution" for e in mutation_events(store)) and len(mutation_events(store)) == 2
    first.check("still_mine")  # the rightful owner is undisturbed and its lease intact
    first.release()
    assert run_implementation(store, provider(tmp_path, WRITE_DOC), project).status is ExecutionStatus.SUCCEEDED


def test_lease_of_a_dead_process_is_recovered_and_audited(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    run_implementation(store, provider(tmp_path, WRITE_DOC), project)
    proc = subprocess.Popen([PY, "-c", "import time; time.sleep(30)"])
    identity = popen_identity(proc)
    proc.kill()
    proc.wait()
    (gitdir_of(project) / OWNER_FILE).write_text(
        json.dumps({"token": "dead", "task_id": TASK, "kind": "IMPLEMENTATION", "execution_id": "gone", "pid": identity.pid,
                    "create_time": identity.create_time, "boot_id": identity.boot_id, "executable": identity.executable}),
        encoding="utf-8",
    )  # fmt: skip
    (worktree(project) / "docs" / "partial.md").write_text("the dead run's leftovers", encoding="utf-8")

    lease = acquire_workspace(store, project, TASK, ExecutionKind.IMPLEMENTATION)
    lease.release()

    (event,) = events(store, LEASE_RECOVERED)
    assert event["task_id"] == TASK and mutation_events(store) == []


def test_unreadable_owner_record_fails_closed(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    run_implementation(store, provider(tmp_path, WRITE_DOC), project)
    (gitdir_of(project) / OWNER_FILE).write_text("{not json", encoding="utf-8")
    with pytest.raises(WorkspaceMutation) as raised:
        acquire_workspace(store, project, TASK, ExecutionKind.IMPLEMENTATION)
    assert raised.value.reason == "ownership_record_unreadable"


def test_tampered_ledger_fails_closed(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    run_implementation(store, provider(tmp_path, WRITE_DOC), project)
    (gitdir_of(project) / LEDGER_FILE).write_text("[]", encoding="utf-8")
    assert run_implementation(store, provider(tmp_path, WRITE_DOC), project).failure_reason == EXTERNAL_WORKSPACE_MUTATION
    assert mutation_events(store)[0]["reason"] == "ownership_record_unreadable"


# --- 9d. validation cannot continue after unexpected mutation -----------------------------------------------------------


def test_validation_does_not_start_on_a_mutated_workspace(tmp_path: Path) -> None:
    validator = Counting()
    Counting.calls = 0
    project, store, coord, sha = advance_to(Stage.VALIDATE, tmp_path, validator=validator)
    foreign = external_commit(project)

    coord.tick()

    assert Counting.calls == 0
    assert store.get_task(TASK)["status"] == TaskStatus.BLOCKED
    assert store.conn.execute("SELECT COUNT(*) FROM evidence WHERE task_id=?", (TASK,)).fetchone()[0] == 0
    (event,) = mutation_events(store)
    assert event["stage"] == "VALIDATE:before_validation" and event["expected_sha"] == sha and event["observed_sha"] == foreign
    assert Stage(store.get_task(TASK)["stage"]) is Stage.VALIDATE
    (blocked,) = events(store, "task.blocked")
    assert blocked["reason"] == EXTERNAL_WORKSPACE_MUTATION


def test_evidence_recorded_during_a_mutation_is_never_accepted(tmp_path: Path) -> None:
    class MutatingValidator(Validator):
        def validate(self, store, task_id, candidate_sha, project):
            status = super().validate(store, task_id, candidate_sha, project)
            (worktree(project) / "docs" / "a.md").write_text("changed while validating\n", encoding="utf-8")
            return status

    _project, store, coord, sha = advance_to(Stage.VALIDATE, tmp_path, validator=MutatingValidator())

    coord.tick()

    (event,) = mutation_events(store)
    assert event["stage"] == "VALIDATE:after_validation" and event["reason"] == "working_tree_modified"
    assert store.get_task(TASK)["status"] == TaskStatus.BLOCKED
    assert Stage(store.get_task(TASK)["stage"]) is Stage.VALIDATE  # PASSED evidence exists but did not advance the task
    assert store.has_evidence(TASK, sha, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)
    with pytest.raises(TargetSelectionError, match="blocked"):
        coord.tick()  # a blocked task is not picked up again
    assert Stage(store.get_task(TASK)["stage"]) is Stage.VALIDATE


def test_blocked_task_cannot_be_resumed_on_the_tampered_workspace(tmp_path: Path) -> None:
    project, store, coord, _sha = advance_to(Stage.VALIDATE, tmp_path)
    external_commit(project)
    coord.tick()
    assert store.unblock_task(TASK)  # an operator retries without fixing the workspace
    coord.tick()
    assert store.get_task(TASK)["status"] == TaskStatus.BLOCKED
    assert len(mutation_events(store)) == 2  # detected again, still not adopted


def test_gate_that_moves_head_in_the_validation_checkout_cannot_pass(tmp_path: Path) -> None:
    project, _store = _setup(tmp_path)
    sneaky = parse_contract(
        {
            "objective": "docs",
            "allowed_files": ["**"],
            "required_tests": [
                {"name": "sneaky", "command": [PY, "-c", "import subprocess; subprocess.run(['git', 'commit', '--allow-empty', '-qm', 'x'], check=True)"]}
            ],
        }
    )
    sha = GitWorkspace(project).head()
    evaluation = evaluate_contract(project, sha, sneaky, run_gates=True)
    assert not evaluation.passed
    assert any(f["code"] == "candidate_workspace_unavailable" and "moved the candidate checkout" in f["message"] for f in evaluation.findings)


# --- 9e. review cannot silently switch SHA ------------------------------------------------------------------------------


def test_review_does_not_follow_a_newer_commit_in_the_worktree(tmp_path: Path) -> None:
    reviewer = CountingReviewer()
    project, store, coord, sha = advance_to(Stage.REVIEW, tmp_path, reviewer=reviewer)
    newer = external_commit(project)
    store.add_candidate(TASK, newer, "someone", durable_handoff=True)  # and a candidate row appears for it

    coord.tick()

    assert reviewer.calls == 0
    assert store.get_task(TASK)["status"] == TaskStatus.BLOCKED
    assert store.conn.execute("SELECT COUNT(*) FROM evidence WHERE task_id=? AND kind=?", (TASK, EvidenceKind.REVIEW)).fetchone()[0] == 0
    (event,) = mutation_events(store)
    assert event["stage"] == "REVIEW:before_review" and event["expected_sha"] == sha and event["observed_sha"] == newer


def test_review_does_not_follow_a_new_candidate_row_with_an_untouched_worktree(tmp_path: Path) -> None:
    reviewer = CountingReviewer()
    _project, store, coord, sha = advance_to(Stage.REVIEW, tmp_path, reviewer=reviewer)
    store.add_candidate(TASK, "f" * 40, "someone", durable_handoff=True)  # provenance changed behind StageMesh's back

    coord.tick()

    assert reviewer.calls == 0 and store.get_task(TASK)["status"] == TaskStatus.BLOCKED
    (event,) = mutation_events(store)
    assert event["reason"] == "candidate_provenance_changed" and event["expected_sha"] == sha and event["observed_sha"] == "f" * 40


def test_review_requires_validation_evidence_for_exactly_this_candidate(tmp_path: Path) -> None:
    _project, store, coord, _sha = advance_to(Stage.REVIEW, tmp_path)
    store.conn.execute("DELETE FROM evidence WHERE task_id=? AND kind=?", (TASK, EvidenceKind.VALIDATION))
    store.conn.commit()
    coord.tick()
    assert mutation_events(store)[0]["reason"] == "candidate_without_validation_evidence"


def test_review_checks_out_the_exact_candidate_in_its_own_workspace(tmp_path: Path) -> None:
    project, _store, _coord, sha = advance_to(Stage.REVIEW, tmp_path)
    newer = external_commit(project)  # the implementation worktree has moved on; the review must not see it
    seen = tmp_path / "review-seen.json"
    script = tmp_path / "reviewer.py"
    script.write_text(
        "import json, os, subprocess, sys\n"
        "head = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()\n"
        f"open({str(seen)!r}, 'w').write(json.dumps({{'cwd': os.getcwd(), 'head': head}}))\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'decision': 'PASS'}))\n",
        encoding="utf-8",
    )
    adapter = RuntimeCommandAdapter("claude", (PY, str(script)))

    response = adapter.review_candidate("review it", project, sha)

    observed = json.loads(seen.read_text(encoding="utf-8"))
    assert json.loads(response) == {"decision": "PASS"}
    assert observed["head"] == sha and observed["head"] != newer
    review_dir = Path(observed["cwd"]).resolve()
    assert review_dir != worktree(project).resolve() and review_dir != project.resolve()
    assert worktree(project) not in review_dir.parents and not review_dir.exists()  # a throwaway clone, gone afterwards


def test_review_agent_that_moves_head_in_its_clone_is_rejected(tmp_path: Path) -> None:
    project, _store, _coord, sha = advance_to(Stage.REVIEW, tmp_path)
    script = tmp_path / "bad-reviewer.py"
    script.write_text(
        "import subprocess, sys\n"
        "subprocess.run(['git', '-c', 'user.name=x', '-c', 'user.email=x@x', 'commit', '--allow-empty', '-qm', 'moved'], check=True)\n"
        "print('{\"decision\": \"PASS\"}')\n",
        encoding="utf-8",
    )
    response = RuntimeCommandAdapter("claude", (PY, str(script))).review_candidate("p", project, sha)
    assert "mutated candidate workspace" in response


# --- 6/9. integration ---------------------------------------------------------------------------------------------------


def test_integration_is_refused_on_a_mutated_workspace(tmp_path: Path) -> None:
    integrator = CountingIntegrator()
    project, store, coord, sha = advance_to(Stage.INTEGRATE, tmp_path, integrator=integrator)
    (worktree(project) / "docs" / "a.md").write_text("late edit\n", encoding="utf-8")

    coord.tick()

    assert integrator.calls == 0 and store.get_task(TASK)["status"] == TaskStatus.BLOCKED
    (event,) = mutation_events(store)
    assert event["stage"] == "INTEGRATE:before_integration" and event["reason"] == "working_tree_modified"
    assert not store.has_evidence(TASK, sha, EvidenceKind.INTEGRATION, EvidenceStatus.PASSED)


def test_implementation_stage_blocks_instead_of_retrying_on_mutation(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    coord = coordinator(project, store, provider(tmp_path, WRITE_DOC))
    coord.tick()  # IMPLEMENT -> VALIDATE
    # an operator sends the task back for rework, then someone edits the workspace before the next attempt
    store.advance_task(TASK, Stage.IMPLEMENT)
    (worktree(project) / "docs" / "a.md").write_text("tampered\n", encoding="utf-8")

    coord.tick()

    assert store.get_task(TASK)["status"] == TaskStatus.BLOCKED
    assert store.conn.execute("SELECT COUNT(*) FROM claims WHERE task_id=? AND active=1", (TASK,)).fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM executions WHERE status='RUNNING'").fetchone()[0] == 0
    assert mutation_events(store)[0]["stage"] == "IMPLEMENTATION:acquire"


# --- operator candidate registration and existing worktrees -------------------------------------------------------------


def test_pinned_candidate_replaces_the_expected_one(tmp_path: Path) -> None:
    project, store, _coord, _sha = advance_to(Stage.VALIDATE, tmp_path)
    adopted = GitWorkspace(project).head()  # an operator-registered commit
    store.add_candidate(TASK, adopted, "operator", durable_handoff=True)
    with pytest.raises(WorkspaceMutation):
        verify_candidate_workspace(store, project, TASK, adopted, "VALIDATE:before_validation")  # not yet pinned: looks like a switch
    pin_candidate(project, TASK, adopted)
    verify_candidate_workspace(store, project, TASK, adopted, "VALIDATE:before_validation")  # an authorized registration


def test_worktree_from_before_this_feature_is_adopted_only_when_it_matches_the_database(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    wt = prepare_task_workspace(project, TASK)  # no ledger: how every existing in-flight worktree looks
    (wt / "docs" / "a.md").write_text("legacy agent output\n", encoding="utf-8")
    sha = GitWorkspace(wt).commit_all("legacy candidate")
    store.add_candidate(TASK, sha, "legacy", durable_handoff=True)

    verify_candidate_workspace(store, project, TASK, sha, "VALIDATE:before_validation")  # HEAD is the recorded candidate

    external_commit(project)
    with pytest.raises(WorkspaceMutation) as raised:
        verify_candidate_workspace(store, project, TASK, sha, "VALIDATE:before_validation")
    assert raised.value.reason == "no_ownership_record_and_head_differs"


def test_tasks_without_a_worktree_are_not_blocked_by_the_guard(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    sha = GitWorkspace(project).head()
    store.add_candidate(TASK, sha, "operator", durable_handoff=True)
    verify_candidate_workspace(store, project, TASK, sha, "VALIDATE:before_validation")
    assert mutation_events(store) == []


def test_observe_reports_head_and_uncommitted_paths(tmp_path: Path) -> None:
    project, _store = _setup(tmp_path)
    (project / "docs" / "a.md").write_text("changed\n", encoding="utf-8")
    (project / "new.txt").write_text("new\n", encoding="utf-8")
    seen = observe(project)
    assert seen["head"] == GitWorkspace(project).head()
    assert {"docs/a.md", "new.txt"} <= set(seen["dirty"]) and seen["dirty_count"] == len(seen["dirty"])


# --- 9g. concurrent tasks with separate worktrees keep working ----------------------------------------------------------


def test_concurrent_tasks_use_separate_worktrees_without_interference(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    store.upsert_task("second", source_id="TASK-2")
    store.close()
    ids = [TASK, "TASK-2"]
    results: dict[str, object] = {}
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def work(task_id: str) -> None:
        local = Store(project / ".stagemesh" / "stagemesh.sqlite3")
        try:
            claim = local.acquire_claim(task_id, f"worker-{task_id}")
            barrier.wait(timeout=30)
            results[task_id] = FakeExecutor().run(local, task_id, claim, project)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)
        finally:
            local.close()

    threads = [threading.Thread(target=work, args=(task_id,)) for task_id in ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert not errors
    assert all(results[t].status is ExecutionStatus.SUCCEEDED for t in ids)  # type: ignore[attr-defined]
    assert worktree(project, ids[0]) != worktree(project, ids[1])
    check = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    try:
        assert mutation_events(check) == []
        for task_id in ids:
            sealed = ledger_of(project, task_id)
            assert sealed["head"] == results[task_id].candidate_sha == sealed["candidate"]  # type: ignore[attr-defined]
        assert results[ids[0]].candidate_sha != results[ids[1]].candidate_sha  # type: ignore[attr-defined]
    finally:
        check.close()


def test_one_tasks_mutation_does_not_block_another_task(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    store.upsert_task("second", source_id="TASK-2")
    assert run_implementation(store, FakeExecutor(), project, TASK).status is ExecutionStatus.SUCCEEDED
    assert run_implementation(store, FakeExecutor(), project, "TASK-2").status is ExecutionStatus.SUCCEEDED
    (worktree(project, TASK) / "stagemesh-task-TASK-1.txt").write_text("tampered", encoding="utf-8")

    assert run_implementation(store, FakeExecutor(), project, TASK).failure_reason == EXTERNAL_WORKSPACE_MUTATION
    verify_candidate_workspace(store, project, "TASK-2", str(store.latest_candidate("TASK-2")["sha"]), "VALIDATE:before_validation")
    assert {e["task_id"] for e in mutation_events(store)} == {TASK}


def test_executor_works_in_a_project_directory_that_does_not_exist_yet(tmp_path: Path) -> None:
    """scripts/invariants.py runs the coordinator against a not-yet-created project; the guard must not need it to exist first."""
    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    task_id = store.upsert_task("work")
    store.advance_task(task_id, Stage.IMPLEMENT)
    assert store.acquire_claim(task_id, "worker-a", lease_seconds=-1)
    project = tmp_path / "project"
    assert not project.exists()

    assert Coordinator(store, project).tick() == 1

    assert store.get_task(task_id)["stage"] == Stage.VALIDATE
    assert ledger_of(project, task_id)["candidate"] == store.latest_candidate(task_id)["sha"]
    store.close()
