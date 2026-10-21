from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from stagemesh.contract_check import check_contract, check_contract_text
from stagemesh.contracts import ChangeContract, GateCommand
from stagemesh.domain import EvidenceKind, EvidenceStatus
from stagemesh.git import GitWorkspace
from stagemesh.persistence import Store
from stagemesh.spec_integrity import (
    CustomSpecAdapter,
    OpenSpecAdapter,
    SpecGovernanceVerdict,
    resolve_pinned_adapter,
    spec_governance_gate,
    validate_prerequisite_manifest,
)
from stagemesh.spec_refs import SpecAdapterError, SpecOwner, SpecSnapshot
from stagemesh.validation import Validator


def valid_manifest(**overrides) -> dict:
    manifest = {
        "schema_version": 1,
        "assessment": "structural-only",
        "adapter": {"name": "openspec", "version": "layout-v1"},
        "base_revision": "base-sha-123",
        "candidate_revision": "candidate-sha-456",
        "changed_files": ["openspec/specs/auth/spec.md"],
        "owners": [
            {
                "id": "openspec/specs/auth/spec.md",
                "path": "openspec/specs/auth/spec.md",
                "status": "active",
                "canonical": True,
                "dependencies": [],
                "requirements": [
                    {
                        "id": "openspec/specs/auth/spec.md#requirement-login",
                        "text": "Must authenticate user securely.",
                        "dependencies": [],
                    }
                ],
            }
        ],
        "requirements": [
            {
                "requirement_id": "openspec/specs/auth/spec.md#requirement-login",
                "owner_id": "openspec/specs/auth/spec.md",
                "path": "openspec/specs/auth/spec.md",
                "change": "modified",
            }
        ],
        "unmapped_files": [],
        "unresolved_references": [],
        "sync_status": "in_sync",
        "validation_status": "passed",
    }
    manifest.update(overrides)
    return manifest


# --- 1. Configured pinned adapter validation vs unpinned rejection ---


def test_configured_pinned_adapters_accepted_and_unpinned_rejected(tmp_path: Path) -> None:
    # OpenSpec layout-v1
    adapter1 = resolve_pinned_adapter("openspec", "layout-v1")
    assert isinstance(adapter1, OpenSpecAdapter)

    # CustomSpecAdapter 1
    (tmp_path / "spec.json").write_text('{"schema_version": 1, "owners": []}', encoding="utf-8")
    adapter2 = resolve_pinned_adapter("custom-json", "1", contract_path="spec.json")
    assert isinstance(adapter2, CustomSpecAdapter)

    # Unpinned or unknown adapters fail closed
    for bad_name, bad_ver in [
        ("unpinned-plugin", "1"),
        ("openspec", "layout-v2"),
        ("custom-json", "2"),
        ("arbitrary-exec", "1"),
    ]:
        with pytest.raises(SpecAdapterError):
            resolve_pinned_adapter(bad_name, bad_ver)

    # Manifest with unpinned adapter is rejected by gate
    bad_manifest = valid_manifest(adapter={"name": "arbitrary-plugin", "version": "v1"})
    verdict = validate_prerequisite_manifest(bad_manifest)
    assert not verdict.ok
    assert any("pinned" in err.lower() or "unsupported" in err.lower() for err in verdict.errors)


# --- 2. Block missing owners/IDs ---


def test_blocks_missing_owners_and_ids() -> None:
    # Missing owner id
    m1 = valid_manifest(
        owners=[{"path": "openspec/specs/auth/spec.md", "status": "active", "requirements": []}]
    )
    v1 = validate_prerequisite_manifest(m1)
    assert not v1.ok
    assert any("id" in err.lower() for err in v1.errors)

    # Requirement missing id
    m2 = valid_manifest(
        owners=[
            {
                "id": "openspec/specs/auth/spec.md",
                "path": "openspec/specs/auth/spec.md",
                "status": "active",
                "requirements": [{"text": "no id"}],
            }
        ]
    )
    v2 = validate_prerequisite_manifest(m2)
    assert not v2.ok
    assert any("id" in err.lower() for err in v2.errors)

    # Requirement change references non-existent owner_id
    m3 = valid_manifest(
        requirements=[
            {
                "requirement_id": "openspec/specs/auth/spec.md#requirement-login",
                "owner_id": "openspec/specs/nonexistent/spec.md",
                "path": "openspec/specs/nonexistent/spec.md",
                "change": "modified",
            }
        ]
    )
    v3 = validate_prerequisite_manifest(m3)
    assert not v3.ok
    assert any("missing owner" in err.lower() for err in v3.errors)


