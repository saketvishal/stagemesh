"""Report-only verifier for exact-SHA independent review evidence.

The approved requirement is an argument supplied by the caller. A task
approved for one independent review is checked for that one review. A task
approved for adverse review fails closed when that review is missing. The
document cannot raise or lower the requirement the caller approved.

Nothing here posts a GitHub status, edits branch protection, or authorizes a
merge. The signing key is an argument and is not read from the repository.
A caller that loads the approved requirement from the pull request, instead
of from the trusted base or task record, can be lied to.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

SCHEMA = "stagemesh.review-evidence.v1"
REPORT_MODE = "report_only"
REQUIREMENT_INDEPENDENT = "independent"
REQUIREMENT_ADVERSE = "adverse"
SUPPORTED_REQUIREMENTS = frozenset({REQUIREMENT_INDEPENDENT, REQUIREMENT_ADVERSE})
ROLE_NORMAL = "normal"
ROLE_ADVERSE = "adverse"
VERDICT_PASS = "PASS"
_REQUIRED_ROLES = {
    REQUIREMENT_INDEPENDENT: frozenset({ROLE_NORMAL}),
    REQUIREMENT_ADVERSE: frozenset({ROLE_NORMAL, ROLE_ADVERSE}),
}
_DOCUMENT_KEYS = frozenset({
    "schema", "repository", "task_id", "requirement", "head_sha", "candidate_sha",
    "contract_hash", "baseline_sha", "policy_digest", "implementer", "reviews",
})
_IDENTITY_KEYS = frozenset({"provider", "command_digest", "alias_digests", "execution_id"})
_REVIEW_KEYS = _IDENTITY_KEYS | frozenset({"verdict", "findings", "completed"})
_SHA40 = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REPOSITORY = re.compile(r"[^/\s]+/[^/\s]+\Z")
_TASK_ID = re.compile(r"[^\s]+\Z")


@dataclass(frozen=True)
class ReviewEvidenceReport:
    """A failed report never means the candidate may merge."""

    mode: str
    accepted: bool
    requirement: str
    failures: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "accepted": self.accepted,
            "requirement": self.requirement,
            "failures": list(self.failures),
        }


def canonical_bytes(document: Mapping[str, object]) -> bytes:
    """Canonical JSON covered by the HMAC. Key order and insignificant space do not matter."""
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")


def sign_review_evidence(document: Mapping[str, object], signing_key: bytes) -> dict[str, object]:
    """Return an envelope. The key stays with the caller; this stores nothing."""
    if not isinstance(signing_key, (bytes, bytearray)) or not bytes(signing_key):
        raise ValueError("signing key must be non-empty bytes")
    if not isinstance(document, Mapping):
        raise TypeError("document must be an object")
    body = dict(document)
    signature = hmac.new(bytes(signing_key), canonical_bytes(body), hashlib.sha256).hexdigest()
    return {"document": body, "signature": signature}


def verify_review_evidence(
    envelope: object,
    *,
    approved_requirement: str,
    expected_repository: str,
    expected_task_id: str,
    expected_head_sha: str,
    expected_candidate_sha: str,
    expected_contract_hash: str,
    expected_baseline_sha: str,
    expected_policy_digest: str,
    signing_key: bytes,
) -> ReviewEvidenceReport:
    """Verify one envelope. Bad input returns a failed report and does not raise."""
    failures: list[str] = []
    requirement = approved_requirement if isinstance(approved_requirement, str) else ""
    if requirement not in SUPPORTED_REQUIREMENTS:
        _add(failures, "unsupported_requirement")
    key = _signing_key(signing_key, failures)
    document = _document(envelope, key, failures)
    if document is None:
        return _report(requirement, failures)
    _binding(document, "repository", expected_repository, _REPOSITORY, "malformed_repository", "repository_mismatch", failures)
    _binding(document, "task_id", expected_task_id, _TASK_ID, "malformed_task", "task_mismatch", failures)
    _binding(document, "head_sha", expected_head_sha, _SHA40, "malformed_head_sha", "head_sha_mismatch", failures)
    _binding(
        document, "candidate_sha", expected_candidate_sha, _SHA40,
        "malformed_candidate_sha", "candidate_sha_mismatch", failures,
    )
    _binding(
        document, "contract_hash", expected_contract_hash, _SHA256,
        "malformed_contract_hash", "contract_hash_mismatch", failures,
    )
    _binding(
        document, "baseline_sha", expected_baseline_sha, _SHA40,
        "malformed_baseline_sha", "baseline_sha_mismatch", failures,
    )
    _binding(
        document, "policy_digest", expected_policy_digest, _SHA256,
        "malformed_policy_digest", "policy_digest_mismatch", failures,
    )
    claimed = document.get("requirement")
    if claimed != requirement:
        _add(failures, "requirement_mismatch")
    if requirement in SUPPORTED_REQUIREMENTS and "malformed_document" not in failures:
        _reviews(document, requirement, failures)
    return _report(requirement, failures)


def _report(requirement: str, failures: list[str]) -> ReviewEvidenceReport:
    return ReviewEvidenceReport(
        mode=REPORT_MODE,
        accepted=not failures,
        requirement=requirement,
        failures=tuple(failures),
    )


def _add(failures: list[str], code: str) -> None:
    if code not in failures:
        failures.append(code)


def _signing_key(signing_key: object, failures: list[str]) -> bytes:
    if isinstance(signing_key, (bytes, bytearray)) and bytes(signing_key):
        return bytes(signing_key)
    _add(failures, "missing_signing_key")
    return b""


def _document(envelope: object, key: bytes, failures: list[str]) -> dict[str, object] | None:
    if not isinstance(envelope, dict) or set(envelope) != {"document", "signature"}:
        _add(failures, "malformed_envelope")
        return None
    signature = envelope.get("signature")
    document = envelope.get("document")
    if not isinstance(document, dict):
        _add(failures, "malformed_document")
        return None
    if not isinstance(signature, str) or not signature:
        _add(failures, "missing_signature")
    elif key and not _signed(document, signature, key):
        _add(failures, "invalid_signature")
    if set(document) != _DOCUMENT_KEYS or document.get("schema") != SCHEMA:
        _add(failures, "malformed_document")
    return document


def _signed(document: Mapping[str, object], signature: str, key: bytes) -> bool:
    if _SHA256.fullmatch(signature) is None:
        return False
    try:
        expected = hmac.new(key, canonical_bytes(document), hashlib.sha256).hexdigest()
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, signature)


def _binding(
    document: Mapping[str, object],
    field: str,
    expected: object,
    pattern: re.Pattern[str],
    malformed: str,
    mismatch: str,
    failures: list[str],
) -> None:
    actual = document.get(field)
    if not isinstance(actual, str) or pattern.fullmatch(actual) is None:
        _add(failures, malformed)
    elif not isinstance(expected, str) or actual != expected:
        _add(failures, mismatch)


def _reviews(document: Mapping[str, object], requirement: str, failures: list[str]) -> None:
    reviews = document.get("reviews")
    implementer = _identity(document.get("implementer"), failures)
    if not isinstance(reviews, dict):
        _add(failures, "malformed_reviews")
        return
    expected_roles = _REQUIRED_ROLES[requirement]
    actual_roles = set(reviews)
    if ROLE_ADVERSE in expected_roles - actual_roles:
        _add(failures, "missing_adverse_review")
    if ROLE_NORMAL in expected_roles - actual_roles:
        _add(failures, "missing_normal_review")
    if actual_roles - expected_roles:
        _add(failures, "unexpected_review_roles")
    actors: list[tuple[str, _Actor]] = []
    if implementer is not None:
        actors.append(("implementer", implementer))
    for role in sorted(expected_roles & actual_roles):
        review = _review(reviews.get(role), failures)
        if review is not None:
            actors.append((role, review))
    _distinct(actors, failures)


@dataclass(frozen=True)
class _Actor:
    provider: str
    aliases: frozenset[str]
    execution_id: str


def _identity(value: object, failures: list[str]) -> _Actor | None:
    if not isinstance(value, dict) or set(value) != _IDENTITY_KEYS:
        _add(failures, "malformed_identity")
        return None
    return _actor(value, failures)


def _review(value: object, failures: list[str]) -> _Actor | None:
    if not isinstance(value, dict) or set(value) != _REVIEW_KEYS:
        _add(failures, "malformed_identity")
        return None
    if value.get("completed") is not True or value.get("verdict") != VERDICT_PASS:
        _add(failures, "incomplete_verdict")
    if value.get("findings") != []:
        _add(failures, "unresolved_findings")
    return _actor(value, failures)


def _alias_set(aliases: object) -> frozenset[str] | None:
    if not isinstance(aliases, list):
        return None
    items: list[str] = []
    for item in aliases:
        if not isinstance(item, str) or _SHA256.fullmatch(item) is None or item in items:
            return None
        items.append(item)
    return frozenset(items)


def _actor(value: Mapping[str, object], failures: list[str]) -> _Actor | None:
    provider = value.get("provider")
    digest = value.get("command_digest")
    execution_id = value.get("execution_id")
    aliases = _alias_set(value.get("alias_digests"))
    if not isinstance(provider, str) or not provider.strip():
        _add(failures, "malformed_identity")
        return None
    if (
        not isinstance(digest, str) or _SHA256.fullmatch(digest) is None
        or aliases is None or digest not in aliases
    ):
        _add(failures, "malformed_identity")
        return None
    if not isinstance(execution_id, str) or not execution_id.strip():
        _add(failures, "malformed_identity")
        return None
    return _Actor(provider.strip(), aliases, execution_id)


def _distinct(actors: list[tuple[str, _Actor]], failures: list[str]) -> None:
    execution_ids = [actor.execution_id for _, actor in actors]
    if len(execution_ids) != len(set(execution_ids)):
        _add(failures, "duplicate_execution")
    for index, (_, actor) in enumerate(actors):
        for _, other in actors[index + 1:]:
            if actor.provider.casefold() == other.provider.casefold():
                _add(failures, "same_provider")
            if actor.aliases & other.aliases:
                _add(failures, "shared_command_digest")
