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


def _independent_pass(rig: Rig, sha: str, reviewer: str) -> str:
    """A passing review by another independent provider, recorded after the finding. Returns its evidence id."""
    Reviewer(adapter=FakeReviewAdapter(reviewer), require_independent=True).review(rig.store, TASK, sha, rig.project)
    return str(rig.store.conn.execute(
        "SELECT id FROM evidence WHERE task_id=? AND candidate_sha=? AND kind='REVIEW' AND status='PASSED' ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (TASK, sha),
    ).fetchone()[0])


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

    support = _independent_pass(rig, sha, "grok")
    record_disposition(
        rig.store, TASK, finding_id, disposition="refuted_with_evidence", actor="repo-owner",
        evidence_ref=[f"review-evidence:{support}", "https://example.invalid/pr/1#comment-9"],
        rationale="the record exists in docs/design/impact.md",
    )

    assert not unresolved_adverse_review_findings(rig.store, TASK, sha)
    history = finding_history(rig.store, TASK, finding_id)
    assert [item["event"] for item in history] == [FINDING_RECORDED, FINDING_DISPOSITION]
    decided = history[-1]
    assert decided["actor"] == "repo-owner" and decided["evidence_ref"].startswith("review-evidence:")
    assert decided["candidate_sha"] == sha and decided["raised_by"] == "claude" and decided["severity"]  # who, which candidate, against whom
    assert decided["support"] == "independent_evidence" and decided["independent_evidence_verified"] is True
    assert decided["evidence_resolved"][0]["provider"] == "grok" and decided["evidence_resolved"][0]["independent"] is True
    assert decided["implementers_checked"] and decided["original_message"]  # the adverse finding itself is still on record
    with pytest.raises(DispositionError):
        record_disposition(rig.store, TASK, finding_id, disposition="waived_by_owner", actor="a", evidence_ref="b", rationale="c")

    # With the disposition recorded, a PASS now counts and the task proceeds.
    second = FakeReviewAdapter("grok")
    rig.coordinator.reviewer = Reviewer(adapter=second, require_independent=True)
    rig.store.advance_task(TASK, Stage.REVIEW)
    rig.tick(1)
    assert rig.stage == Stage.INTEGRATE  # the independent PASS cited as evidence now counts (no further reviewer call needed)


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


def _finding(rig: Rig, sha: str) -> str:
    return unresolved_adverse_review_findings(rig.store, TASK, sha)[0]["finding_id"]


def test_an_implementer_cannot_dispose_of_findings_on_its_own_candidate(tmp_path: Path) -> None:
    rig, sha = _rig_with_adverse_review(tmp_path)
    finding_id = _finding(rig, sha)
    support = _independent_pass(rig, sha, "grok")
    implementer = str(rig.store.latest_candidate(TASK)["produced_by"])

    for who in (implementer, implementer.upper(), f"{implementer}:session-2", "agent", "stagemesh", "system"):
        with pytest.raises(DispositionError, match="implemented|automation"):
            record_disposition(
                rig.store, TASK, finding_id, disposition="waived_by_owner", actor=who,
                evidence_ref=f"review-evidence:{support}", rationale="the finding is wrong",
            )
    assert unresolved_adverse_review_findings(rig.store, TASK, sha)  # nothing changed
    assert [item["event"] for item in finding_history(rig.store, TASK, finding_id)] == [FINDING_RECORDED]


def test_a_provider_process_cannot_record_a_disposition(tmp_path: Path, monkeypatch) -> None:
    rig, sha = _rig_with_adverse_review(tmp_path)
    monkeypatch.setenv("STAGEMESH_PROVIDER_PROCESS", "1")
    with pytest.raises(DispositionError, match="provider process"):
        record_disposition(rig.store, TASK, _finding(rig, sha), disposition="waived_by_owner", actor="a-person", evidence_ref="note", rationale="x")
    assert unresolved_adverse_review_findings(rig.store, TASK, sha)


