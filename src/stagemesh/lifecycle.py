from __future__ import annotations

from dataclasses import dataclass

from .domain import EvidenceKind, EvidenceStatus, Stage


class LifecycleError(RuntimeError):
    pass


NEXT_STAGE = {
    Stage.PLAN: Stage.IMPLEMENT,
    Stage.IMPLEMENT: Stage.VALIDATE,
    Stage.VALIDATE: Stage.REVIEW,
    Stage.REVIEW: Stage.INTEGRATE,
    Stage.INTEGRATE: Stage.DONE,
}


@dataclass(frozen=True)
class StageDecision:
    current: Stage
    target: Stage
    reason: str


def next_stage(stage: Stage) -> Stage:
    return NEXT_STAGE.get(stage, Stage.DONE)


def evidence_allows_advance(
    *, current: Stage, candidate_sha: str | None, evidence_sha: str, kind: EvidenceKind, status: EvidenceStatus
) -> StageDecision:
    if status is not EvidenceStatus.PASSED:
        raise LifecycleError(f"{kind} evidence is not passing")
    if current in {Stage.VALIDATE, Stage.REVIEW, Stage.INTEGRATE}:
        if not candidate_sha:
            raise LifecycleError("exact candidate SHA is required")
        if evidence_sha != candidate_sha:
            raise LifecycleError("evidence for one SHA cannot advance another SHA")
    expected = {
        Stage.VALIDATE: EvidenceKind.VALIDATION,
        Stage.REVIEW: EvidenceKind.REVIEW,
        Stage.INTEGRATE: EvidenceKind.INTEGRATION,
    }.get(current)
    if expected and kind is not expected:
        raise LifecycleError(f"{kind} cannot advance {current}")
    return StageDecision(current=current, target=next_stage(current), reason=f"{kind} passed")
