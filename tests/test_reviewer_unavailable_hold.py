from __future__ import annotations

import json
import sys
from pathlib import Path

from stagemesh.block_kinds import (
    BlockKind,
    classify_block,
    reenter_blocked_task,
    reentry_refusal,
    runtime_fingerprint,
)
from stagemesh.contract_binding import contract_for_candidate
from stagemesh.coordinator import Coordinator
from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage, TaskStatus
from stagemesh.execution import SubprocessExecutor
from stagemesh.git import GitWorkspace
from stagemesh.integration import Integrator
from stagemesh.persistence import Store
from stagemesh.provider_pool import ProviderLog, ProviderPool
from stagemesh.providers import RuntimeCommandAdapter
from stagemesh.recovery import resume_task
from stagemesh.review import Reviewer
from stagemesh.validation import Validator

TASK = "TASK-HOLD-1"
PROSE_INVARIANT = "review: preserve public API invariants and contracts"


def _setup_rig(
    tmp_path: Path,
    *,
    invariants: list[str] | None = None,
    review_pool_modes: dict[str, str | None] | None = None,
    adapter=None,
    require_independent: bool = True,
):
    project = tmp_path / "repo"
    project.mkdir()
    git = GitWorkspace(project)
    git.init_if_needed()
    git.run("config", "user.email", "test@example.invalid")
    git.run("config", "user.name", "StageMesh Test")
    (project / "docs").mkdir()
    (project / "docs" / "a.md").write_text("initial\n", encoding="utf-8")
    base = git.commit_all("base")
    branch = git.run("symbolic-ref", "HEAD").stdout.strip()
    git.run("branch", "integration")

    contracts = project / ".stagemesh" / "contracts"
    contracts.mkdir(parents=True)
    contract_data = {
        "objective": "test review holds",
        "allowed_files": ["docs/**"],
        "required_tests": [{"name": "docs-static", "command": [sys.executable, "-c", "pass"]}],
    }
    if invariants is not None:
        contract_data["invariants"] = invariants
    (contracts / f"{TASK}.json").write_text(json.dumps(contract_data), encoding="utf-8")

    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    store.upsert_task("completion", source_id=TASK)
    store.advance_task(TASK, Stage.IMPLEMENT)

    script = tmp_path / "provider.py"
    script.write_text(
        "import pathlib\n"
        "pathlib.Path('docs/a.md').write_text('implemented content\\n')\n",
        encoding="utf-8",
    )

    pool = None
    if review_pool_modes is not None:
        adapters = [
            RuntimeCommandAdapter(
                name,
                (sys.executable, str(script), mode, name) if mode else ("stagemesh-no-such-binary-xyz",),
            )
            for name, mode in review_pool_modes.items()
        ]
        log = ProviderLog(echo=False)
        pool = ProviderPool(
            adapters,
            pools={"IMPLEMENT": tuple(review_pool_modes), "REVIEW": tuple(review_pool_modes)},
            require_independent=require_independent,
            log=log,
        )

    reviewer = Reviewer(
        adapter=adapter,
        require_independent=require_independent,
        review_pool=pool,
    )
    integrator = Integrator(
        integration_ref="refs/heads/integration",
        require_independent_review=require_independent,
    )
    coordinator = Coordinator(
        store,
        project,
        executor=SubprocessExecutor([sys.executable, str(script)], name="codex"),
        validator=Validator(),
        reviewer=reviewer,
        integrator=integrator,
        require_independent_review=require_independent,
    )
    return {
        "project": project,
        "store": store,
        "git": git,
        "base": base,
        "branch": branch,
        "coordinator": coordinator,
        "reviewer": reviewer,
        "pool": pool,
    }


