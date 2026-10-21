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
    # Recording consumes the discovered identity, including configuration and
    # environment binding, even when no services are configured.
    fp = discover_validation_services(project).fingerprint

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
        project=project,
        artifact_root=artifact_root,
    )

    assert "e2e_evidence" in updated
    assert updated["e2e_evidence"]["candidate_sha"] == bound.candidate_sha
    assert updated["e2e_evidence"]["result"] == "PASS"
    assert updated["e2e_evidence"]["artifacts"][0]["name"] == "api_log"
    assert updated["e2e_evidence"] == manifest.to_dict()


def test_record_rejects_unbound_empty_service_fingerprint(test_env):
    project, artifact_root, bound, _, _ = test_env
    manifest = build_e2e_evidence(
        bound, candidate_sha=bound.candidate_sha,
        service_fingerprint=compute_service_fingerprint([]),
        kind="api", command=["pytest", "tests/api"], result="PASS",
    )
    payload = {"candidate_sha": bound.candidate_sha, "status": "PASSED"}
    before = payload.copy()
    with pytest.raises(E2EEvidenceValidationError, match="service fingerprint mismatch"):
        record_e2e_evidence_in_validation(
            payload, manifest, bound, project=project, artifact_root=artifact_root,
        )
    assert payload == before


@pytest.fixture
def api_manifest(test_env):
    project, artifact_root, bound, _, api_log = test_env
    return build_e2e_evidence(
        bound, candidate_sha=bound.candidate_sha,
        service_fingerprint=discover_validation_services(project).fingerprint,
        kind="api", command=["pytest", "tests/api"], result="PASS",
        artifacts=[build_artifact_reference("api_log", api_log, artifact_root)],
        artifact_root=artifact_root,
    )


@pytest.mark.parametrize("entry", ["build", "read", "record"])
def test_artifacts_require_root(test_env, api_manifest, entry):
    project, artifact_root, bound, _, _ = test_env
    with pytest.raises(E2EEvidenceValidationError, match="artifact root"):
        if entry == "build":
            build_e2e_evidence(
                bound, candidate_sha=bound.candidate_sha,
                service_fingerprint=api_manifest.service_fingerprint,
                kind="api", command=api_manifest.command, result="PASS",
                artifacts=api_manifest.artifacts,
            )
        elif entry == "read":
            source = project / "evidence.json"
            source.write_text(json.dumps(api_manifest.to_dict()), encoding="utf-8")
            read_e2e_evidence(source, bound)
        else:
            record_e2e_evidence_in_validation({}, api_manifest, bound, project=project)


@pytest.mark.parametrize("entry", ["read", "record"])
@pytest.mark.parametrize("count", [64, 65])
def test_artifact_limit_at_acceptance(test_env, api_manifest, entry, count):
    project, artifact_root, bound, _, _ = test_env
    manifest = replace(api_manifest, artifacts=api_manifest.artifacts * count)
    def accept():
        if entry == "record":
            return record_e2e_evidence_in_validation(
                {}, manifest, bound, project=project, artifact_root=artifact_root,
            )
        source = project / "evidence.json"
        source.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
        return read_e2e_evidence(source, bound, artifact_root=artifact_root)
    if count == 65:
        with pytest.raises(E2EEvidenceValidationError, match="count exceeds"):
            accept()
    else:
        assert accept()


@pytest.mark.parametrize("script", ["test:api", "test:e2e"])
def test_script_definition_changes_service_identity(test_env, script):
    project, _, _, _, _ = test_env
    package = project / "package.json"
    package.write_text(json.dumps({"scripts": {script: "runner --url http://localhost:8000"}}))
    before = discover_validation_services(project)
    package.write_text(json.dumps({"scripts": {script: "runner --url http://localhost:9000"}}))
    after = discover_validation_services(project)
    assert before.services[0].command == after.services[0].command
    assert before.fingerprint != after.fingerprint


def test_npm_run_with_extra_arguments_binds_the_script_body(test_env):
    project, _, _, _, _ = test_env
    (project / "package.json").write_text(json.dumps({"scripts": {"e2e": "playwright test"}}))
    config = project / ".stagemesh" / "config.json"
    config.parent.mkdir(exist_ok=True)
    config.write_text(json.dumps({
        "validation_services": [{
            "name": "browser", "kind": "browser",
            "command": ["npm", "run", "e2e", "--", "--project=chromium"],
        }]
    }))
    bound = discover_validation_services(project)
    assert bound.services[0].script_definition == "playwright test"
    (project / "package.json").write_text(json.dumps({"scripts": {"e2e": "echo skipped"}}))
    changed = discover_validation_services(project)
    assert changed.fingerprint != bound.fingerprint


def test_broken_package_json_does_not_drop_service_discovery(test_env):
    project, _, _, _, _ = test_env
    (project / "package.json").write_text("{", encoding="utf-8")
    with pytest.raises(Exception, match="package.json"):
        discover_validation_services(project)


