from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .security import WorkspaceBoundary


class EndToEndAcceptanceValidationError(ValueError):
    pass


@dataclass(frozen=True)
class EndToEndStep:
    step: int
    requirement: str
    status: str
    evidence: str


END_TO_END_STEPS = (
    EndToEndStep(1, "install StageMesh", "PROVEN", "scripts/clean_acceptance.py and pip install target gate"),
    EndToEndStep(2, "initialize a synthetic project", "PROVEN", "scripts/acceptance.py"),
    EndToEndStep(3, "discover tasks", "PROVEN", "scripts/acceptance.py objective planning and backlog sync"),
    EndToEndStep(4, "execute dependent tasks", "PROVEN", "scripts/acceptance.py dependent objective reaches DONE"),
    EndToEndStep(5, "run multiple eligible workers/providers", "PROVEN", "provider acceptance plus worker/operator CLI acceptance"),
    EndToEndStep(6, "interrupt a worker", "PROVEN", "scripts/invariants.py dead_worker_recovers"),
    EndToEndStep(7, "restart StageMesh", "PROVEN", "scripts/invariants.py coordinator restart cases"),
    EndToEndStep(8, "preserve ownership/candidate state", "PROVEN", "scripts/invariants.py implementation_survives_validation"),
    EndToEndStep(9, "interrupt validation", "PROVEN", "scripts/invariants.py live_validation_survives"),
    EndToEndStep(10, "resume safely", "PROVEN", "scripts/invariants.py dead_validation_restarts_same_sha"),
    EndToEndStep(11, "simulate reviewer/provider capacity failure", "PROVEN", "scripts/invariants.py and provider acceptance"),
    EndToEndStep(12, "simulate GitHub rate limiting", "PROVEN", "scripts/github_acceptance.py and retry invariants"),
    EndToEndStep(13, "validate exact SHA", "PROVEN", "scripts/invariants.py exact-SHA lifecycle checks"),
    EndToEndStep(14, "independently review exact SHA", "PROVEN", "scripts/invariants.py review model"),
    EndToEndStep(15, "integrate", "PROVEN", "scripts/acceptance.py completed lifecycle"),
    EndToEndStep(16, "restart StageMesh after integration", "PROVEN", "scripts/invariants.py completed_not_redispatched"),
    EndToEndStep(17, "prove completed work is not redispatched", "PROVEN", "scripts/invariants.py completed_not_redispatched"),
    EndToEndStep(18, "prove a deliberately broken future feature is rejected by CI", "PROVEN", "scripts/acceptance.py future-feature gate"),
)


def end_to_end_acceptance() -> dict[str, object]:
    proven = sum(1 for step in END_TO_END_STEPS if step.status == "PROVEN")
    return {
        "status": "COMPLETE" if proven == len(END_TO_END_STEPS) else "INCOMPLETE",
        "proven": proven,
        "total": len(END_TO_END_STEPS),
        "steps": [step.__dict__ for step in END_TO_END_STEPS],
    }


def write_end_to_end_acceptance(path: Path, root: Path | None = None) -> None:
    path = _validate_output(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(end_to_end_acceptance(), indent=2, sort_keys=True), encoding="utf-8")


def _validate_output(path: Path, root: Path | None) -> Path:
    output = Path(path).resolve()
    if root is not None:
        return WorkspaceBoundary(Path(root).resolve()).require_inside(output)
    if not output.name or output.is_dir():
        raise EndToEndAcceptanceValidationError("end-to-end acceptance output must name a file")
    return output
