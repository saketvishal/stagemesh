from __future__ import annotations

import json
from dataclasses import dataclass

from .persistence import Store


@dataclass(frozen=True)
class WorkPacket:
    id: str
    task_id: str
    stage: str
    candidate_sha: str | None
    payload: dict[str, object]


class WorkQueue:
    def __init__(self, store: Store):
        self.store = store

    def enqueue(
        self, task_id: str, stage: str, worker_id: str | None = None, candidate_sha: str | None = None
    ) -> str:
        return self.store.enqueue_work(task_id, stage, worker_id, candidate_sha, {})

    def poll(self, worker_id: str, limit: int = 1, lease_seconds: float = 300) -> list[WorkPacket]:
        return [
            WorkPacket(row["id"], row["task_id"], row["stage"], row["candidate_sha"], json.loads(row["payload"]))
            for row in self.store.claim_work_packets(worker_id, limit, lease_seconds)
        ]

    def renew(self, packet_id: str, worker_id: str) -> bool:
        return self.store.renew_work_packet(packet_id, worker_id)

    def ack(self, packet_id: str, status: str, payload: dict[str, object] | None = None) -> None:
        self.store.ack_work_packet(packet_id, status, payload or {})
