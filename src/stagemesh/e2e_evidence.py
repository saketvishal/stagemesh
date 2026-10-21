"""Bounded E2E evidence manifests bound to exact candidate and service identity."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .contracts import CONTRACT_VERSION, BoundChangeContract, canonical_contract_json
from .release import ReleaseValidationError, validate_candidate_sha
from .security import WorkspaceBoundary


class E2EEvidenceValidationError(ValueError):
    pass


MAX_ARTIFACTS = 64
MAX_ARTIFACT_SIZE = 50 * 1024 * 1024  # 50 MB per artifact
MAX_MANIFEST_BYTES = 256 * 1024       # 256 KB for manifest JSON
ALLOWED_KINDS = {"browser", "api", "hybrid"}
ALLOWED_RESULTS = {"PASS", "FAIL"}


@dataclass(frozen=True)
class ArtifactReference:
    name: str
    path: str
    sha256: str
    size: int
    content_type: str = "application/octet-stream"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "content_type": self.content_type,
        }


@dataclass(frozen=True)
class E2EEvidenceManifest:
    candidate_sha: str
    contract_digest: str
    baseline_sha: str
    service_fingerprint: str
    kind: str  # "browser", "api", "hybrid"
    command: tuple[str, ...]
    result: str  # "PASS" | "FAIL"
    artifacts: tuple[ArtifactReference, ...]
    metadata: dict[str, Any]
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "candidate_sha": self.candidate_sha,
            "contract_digest": self.contract_digest,
            "baseline_sha": self.baseline_sha,
            "service_fingerprint": self.service_fingerprint,
            "kind": self.kind,
            "command": list(self.command),
            "result": self.result,
            "artifacts": [a.to_dict() for a in self.artifacts],
            "metadata": dict(self.metadata),
        }


def validate_artifact_reference(
    ref: ArtifactReference,
    artifact_root: Path,
) -> ArtifactReference:
    """Validate that artifact reference has no path traversal, exists, and checksum matches."""
    if not isinstance(ref.name, str) or not ref.name.strip():
        raise E2EEvidenceValidationError("artifact name must be a non-empty string")
    if not isinstance(ref.path, str) or not ref.path.strip():
        raise E2EEvidenceValidationError("artifact path must be a non-empty string")

    norm_path = ref.path.replace("\\", "/").strip()
    # Check traversal or root
    if (
        norm_path.startswith("/")
        or norm_path.startswith("../")
        or "/../" in norm_path
        or norm_path == ".."
        or ":/" in norm_path
        or ":\\" in norm_path
    ):
        raise E2EEvidenceValidationError(f"artifact path '{ref.path}' contains traversal or absolute root")

    if not isinstance(ref.sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", ref.sha256):
        raise E2EEvidenceValidationError(f"artifact '{ref.name}' must have a valid sha256 hex digest")
    expected_sha = ref.sha256.lower()

    if not isinstance(ref.size, int) or ref.size < 0 or ref.size > MAX_ARTIFACT_SIZE:
        raise E2EEvidenceValidationError(f"artifact '{ref.name}' size must be non-negative and under limit")

    root = artifact_root.resolve()
    target = (root / norm_path).resolve()
    # Ensure within boundary
    try:
        WorkspaceBoundary(root).require_inside(target)
    except Exception as exc:
        raise E2EEvidenceValidationError(f"artifact path '{ref.path}' escapes artifact root") from exc

    if not target.is_file():
        raise E2EEvidenceValidationError(f"artifact file '{ref.path}' does not exist")

    actual_size = target.stat().st_size
    if actual_size != ref.size:
        raise E2EEvidenceValidationError(
            f"artifact '{ref.name}' size mismatch: expected {ref.size}, got {actual_size}"
        )

    hasher = hashlib.sha256()
    with target.open("rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    actual_sha = hasher.hexdigest().lower()
    if actual_sha != expected_sha:
        raise E2EEvidenceValidationError(
            f"artifact '{ref.name}' sha256 mismatch: expected {expected_sha}, got {actual_sha}"
        )

    return ArtifactReference(
        name=ref.name.strip(),
        path=norm_path,
        sha256=expected_sha,
        size=actual_size,
        content_type=ref.content_type if isinstance(ref.content_type, str) else "application/octet-stream",
    )


def build_artifact_reference(
    name: str,
    file_path: Path,
    artifact_root: Path,
    content_type: str = "application/octet-stream",
) -> ArtifactReference:
    """Build a validated ArtifactReference from an existing file within artifact_root."""
    root = artifact_root.resolve()
    target = file_path.resolve()
    WorkspaceBoundary(root).require_inside(target)
    if not target.is_file():
        raise E2EEvidenceValidationError(f"file '{file_path}' does not exist or is not a file")

    rel_path = target.relative_to(root).as_posix()
    size = target.stat().st_size
    hasher = hashlib.sha256()
    with target.open("rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    sha256_hex = hasher.hexdigest().lower()

    ref = ArtifactReference(
        name=name.strip(),
        path=rel_path,
        sha256=sha256_hex,
        size=size,
        content_type=content_type,
    )
    return validate_artifact_reference(ref, root)


def build_e2e_evidence(
    bound: BoundChangeContract,
    *,
    candidate_sha: str,
    service_fingerprint: str,
    kind: str,
    command: tuple[str, ...] | list[str],
    result: str,
    artifacts: tuple[ArtifactReference, ...] | list[ArtifactReference] = (),
    metadata: dict[str, Any] | None = None,
    artifact_root: Path | None = None,
) -> E2EEvidenceManifest:
    """Construct and validate an E2EEvidenceManifest."""
    # Candidate sha validation
    try:
        norm_candidate = validate_candidate_sha(candidate_sha)
    except ReleaseValidationError as exc:
        raise E2EEvidenceValidationError(str(exc)) from exc

    # Contract binding checks
    if bound.candidate_sha:
        bound_cand = validate_candidate_sha(bound.candidate_sha)
        if norm_candidate != bound_cand:
            raise E2EEvidenceValidationError(
                f"candidate sha mismatch: bound contract has {bound_cand}, got {norm_candidate}"
            )

    if not bound.baseline_sha:
        raise E2EEvidenceValidationError("bound contract missing baseline_sha")
    baseline = bound.baseline_sha.lower()

    if not service_fingerprint or not isinstance(service_fingerprint, str):
        raise E2EEvidenceValidationError("service_fingerprint must be a non-empty string")

    norm_kind = kind.lower().strip() if isinstance(kind, str) else ""
    if norm_kind not in ALLOWED_KINDS:
        raise E2EEvidenceValidationError(
            f"kind must be one of {sorted(ALLOWED_KINDS)}, got {kind!r}"
        )

    if not command:
        raise E2EEvidenceValidationError("command must not be empty")
    cmd_tuple = tuple(str(c) for c in command)

    norm_result = result.upper().strip() if isinstance(result, str) else ""
    if norm_result not in ALLOWED_RESULTS:
        raise E2EEvidenceValidationError(
            f"result must be one of {sorted(ALLOWED_RESULTS)}, got {result!r}"
        )

    if len(artifacts) > MAX_ARTIFACTS:
        raise E2EEvidenceValidationError(f"artifacts count exceeds limit of {MAX_ARTIFACTS}")

    validated_artifacts: list[ArtifactReference] = []
    if artifact_root is not None:
        for a in artifacts:
            validated_artifacts.append(validate_artifact_reference(a, artifact_root))
    elif artifacts:
        raise E2EEvidenceValidationError("artifact root is required for artifact references")

    manifest = E2EEvidenceManifest(
        candidate_sha=norm_candidate,
        contract_digest=bound.digest,
        baseline_sha=baseline,
        service_fingerprint=service_fingerprint.strip(),
        kind=norm_kind,
        command=cmd_tuple,
        result=norm_result,
        artifacts=tuple(validated_artifacts),
        metadata=dict(metadata or {}),
        version=1,
    )
    return manifest


def validate_e2e_evidence_manifest(
    manifest: E2EEvidenceManifest,
    bound: BoundChangeContract,
    *,
    expected_candidate_sha: str | None = None,
    expected_service_fingerprint: str | None = None,
    artifact_root: Path | None = None,
) -> None:
    """Validate manifest identity against bound contract, candidate, and fingerprint."""
    if manifest.version != 1:
        raise E2EEvidenceValidationError(f"unsupported manifest version {manifest.version}")

    norm_cand = validate_candidate_sha(manifest.candidate_sha)
    if expected_candidate_sha:
        exp_cand = validate_candidate_sha(expected_candidate_sha)
        if norm_cand != exp_cand:
            raise E2EEvidenceValidationError(
                f"candidate sha changed: expected {exp_cand}, manifest has {norm_cand}"
            )

    if bound.candidate_sha:
        bound_cand = validate_candidate_sha(bound.candidate_sha)
        if norm_cand != bound_cand:
            raise E2EEvidenceValidationError(
                f"candidate sha does not match bound contract: expected {bound_cand}, got {norm_cand}"
            )

    if manifest.contract_digest != bound.digest:
        raise E2EEvidenceValidationError(
            f"contract digest mismatch: expected {bound.digest}, manifest has {manifest.contract_digest}"
        )

    if bound.baseline_sha and manifest.baseline_sha.lower() != bound.baseline_sha.lower():
        raise E2EEvidenceValidationError(
            f"baseline sha mismatch: expected {bound.baseline_sha}, manifest has {manifest.baseline_sha}"
        )

    if expected_service_fingerprint:
        if manifest.service_fingerprint != expected_service_fingerprint:
            raise E2EEvidenceValidationError(
                f"service fingerprint mismatch: expected {expected_service_fingerprint}, got {manifest.service_fingerprint}"
            )

    if manifest.kind not in ALLOWED_KINDS:
        raise E2EEvidenceValidationError(f"invalid manifest kind: {manifest.kind}")

    if manifest.result not in ALLOWED_RESULTS:
        raise E2EEvidenceValidationError(f"invalid manifest result: {manifest.result}")

    if not manifest.command:
        raise E2EEvidenceValidationError("manifest command cannot be empty")

    if len(manifest.artifacts) > MAX_ARTIFACTS:
        raise E2EEvidenceValidationError(f"artifacts count exceeds limit of {MAX_ARTIFACTS}")
    if manifest.artifacts and artifact_root is None:
        raise E2EEvidenceValidationError("artifact root is required for artifact references")
    for a in manifest.artifacts:
        validate_artifact_reference(a, artifact_root)


def write_e2e_evidence(
    destination: Path,
    manifest: E2EEvidenceManifest,
    bound: BoundChangeContract,
    *,
    artifact_root: Path | None = None,
) -> None:
    """Persist manifest as JSON atomically without overwriting."""
    validate_e2e_evidence_manifest(manifest, bound, artifact_root=artifact_root)
    raw = json.dumps(manifest.to_dict(), indent=2, sort_keys=True).encode("utf-8")
    if len(raw) > MAX_MANIFEST_BYTES:
        raise E2EEvidenceValidationError("manifest exceeds maximum byte size")

    dest = destination.resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("xb") as f:
        f.write(raw)


def read_e2e_evidence(
    source: Path,
    bound: BoundChangeContract,
    *,
    expected_candidate_sha: str | None = None,
    expected_service_fingerprint: str | None = None,
    artifact_root: Path | None = None,
) -> E2EEvidenceManifest:
    """Read and validate manifest from JSON file."""
    src = source.resolve()
    if not src.is_file():
        raise E2EEvidenceValidationError(f"manifest file does not exist: {source}")

    raw = src.read_bytes()
    if len(raw) > MAX_MANIFEST_BYTES:
        raise E2EEvidenceValidationError("manifest exceeds maximum byte size")

    try:
        data = json.loads(raw)
    except Exception as exc:
        raise E2EEvidenceValidationError("manifest is not valid JSON") from exc

    if not isinstance(data, dict):
        raise E2EEvidenceValidationError("manifest root must be a dict")

    # Required fields
    required = {
        "version",
        "candidate_sha",
        "contract_digest",
        "baseline_sha",
        "service_fingerprint",
        "kind",
        "command",
        "result",
        "artifacts",
    }
    missing = required - set(data.keys())
    if missing:
        raise E2EEvidenceValidationError(f"manifest missing required fields: {missing}")

    artifacts: list[ArtifactReference] = []
    for item in data.get("artifacts", []):
        if not isinstance(item, dict):
            raise E2EEvidenceValidationError("artifact entry must be a dictionary")
        artifacts.append(
            ArtifactReference(
                name=item["name"],
                path=item["path"],
                sha256=item["sha256"],
                size=item["size"],
                content_type=item.get("content_type", "application/octet-stream"),
            )
        )

    cmd = tuple(data["command"]) if isinstance(data["command"], list) else (str(data["command"]),)
    manifest = E2EEvidenceManifest(
        candidate_sha=str(data["candidate_sha"]),
        contract_digest=str(data["contract_digest"]),
        baseline_sha=str(data["baseline_sha"]),
        service_fingerprint=str(data["service_fingerprint"]),
        kind=str(data["kind"]),
        command=cmd,
        result=str(data["result"]),
        artifacts=tuple(artifacts),
        metadata=dict(data.get("metadata", {})),
        version=int(data.get("version", 1)),
    )

    validate_e2e_evidence_manifest(
        manifest,
        bound,
        expected_candidate_sha=expected_candidate_sha,
        expected_service_fingerprint=expected_service_fingerprint,
        artifact_root=artifact_root,
    )
    return manifest
