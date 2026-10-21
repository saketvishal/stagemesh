"""Tests for bounded E2E evidence manifest: candidate binding, service identity, and artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
import pytest

from stagemesh.contracts import BoundChangeContract, ChangeContract, canonical_contract_json
from stagemesh.validation_services import (
    ConfiguredService,
    ValidationServicesManifest,
    compute_service_fingerprint,
    discover_validation_services,
)
from stagemesh.e2e_evidence import (
    ArtifactReference,
    E2EEvidenceManifest,
    E2EEvidenceValidationError,
    build_artifact_reference,
    build_e2e_evidence,
    read_e2e_evidence,
    validate_artifact_reference,
    validate_e2e_evidence_manifest,
    write_e2e_evidence,
)
from stagemesh.validation import record_e2e_evidence_in_validation


@pytest.fixture
def test_env(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()

    # Create dummy artifact files
    screenshot = artifact_root / "screenshot.png"
    screenshot.write_bytes(b"PNG_DATA_1234567890")
    api_log = artifact_root / "api_response.json"
    api_log.write_bytes(b'{"status": "ok", "latency_ms": 42}')

    contract = ChangeContract(
        objective="Implement E2E test verification",
        allowed_files=("src/**",),
    )
    canonical = canonical_contract_json(contract)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    baseline_sha = "1" * 40
    candidate_sha = "2" * 40
    bound = BoundChangeContract(
        contract=contract,
        version=1,
        digest=digest,
        canonical_json=canonical,
        baseline_sha=baseline_sha,
        candidate_sha=candidate_sha,
    )
    return project, artifact_root, bound, screenshot, api_log


def test_discover_validation_services_from_config_and_package_json(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    dot_sm = project / ".stagemesh"
    dot_sm.mkdir()
    (dot_sm / "config.json").write_text(
        json.dumps({
            "validation_services": [
                {
                    "name": "mock-auth-service",
                    "kind": "api",
                    "command": ["python", "-m", "mock_auth"],
                    "port": 8080,
                }
            ]
        }),
        encoding="utf-8",
    )
    (project / "package.json").write_text(
        json.dumps({
            "scripts": {
                "test:e2e": "playwright test",
            }
        }),
        encoding="utf-8",
    )

    manifest = discover_validation_services(project)
    names = [s.name for s in manifest.services]
    assert "mock-auth-service" in names
    assert "browser" in names
    assert manifest.fingerprint
    assert len(manifest.fingerprint) == 32


def test_browser_and_api_evidence_without_browser_installation(test_env):
    project, artifact_root, bound, _, api_log = test_env
    # API-only evidence does not require browser installation or browser services
    api_ref = build_artifact_reference("api_log", api_log, artifact_root, content_type="application/json")
    services = (
        ConfiguredService(name="api-gateway", kind="api", command=("python", "-m", "api.server")),
    )
    fp = compute_service_fingerprint(services)

    manifest = build_e2e_evidence(
        bound,
        candidate_sha=bound.candidate_sha,
        service_fingerprint=fp,
        kind="api",
        command=["pytest", "tests/test_api_e2e.py"],
        result="PASS",
        artifacts=[api_ref],
        metadata={"suite": "api_contract"},
        artifact_root=artifact_root,
    )

    assert manifest.kind == "api"
    assert manifest.result == "PASS"
    assert manifest.candidate_sha == bound.candidate_sha
    assert manifest.service_fingerprint == fp
    assert len(manifest.artifacts) == 1
    assert manifest.artifacts[0].name == "api_log"


def test_round_trip_write_and_read(test_env, tmp_path: Path):
    project, artifact_root, bound, screenshot, _ = test_env
    ref = build_artifact_reference("screenshot", screenshot, artifact_root, content_type="image/png")
    fp = compute_service_fingerprint([])

    manifest = build_e2e_evidence(
        bound,
        candidate_sha=bound.candidate_sha,
        service_fingerprint=fp,
        kind="browser",
        command=["npm", "run", "e2e"],
        result="PASS",
        artifacts=[ref],
        artifact_root=artifact_root,
    )

    dest = tmp_path / "e2e_manifest.json"
    write_e2e_evidence(dest, manifest, bound, artifact_root=artifact_root)

    loaded = read_e2e_evidence(
        dest,
        bound,
        expected_candidate_sha=bound.candidate_sha,
        expected_service_fingerprint=fp,
        artifact_root=artifact_root,
    )

    assert loaded == manifest
    assert loaded.artifacts[0].sha256 == ref.sha256
    assert loaded.artifacts[0].size == screenshot.stat().st_size


def test_reject_changed_candidate_identity(test_env):
    project, artifact_root, bound, screenshot, _ = test_env
    ref = build_artifact_reference("screenshot", screenshot, artifact_root)
    fp = compute_service_fingerprint([])

    # Candidate mismatch against bound contract
    with pytest.raises(E2EEvidenceValidationError, match="candidate sha mismatch"):
        build_e2e_evidence(
            bound,
            candidate_sha="3" * 40,
            service_fingerprint=fp,
            kind="browser",
            command=["npm", "run", "e2e"],
            result="PASS",
            artifacts=[ref],
            artifact_root=artifact_root,
        )


def test_reject_changed_service_fingerprint(test_env):
    project, artifact_root, bound, screenshot, _ = test_env
    ref = build_artifact_reference("screenshot", screenshot, artifact_root)
    fp = compute_service_fingerprint([])

    manifest = build_e2e_evidence(
        bound,
        candidate_sha=bound.candidate_sha,
        service_fingerprint=fp,
        kind="browser",
        command=["npm", "run", "e2e"],
        result="PASS",
        artifacts=[ref],
        artifact_root=artifact_root,
    )

    # Validate fails when expected fingerprint differs
    with pytest.raises(E2EEvidenceValidationError, match="service fingerprint mismatch"):
        validate_e2e_evidence_manifest(
            manifest,
            bound,
            expected_service_fingerprint="different_fp_12345",
            artifact_root=artifact_root,
        )


def test_reject_path_traversal_in_artifacts(test_env):
    project, artifact_root, bound, _, _ = test_env
    # Reference pointing to parent traversal
    bad_ref = ArtifactReference(
        name="hack",
        path="../etc/passwd",
        sha256="0" * 64,
        size=10,
    )
    with pytest.raises(E2EEvidenceValidationError, match="traversal"):
        validate_artifact_reference(bad_ref, artifact_root)

    bad_ref_abs = ArtifactReference(
        name="hack_abs",
        path="/etc/passwd",
        sha256="0" * 64,
        size=10,
    )
    with pytest.raises(E2EEvidenceValidationError, match="traversal"):
        validate_artifact_reference(bad_ref_abs, artifact_root)


def test_reject_missing_artifact_file(test_env):
    project, artifact_root, bound, _, _ = test_env
    missing_ref = ArtifactReference(
        name="missing",
        path="non_existent_file.png",
        sha256="0" * 64,
        size=10,
    )
    with pytest.raises(E2EEvidenceValidationError, match="does not exist"):
        validate_artifact_reference(missing_ref, artifact_root)


def test_reject_artifact_content_tampering_or_checksum_mismatch(test_env):
    project, artifact_root, bound, screenshot, _ = test_env
    ref = build_artifact_reference("screenshot", screenshot, artifact_root)
    # Tamper with file after ref build
    screenshot.write_bytes(b"TAMPERED_BYTES_SOMETHING_ELSE")

    with pytest.raises(E2EEvidenceValidationError, match="mismatch"):
        validate_artifact_reference(ref, artifact_root)


def test_reject_incomplete_results_and_empty_commands(test_env):
    project, artifact_root, bound, _, _ = test_env
    fp = compute_service_fingerprint([])

    # Incomplete / invalid result status
    with pytest.raises(E2EEvidenceValidationError, match="result must be one of"):
        build_e2e_evidence(
            bound,
            candidate_sha=bound.candidate_sha,
            service_fingerprint=fp,
            kind="api",
            command=["pytest", "api"],
            result="PARTIAL",
        )

    # Empty command
    with pytest.raises(E2EEvidenceValidationError, match="command must not be empty"):
        build_e2e_evidence(
            bound,
            candidate_sha=bound.candidate_sha,
            service_fingerprint=fp,
            kind="api",
            command=[],
            result="PASS",
        )


def test_record_e2e_evidence_in_validation_integrates_cleanly(test_env):
    project, artifact_root, bound, _, api_log = test_env
    api_ref = build_artifact_reference("api_log", api_log, artifact_root, content_type="application/json")
    fp = compute_service_fingerprint([])

    manifest = build_e2e_evidence(
        bound,
        candidate_sha=bound.candidate_sha,
        service_fingerprint=fp,
        kind="api",
        command=["pytest", "tests/api"],
        result="PASS",
        artifacts=[api_ref],
        artifact_root=artifact_root,
    )

    validation_payload = {
        "validator": "contract",
        "candidate_sha": bound.candidate_sha,
        "status": "PASSED",
    }
    updated = record_e2e_evidence_in_validation(
        validation_payload,
        manifest,
        bound,
        artifact_root=artifact_root,
    )

    assert "e2e_evidence" in updated
    assert updated["e2e_evidence"]["candidate_sha"] == bound.candidate_sha
    assert updated["e2e_evidence"]["result"] == "PASS"
    assert updated["e2e_evidence"]["artifacts"][0]["name"] == "api_log"
