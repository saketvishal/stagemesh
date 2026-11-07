"""Provider-neutral governance for project-owned visual comparison inputs and evidence."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from .security import WorkspaceBoundary

MAX_VISUAL_INPUTS = 64
MAX_VISUAL_ARTIFACTS = 96
MAX_VISUAL_INPUT_SIZE = 50 * 1024 * 1024
VISUAL_INPUT_ROLES = {"reference", "fixture", "tolerance", "gate", "package"}
VISUAL_ARTIFACT_ROLES = {"actual", "reference", "diff"}
_UPDATE_MODES = (
    "--update-snapshot",
    "update-snapshots",
    "update-golden",
    "update-goldens",
    "refresh-baseline",
    "approve-baseline",
    "pw_update_snapshots",
    "bless",
)


class VisualReferenceError(ValueError):
    pass


class VisualResultError(VisualReferenceError):
    def __init__(self, message: str, *, failure_kind: str):
        super().__init__(message)
        self.failure_kind = failure_kind


@dataclass(frozen=True)
class VisualInput:
    path: str
    role: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "role": self.role}


@dataclass(frozen=True)
class VisualArtifact:
    requirement_id: str
    role: str
    path: str
    content_type: str = "application/octet-stream"

    def to_dict(self) -> dict[str, str]:
        return {
            "requirement_id": self.requirement_id,
            "role": self.role,
            "path": self.path,
            "content_type": self.content_type,
        }


@dataclass(frozen=True)
class VisualComparison:
    algorithm: str
    max_difference_ratio: float
    stability_runs: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "max_difference_ratio": self.max_difference_ratio,
            "stability_runs": self.stability_runs,
        }


@dataclass(frozen=True)
class VisualReferencePolicy:
    revision: str
    inputs: tuple[VisualInput, ...]
    artifacts: tuple[VisualArtifact, ...]
    comparison: VisualComparison
    functional_gates: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "inputs": [item.to_dict() for item in self.inputs],
            "artifacts": [item.to_dict() for item in self.artifacts],
            "comparison": self.comparison.to_dict(),
            "functional_gates": list(self.functional_gates),
        }


@dataclass(frozen=True)
class VisualInputSnapshot:
    path: str
    role: str
    sha256: str
    size: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "role": self.role,
            "sha256": self.sha256,
            "size": self.size,
        }


def parse_visual_reference(
    value: object,
    *,
    requirement_ids: tuple[str, ...],
) -> VisualReferencePolicy | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise VisualReferenceError("visual_reference must be an object")
    allowed = {"revision", "inputs", "artifacts", "comparison", "functional_gates"}
    unknown = set(value) - allowed
    if unknown:
        raise VisualReferenceError("visual_reference contains unknown fields")
    revision = value.get("revision")
    if not isinstance(revision, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", revision):
        raise VisualReferenceError("visual_reference revision must be a bounded identifier")

    raw_inputs = value.get("inputs")
    if not isinstance(raw_inputs, list) or not 1 <= len(raw_inputs) <= MAX_VISUAL_INPUTS:
        raise VisualReferenceError("visual_reference inputs must be a bounded non-empty list")
    inputs: list[VisualInput] = []
    for item in raw_inputs:
        if not isinstance(item, dict) or set(item) != {"path", "role"}:
            raise VisualReferenceError("visual_reference input needs exactly path and role")
        path, role = item.get("path"), item.get("role")
        if not isinstance(path, str) or not _safe_relative(path):
            raise VisualReferenceError("visual_reference input path must stay inside the service workspace")
        if role not in VISUAL_INPUT_ROLES:
            raise VisualReferenceError("visual_reference input role is unsupported")
        inputs.append(VisualInput(path=path, role=role))
    if len({item.path for item in inputs}) != len(inputs):
        raise VisualReferenceError("visual_reference input paths must be unique")
    if {item.role for item in inputs} != VISUAL_INPUT_ROLES:
        raise VisualReferenceError("visual_reference must declare reference, fixture, tolerance, gate, and package inputs")

    raw_artifacts = value.get("artifacts")
    if not isinstance(raw_artifacts, list) or not 1 <= len(raw_artifacts) <= MAX_VISUAL_ARTIFACTS:
        raise VisualReferenceError("visual_reference artifacts must be a bounded non-empty list")
    artifacts: list[VisualArtifact] = []
    for item in raw_artifacts:
        if not isinstance(item, dict) or not {"requirement_id", "role", "path"} <= set(item):
            raise VisualReferenceError("visual_reference artifact needs requirement_id, role, and path")
        if set(item) - {"requirement_id", "role", "path", "content_type"}:
            raise VisualReferenceError("visual_reference artifact contains unknown fields")
        requirement_id, role, path = item.get("requirement_id"), item.get("role"), item.get("path")
        content_type = item.get("content_type", "application/octet-stream")
        if requirement_id not in requirement_ids:
            raise VisualReferenceError("visual_reference artifact names an unknown requirement")
        if role not in VISUAL_ARTIFACT_ROLES:
            raise VisualReferenceError("visual_reference artifact role is unsupported")
        if not isinstance(path, str) or not _safe_relative(path):
            raise VisualReferenceError("visual_reference artifact path must stay inside the service workspace")
        if not isinstance(content_type, str) or not re.fullmatch(r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+", content_type):
            raise VisualReferenceError("visual_reference artifact content_type is invalid")
        artifacts.append(VisualArtifact(requirement_id, role, path, content_type))
    if len({(item.requirement_id, item.role) for item in artifacts}) != len(artifacts):
        raise VisualReferenceError("visual_reference artifact requirement roles must be unique")
    for requirement_id in requirement_ids:
        roles = {item.role for item in artifacts if item.requirement_id == requirement_id}
        if roles != VISUAL_ARTIFACT_ROLES:
            raise VisualReferenceError("each visual requirement needs actual, reference, and diff artifacts")
    reference_inputs = {item.path for item in inputs if item.role == "reference"}
    if any(item.path not in reference_inputs for item in artifacts if item.role == "reference"):
        raise VisualReferenceError("visual reference artifacts must use declared immutable reference inputs")

    raw_comparison = value.get("comparison")
    if not isinstance(raw_comparison, dict) or set(raw_comparison) != {
        "algorithm",
        "max_difference_ratio",
        "stability_runs",
    }:
        raise VisualReferenceError("visual_reference comparison policy is incomplete or contains unknown fields")
    algorithm = raw_comparison.get("algorithm")
    ratio = raw_comparison.get("max_difference_ratio")
    stability_runs = raw_comparison.get("stability_runs")
    if not isinstance(algorithm, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", algorithm):
        raise VisualReferenceError("visual comparison algorithm must be a bounded identifier")
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 <= float(ratio) <= 1:
        raise VisualReferenceError("visual max_difference_ratio must be between 0 and 1")
    if isinstance(stability_runs, bool) or not isinstance(stability_runs, int) or not 1 <= stability_runs <= 5:
        raise VisualReferenceError("visual stability_runs must be between 1 and 5")

    functional_gates = value.get("functional_gates")
    if not isinstance(functional_gates, list) or not functional_gates:
        raise VisualReferenceError("visual_reference functional_gates must be a non-empty list")
    if any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", name) for name in functional_gates):
        raise VisualReferenceError("visual_reference functional gate name is invalid")
    if len(set(functional_gates)) != len(functional_gates):
        raise VisualReferenceError("visual_reference functional gates must be unique")

    return VisualReferencePolicy(
        revision=revision,
        inputs=tuple(inputs),
        artifacts=tuple(artifacts),
        comparison=VisualComparison(algorithm, float(ratio), stability_runs),
        functional_gates=tuple(functional_gates),
    )


def snapshot_visual_inputs(root: Path, policy: VisualReferencePolicy) -> tuple[VisualInputSnapshot, ...]:
    workspace = root.resolve()
    boundary = WorkspaceBoundary(workspace)
    snapshots: list[VisualInputSnapshot] = []
    for declared in policy.inputs:
        target = (workspace / declared.path).resolve()
        try:
            boundary.require_inside(target)
        except Exception as exc:
            raise VisualReferenceError("visual_reference input escapes the service workspace") from exc
        if not target.is_file():
            raise VisualReferenceError(f"visual_reference input is not a regular file: {declared.path}")
        size = target.stat().st_size
        if size > MAX_VISUAL_INPUT_SIZE:
            raise VisualReferenceError(f"visual_reference input exceeds size limit: {declared.path}")
        digest = hashlib.sha256()
        with target.open("rb") as stream:
            while chunk := stream.read(65536):
                digest.update(chunk)
        snapshots.append(VisualInputSnapshot(declared.path, declared.role, digest.hexdigest(), size))
    return tuple(snapshots)


def visual_policy_fingerprint(
    policy: VisualReferencePolicy,
    snapshots: tuple[VisualInputSnapshot, ...],
) -> str:
    payload = {
        "policy": policy.to_dict(),
        "inputs": [item.to_dict() for item in snapshots],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def reject_update_modes(*values: object) -> None:
    text = "\n".join(
        str(part).casefold()
        for value in values
        for part in (value if isinstance(value, (list, tuple)) else (value,))
        if part is not None
    )
    if any(marker in text for marker in _UPDATE_MODES):
        raise VisualReferenceError("visual_reference evaluation cannot enable golden or baseline update modes")


def validate_visual_result(check: dict[str, Any], policy: VisualReferencePolicy) -> dict[str, Any]:
    ratio = check.get("difference_ratio")
    stable = check.get("stable")
    render_count = check.get("render_count")
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 <= float(ratio) <= 1:
        raise VisualResultError("visual result needs a bounded difference_ratio", failure_kind="environment")
    if stable is not True:
        raise VisualResultError("visual rendering was unstable", failure_kind="environment")
    if isinstance(render_count, bool) or not isinstance(render_count, int) or render_count < policy.comparison.stability_runs:
        raise VisualResultError("visual result did not perform the required stability runs", failure_kind="environment")
    if float(ratio) > policy.comparison.max_difference_ratio:
        raise VisualResultError("visual difference exceeds the approved threshold", failure_kind="assertion")
    return {
        "difference_ratio": float(ratio),
        "stable": True,
        "render_count": render_count,
    }


def _safe_relative(value: str) -> bool:
    if not value or "\0" in value:
        return False
    path = PureWindowsPath(value)
    return not path.is_absolute() and not path.drive and ".." not in path.parts and ":" not in value
