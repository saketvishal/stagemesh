"""Fail-closed planning primitives for reducing redundant hosted CI work."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath


class CIOptimizationError(ValueError):
    """Raised when optimization input cannot be used safely."""


@dataclass(frozen=True)
class BatchSizeRecommendation:
    current_size: int
    recommended_size: int
    minimum_size: int
    maximum_size: int
    action: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "current_size": self.current_size,
            "maximum_size": self.maximum_size,
            "minimum_size": self.minimum_size,
            "reason": self.reason,
            "recommended_size": self.recommended_size,
        }


def recommend_batch_size(
    *,
    current_size: int,
    minimum_size: int,
    maximum_size: int,
    queue_delay_seconds: float,
    runner_utilization: float,
    recent_failure_rate: float,
) -> BatchSizeRecommendation:
    """Return a bounded recommendation; it never changes a running window."""
    if minimum_size < 1 or maximum_size < minimum_size:
        raise CIOptimizationError("batch size bounds must satisfy 1 <= minimum <= maximum")
    if not minimum_size <= current_size <= maximum_size:
        raise CIOptimizationError("current batch size must be inside the configured bounds")
    if queue_delay_seconds < 0:
        raise CIOptimizationError("queue delay must be non-negative")
    if not 0.0 <= runner_utilization <= 1.0:
        raise CIOptimizationError("runner utilization must be between 0 and 1")
    if not 0.0 <= recent_failure_rate <= 1.0:
        raise CIOptimizationError("recent failure rate must be between 0 and 1")

    recommended = current_size
    reason = "signals are inside the stable band"
    if recent_failure_rate >= 0.25:
        recommended = max(minimum_size, (current_size + 1) // 2)
        reason = "recent combined-candidate failures favor smaller isolation sets"
    elif queue_delay_seconds >= 600 or runner_utilization >= 0.85:
        recommended = min(maximum_size, current_size + 1)
        reason = "runner pressure favors fewer, larger hosted-CI publications"
    elif queue_delay_seconds <= 60 and runner_utilization <= 0.50:
        recommended = max(minimum_size, current_size - 1)
        reason = "low runner pressure permits smaller failure-isolation sets"

    action = "hold"
    if recommended > current_size:
        action = "grow"
    elif recommended < current_size:
        action = "shrink"
    return BatchSizeRecommendation(
        current_size=current_size,
        recommended_size=recommended,
        minimum_size=minimum_size,
        maximum_size=maximum_size,
        action=action,
        reason=reason,
    )


@dataclass(frozen=True)
class BisectProbe:
    task_ids: tuple[str, ...]
    failed: bool

    def to_dict(self) -> dict[str, object]:
        return {"failed": self.failed, "task_ids": list(self.task_ids)}


@dataclass(frozen=True)
class BisectResult:
    status: str
    suspected_task_ids: tuple[str, ...]
    probes: tuple[BisectProbe, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "probes": [probe.to_dict() for probe in self.probes],
            "status": self.status,
            "suspected_task_ids": list(self.suspected_task_ids),
        }


def bisect_failing_batch(
    task_ids: Sequence[str],
    probe_failed: Callable[[tuple[str, ...]], bool],
    *,
    max_probes: int = 8,
) -> BisectResult:
    """Narrow one failing batch without publishing, merging, or invoking providers."""
    candidates = tuple(task_ids)
    if not candidates or any(not item.strip() for item in candidates):
        raise CIOptimizationError("task ids must be a non-empty sequence of non-empty strings")
    if len(set(candidates)) != len(candidates):
        raise CIOptimizationError("task ids must be unique")
    if max_probes < 1:
        raise CIOptimizationError("max probes must be positive")

    probes: list[BisectProbe] = []

    def run(items: tuple[str, ...]) -> bool:
        if len(probes) >= max_probes:
            raise _ProbeBudgetExhausted
        failed = bool(probe_failed(items))
        probes.append(BisectProbe(items, failed))
        return failed

    try:
        if not run(candidates):
            return BisectResult("batch_not_reproduced", (), tuple(probes))
        current = candidates
        while len(current) > 1:
            midpoint = len(current) // 2
            left = current[:midpoint]
            right = current[midpoint:]
            if run(left):
                current = left
                continue
            if run(right):
                current = right
                continue
            return BisectResult("interaction_not_isolated", current, tuple(probes))
        return BisectResult("isolated", current, tuple(probes))
    except _ProbeBudgetExhausted:
        return BisectResult("probe_budget_exhausted", current, tuple(probes))


class _ProbeBudgetExhausted(Exception):
    pass


_HEX_40 = re.compile(r"^[0-9a-f]{40}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class CacheArtifact:
    path: str
    sha256: str
    size_bytes: int

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "sha256": self.sha256, "size_bytes": self.size_bytes}


@dataclass(frozen=True)
class CacheManifest:
    candidate_sha: str
    policy_fingerprint: str
    platform: str
    targets: tuple[str, ...]
    artifacts: tuple[CacheArtifact, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "candidate_sha": self.candidate_sha,
            "platform": self.platform,
            "policy_fingerprint": self.policy_fingerprint,
            "schema": "stagemesh.ci-cache-manifest/1",
            "targets": list(self.targets),
        }


def parse_cache_manifest(payload: Mapping[str, object]) -> CacheManifest:
    """Validate an untrusted remote-cache manifest before any artifact is reused."""
    allowed = {
        "schema", "candidate_sha", "policy_fingerprint", "platform", "targets", "artifacts"
    }
    if set(payload) != allowed:
        raise CIOptimizationError("cache manifest fields do not match the versioned schema")
    if payload.get("schema") != "stagemesh.ci-cache-manifest/1":
        raise CIOptimizationError("unsupported cache manifest schema")
    candidate_sha = payload.get("candidate_sha")
    policy = payload.get("policy_fingerprint")
    platform = payload.get("platform")
    targets = payload.get("targets")
    artifacts = payload.get("artifacts")
    _validate_cache_identity(candidate_sha, policy, platform, targets)
    parsed = _parse_artifacts(artifacts)
    assert isinstance(candidate_sha, str)
    assert isinstance(policy, str)
    assert isinstance(platform, str)
    assert isinstance(targets, list)
    return CacheManifest(candidate_sha, policy, platform, tuple(targets), tuple(parsed))


def _validate_cache_identity(
    candidate_sha: object, policy: object, platform: object, targets: object
) -> None:
    if not isinstance(candidate_sha, str) or not _HEX_40.fullmatch(candidate_sha):
        raise CIOptimizationError("candidate sha must be a full lowercase git sha")
    if not isinstance(policy, str) or not _HEX_64.fullmatch(policy):
        raise CIOptimizationError("policy fingerprint must be a lowercase sha256 digest")
    if not isinstance(platform, str) or not platform or len(platform) > 100:
        raise CIOptimizationError("platform must be a bounded non-empty string")
    if not isinstance(targets, list) or not targets or any(
        not isinstance(target, str) or not target or len(target) > 200 for target in targets
    ):
        raise CIOptimizationError("targets must be a non-empty list of bounded strings")
    if len(set(targets)) != len(targets):
        raise CIOptimizationError("targets must be unique")


def _parse_artifacts(value: object) -> list[CacheArtifact]:
    if not isinstance(value, list) or not value or len(value) > 100:
        raise CIOptimizationError("artifacts must be a non-empty bounded list")
    parsed: list[CacheArtifact] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"path", "sha256", "size_bytes"}:
            raise CIOptimizationError("artifact fields do not match the versioned schema")
        path = item.get("path")
        digest = item.get("sha256")
        size = item.get("size_bytes")
        if not isinstance(path, str) or not _safe_relative_path(path):
            raise CIOptimizationError("artifact path must be safe and project-relative")
        if not isinstance(digest, str) or not _HEX_64.fullmatch(digest):
            raise CIOptimizationError("artifact sha256 must be a lowercase digest")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= 1_000_000_000:
            raise CIOptimizationError("artifact size must be between 0 and 1 GB")
        parsed.append(CacheArtifact(path, digest, size))
    if len({item.path for item in parsed}) != len(parsed):
        raise CIOptimizationError("artifact paths must be unique")
    return parsed


def verify_cache_artifact(artifact: CacheArtifact, content: bytes) -> bool:
    """Require both declared size and digest to match before reuse."""
    return len(content) == artifact.size_bytes and hashlib.sha256(content).hexdigest() == artifact.sha256


def _safe_relative_path(value: str) -> bool:
    if "\\" in value or ":" in value or "\x00" in value or value.startswith("/"):
        return False
    path = PurePosixPath(value)
    return bool(path.parts) and all(part not in {"", ".", ".."} for part in path.parts)
