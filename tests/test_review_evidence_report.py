"""Report-only exact-SHA review evidence. These tests do not call a provider."""

from __future__ import annotations

import hashlib
from pathlib import Path

from stagemesh.review_evidence_report import (
    SCHEMA,
    sign_review_evidence,
    verify_review_evidence,
)

HEAD = "a" * 40
OTHER_HEAD = "b" * 40
CANDIDATE = "c" * 40
OTHER_CANDIDATE = "d" * 40
BASELINE = "e" * 40
CONTRACT = "f" * 64
OTHER_CONTRACT = "1" * 64
POLICY = "2" * 64
KEY = b"prototype-test-key"
IMPLEMENTER_DIGEST = hashlib.sha256(b"implementer").hexdigest()
NORMAL_DIGEST = hashlib.sha256(b"normal").hexdigest()
ADVERSE_DIGEST = hashlib.sha256(b"adverse").hexdigest()


def _identity(
    provider: str, digest: str, execution_id: str, aliases: list[str] | None = None,
) -> dict[str, object]:
    return {
        "provider": provider,
        "command_digest": digest,
        "alias_digests": [digest] if aliases is None else aliases,
        "execution_id": execution_id,
    }


def _review(
    provider: str,
    digest: str,
    execution_id: str,
    *,
    verdict: str = "PASS",
    findings: list[object] | None = None,
    completed: bool = True,
) -> dict[str, object]:
    return {
        **_identity(provider, digest, execution_id),
        "verdict": verdict,
        "findings": [] if findings is None else findings,
        "completed": completed,
    }


def _document(
    requirement: str = "independent",
    reviews: dict[str, object] | None = None,
    **overrides: object,
) -> dict[str, object]:
    if reviews is None:
        reviews = {"normal": _review("claude", NORMAL_DIGEST, "normal-1")}
    document: dict[str, object] = {
        "schema": SCHEMA,
        "repository": "saketvishal/stagemesh",
        "task_id": "808",
        "requirement": requirement,
        "head_sha": HEAD,
        "candidate_sha": CANDIDATE,
        "contract_hash": CONTRACT,
        "baseline_sha": BASELINE,
        "policy_digest": POLICY,
        "implementer": _identity("agy", IMPLEMENTER_DIGEST, "impl-1"),
        "reviews": reviews,
    }
    document.update(overrides)
    return document


def _verify(
    document: dict[str, object] | None = None,
    *,
    requirement: str = "independent",
    key: bytes = KEY,
    envelope: object | None = None,
    expected_head_sha: str = HEAD,
    expected_candidate_sha: str = CANDIDATE,
    expected_contract_hash: str = CONTRACT,
):
    body = _document(requirement) if document is None else document
    signed = sign_review_evidence(body, key) if envelope is None else envelope
    return verify_review_evidence(
        signed,
        approved_requirement=requirement,
        expected_repository="saketvishal/stagemesh",
        expected_task_id="808",
        expected_head_sha=expected_head_sha,
        expected_candidate_sha=expected_candidate_sha,
        expected_contract_hash=expected_contract_hash,
        expected_baseline_sha=BASELINE,
        expected_policy_digest=POLICY,
        signing_key=key,
    )


def _adverse_document(**review_overrides: object) -> dict[str, object]:
    adverse = _review("human-not-used", ADVERSE_DIGEST, "adverse-1")
    adverse.update(review_overrides)
    return _document("adverse", {
        "normal": _review("claude", NORMAL_DIGEST, "normal-1"),
        "adverse": adverse,
    })


def test_one_independent_review_passes_without_a_third_provider() -> None:
    report = _verify()
    assert report.mode == "report_only"
    assert report.accepted is True
    assert report.requirement == "independent"
    assert report.failures == ()
    assert "missing_adverse_review" not in report.failures
    assert report.to_dict()["accepted"] is True


def test_independent_evidence_cannot_silently_become_a_three_provider_task() -> None:
    document = _document("independent", {
        "normal": _review("claude", NORMAL_DIGEST, "normal-1"),
        "adverse": _review("other", ADVERSE_DIGEST, "adverse-1"),
    })
    report = _verify(document)
    assert report.accepted is False
    assert "unexpected_review_roles" in report.failures
    assert "missing_adverse_review" not in report.failures


def test_document_cannot_upgrade_an_independent_task_to_adverse() -> None:
    report = _verify(_document("adverse", {
        "normal": _review("claude", NORMAL_DIGEST, "normal-1"),
        "adverse": _review("other", ADVERSE_DIGEST, "adverse-1"),
    }))
    assert report.accepted is False
    assert report.failures == ("requirement_mismatch", "unexpected_review_roles")


def test_adverse_requirement_fails_closed_without_the_adverse_review() -> None:
    document = _document("adverse", {"normal": _review("claude", NORMAL_DIGEST, "normal-1")})
    report = _verify(document, requirement="adverse")
    assert report.accepted is False
    assert "missing_adverse_review" in report.failures


def test_document_cannot_downgrade_an_adverse_task() -> None:
    report = _verify(_document("independent"), requirement="adverse")
    assert report.accepted is False
    assert "requirement_mismatch" in report.failures
    assert "missing_adverse_review" in report.failures


