from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from .persistence import Store


@dataclass(frozen=True)
class Finding:
    identity: str
    candidate_sha: str
    severity: str
    message: str
    status: str = "OPEN"


def finding_identity(candidate_sha: str, message: str, path: str | None = None) -> str:
    raw = f"{candidate_sha}\0{path or ''}\0{message}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


class RemediationPolicy:
    def __init__(self, max_attempts: int = 3):
        self.max_attempts = max_attempts

    def should_remediate(self, store: Store, finding_id: str) -> bool:
        finding = store.get_finding(finding_id)
        if finding is None or finding["status"] != "OPEN":
            return False
        return store.remediation_attempt_count(finding_id) < self.max_attempts

    def record_attempt(self, store: Store, finding_id: str, status: str, payload: dict[str, object] | None = None) -> str:
        return store.add_remediation_attempt(finding_id, status, payload or {"attempted_at": time.time()})
