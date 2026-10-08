from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from stagemesh.contracts import CONTRACT_VERSION, canonical_contract_json, parse_contract
from stagemesh.domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus
from stagemesh.git import GitWorkspace
from stagemesh.persistence import Store
from stagemesh.review import Reviewer

TASK = "T-146"
CONTRACT = {
    "objective": "change the readme",
    "allowed_files": ["README.md"],
    "required_tests": [],
}


class JsonReviewAdapter:
    def __init__(self, response: str, *, name: str = "reviewer") -> None:
        self.name = name
        self.response = response

    def review(self, prompt: str) -> str:
        return self.response


class RaisingReviewAdapter:
    name = "reviewer"

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def review(self, prompt: str) -> str:
        raise self.exc


def _project_with_candidate(tmp_path: Path) -> tuple[Path, Store, str]:
    project = tmp_path / "repo"
    project.mkdir()
    git = GitWorkspace(project)
    git.init_if_needed()
    git.run("config", "user.email", "test@example.invalid")
    git.run("config", "user.name", "StageMesh Test")
    runtime = project / ".stagemesh"
    (runtime / "contracts").mkdir(parents=True)
    (runtime / "contracts" / f"{TASK}.json").write_text(json.dumps(CONTRACT), encoding="utf-8")
    (project / "README.md").write_text("base\n", encoding="utf-8")
    git.commit_all("base")
    baseline = git.run("rev-parse", "HEAD").stdout.strip()

    (project / "README.md").write_text("changed\n", encoding="utf-8")
    git.commit_all("candidate")
    candidate = git.run("rev-parse", "HEAD").stdout.strip()

    store = Store(runtime / "stagemesh.sqlite3")
    store.migrate()
    store.upsert_task("review terminalization", source="local", source_id=TASK)
    canonical = canonical_contract_json(parse_contract(CONTRACT))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    store.bind_task_contract(TASK, baseline, CONTRACT_VERSION, digest, canonical)
    store.add_candidate(TASK, candidate, produced_by="codex", durable_handoff=True)
    return project, store, candidate


def _review_execution(store: Store):
    return store.conn.execute(
        "SELECT * FROM executions WHERE kind=? ORDER BY started_at DESC LIMIT 1",
        (ExecutionKind.REVIEW,),
    ).fetchone()


@pytest.mark.parametrize(
    ("response", "evidence_status", "execution_status", "result"),
    [
        ('{"decision":"PASS"}', EvidenceStatus.PASSED, ExecutionStatus.SUCCEEDED, None),
        ('{"decision":"FAIL","findings":[{"severity":"error","message":"needs work"}]}', EvidenceStatus.FAILED, ExecutionStatus.FAILED, "findings"),
    ],
)
def test_builtin_review_execution_terminalizes_on_pass_and_fail(
    tmp_path: Path,
    response: str,
    evidence_status: EvidenceStatus,
    execution_status: ExecutionStatus,
    result: str | None,
) -> None:
    project, store, candidate = _project_with_candidate(tmp_path)

    status = Reviewer(adapter=JsonReviewAdapter(response), require_independent=True).review(store, TASK, candidate, project)

    execution = _review_execution(store)
    assert status == evidence_status
    assert execution["pid"] is None
    assert execution["actor"] == "reviewer"
    assert execution["status"] == execution_status
    assert execution["result"] == result


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (RuntimeError("review provider crashed"), "RuntimeError: review provider crashed"),
        (TimeoutError("review provider timed out"), "TimeoutError: review provider timed out"),
    ],
)
def test_builtin_review_execution_terminalizes_exception_and_timeout_as_capacity(
    tmp_path: Path,
    exc: Exception,
    reason: str,
) -> None:
    project, store, candidate = _project_with_candidate(tmp_path)

    status = Reviewer(adapter=RaisingReviewAdapter(exc), require_independent=True).review(store, TASK, candidate, project)

    execution = _review_execution(store)
    evidence = store.conn.execute(
        "SELECT payload FROM evidence WHERE kind=? AND status=? ORDER BY created_at DESC LIMIT 1",
        (EvidenceKind.REVIEW, EvidenceStatus.CAPACITY),
    ).fetchone()
    payload = json.loads(evidence["payload"])
    assert status == EvidenceStatus.CAPACITY
    assert execution["pid"] is None
    assert execution["status"] == ExecutionStatus.FAILED
    assert execution["result"] == reason
    assert payload["review_infrastructure_failure"] == reason


def test_builtin_review_execution_terminalizes_before_interrupt_bubbles(tmp_path: Path) -> None:
    project, store, candidate = _project_with_candidate(tmp_path)

    with pytest.raises(KeyboardInterrupt):
        Reviewer(adapter=RaisingReviewAdapter(KeyboardInterrupt("operator stop")), require_independent=True).review(
            store, TASK, candidate, project
        )

    execution = _review_execution(store)
    assert execution["pid"] is None
    assert execution["status"] == ExecutionStatus.FAILED
    assert execution["result"] == "KeyboardInterrupt: operator stop"