def test_completed_adverse_pair_from_distinct_identities_passes() -> None:
    report = _verify(_adverse_document(), requirement="adverse")
    assert report.accepted is True
    assert report.failures == ()
    assert report.mode == "report_only"


def test_same_provider_and_shared_command_digest_fail() -> None:
    same = _verify(_adverse_document(provider="claude"), requirement="adverse")
    assert same.accepted is False
    assert "same_provider" in same.failures
    shared = _adverse_document()
    shared_review = shared["reviews"]["adverse"]
    assert isinstance(shared_review, dict)
    shared_review["alias_digests"] = [ADVERSE_DIGEST, NORMAL_DIGEST]
    report = _verify(shared, requirement="adverse")
    assert report.accepted is False
    assert "shared_command_digest" in report.failures


def test_casefolded_provider_names_are_the_same_actor() -> None:
    report = _verify(_document(reviews={"normal": _review("AGY", NORMAL_DIGEST, "normal-1")}))
    assert report.accepted is False
    assert "same_provider" in report.failures


def test_a_valid_signature_for_another_head_or_candidate_does_not_pass() -> None:
    head = _verify(expected_head_sha=OTHER_HEAD)
    candidate = _verify(expected_candidate_sha=OTHER_CANDIDATE)
    contract = _verify(expected_contract_hash=OTHER_CONTRACT)
    assert head.accepted is False and "head_sha_mismatch" in head.failures
    assert candidate.accepted is False and "candidate_sha_mismatch" in candidate.failures
    assert contract.accepted is False and "contract_hash_mismatch" in contract.failures
    assert "invalid_signature" not in head.failures


def test_editing_the_signed_candidate_invalidates_the_signature() -> None:
    envelope = sign_review_evidence(_document(), KEY)
    document = envelope["document"]
    assert isinstance(document, dict)
    document["candidate_sha"] = OTHER_CANDIDATE
    report = _verify(envelope=envelope, expected_candidate_sha=OTHER_CANDIDATE)
    assert report.accepted is False
    assert "invalid_signature" in report.failures


def test_missing_or_wrong_key_fails_closed() -> None:
    missing = verify_review_evidence(
        {"document": _document(), "signature": ""},
        approved_requirement="independent",
        expected_repository="saketvishal/stagemesh",
        expected_task_id="808",
        expected_head_sha=HEAD,
        expected_candidate_sha=CANDIDATE,
        expected_contract_hash=CONTRACT,
        expected_baseline_sha=BASELINE,
        expected_policy_digest=POLICY,
        signing_key=b"",
    )
    wrong = _verify(key=b"other-key", envelope=sign_review_evidence(_document(), KEY))
    assert missing.accepted is False
    assert "missing_signing_key" in missing.failures
    assert "missing_signature" in missing.failures
    assert wrong.accepted is False and "invalid_signature" in wrong.failures


def test_incomplete_cancelled_and_non_pass_verdicts_never_pass() -> None:
    for verdict, completed in (
        ("REVIEW_INCOMPLETE", True),
        ("CAPACITY", True),
        ("FAIL", True),
        ("PASS", False),
    ):
        report = _verify(_document(reviews={
            "normal": _review("claude", NORMAL_DIGEST, "normal-1", verdict=verdict, completed=completed),
        }))
        assert report.accepted is False
        assert "incomplete_verdict" in report.failures


def test_pass_with_findings_is_not_complete() -> None:
    report = _verify(_document(reviews={
        "normal": _review(
            "claude", NORMAL_DIGEST, "normal-1",
            findings=[{"severity": "error", "message": "open"}],
        ),
    }))
    assert report.accepted is False
    assert "unresolved_findings" in report.failures


def test_reordered_json_keeps_a_valid_signature() -> None:
    document = _document()
    envelope = sign_review_evidence(document, KEY)
    reversed_document = {key: document[key] for key in reversed(tuple(document))}
    envelope["document"] = reversed_document
    report = _verify(envelope=envelope)
    assert report.accepted is True


def test_human_requirement_is_not_an_active_fallback() -> None:
    report = _verify(_document("independent"), requirement="human")
    assert report.accepted is False
    assert report.failures == ("unsupported_requirement", "requirement_mismatch")
    assert report.mode == "report_only"


def test_duplicate_execution_id_fails() -> None:
    report = _verify(_document(reviews={"normal": _review("claude", NORMAL_DIGEST, "impl-1")}))
    assert report.accepted is False
    assert "duplicate_execution" in report.failures


def test_verifier_is_not_wired_into_commands_or_workflows() -> None:
    root = Path(__file__).resolve().parents[1]
    cli = (root / "src" / "stagemesh" / "cli.py").read_text(encoding="utf-8")
    coordinator = (root / "src" / "stagemesh" / "coordinator.py").read_text(encoding="utf-8")
    workflows = sorted((root / ".github" / "workflows").glob("*.yml"))
    assert "review_evidence_report" not in cli
    assert "review_evidence_report" not in coordinator
    assert workflows
    assert all("review_evidence_report" not in path.read_text(encoding="utf-8") for path in workflows)