def test_refuting_a_material_finding_needs_independent_ledger_evidence(tmp_path: Path) -> None:
    rig, sha = _rig_with_adverse_review(tmp_path)
    finding_id = _finding(rig, sha)
    implementer = str(rig.store.latest_candidate(TASK)["produced_by"])
    own = _independent_pass(rig, sha, "claude")  # the reviewer that raised the finding: not independent support for refuting it
    validation = str(rig.store.conn.execute(
        "SELECT id FROM evidence WHERE task_id=? AND candidate_sha=? AND kind='VALIDATION' ORDER BY created_at DESC, rowid DESC LIMIT 1", (TASK, sha),
    ).fetchone()[0])

    for refs in (["I checked, it is fine"], ["https://example.invalid/pr/1"], [f"review-evidence:{own}"], [f"validation-evidence:{validation}"],
                 ["review-evidence:does-not-exist"]):
        with pytest.raises(DispositionError, match="independent evidence"):
            record_disposition(rig.store, TASK, finding_id, disposition="refuted_with_evidence", actor="repo-owner", evidence_ref=refs, rationale="x")
    assert unresolved_adverse_review_findings(rig.store, TASK, sha)

    # Evidence belonging to another candidate never counts, even from an independent reviewer.
    other = str(rig.store.add_evidence(TASK, "f" * 40, EvidenceKind.REVIEW, EvidenceStatus.PASSED,
                                       {"review_provider": "grok", "independent_reviewer": True, "implementer_provider": implementer}))
    with pytest.raises(DispositionError, match="independent evidence"):
        record_disposition(rig.store, TASK, finding_id, disposition="refuted_with_evidence", actor="repo-owner",
                           evidence_ref=f"review-evidence:{other}", rationale="x")


def test_an_owner_waiver_is_allowed_but_recorded_as_attestation_not_independent_evidence(tmp_path: Path) -> None:
    rig, sha = _rig_with_adverse_review(tmp_path)
    finding_id = _finding(rig, sha)

    payload = record_disposition(rig.store, TASK, finding_id, disposition="waived_by_owner", actor="repo-owner",
                                 evidence_ref="https://example.invalid/decision/7", rationale="accepted risk for this release")

    assert payload["support"] == "owner_attestation" and payload["independent_evidence_verified"] is False
    assert not unresolved_adverse_review_findings(rig.store, TASK, sha)
    recorded = _events(rig, FINDING_RECORDED)
    assert len(recorded) == 1 and recorded[0]["finding_id"] == finding_id  # the adverse finding is still there, unedited


def test_a_disposition_interrupted_before_the_close_is_completed_once_and_never_duplicated(tmp_path: Path) -> None:
    from stagemesh.audit import record_audit

    rig, sha = _rig_with_adverse_review(tmp_path)
    finding_id = _finding(rig, sha)
    record_audit(rig.store, FINDING_DISPOSITION, {"task_id": TASK, "finding_id": finding_id, "candidate_sha": sha, "disposition": "waived_by_owner",
                                                  "actor": "repo-owner", "evidence_ref": "x", "rationale": "r"})  # crash: audit written, finding not yet closed

    with pytest.raises(DispositionError, match="already has a recorded disposition"):
        record_disposition(rig.store, TASK, finding_id, disposition="waived_by_owner", actor="someone-else", evidence_ref="y", rationale="r2")

    assert not unresolved_adverse_review_findings(rig.store, TASK, sha)  # finished from the recorded decision
    assert len(_events(rig, FINDING_DISPOSITION)) == 1 and _events(rig, FINDING_DISPOSITION)[0]["actor"] == "repo-owner"


def test_the_provider_environment_marks_provider_processes() -> None:
    from stagemesh.git_identity import provider_environment

    assert provider_environment(None, {"PATH": "x"})["STAGEMESH_PROVIDER_PROCESS"] == "1"