def test_invariant_review_unavailable_records_blocked_external_dependency_at_review(tmp_path: Path):
    rig = _setup_rig(
        tmp_path,
        invariants=[PROSE_INVARIANT],
        review_pool_modes={"codex": "ok", "claude": None},
        require_independent=True,
    )
    coordinator = rig["coordinator"]
    store = rig["store"]
    project = rig["project"]

    # Tick 1: IMPLEMENT -> creates candidate
    coordinator.tick()
    assert store.get_task(TASK)["stage"] == Stage.VALIDATE
    cand = store.latest_candidate(TASK)
    assert cand is not None
    candidate_sha = cand["sha"]

    # Tick 2: VALIDATE -> passes validation
    coordinator.tick()
    assert store.get_task(TASK)["stage"] == Stage.REVIEW
    bound = contract_for_candidate(store, TASK, candidate_sha, project)
    assert store.has_bound_evidence(TASK, candidate_sha, EvidenceKind.VALIDATION, bound.digest, EvidenceStatus.PASSED)

    # Tick 3: REVIEW -> mandatory reviewer cannot be selected for review: invariants
    coordinator.tick()

    task = store.get_task(TASK)
    assert task["stage"] == Stage.REVIEW
    assert task["status"] == TaskStatus.BLOCKED

    # Check block classification
    block = classify_block(store, TASK)
    assert block is not None
    assert block.kind == BlockKind.EXTERNAL_DEPENDENCY
    assert "invariant_review_unavailable" in block.reason

    # Audit event checks
    audit_row = store.conn.execute(
        "SELECT payload FROM audit_events WHERE event_type='task.block_classified' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert audit_row is not None
    audit_payload = json.loads(audit_row["payload"])
    assert audit_payload["kind"] == "BLOCKED_EXTERNAL_DEPENDENCY"
    assert audit_payload["stage"] == "REVIEW"
    assert audit_payload["candidate_sha"] == candidate_sha
    assert audit_payload["review_infrastructure_failure"] == "invariant_review_unavailable"
    assert audit_payload["providers_considered"] is not None

    # Rejected provider reasons are kept
    considered = {c["provider"]: c["reason"] for c in audit_payload["providers_considered"]}
    assert "codex" in considered and "not_independent" in considered["codex"]
    assert "claude" in considered and "cli_not_installed" in considered["claude"]
    assert "codex" in block.reason and "claude" in block.reason

    # Evidence and candidate preservation
    assert store.latest_candidate(TASK)["sha"] == candidate_sha
    assert store.has_bound_evidence(TASK, candidate_sha, EvidenceKind.VALIDATION, bound.digest, EvidenceStatus.PASSED)
    # Missing reviewer does not receive PASS review
    review_evidences = store.conn.execute(
        "SELECT status, payload FROM evidence WHERE task_id=? AND kind=? ORDER BY rowid",
        (TASK, EvidenceKind.REVIEW),
    ).fetchall()
    assert review_evidences
    statuses = [r["status"] for r in review_evidences]
    assert EvidenceStatus.PASSED not in statuses
    assert EvidenceStatus.CAPACITY in statuses

    # Automatic re-entry is refused
    fingerprint = runtime_fingerprint(project)
    refusal = reentry_refusal(store, TASK, fingerprint)
    assert refusal is not None
    assert refusal.startswith("BLOCKED_EXTERNAL_DEPENDENCY")
    assert reenter_blocked_task(store, TASK, fingerprint) is None
    assert store.get_task(TASK)["status"] == TaskStatus.BLOCKED

    # Explicit operator retry unblocks
    resumed = resume_task(store, TASK)
    assert resumed["unblocked"] is True
    assert store.get_task(TASK)["status"] == TaskStatus.OPEN
    assert store.get_task(TASK)["stage"] == Stage.REVIEW


def test_independent_review_unavailable_still_blocks_and_does_not_pass(tmp_path: Path):
    # Without review: invariants, missing reviewer yields independent_review_unavailable
    rig = _setup_rig(
        tmp_path,
        invariants=None,
        review_pool_modes={"codex": "ok", "claude": None},
        require_independent=True,
    )
    coordinator = rig["coordinator"]
    store = rig["store"]
    project = rig["project"]

    coordinator.tick()  # IMPLEMENT
    coordinator.tick()  # VALIDATE
    coordinator.tick()  # REVIEW

    task = store.get_task(TASK)
    assert task["stage"] == Stage.REVIEW
    assert task["status"] == TaskStatus.BLOCKED

    block = classify_block(store, TASK)
    assert block is not None
    assert block.kind == BlockKind.EXTERNAL_DEPENDENCY
    assert "independent_review_unavailable" in block.reason

    # No PASS review
    passed = store.conn.execute(
        "SELECT id FROM evidence WHERE task_id=? AND kind=? AND status=?",
        (TASK, EvidenceKind.REVIEW, EvidenceStatus.PASSED),
    ).fetchall()
    assert len(passed) == 0

    # Automatic re-entry refused
    fingerprint = runtime_fingerprint(project)
    assert reenter_blocked_task(store, TASK, fingerprint) is None
    assert store.get_task(TASK)["status"] == TaskStatus.BLOCKED
