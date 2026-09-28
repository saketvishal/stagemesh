from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .completion_audit import completion_audit


@dataclass(frozen=True)
class MatrixRow:
    area: str
    status: str
    evidence: str


def acceptance_matrix() -> dict[str, object]:
    audit = completion_audit()
    rows = [
        MatrixRow(str(item["requirement"]), str(item["status"]), str(item["evidence"]))
        for item in audit["items"]
    ]
    proven = sum(1 for row in rows if row.status == "PROVEN")
    return {
        "status": "COMPLETE" if proven == len(rows) else "INCOMPLETE",
        "proven": proven,
        "total": len(rows),
        "rows": [row.__dict__ for row in rows],
    }


def write_acceptance_matrix(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(acceptance_matrix(), indent=2, sort_keys=True), encoding="utf-8")
