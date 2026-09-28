from __future__ import annotations

import time
from dataclasses import dataclass

from .domain import ProcessIdentity
from .persistence import Store


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
    now = time.time()
    store.heartbeat_worker(worker_id, now, now + lease_seconds)
