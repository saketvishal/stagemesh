from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from .persistence import Store
from .release import ReleaseValidationError, validate_candidate_sha


class ExternalEvidenceValidationError(ValueError):
    pass


EXTERNAL_EVIDENCE_KINDS = {"hosted-ci", "live-github", "live-provider", "postgres"}
EXTERNAL_EVIDENCE_STATUSES = {"PASS", "FAIL"}


@dataclass(frozen=True)
class ExternalEvidenceRecord:
    id: str
    kind: str
    status: str
    url: str
    candidate_sha: str | None
    notes: str


def record_external_evidence(
    store: Store,
    kind: str,
    status: str,
    url: str,
    candidate_sha: str | None = None,
    notes: str = "",
) -> str:
    normalized_kind = _validate_kind(kind)
    normalized_status = _validate_status(status)
    normalized_url = _validate_url(url)
    if normalized_status == "PASS" and not candidate_sha:
        raise ExternalEvidenceValidationError("passing external evidence must include a candidate SHA")
    try:
        normalized_sha = validate_candidate_sha(candidate_sha) if candidate_sha else None
    except ReleaseValidationError as exc:
        raise ExternalEvidenceValidationError(str(exc)) from exc
    return store.add_external_evidence(normalized_kind, normalized_status, normalized_url, normalized_sha, notes)


def external_evidence_records(store: Store) -> list[ExternalEvidenceRecord]:
    records: list[ExternalEvidenceRecord] = []
    seen: set[tuple[str, str, str, str | None, str]] = set()
    for row in store.external_evidence():
        key = (row["kind"], row["status"], row["url"], row["candidate_sha"], row["notes"])
        if key in seen:
            continue
        seen.add(key)
        records.append(
            ExternalEvidenceRecord(
                id=row["id"],
                kind=row["kind"],
                status=row["status"],
                url=row["url"],
                candidate_sha=row["candidate_sha"],
                notes=row["notes"],
            )
        )
    return records


def _validate_kind(kind: str) -> str:
    if kind not in EXTERNAL_EVIDENCE_KINDS:
        raise ExternalEvidenceValidationError(f"external evidence kind must be one of: {', '.join(sorted(EXTERNAL_EVIDENCE_KINDS))}")
    return kind


def _validate_status(status: str) -> str:
    normalized = status.upper() if isinstance(status, str) else ""
    if normalized not in EXTERNAL_EVIDENCE_STATUSES:
        raise ExternalEvidenceValidationError(f"external evidence status must be one of: {', '.join(sorted(EXTERNAL_EVIDENCE_STATUSES))}")
    return normalized


def _validate_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ExternalEvidenceValidationError("external evidence url must be an absolute http(s) URL")
    return url