# --- 3. Block contradictory declared active statuses ---


def test_blocks_contradictory_declared_active_statuses() -> None:
    # Canonical True but status history
    m1 = valid_manifest(
        owners=[
            {
                "id": "spec-1",
                "path": "openspec/specs/spec.md",
                "status": "history",
                "canonical": True,
                "requirements": [],
            }
        ],
        requirements=[],
    )
    v1 = validate_prerequisite_manifest(m1)
    assert not v1.ok
    assert any("contradictory" in err.lower() for err in v1.errors)

    # Historical archive path declared as active
    m2 = valid_manifest(
        owners=[
            {
                "id": "archive-1",
                "path": "openspec/changes/archive/2026/specs/spec.md",
                "status": "active",
                "canonical": True,
                "requirements": [],
            }
        ],
        requirements=[],
    )
    v2 = validate_prerequisite_manifest(m2)
    assert not v2.ok
    assert any("historical" in err.lower() or "archive" in err.lower() or "contradictory" in err.lower() for err in v2.errors)

    # Duplicate owner with contradictory statuses
    m3 = valid_manifest(
        owners=[
            {"id": "spec-1", "path": "specs/a.md", "status": "active", "canonical": True, "requirements": []},
            {"id": "spec-1", "path": "specs/a.md", "status": "history", "canonical": False, "requirements": []},
        ],
        requirements=[],
    )
    v3 = validate_prerequisite_manifest(m3)
    assert not v3.ok
    assert any("contradictory" in err.lower() or "duplicate" in err.lower() for err in v3.errors)


# --- 4. Block missing affected references ---


def test_blocks_missing_affected_references() -> None:
    # Unresolved reference on affected requirement
    m = valid_manifest(
        unresolved_references=[
            {
                "source_id": "openspec/specs/auth/spec.md#requirement-login",
                "target": "openspec/specs/missing/spec.md#requirement-missing",
                "reason": "no-active-target",
            }
        ]
    )
    verdict = validate_prerequisite_manifest(m)
    assert not verdict.ok
    assert any("missing affected reference" in err.lower() for err in verdict.errors)


# --- 5. Block skipped required validation/sync ---


@pytest.mark.parametrize(
    "flag,value",
    [
        ("sync_status", "skipped"),
        ("sync_status", "out_of_sync"),
        ("validation_status", "skipped"),
        ("validation_status", "failed"),
        ("skipped_validation", True),
        ("skipped_sync", True),
        ("sync_required", True),  # sync_required=True without synced=True
    ],
)
def test_blocks_skipped_required_validation_and_sync(flag, value) -> None:
    manifest = valid_manifest(**{flag: value})
    verdict = validate_prerequisite_manifest(manifest)
    assert not verdict.ok
    assert any("skipped" in err.lower() or "sync" in err.lower() or "validation" in err.lower() for err in verdict.errors)


# --- 6. Ambiguous semantic overlap is a review finding, not deterministic proof ---


def test_ambiguous_semantic_overlap_is_review_finding() -> None:
    # Manifest with unmapped files
    m = valid_manifest(unmapped_files=["src/auth/provider.py", "docs/notes.txt"])
    verdict = validate_prerequisite_manifest(m)
    # Must NOT fail the deterministic gate
    assert verdict.ok
    assert len(verdict.errors) == 0
    # Must record review findings
    assert len(verdict.review_findings) > 0
    assert any("unmapped" in rf.lower() or "semantic overlap" in rf.lower() for rf in verdict.review_findings)


# --- 7. Historical docs preservation and client neutrality ---


def test_preserves_historical_docs_and_client_neutrality() -> None:
    # Historical document with status='history' and canonical=False is accepted
    m = valid_manifest(
        owners=[
            {
                "id": "openspec/changes/archive/v1/specs/old.md",
                "path": "openspec/changes/archive/v1/specs/old.md",
                "status": "history",
                "canonical": False,
                "requirements": [
                    {
                        "id": "openspec/changes/archive/v1/specs/old.md#requirement-old",
                        "text": "Deprecated requirement.",
                        "dependencies": [],
                    }
                ],
            }
        ],
        requirements=[],
        changed_files=["openspec/changes/archive/v1/specs/old.md"],
    )
    verdict = validate_prerequisite_manifest(m)
    assert verdict.ok
    assert not verdict.errors


