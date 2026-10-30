"""Focused regression tests for review-infrastructure failure recovery and stage-specific resumption."""
from __future__ import annotations

import json
from pathlib import Path

from test_distinct_review_roles import ControlledAdapter
from test_parallel import Rig, ScriptedExecutor
from test_review_reliability import ScriptedReviewAdapter, _coordinator

from stagemesh.block_kinds import BlockKind, classify_block
from stagemesh.config import AdverseReviewPolicy
from stagemesh.contract_binding import contract_for_candidate
from stagemesh.coordinator import Coordinator
from stagemesh.domain import (
    EvidenceKind,
    EvidenceStatus,
    ExecutionKind,
    ExecutionStatus,
    Stage,
    TaskStatus,
)
from stagemesh.provider_pool import REVIEW, ProviderLog, ProviderPool
from stagemesh.recovery import _resume_at_validate, resume_task
from stagemesh.review import INFRASTRUCTURE_FAILURE, Reviewer
from stagemesh.review_identity import command_snapshot, record_implementation_identity

TASK = "TASK-1"


def _advance_to_stage(rig: Rig, coord: Coordinator, task_id: str, stage: Stage, limit: int = 30) -> None:
    for _ in range(limit):
        task = rig.store.get_task(task_id)
        if task is not None and task["stage"] == stage:
            return
        coord.tick()
    raise AssertionError(f"task {task_id} did not reach {stage} within {limit} ticks")


def test_review_authentication_failure_safely_blocks_with_external_dependency(tmp_path: Path) -> None:
    """When review encounters an infrastructure authentication/quota failure, it safely blocks as

    BLOCKED_EXTERNAL_DEPENDENCY, preserving the validated candidate and passing validation evidence.
    """
    rig = Rig(tmp_path, [TASK])
    executor = ScriptedExecutor(rig.files)
    # Simulate an external provider returning authentication/capacity failure
    auth_failure_resp = json.dumps({"decision": INFRASTRUCTURE_FAILURE, "reason": "authentication_failure"})
    adapter = ScriptedReviewAdapter(auth_failure_resp)
    coord = _coordinator(rig, executor, adapter)

    # Advance task until it reaches REVIEW stage (PLAN -> IMPLEMENT -> VALIDATE -> REVIEW)
    _advance_to_stage(rig, coord, TASK, Stage.REVIEW)
    candidate_sha = rig.store.latest_candidate(TASK)["sha"]

    # Verify validation evidence passed before review
    assert rig.store.has_evidence(TASK, candidate_sha, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)

    # Tick coordinator: review runs, encounters authentication_failure, and safely blocks
    progressed = coord.tick()
    assert progressed == 1

    # Task is now BLOCKED at Stage.REVIEW
    task = rig.store.get_task(TASK)
    assert task["stage"] == Stage.REVIEW
    assert task["status"] == TaskStatus.BLOCKED

    # Verified block classification is EXTERNAL_DEPENDENCY
    info = classify_block(rig.store, TASK)
    assert info is not None
    assert info.kind == BlockKind.EXTERNAL_DEPENDENCY
    assert "authentication_failure" in info.reason
    assert candidate_sha[:10] in info.reason

    # Candidate and passing validation evidence remain strictly intact
    assert rig.store.latest_candidate(TASK)["sha"] == candidate_sha
    assert rig.store.has_evidence(TASK, candidate_sha, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)
    assert rig.store.open_findings_for_candidate(TASK, candidate_sha) == []


def test_stage_specific_resumption_resumes_validated_candidate_directly_at_review(tmp_path: Path) -> None:
    """A task with a validated candidate resumes directly at REVIEW without repeating implementation or validation."""
    rig = Rig(tmp_path, [TASK])
    executor = ScriptedExecutor(rig.files)
    auth_failure_resp = json.dumps({"decision": INFRASTRUCTURE_FAILURE, "reason": "authentication_failure"})
    adapter = ScriptedReviewAdapter(auth_failure_resp)
    coord = _coordinator(rig, executor, adapter)

    _advance_to_stage(rig, coord, TASK, Stage.REVIEW)
    candidate_sha = rig.store.latest_candidate(TASK)["sha"]
    coord.tick()

    task = rig.store.get_task(TASK)
    assert task["status"] == TaskStatus.BLOCKED
    assert task["stage"] == Stage.REVIEW

    # Operator unblocks / resumes task using resume_task
    res = resume_task(rig.store, TASK)
    assert res["unblocked"] is True
    assert res["stage"] == Stage.REVIEW

    # Task is OPEN and directly at REVIEW
    task = rig.store.get_task(TASK)
    assert task["status"] == TaskStatus.OPEN
    assert task["stage"] == Stage.REVIEW

    # Validate that execution count for VALIDATION has not increased
    val_count = rig.store.conn.execute(
        "SELECT COUNT(*) FROM executions WHERE task_id=? AND kind=?", (TASK, ExecutionKind.VALIDATION)
    ).fetchone()[0]
    assert val_count == 1