def test_record_rejects_service_change_without_mutating_payload(test_env, api_manifest):
    project, artifact_root, bound, _, _ = test_env
    (project / "package.json").write_text('{"scripts":{"test:api":"new-api-tests"}}')
    payload = {"candidate_sha": bound.candidate_sha}
    with pytest.raises(E2EEvidenceValidationError, match="service fingerprint mismatch"):
        record_e2e_evidence_in_validation(
            payload, api_manifest, bound, project=project, artifact_root=artifact_root,
        )
    assert "e2e_evidence" not in payload


@pytest.mark.parametrize("damage", ["missing", "traversal", "checksum", "size"])
@pytest.mark.parametrize("entry", ["read", "record"])
def test_acceptance_validates_artifacts(test_env, api_manifest, damage, entry):
    project, artifact_root, bound, _, api_log = test_env
    ref = api_manifest.artifacts[0]
    if damage == "missing":
        api_log.unlink()
    elif damage == "traversal":
        ref = replace(ref, path="nested/../api_response.json")
    elif damage == "checksum":
        ref = replace(ref, sha256="invalid")
    else:
        ref = replace(ref, size=-1)
    manifest = replace(api_manifest, artifacts=(ref,))
    with pytest.raises(E2EEvidenceValidationError):
        if entry == "read":
            source = project / "evidence.json"
            source.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
            read_e2e_evidence(source, bound, artifact_root=artifact_root)
        else:
            record_e2e_evidence_in_validation(
                {}, manifest, bound, project=project, artifact_root=artifact_root,
            )


@pytest.mark.parametrize("damage", [None, "service", "during_run", "artifact", "command", "result", "candidate"])
def test_validator_records_bound_evidence(test_env, api_manifest, monkeypatch, damage):
    from stagemesh.contracts import ContractEvaluation, GateResult
    from stagemesh.domain import EvidenceStatus
    from stagemesh.persistence import Store
    from stagemesh.validation import Validator

    project, artifact_root, bound, _, api_log = test_env
    monkeypatch.setattr("stagemesh.validation.contract_for_candidate", lambda *args: bound)
    monkeypatch.setattr("stagemesh.validation.changed_files", lambda *args: ())
    def evaluate(*args, **kwargs):
        if damage == "during_run":
            (project / "package.json").write_text('{"scripts":{"test:api":"changed"}}')
        return ContractEvaluation("PASSED", (), (), (
            GateResult("api", "PASSED", api_manifest.command, 0),
        ))
    monkeypatch.setattr("stagemesh.validation.evaluate_contract", evaluate)
    manifest = api_manifest
    if damage == "service":
        manifest = replace(manifest, service_fingerprint="old-service")
    elif damage == "artifact":
        api_log.unlink()
    elif damage == "command":
        manifest = replace(manifest, command=("other-command",))
    elif damage == "result":
        manifest = replace(manifest, result="FAIL")
    elif damage == "candidate":
        manifest = replace(manifest, candidate_sha="not-a-commit")
    store = Store(project / "state.sqlite3")
    store.migrate()
    try:
        task_id = store.upsert_task("API evidence", source_id="E2E")
        store.add_candidate(task_id, bound.candidate_sha, "test", True)
        status = Validator().validate(
            store, task_id, bound.candidate_sha, project,
            e2e_evidence=manifest, artifact_root=artifact_root,
        )
        payload = json.loads(store.conn.execute(
            "SELECT payload FROM evidence WHERE kind='VALIDATION'"
        ).fetchone()[0])
        if damage is None:
            assert status is EvidenceStatus.PASSED
            assert payload["e2e_evidence"] == manifest.to_dict()
        else:
            assert status is EvidenceStatus.FAILED
            assert "e2e_evidence" not in payload
            assert any(f["code"] == "invalid_e2e_evidence" for f in payload["findings"])
        assert not list(store.running_executions())
    finally:
        store.close()


def test_manifest_size_limit_bounds_the_read(test_env, monkeypatch):
    from io import BytesIO
    from stagemesh.e2e_evidence import MAX_MANIFEST_BYTES

    project, artifact_root, bound, _, _ = test_env
    source = project / "oversized-evidence.json"
    source.write_bytes(b"x")
    calls = []
    class BoundedReader(BytesIO):
        def read(self, size=-1):
            calls.append(size)
            assert size == MAX_MANIFEST_BYTES + 1
            return super().read(size)
    original_open = Path.open
    def open_manifest(path, mode="r", *args, **kwargs):
        if path == source.resolve() and mode == "rb":
            return BoundedReader(b"x" * (MAX_MANIFEST_BYTES + 2))
        return original_open(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", open_manifest)
    with pytest.raises(E2EEvidenceValidationError, match="maximum byte size"):
        read_e2e_evidence(source, bound, artifact_root=artifact_root)
    assert calls == [MAX_MANIFEST_BYTES + 1]