# --- 8. Contract preflight check enforces spec governance before implementation ---


def test_contract_check_blocks_invalid_manifest_before_implementation(tmp_path: Path) -> None:
    contracts = tmp_path / ".stagemesh" / "contracts"
    contracts.mkdir(parents=True)
    task_id = "TASK-SPEC-1"
    contract_data = {
        "objective": "Spec governed change",
        "explicit": True,
        "spec_refs": ["openspec/specs/auth/spec.md"],
        "required_tests": [{"name": "unit", "command": "pytest -q"}],
    }
    (contracts / f"{task_id}.json").write_text(json.dumps(contract_data), encoding="utf-8")

    # Reject when invalid prerequisite manifest is provided
    bad_manifest = valid_manifest(sync_status="skipped")
    report = check_contract(
        tmp_path,
        task_id=task_id,
        prerequisite_manifest=bad_manifest,
    )
    assert not report["ok"]
    assert any("skipped" in err.lower() or "sync" in err.lower() for err in report["errors"])

    # Pass when valid manifest is provided, recording review findings if unmapped files exist
    ok_manifest = valid_manifest(unmapped_files=["src/helper.py"])
    report_ok = check_contract(
        tmp_path,
        task_id=task_id,
        prerequisite_manifest=ok_manifest,
    )
    assert report_ok["ok"]
    assert any("unmapped" in w.lower() or "semantic overlap" in w.lower() for w in report_ok.get("warnings", []))


# --- 10. Regressions for Review Findings ---


def test_adapter_pin_and_validation_sync_evidence_required_cannot_bypass_by_omission() -> None:
    # Finding 1 probe: {"schema_version":1,"adapter":{"name":"openspec"},"owners":[]}
    probe = {"schema_version": 1, "adapter": {"name": "openspec"}, "owners": []}
    verdict = validate_prerequisite_manifest(probe)
    assert not verdict.ok
    assert any("version" in err.lower() or "pin" in err.lower() for err in verdict.errors)
    assert any("validation" in err.lower() for err in verdict.errors)
    assert any("sync" in err.lower() for err in verdict.errors)

    # Manifest with adapter but missing version
    m_no_ver = valid_manifest(adapter={"name": "openspec"})
    v_no_ver = validate_prerequisite_manifest(m_no_ver)
    assert not v_no_ver.ok
    assert any("version" in err.lower() or "pin" in err.lower() for err in v_no_ver.errors)

    # Manifest missing validation_status
    m_no_val = valid_manifest()
    del m_no_val["validation_status"]
    v_no_val = validate_prerequisite_manifest(m_no_val)
    assert not v_no_val.ok
    assert any("validation" in err.lower() for err in v_no_val.errors)

    # Manifest missing sync_status
    m_no_sync = valid_manifest()
    del m_no_sync["sync_status"]
    v_no_sync = validate_prerequisite_manifest(m_no_sync)
    assert not v_no_sync.ok
    assert any("sync" in err.lower() for err in v_no_sync.errors)


def test_missing_affected_ids_and_dependencies_validated_against_declared_records() -> None:
    # Finding 4 probe: active owner dependency on 'absent' and affected requirement 'nonexistent'
    probe = valid_manifest(
        owners=[
            {
                "id": "owner-1",
                "path": "openspec/specs/auth/spec.md",
                "status": "active",
                "canonical": True,
                "dependencies": ["absent"],
                "requirements": [
                    {
                        "id": "openspec/specs/auth/spec.md#requirement-login",
                        "text": "Must authenticate.",
                        "dependencies": [],
                    }
                ],
            }
        ],
        requirements=[
            {
                "requirement_id": "nonexistent",
                "owner_id": "owner-1",
                "path": "openspec/specs/auth/spec.md",
                "change": "modified",
            }
        ],
        unresolved_references=[],  # Deliberately empty to test against declared records
    )
    verdict = validate_prerequisite_manifest(probe)
    assert not verdict.ok
    assert any("absent" in err for err in verdict.errors)
    assert any("nonexistent" in err for err in verdict.errors)