def test_resume_requires_verified_adverse_pair_when_policy_is_configured(tmp_path: Path) -> None:
    """A normal-role PASS alone cannot let retry-task skip the required adverse role."""
    rig = Rig(tmp_path, [TASK])
    executor = ScriptedExecutor(rig.files)
    coord = _coordinator(rig, executor, ScriptedReviewAdapter('{"decision":"PASS"}'))
    policy = AdverseReviewPolicy()

    _advance_to_stage(rig, coord, TASK, Stage.REVIEW)
    candidate_sha = rig.store.latest_candidate(TASK)["sha"]
    producer = rig.store.conn.execute(
        "SELECT produced_by FROM candidates WHERE task_id=? AND sha=?", (TASK, candidate_sha)
    ).fetchone()["produced_by"]
    implementation = rig.store.start_execution(
        task_id=TASK,
        claim_id=None,
        kind=ExecutionKind.IMPLEMENTATION,
        candidate_sha=candidate_sha,
        actor=producer,
    )
    rig.store.finish_execution(implementation, ExecutionStatus.SUCCEEDED, candidate_sha, actor=producer)
    record_implementation_identity(
        rig.store,
        TASK,
        candidate_sha,
        implementation,
        command_snapshot(producer, ("scripted-implementation",)),
    )
    pool = ProviderPool(
        [ControlledAdapter("claude"), ControlledAdapter("grok")],
        {REVIEW: ("claude", "grok")},
        policy="priority",
        log=ProviderLog(echo=False),
    )
    reviewer = Reviewer(
        review_pool=pool,
        require_independent=True,
        separate_roles=True,
        adverse_policy=policy,
    )
    normal_status = reviewer.review(rig.store, TASK, candidate_sha, rig.project)
    assert normal_status == EvidenceStatus.PASSED
    rig.store.block_task(TASK)

    normal_only = resume_task(rig.store, TASK, adverse_policy=policy)
    assert normal_only == {"unblocked": True, "stage": Stage.REVIEW}
    assert rig.store.get_task(TASK)["status"] == TaskStatus.OPEN

    assert reviewer.review(rig.store, TASK, candidate_sha, rig.project) == EvidenceStatus.PASSED
    rig.store.block_task(TASK)

    complete_pair = resume_task(rig.store, TASK, adverse_policy=policy)
    assert complete_pair == {"unblocked": True, "stage": Stage.INTEGRATE}
    assert rig.store.get_task(TASK)["status"] == TaskStatus.OPEN


def test_resume_at_validate_advances_validated_candidate_to_review(tmp_path: Path) -> None:
    """_resume_at_validate advances a candidate that already passed validation to Stage.REVIEW rather than re-validating."""
    rig = Rig(tmp_path, [TASK])
    executor = ScriptedExecutor(rig.files)
    adapter = ScriptedReviewAdapter('{"decision":"PASS"}')
    coord = _coordinator(rig, executor, adapter)

    _advance_to_stage(rig, coord, TASK, Stage.REVIEW)
    candidate_sha = rig.store.latest_candidate(TASK)["sha"]

    # Suppose a repair reset task back to IMPLEMENT while candidate retains passed validation
    rig.store.advance_task(TASK, Stage.IMPLEMENT)
    rig.store.block_task(TASK)

    task = rig.store.get_task(TASK)
    assert task["stage"] == Stage.IMPLEMENT
    assert task["status"] == TaskStatus.BLOCKED

    # Calling _resume_at_validate resumes directly at REVIEW
    changes = _resume_at_validate(rig.store, TASK, task)
    assert changes["unblocked"] is True
    assert changes["stage_reset"] is True

    updated = rig.store.get_task(TASK)
    assert updated["stage"] == Stage.REVIEW
    assert updated["status"] == TaskStatus.OPEN


def test_review_success_after_recovery_advances_to_integrate_and_completes(tmp_path: Path) -> None:
    """When an unblocked/recovered task encounters a working reviewer, it advances to INTEGRATE."""
    rig = Rig(tmp_path, [TASK])
    executor = ScriptedExecutor(rig.files)
    # Starts with auth failure, then recovers with PASS
    flaky_adapter = ScriptedReviewAdapter(
        json.dumps({"decision": INFRASTRUCTURE_FAILURE, "reason": "authentication_failure"}),
        '{"decision":"PASS"}',
    )
    coord = _coordinator(rig, executor, flaky_adapter)

    _advance_to_stage(rig, coord, TASK, Stage.REVIEW)
    candidate_sha = rig.store.latest_candidate(TASK)["sha"]

    # First tick blocks on auth failure
    coord.tick()
    assert rig.store.get_task(TASK)["status"] == TaskStatus.BLOCKED

    # Unblock / resume
    resume_task(rig.store, TASK)
    assert rig.store.get_task(TASK)["status"] == TaskStatus.OPEN

    # Second tick: reviewer answers PASS
    progressed = coord.tick()
    assert progressed == 1

    # Task advances to INTEGRATE with EvidenceKind.REVIEW PASSED
    task = rig.store.get_task(TASK)
    assert task["stage"] == Stage.INTEGRATE
    bound = contract_for_candidate(rig.store, TASK, candidate_sha, rig.project)
    assert rig.store.has_bound_evidence(TASK, candidate_sha, EvidenceKind.REVIEW, bound.digest, EvidenceStatus.PASSED)
