from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .completion_audit import completion_audit
from .persistence import Store
from .security import WorkspaceBoundary


class AcceptanceMatrixValidationError(ValueError):
    pass


@dataclass(frozen=True)
class MatrixRow:
    area: str
    status: str
    evidence: str


def acceptance_matrix(store: Store | None = None) -> dict[str, object]:
    audit = completion_audit(store)
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


def write_acceptance_matrix(path: Path, store: Store | None = None, root: Path | None = None) -> None:
    path = _validate_output(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(acceptance_matrix(store), indent=2, sort_keys=True), encoding="utf-8")


def _validate_output(path: Path, root: Path | None) -> Path:
    output = Path(path).resolve()
    if root is not None:
        return WorkspaceBoundary(Path(root).resolve()).require_inside(output)
    if not output.name or output.is_dir():
        raise AcceptanceMatrixValidationError("acceptance matrix output must name a file")
    return output
