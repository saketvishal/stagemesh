"""Scenario 5: conflicting independent reviews cannot land a candidate with an unresolved material finding."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage, TaskStatus
from stagemesh.review import Reviewer
from stagemesh.review_disposition import (
    FINDING_DISPOSITION,
    FINDING_RECORDED,
    DispositionError,
    finding_history,
    record_disposition,
    unresolved_adverse_review_findings,
)

from test_completion_integrity import TASK, FakeReviewAdapter, Rig

FAIL = '{"decision":"FAIL_WITH_FINDINGS","findings":[{"severity":"error","message":"docs/a.md omits the required Engineering Impact record","path":"docs/a.md"}]}'


def _events(rig: Rig, event_type: str) -> list[dict]:
    rows = rig.store.conn.execute("SELECT payload FROM audit_events WHERE event_type=? ORDER BY created_at, rowid", (event_type,)).fetchall()
    return [json.loads(r[0]) for r in rows]


def _rig_with_adverse_review(tmp_path: Path) -> tuple[Rig, str]:
    rig = Rig(tmp_path, FakeReviewAdapter("claude", FAIL), integration_ref="refs/heads/integration")
    rig.tick(3)  # implement, validate, review (FAIL_WITH_FINDINGS -> remediation queued)
    sha = str(rig.store.latest_candidate(TASK)["sha"])
    assert rig.evidence(EvidenceKind.REVIEW)[0][0] == EvidenceStatus.FAILED
    return rig, sha


def test_findings_keep_identity_provenance_and_history(tmp_path: Path) -> None:
    rig, sha = _rig_with_adverse_review(tmp_path)

    pending, = unresolved_adverse_review_findings(rig.store, TASK, sha)

    assert pending["reviewer"] == "claude" and pending["candidate_sha"] == sha
    assert pending["contract_hash"] and pending["baseline_sha"] and pending["review_evidence_id"]
    history = finding_history(rig.store, TASK, pending["finding_id"])
    assert [item["event"] for item in history] == [FINDING_RECORDED]


def test_a_later_pass_from_another_reviewer_does_not_erase_the_adverse_finding(tmp_path: Path) -> None:
    rig, sha = _rig_with_adverse_review(tmp_path)
    second = FakeReviewAdapter("grok")  # a different independent reviewer that answers PASS
    rig.coordinator.reviewer = Reviewer(adapter=second, require_independent=True)
    rig.store.advance_task(TASK, Stage.REVIEW)  # the retry path puts the same candidate back in front of a reviewer

    rig.tick(1)

    assert second.calls == 0  # reviewers are not retried until one passes
    assert rig.stage == Stage.IMPLEMENT  # the finding is remediated instead
    assert _events(rig, "review.adverse_finding_unresolved")
    # Even a PASS that does exist (recorded through another path) cannot satisfy the review while the finding is open.
    Reviewer(adapter=second, require_independent=True).review(rig.store, TASK, sha, rig.project)
    assert [s for s, _ in rig.evidence(EvidenceKind.REVIEW)].count(EvidenceStatus.PASSED) == 1
    bound = rig.coordinator.reviewer and __import__("stagemesh.contract_binding", fromlist=["x"]).contract_for_candidate(rig.store, TASK, sha, rig.project)
    assert rig.coordinator._review_satisfied(TASK, sha, bound.digest) is False


def test_nothing_integrates_while_the_finding_is_open(tmp_path: Path) -> None:
    rig, sha = _rig_with_adverse_review(tmp_path)
    Reviewer(adapter=FakeReviewAdapter("grok"), require_independent=True).review(rig.store, TASK, sha, rig.project)
    rig.store.advance_task(TASK, Stage.INTEGRATE)

    rig.tick(1)

    assert rig.store.get_task(TASK)["status"] == TaskStatus.BLOCKED
    assert rig.evidence(EvidenceKind.INTEGRATION) == []
    assert "unresolved adverse review finding" in rig.store.conn.execute(
        "SELECT payload FROM audit_events WHERE event_type='task.block_classified' ORDER BY created_at DESC, rowid DESC LIMIT 1"
    ).fetchone()[0]


def test_an_explicit_evidenced_disposition_is_required_and_recorded(tmp_path: Path) -> None:
    rig, sha = _rig_with_adverse_review(tmp_path)
    finding_id = unresolved_adverse_review_findings(rig.store, TASK, sha)[0]["finding_id"]

    for bad in (dict(actor="", evidence_ref="pr#1", rationale="x"), dict(actor="owner", evidence_ref=" ", rationale="x"),
                dict(actor="owner", evidence_ref="pr#1", rationale="")):
        with pytest.raises(DispositionError):
            record_disposition(rig.store, TASK, finding_id, disposition="waived_by_owner", **bad)
    with pytest.raises(DispositionError):
        record_disposition(rig.store, TASK, finding_id, disposition="ignored", actor="owner", evidence_ref="pr#1", rationale="x")
    assert unresolved_adverse_review_findings(rig.store, TASK, sha)  # still unresolved

    record_disposition(
        rig.store, TASK, finding_id, disposition="refuted_with_evidence", actor="repo-owner",
        evidence_ref="https://example.invalid/pr/1#comment-9", rationale="the record exists in docs/design/impact.md",
    )

    assert not unresolved_adverse_review_findings(rig.store, TASK, sha)
    history = finding_history(rig.store, TASK, finding_id)
    assert [item["event"] for item in history] == [FINDING_RECORDED, FINDING_DISPOSITION]
    assert history[-1]["actor"] == "repo-owner" and history[-1]["evidence_ref"].startswith("https://")
    with pytest.raises(DispositionError):
        record_disposition(rig.store, TASK, finding_id, disposition="waived_by_owner", actor="a", evidence_ref="b", rationale="c")

    # With the disposition recorded, a PASS now counts and the task proceeds.
    second = FakeReviewAdapter("grok")
    rig.coordinator.reviewer = Reviewer(adapter=second, require_independent=True)
    rig.store.advance_task(TASK, Stage.REVIEW)
    rig.tick(1)
    assert second.calls == 1 and rig.stage == Stage.INTEGRATE


def test_a_remediated_candidate_needs_its_own_final_review(tmp_path: Path) -> None:
    rig, first = _rig_with_adverse_review(tmp_path)
    rig.coordinator.reviewer = Reviewer(adapter=FakeReviewAdapter("grok"), require_independent=True)

    rig.tick(3)  # remediation produces a new candidate, which is validated and then reviewed afresh

    latest = str(rig.store.latest_candidate(TASK)["sha"])
    reviewed = {row["candidate_sha"] for row in rig.store.conn.execute("SELECT candidate_sha FROM evidence WHERE kind='REVIEW' AND status='PASSED'")}
    if latest != first:
        assert reviewed <= {latest}  # the earlier candidate's review was never carried over to the remediation


def _authorize_recheck(rig: Rig, sha: str) -> None:
    """What a no-op remediation does: ask for one re-check of the candidate it declined to change."""
    from stagemesh.audit import record_audit

    record_audit(rig.store, "task.recheck_existing_candidate", {"task_id": TASK, "candidate_sha": sha, "from_stage": "IMPLEMENT", "to_stage": "REVIEW", "reason": "no_implementation_change"})
    rig.store.advance_task(TASK, Stage.REVIEW)


def test_the_reviewer_that_raised_a_finding_may_withdraw_it_on_the_one_authorized_recheck(tmp_path: Path) -> None:
    rig, sha = _rig_with_adverse_review(tmp_path)
    _authorize_recheck(rig, sha)
    same = FakeReviewAdapter("claude")  # the reviewer that raised the finding now passes the unchanged candidate
    rig.coordinator.reviewer = Reviewer(adapter=same, require_independent=True)

    rig.tick(1)

    assert same.calls == 1 and rig.stage == Stage.INTEGRATE
    finding_id = _events(rig, FINDING_RECORDED)[0]["finding_id"]
    withdrawn = finding_history(rig.store, TASK, finding_id)[-1]
    assert withdrawn["event"] == FINDING_DISPOSITION and withdrawn["disposition"] == "withdrawn_by_reviewer"
    assert withdrawn["actor"] == "claude" and withdrawn["evidence_ref"].startswith("review-evidence:")  # explicit, attributable, evidenced


def test_a_different_reviewers_pass_on_the_recheck_does_not_resolve_the_finding(tmp_path: Path) -> None:
    rig, sha = _rig_with_adverse_review(tmp_path)
    _authorize_recheck(rig, sha)
    other = FakeReviewAdapter("grok")
    rig.coordinator.reviewer = Reviewer(adapter=other, require_independent=True)

    rig.tick(1)

    assert other.calls == 1 and rig.stage != Stage.INTEGRATE
    assert unresolved_adverse_review_findings(rig.store, TASK, sha)  # still open: another reviewer cannot erase it
    rig.store.advance_task(TASK, Stage.REVIEW)
    rig.tick(1)
    assert other.calls == 1  # the single authorized re-check is spent; reviewers are not retried until one passes
