from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .security import WorkspaceBoundary


class EndToEndAcceptanceValidationError(ValueError):
    pass


@dataclass(frozen=True)
class EndToEndRequirement:
    step: int
    requirement: str


END_TO_END_REQUIREMENTS = (
    EndToEndRequirement(1, "install StageMesh"),
    EndToEndRequirement(2, "initialize a synthetic project"),
    EndToEndRequirement(3, "discover tasks"),
    EndToEndRequirement(4, "generate and independently challenge the plan"),
    EndToEndRequirement(5, "revise a rejected plan once"),
    EndToEndRequirement(6, "accept the revised plan"),
    EndToEndRequirement(7, "surface the approval boundary"),
    EndToEndRequirement(8, "record accountable approval"),
    EndToEndRequirement(9, "execute the approved task"),
    EndToEndRequirement(10, "validate the exact candidate SHA"),
    EndToEndRequirement(11, "independently review the exact candidate SHA"),
    EndToEndRequirement(12, "interrupt active work"),
    EndToEndRequirement(13, "resume safely without duplicate execution"),
    EndToEndRequirement(14, "integrate the candidate locally"),
    EndToEndRequirement(15, "restart StageMesh after integration"),
    EndToEndRequirement(16, "prove completed work is not redispatched"),
    EndToEndRequirement(17, "produce an observable audit trace"),
    EndToEndRequirement(18, "reject a deliberately failing qualification"),
)


def end_to_end_acceptance(
    receipt: dict[str, Any] | Path | None = None,
    candidate_sha: str | None = None,
) -> dict[str, object]:
    """Report executable journey evidence, never static source-code claims."""

    payload, receipt_problem = _load_receipt(receipt)
    receipt_sha = payload.get("candidate_sha") if payload else None
    if candidate_sha is not None and receipt_sha != candidate_sha:
        receipt_problem = "receipt candidate does not match the requested candidate"
    if payload and payload.get("status") != "PASS":
        receipt_problem = "development journey did not pass"
    if payload and payload.get("source_dirty") is not False:
        receipt_problem = "development journey did not run from a clean candidate"

    observed = _observed_steps(payload)
    steps: list[dict[str, object]] = []
    for requirement in END_TO_END_REQUIREMENTS:
        row = observed.get(requirement.step)
        proven = (
            receipt_problem is None
            and row is not None
            and row.get("status") == "PASS"
            and isinstance(row.get("evidence"), str)
            and bool(row["evidence"].strip())
        )
        steps.append(
            {
                "step": requirement.step,
                "requirement": requirement.requirement,
                "status": "PROVEN" if proven else "NOT_RUN",
                "evidence": str(row.get("evidence", "")) if proven and row else "",
            }
        )
    proven_count = sum(step["status"] == "PROVEN" for step in steps)
    complete = receipt_problem is None and proven_count == len(END_TO_END_REQUIREMENTS)
    return {
        "status": "COMPLETE" if complete else "INCOMPLETE",
        "proven": proven_count,
        "total": len(END_TO_END_REQUIREMENTS),
        "candidate_sha": receipt_sha,
        "receipt_problem": None if complete else receipt_problem or "one or more journey steps did not pass",
        "steps": steps,
    }


def write_end_to_end_acceptance(
    path: Path,
    root: Path | None = None,
    *,
    receipt: dict[str, Any] | Path | None = None,
    candidate_sha: str | None = None,
) -> None:
    path = _validate_output(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(end_to_end_acceptance(receipt, candidate_sha), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _load_receipt(receipt: dict[str, Any] | Path | None) -> tuple[dict[str, Any] | None, str | None]:
    if receipt is None:
        return None, "development journey has not been executed"
    if isinstance(receipt, Path):
        try:
            loaded = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None, "development journey receipt is unreadable"
    else:
        loaded = receipt
    if not isinstance(loaded, dict) or loaded.get("schema_version") != 1:
        return None, "development journey receipt is malformed"
    if not isinstance(loaded.get("candidate_sha"), str) or re.fullmatch(r"[0-9a-f]{7,40}", loaded["candidate_sha"]) is None:
        return None, "development journey receipt has no candidate SHA"
    if not isinstance(loaded.get("steps"), list):
        return None, "development journey receipt has no step evidence"
    return loaded, None


def _observed_steps(payload: dict[str, Any] | None) -> dict[int, dict[str, Any]]:
    if payload is None:
        return {}
    observed: dict[int, dict[str, Any]] = {}
    for row in payload.get("steps", []):
        if not isinstance(row, dict) or not isinstance(row.get("step"), int):
            continue
        if row["step"] in observed:
            continue
        observed[row["step"]] = row
    return observed


def _validate_output(path: Path, root: Path | None) -> Path:
    output = Path(path).resolve()
    if root is not None:
        return WorkspaceBoundary(Path(root).resolve()).require_inside(output)
    if not output.name or output.is_dir():
        raise EndToEndAcceptanceValidationError("end-to-end acceptance output must name a file")
    return output
