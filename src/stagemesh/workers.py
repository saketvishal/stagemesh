from __future__ import annotations

import time
from dataclasses import dataclass

from .domain import ProcessIdentity
from .persistence import Store


class WorkerValidationError(ValueError):
    pass


@dataclass(frozen=True)
class WorkerRecord:
    id: str
    provider: str
    capabilities: frozenset[str]
    heartbeat_at: float
    lease_expires_at: float
    identity: ProcessIdentity

    @property
    def lease_active(self) -> bool:
        return self.lease_expires_at >= time.time()


def register_worker(
    store: Store,
    worker_id: str,
    provider: str,
    capabilities: set[str],
    identity: ProcessIdentity,
    lease_seconds: float = 300,
) -> None:
    worker_id = _validate_text(worker_id, "worker id")
    provider = _validate_text(provider, "worker provider")
    capabilities = _validate_capabilities(capabilities)
    lease_seconds = _validate_lease_seconds(lease_seconds)
    now = time.time()
    store.upsert_worker(
        worker_id=worker_id,
        provider=provider,
        capabilities=sorted(capabilities),
        pid=identity.pid,
        process_create_time=identity.create_time,
        boot_id=identity.boot_id,
        executable=identity.executable,
        heartbeat_at=now,
        lease_expires_at=now + lease_seconds,
    )


def heartbeat_worker(store: Store, worker_id: str, lease_seconds: float = 300) -> None:
    worker_id = _validate_text(worker_id, "worker id")
    lease_seconds = _validate_lease_seconds(lease_seconds)
    now = time.time()
    store.heartbeat_worker(worker_id, now, now + lease_seconds)


def _validate_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkerValidationError(f"{field} must be a non-empty string")
    if len(value) > 200:
        raise WorkerValidationError(f"{field} must be 200 characters or fewer")
    return value.strip()


def _validate_capabilities(capabilities: set[str]) -> set[str]:
    if not capabilities:
        raise WorkerValidationError("worker capabilities must not be empty")
    normalized = {_validate_text(capability, "worker capability") for capability in capabilities}
    if len(normalized) != len(capabilities):
        raise WorkerValidationError("worker capabilities must be unique after trimming")
    return normalized


def _validate_lease_seconds(lease_seconds: float) -> float:
    if not isinstance(lease_seconds, (int, float)):
        raise WorkerValidationError("worker lease seconds must be numeric")
    if lease_seconds <= 0:
        raise WorkerValidationError("worker lease seconds must be positive")
    return lease_seconds