def test_validator_binds_candidate_base_and_diff(tmp_path: Path) -> None:
    # Finding 2: Validator must compare candidate_revision, base_revision, and changed_files
    project = tmp_path / "project"
    project.mkdir()
    git = GitWorkspace(project)
    git.init_if_needed()
    git.run("config", "user.email", "test@example.invalid")
    git.run("config", "user.name", "Test Owner")
    (project / ".gitignore").write_text(".stagemesh/\n", encoding="utf-8")
    (project / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (project / "test_app.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    base_sha = git.commit_all("baseline")

    store = Store(project / ".stagemesh/state.sqlite3")
    store.migrate()
    task_id = store.upsert_task("Implement spec requirement", source_id="TASK-BIND-1")
    contract_dir = project / ".stagemesh/contracts"
    contract_dir.mkdir(parents=True)
    contract_payload = {
        "objective": "Implement spec requirement",
        "allowed_files": ["app.py", "openspec/specs/auth/spec.md"],
        "spec_refs": ["openspec/specs/auth/spec.md"],
        "required_tests": [{"name": "unit", "command": [sys.executable, "-m", "pytest", "-q", "test_app.py"]}],
    }
    (contract_dir / f"{task_id}.json").write_text(json.dumps(contract_payload), encoding="utf-8")

    (project / "app.py").write_text("print('updated')\n", encoding="utf-8")
    cand_sha = git.commit_all("implementation")
    store.add_candidate(task_id, cand_sha, "implementer", True)

    try:
        # 1. Stale manifest with mismatched candidate_revision must fail
        stale_cand = valid_manifest(
            base_revision=base_sha,
            candidate_revision="stale-candidate-sha",
            changed_files=["app.py"],
        )
        status1 = Validator().validate(store, task_id, cand_sha, project, prerequisite_manifest=stale_cand)
        assert status1 is EvidenceStatus.FAILED

        # 2. Stale manifest with mismatched base_revision must fail
        stale_base = valid_manifest(
            base_revision="stale-base-sha",
            candidate_revision=cand_sha,
            changed_files=["app.py"],
        )
        status2 = Validator().validate(store, task_id, cand_sha, project, prerequisite_manifest=stale_base)
        assert status2 is EvidenceStatus.FAILED

        # 3. Stale manifest with mismatched changed_files diff must fail
        stale_diff = valid_manifest(
            base_revision=base_sha,
            candidate_revision=cand_sha,
            changed_files=["unrelated.py"],
        )
        status3 = Validator().validate(store, task_id, cand_sha, project, prerequisite_manifest=stale_diff)
        assert status3 is EvidenceStatus.FAILED

        # 4. Correctly bound manifest passes
        correct = valid_manifest(
            base_revision=base_sha,
            candidate_revision=cand_sha,
            changed_files=["app.py"],
        )
        status4 = Validator().validate(store, task_id, cand_sha, project, prerequisite_manifest=correct)
        assert status4 is EvidenceStatus.PASSED
    finally:
        store.close()


def test_preflight_blocks_missing_or_corrupt_manifest_for_governed_changes(tmp_path: Path) -> None:
    # Finding 3: Preflight must require manifest for governed changes and report corrupt JSON
    contracts = tmp_path / ".stagemesh" / "contracts"
    contracts.mkdir(parents=True)
    task_id = "TASK-SPEC-GOV"
    contract_data = {
        "objective": "Spec governed change",
        "explicit": True,
        "spec_refs": ["openspec/specs/auth/spec.md"],
        "required_tests": [{"name": "unit", "command": "pytest -q"}],
    }
    contract_file = contracts / f"{task_id}.json"
    contract_file.write_text(json.dumps(contract_data), encoding="utf-8")

    # 1. Missing manifest for governed change fails preflight
    report_missing = check_contract(tmp_path, task_id=task_id)
    assert not report_missing["ok"]
    assert any("manifest" in err.lower() and "require" in err.lower() for err in report_missing["errors"])

    # 2. Corrupt manifest file reports loading failure instead of silent bypass
    manifest_file = contracts / f"{task_id}.spec_manifest.json"
    manifest_file.write_text("NOT_VALID_JSON{", encoding="utf-8")
    report_corrupt = check_contract(tmp_path, task_id=task_id)
    assert not report_corrupt["ok"]
    assert any("corrupt" in err.lower() or "cannot read" in err.lower() or "json" in err.lower() for err in report_corrupt["errors"])



def test_validator_enforces_spec_governance_before_review(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    git = GitWorkspace(project)
    git.init_if_needed()
    git.run("config", "user.email", "test@example.invalid")
    git.run("config", "user.name", "Test Owner")
    (project / ".gitignore").write_text(".stagemesh/\n", encoding="utf-8")
    (project / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (project / "test_app.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    base_sha = git.commit_all("baseline")

    store = Store(project / ".stagemesh/state.sqlite3")
    store.migrate()
    task_id = store.upsert_task("Implement spec requirement", source_id="TASK-GOV-1")
    contract_dir = project / ".stagemesh/contracts"
    contract_dir.mkdir(parents=True)
    contract_payload = {
        "objective": "Implement spec requirement",
        "allowed_files": ["app.py", "openspec/specs/auth/spec.md"],
        "spec_refs": ["openspec/specs/auth/spec.md"],
        "required_tests": [{"name": "unit", "command": [sys.executable, "-m", "pytest", "-q", "test_app.py"]}],
    }
    (contract_dir / f"{task_id}.json").write_text(json.dumps(contract_payload), encoding="utf-8")

    (project / "app.py").write_text("print('updated')\n", encoding="utf-8")
    sha = git.commit_all("implementation")
    store.add_candidate(task_id, sha, "implementer", True)

    try:
        # Invalid prerequisite manifest fails validation before review
        bad_manifest = valid_manifest(
            candidate_revision=sha,
            base_revision=base_sha,
            changed_files=["app.py"],
            owners=[{"id": "", "path": "openspec/specs/auth/spec.md", "status": "active"}],
        )
        status = Validator().validate(
            store, task_id, sha, project, prerequisite_manifest=bad_manifest
        )
        assert status is EvidenceStatus.FAILED
        row = store.conn.execute(
            "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind='VALIDATION' ORDER BY created_at DESC LIMIT 1",
            (task_id, sha),
        ).fetchone()
        payload = json.loads(row["payload"])
        assert "spec_governance" in payload
        assert not payload["spec_governance"]["ok"]

        # Valid prerequisite manifest with unmapped files passes validation with review finding
        ok_manifest = valid_manifest(
            candidate_revision=sha,
            base_revision=base_sha,
            changed_files=["app.py"],
            unmapped_files=["unmapped_audit.log"],
        )
        status_ok = Validator().validate(
            store, task_id, sha, project, prerequisite_manifest=ok_manifest
        )
        assert status_ok is EvidenceStatus.PASSED
        row_ok = store.conn.execute(
            "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind='VALIDATION' ORDER BY created_at DESC LIMIT 1",
            (task_id, sha),
        ).fetchone()
        payload_ok = json.loads(row_ok["payload"])
        assert payload_ok["spec_governance"]["ok"]
        assert any(
            f.get("code") == "spec_semantic_overlap" and f.get("severity") == "warning"
            for f in payload_ok["findings"]
        )
    finally:
        store.close()


def test_revision_identity_rejects_prefixes_and_accepts_the_exact_value() -> None:
    exact = valid_manifest()
    assert validate_prerequisite_manifest(
        exact,
        expected_candidate_revision="candidate-sha-456",
        expected_base_revision="base-sha-123",
        expected_changed_files=["openspec/specs/auth/spec.md"],
    ).ok

    short = valid_manifest(candidate_revision="candidate")
    short_verdict = validate_prerequisite_manifest(short, expected_candidate_revision="candidate-sha-456")
    assert not short_verdict.ok
    assert any("candidate_revision" in err for err in short_verdict.errors)

    other_direction = valid_manifest(candidate_revision="candidate-sha-456-extra")
    assert not validate_prerequisite_manifest(other_direction, expected_candidate_revision="candidate-sha-456").ok

    stale = valid_manifest(base_revision="base-sha-122")
    assert not validate_prerequisite_manifest(stale, expected_base_revision="base-sha-123").ok

    malformed = valid_manifest(candidate_revision=456)
    assert not validate_prerequisite_manifest(malformed, expected_candidate_revision="456").ok

    shared_prefix = valid_manifest(candidate_revision="abc1234")
    assert not validate_prerequisite_manifest(shared_prefix, expected_candidate_revision="abc1234deadbeef").ok


def test_preflight_forwards_expected_revisions() -> None:
    text = json.dumps(
        {
            "objective": "governed",
            "explicit": True,
            "spec_refs": ["openspec/specs/auth/spec.md"],
            "allowed_files": ["openspec/specs/auth/spec.md"],
            "required_tests": [{"name": "unit", "command": ["pytest", "-q"]}],
        }
    )
    manifest = valid_manifest(candidate_revision="abc")
    report = check_contract_text(text, prerequisite_manifest=manifest, expected_candidate_revision="abcdef")
    assert not report["ok"]
    assert any("candidate_revision" in err for err in report["errors"])
