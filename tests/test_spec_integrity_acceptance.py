from __future__ import annotations

from pathlib import Path

from stagemesh.spec_integrity import (
    OpenSpecAdapter,
    spec_audit_report,
    validate_prerequisite_manifest,
)


def _owner(owner_id: str, path: str, requirement_id: str, text: str, *, dependencies: list[str] | None = None) -> dict:
    return {
        "id": owner_id,
        "path": path,
        "status": "active",
        "canonical": True,
        "dependencies": [],
        "requirements": [
            {"id": requirement_id, "text": text, "dependencies": list(dependencies or [])}
        ],
    }


def _manifest(**overrides: object) -> dict[str, object]:
    manifest: dict[str, object] = {
        "schema_version": 1,
        "assessment": "structural-only",
        "adapter": {"name": "openspec", "version": "layout-v1"},
        "base_revision": "b" * 40,
        "candidate_revision": "c" * 40,
        "changed_files": [],
        "owners": [_owner("auth", "openspec/specs/auth/spec.md", "AUTH-LOGIN", "Users must log in.")],
        "requirements": [],
        "unmapped_files": [],
        "unresolved_references": [],
        "sync_status": "in_sync",
        "validation_status": "passed",
    }
    manifest.update(overrides)
    return manifest


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_navigation_change_with_two_active_authorities_blocks_with_ids_and_paths() -> None:
    current = _owner("navigation-current", "openspec/specs/navigation/spec.md", "NAV-MOBILE", "Use bottom tabs.")
    old_active = _owner("navigation-old", "docs/sdd/navigation-old.md", "NAV-MOBILE", "Use a side drawer.")

    verdict = validate_prerequisite_manifest(_manifest(owners=[current, old_active]))

    assert not verdict.ok
    finding = next(item for item in verdict.errors if "duplicate authoritative requirement" in item)
    assert "NAV-MOBILE" in finding and "docs/sdd/navigation-old.md" in finding


def test_auth_change_omitting_declared_route_dependency_blocks() -> None:
    auth = _owner(
        "auth",
        "openspec/specs/auth/spec.md",
        "AUTH-LOGIN",
        "Users must log in.",
        dependencies=["ROUTE-AUTH-CALLBACK"],
    )

    verdict = validate_prerequisite_manifest(_manifest(owners=[auth]))

    assert not verdict.ok
    assert any("ROUTE-AUTH-CALLBACK" in item and "AUTH-LOGIN" in item for item in verdict.errors)


def test_differently_worded_possible_duplicate_is_review_uncertainty_not_proof() -> None:
    verdict = validate_prerequisite_manifest(
        _manifest(semantic_overlap="NAV-MOBILE may overlap NAV-SHELL despite different wording")
    )

    assert verdict.ok
    assert verdict.errors == ()
    assert verdict.review_findings == (
        "ambiguous semantic overlap: NAV-MOBILE may overlap NAV-SHELL despite different wording",
    )


def test_requirement_update_with_stale_browser_evidence_blocks_exact_candidate() -> None:
    verdict = validate_prerequisite_manifest(
        _manifest(
            changed_files=["openspec/specs/auth/spec.md"],
            requirements=[
                {
                    "requirement_id": "AUTH-LOGIN",
                    "owner_id": "auth",
                    "path": "openspec/specs/auth/spec.md",
                    "change": "modified",
                }
            ],
            behavioral_evidence=[
                {
                    "kind": "browser",
                    "gate": "browser-e2e",
                    "status": "passed",
                    "candidate_revision": "old-candidate",
                }
            ],
        ),
        expected_candidate_revision="c" * 40,
    )

    assert not verdict.ok
    assert any("exact candidate revision" in item for item in verdict.errors)


def test_historical_sdd_is_preserved_but_not_active_authority(tmp_path: Path) -> None:
    _write(tmp_path, "openspec/specs/navigation/spec.md", "### Requirement: Current\nUse bottom tabs.\n")
    historical = tmp_path / "openspec/changes/archive/old/specs/navigation/spec.md"
    _write(tmp_path, historical.relative_to(tmp_path).as_posix(), "### Requirement: Old\nUse a drawer.\n")

    report = spec_audit_report(tmp_path, OpenSpecAdapter(), project_revision="c" * 40)

    assert report["status"] == "passed"
    assert report["counts"] == {"active_owners": 1, "historical_owners": 1, "pending_changes": 0}
    assert historical.exists()


def test_benign_scoped_requirement_change_passes_with_exact_behavioral_evidence() -> None:
    candidate = "c" * 40
    verdict = validate_prerequisite_manifest(
        _manifest(
            requirements=[
                {
                    "requirement_id": "AUTH-LOGIN",
                    "owner_id": "auth",
                    "path": "openspec/specs/auth/spec.md",
                    "change": "modified",
                }
            ],
            behavioral_evidence=[
                {
                    "kind": "browser",
                    "gate": "browser-e2e",
                    "status": "passed",
                    "candidate_revision": candidate,
                }
            ],
        ),
        expected_candidate_revision=candidate,
        expected_base_revision="b" * 40,
    )

    assert verdict.ok and verdict.errors == ()


def test_unresolved_owner_decision_escalates_without_guessing() -> None:
    verdict = validate_prerequisite_manifest(
        _manifest(owner_decision_required="Founder must choose drawer or bottom-tab navigation")
    )

    assert verdict.ok
    assert any("unresolved owner decision" in item and "drawer or bottom-tab" in item for item in verdict.review_findings)


def test_waiver_request_requires_bounded_reason_and_never_overrides_stale_evidence() -> None:
    stale = _manifest(candidate_revision="stale", waiver_request={"actor": "owner", "reason": "temporary exception"})
    verdict = validate_prerequisite_manifest(stale, expected_candidate_revision="c" * 40)
    assert not verdict.ok and any("does not match candidate" in item for item in verdict.errors)
    assert any("waiver request recorded" in item for item in verdict.review_findings)

    malformed = validate_prerequisite_manifest(
        _manifest(waiver_request={"actor": "owner", "reason": "x" * 501})
    )
    assert not malformed.ok
    assert any("at most 500" in item for item in malformed.errors)

    non_list = validate_prerequisite_manifest(_manifest(requirements={"change": "modified"}))
    assert not non_list.ok
    assert "manifest requirements must be a list" in non_list.errors
