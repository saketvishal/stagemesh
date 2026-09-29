from __future__ import annotations

import json
from dataclasses import dataclass

from .domain import Stage
from .persistence import Store


class WorkQueueError(ValueError):
    pass


ACK_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED"}


@dataclass(frozen=True)
class WorkPacket:
    id: str
    task_id: str
    stage: str
    candidate_sha: str | None
    payload: dict[str, object]


@dataclass(frozen=True)
class WorkPacketSnapshot:
    id: str
    task_id: str
    stage: str
    worker_id: str | None
    candidate_sha: str | None
    status: str
    payload: dict[str, object]
    created_at: float
    updated_at: float


class WorkQueue:
    def __init__(self, store: Store):
        self.store = store

    def enqueue(
        self, task_id: str, stage: str, worker_id: str | None = None, candidate_sha: str | None = None
    ) -> str:
        task_id = _validate_text(task_id, "work packet task id")
        stage = _validate_stage(stage)
        worker_id = _validate_optional_text(worker_id, "work packet worker id")
        candidate_sha = _validate_optional_text(candidate_sha, "work packet candidate sha")
        return self.store.enqueue_work(task_id, stage, worker_id, candidate_sha, {})

    def poll(self, worker_id: str, limit: int = 1, lease_seconds: float = 300) -> list[WorkPacket]:
        worker_id = _validate_text(worker_id, "work packet worker id")
        limit = _validate_limit(limit)
        lease_seconds = _validate_lease_seconds(lease_seconds)
        return [
            WorkPacket(row["id"], row["task_id"], row["stage"], row["candidate_sha"], json.loads(row["payload"]))
            for row in self.store.claim_work_packets(worker_id, limit, lease_seconds)
        ]

    def renew(self, packet_id: str, worker_id: str) -> bool:
        packet_id = _validate_text(packet_id, "work packet id")
        worker_id = _validate_text(worker_id, "work packet worker id")
        return self.store.renew_work_packet(packet_id, worker_id)

    def ack(self, packet_id: str, status: str, payload: dict[str, object] | None = None) -> None:
        packet_id = _validate_text(packet_id, "work packet id")
        normalized = status.upper()
        if normalized not in ACK_STATUSES:
            raise WorkQueueError(f"work packet ack status must be one of: {', '.join(sorted(ACK_STATUSES))}")
        if not self.store.ack_work_packet(packet_id, normalized, payload or {}):
            raise WorkQueueError(f"work packet is not claimed or does not exist: {packet_id}")

    def list(self) -> list[WorkPacketSnapshot]:
        return [
            WorkPacketSnapshot(
                row["id"],
                row["task_id"],
                row["stage"],
                row["worker_id"],
                row["candidate_sha"],
                row["status"],
                json.loads(row["payload"]),
                row["created_at"],
                row["updated_at"],
            )
            for row in self.store.work_packets()
        ]


def _validate_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkQueueError(f"{field} must be a non-empty string")
    if len(value) > 200:
        raise WorkQueueError(f"{field} must be 200 characters or fewer")
    return value.strip()


def _validate_optional_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    return _validate_text(value, field)


def _validate_stage(stage: str) -> str:
    stage = _validate_text(stage, "work packet stage")
    if stage not in {str(item) for item in Stage}:
        raise WorkQueueError(f"work packet stage is unsupported: {stage}")
    return stage


def _validate_limit(limit: int) -> int:
    if not isinstance(limit, int):
        raise WorkQueueError("work packet poll limit must be an integer")
    if limit < 1:
        raise WorkQueueError("work packet poll limit must be at least 1")
    if limit > 100:
        raise WorkQueueError("work packet poll limit must be 100 or fewer")
    return limit


def _validate_lease_seconds(lease_seconds: float) -> float:
    if not isinstance(lease_seconds, (int, float)):
        raise WorkQueueError("work packet lease seconds must be numeric")
    if lease_seconds <= 0:
        raise WorkQueueError("work packet lease seconds must be positive")
    return lease_seconds
