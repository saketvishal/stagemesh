from __future__ import annotations

from dataclasses import dataclass

from .persistence import Store


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
    return store.add_external_evidence(kind, status, url, candidate_sha, notes)


def external_evidence_records(store: Store) -> list[ExternalEvidenceRecord]:
    return [
        ExternalEvidenceRecord(
            id=row["id"],
            kind=row["kind"],
            status=row["status"],
            url=row["url"],
            candidate_sha=row["candidate_sha"],
            notes=row["notes"],
        )
        for row in store.external_evidence()
    ]
