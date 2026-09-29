from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .distributed import ACK_STATUSES, WorkPacketSnapshot, WorkQueue

ENVELOPE_VERSION = 1


class WorkTransportError(ValueError):
    pass


@dataclass(frozen=True)
class WorkAck:
    packet_id: str
    status: str
    payload: dict[str, object]


def packet_envelope(packet: WorkPacketSnapshot) -> dict[str, object]:
    return {
        "version": ENVELOPE_VERSION,
        "kind": "stagemesh.work_packet",
        "packet": {
            "id": packet.id,
            "task_id": packet.task_id,
            "stage": packet.stage,
            "worker_id": packet.worker_id,
            "candidate_sha": packet.candidate_sha,
            "status": packet.status,
            "payload": packet.payload,
            "created_at": packet.created_at,
            "updated_at": packet.updated_at,
        },
    }


def ack_envelope(packet_id: str, status: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    packet_id = _validate_text(packet_id, "work ack packet id")
    normalized = _validate_status(status)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise WorkTransportError("work ack payload must be an object")
    return {
        "version": ENVELOPE_VERSION,
        "kind": "stagemesh.work_ack",
        "ack": {
            "packet_id": packet_id,
            "status": normalized,
            "payload": payload,
        },
    }


def write_packet_envelope(packet: WorkPacketSnapshot, output: Path) -> dict[str, object]:
    data = packet_envelope(packet)
    _write_json(output, data)
    return data


def write_ack_envelope(packet_id: str, status: str, output: Path, payload: dict[str, object] | None = None) -> dict[str, object]:
    data = ack_envelope(packet_id, status, payload)
    _write_json(output, data)
    return data


def read_ack_envelope(path: Path) -> WorkAck:
    data = _read_json(path)
    if data.get("version") != ENVELOPE_VERSION:
        raise WorkTransportError("work ack envelope version is unsupported")
    if data.get("kind") != "stagemesh.work_ack":
        raise WorkTransportError("work ack envelope kind is unsupported")
    ack = data.get("ack")
    if not isinstance(ack, dict):
        raise WorkTransportError("work ack envelope must contain an ack object")
    packet_id = _validate_text(ack.get("packet_id"), "work ack packet id")
    status = _validate_status(ack.get("status"))
    payload = ack.get("payload", {})
    if not isinstance(payload, dict):
        raise WorkTransportError("work ack payload must be an object")
    return WorkAck(packet_id, status, payload)


def import_ack(queue: WorkQueue, path: Path) -> WorkAck:
    ack = read_ack_envelope(path)
    queue.ack(ack.packet_id, ack.status, ack.payload)
    return ack


def _write_json(output: Path, data: dict[str, object]) -> None:
    if not output.name:
        raise WorkTransportError("work transport output must name a file")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _read_json(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise WorkTransportError(f"work transport envelope is not readable: {path}") from exc
    except json.JSONDecodeError as exc:
        raise WorkTransportError("work transport envelope must be valid JSON") from exc
    if not isinstance(data, dict):
        raise WorkTransportError("work transport envelope must be an object")
    return data


def _validate_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkTransportError(f"{field} must be a non-empty string")
    if len(value.strip()) > 200:
        raise WorkTransportError(f"{field} must be 200 characters or fewer")
    return value.strip()


def _validate_status(value: object) -> str:
    status = _validate_text(value, "work ack status").upper()
    if status not in ACK_STATUSES:
        raise WorkTransportError(f"work ack status must be one of: {', '.join(sorted(ACK_STATUSES))}")
    return status
