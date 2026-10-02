from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from .persistence import Store


class RemediationValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Finding:
    identity: str
    candidate_sha: str
    severity: str
    message: str
    status: str = "OPEN"


def finding_identity(candidate_sha: str, message: str, path: str | None = None) -> str:
    candidate_sha = _validate_text(candidate_sha, "candidate sha")
    message = _validate_text(message, "finding message", 2000)
    path = _validate_optional_text(path, "finding path", 1000)
    raw = f"{candidate_sha}\0{path or ''}\0{message}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


class RemediationPolicy:
    def __init__(self, max_attempts: int = 3):
        if not isinstance(max_attempts, int):
            raise RemediationValidationError("remediation max attempts must be an integer")
        if max_attempts < 1:
            raise RemediationValidationError("remediation max attempts must be at least 1")
        self.max_attempts = max_attempts

    def should_remediate(self, store: Store, finding_id: str) -> bool:
        finding = store.get_finding(finding_id)
        if finding is None or finding["status"] != "OPEN":
            return False
        return store.remediation_attempt_count(finding_id) < self.max_attempts

    def record_attempt(self, store: Store, finding_id: str, status: str, payload: dict[str, object] | None = None) -> str:
        return store.add_remediation_attempt(finding_id, status, payload or {"attempted_at": time.time()})


def _validate_text(value: str, field: str, max_length: int = 200) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RemediationValidationError(f"{field} must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise RemediationValidationError(f"{field} must be {max_length} characters or fewer")
    return normalized


def _validate_optional_text(value: str | None, field: str, max_length: int = 200) -> str | None:
    if value is None:
        return None
    return _validate_text(value, field, max_length)
