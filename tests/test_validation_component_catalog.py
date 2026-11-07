from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.run_ci_component_tests import load_targets
from stagemesh.ci_validation_plan import (
    COMPONENT,
    FULL,
    OPERATOR,
    ci_validation_policy_fingerprint,
    derive_ci_validation_plan,
)
from stagemesh.contracts import ChangeContract
from stagemesh.validation_components import (
    AUDITED_ROOTS,
    component_catalog_fingerprint,
    fast_path_roots,
)
from stagemesh.validation_plan import derive_validation_plan

ROOT = Path(__file__).resolve().parents[1]


def test_catalog_has_unique_existing_roots_and_test_contracts() -> None:
    paths = [root.path for root in AUDITED_ROOTS]
    assert len(paths) == len(set(paths))
    assert fast_path_roots()
    for root in AUDITED_ROOTS:
        assert (ROOT / root.path).is_file(), root.path
        assert root.direct_tests
        assert root.dependent_tests
        for target in (*root.direct_tests, *root.dependent_tests):
            assert target.startswith("tests/test_") and target.endswith(".py")
            assert (ROOT / target).is_file(), target


@pytest.mark.parametrize("root", fast_path_roots(), ids=lambda root: root.component)
def test_local_and_hosted_planners_share_each_fast_path(root) -> None:
    local = derive_validation_plan(
        ChangeContract(objective="Audited component", allowed_files=(root.path,)),
        (root.path,),
    )
    hosted = derive_ci_validation_plan((root.path,))

    assert local.full_regression_required is False
    assert local.affected_components == (root.component,)
    assert local.direct_test_targets == tuple(sorted(set(root.direct_tests)))
    assert local.dependent_test_targets == tuple(sorted(set(root.dependent_tests)))
    assert local.component_policy_fingerprint == component_catalog_fingerprint()
    assert hosted.mode == (OPERATOR if root.family == "operator" else COMPONENT)
    assert hosted.components == (root.component,)
    assert hosted.direct_test_targets == local.direct_test_targets
    assert hosted.dependent_test_targets == tuple(
        target for target in local.dependent_test_targets if target not in local.direct_test_targets
    )
    assert hosted.policy_fingerprint == ci_validation_policy_fingerprint()


def test_compatible_fast_path_families_union_without_claiming_unknown_coverage() -> None:
    paths = (
        "src/stagemesh/capabilities.py",
        "src/stagemesh/task_sources.py",
        "docs/status.md",
    )
    local = derive_validation_plan(ChangeContract("Union", allowed_files=paths), paths)
    hosted = derive_ci_validation_plan(paths)

    assert local.full_regression_required is False
    assert local.affected_components == ("capability_registry", "task_source_sync")
    assert hosted.mode == COMPONENT
    assert hosted.components == local.affected_components
    assert set(hosted.direct_test_targets) == set(local.direct_test_targets)


def test_exclusive_operator_family_mixed_with_another_family_fails_broad() -> None:
    paths = ("src/stagemesh/activity.py", "src/stagemesh/capabilities.py")
    local = derive_validation_plan(ChangeContract("Mixed", allowed_files=paths), paths)
    hosted = derive_ci_validation_plan(paths)

    assert local.affected_components == ("activity_read_model", "capability_registry")
    assert local.full_regression_required is True
    assert "no complete audited fast path" in " ".join(local.full_regression_reasons)
    assert hosted.mode == FULL
    assert "cannot be combined" in " ".join(hosted.reasons)


def test_cross_cutting_audited_root_records_impact_but_never_becomes_fast() -> None:
    path = "src/stagemesh/review.py"
    local = derive_validation_plan(ChangeContract("Review", allowed_files=(path,)), (path,))
    hosted = derive_ci_validation_plan((path,))

    assert local.affected_components == ("review",)
    assert local.full_regression_required is True
    assert hosted.mode == FULL


def test_component_runner_rejects_missing_or_stale_policy_identity(tmp_path: Path) -> None:
    plan = derive_ci_validation_plan(("src/stagemesh/capabilities.py",)).to_dict()
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    assert load_targets(plan_path, "linux")

    plan.pop("policy_fingerprint")
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(ValueError, match="policy fingerprint"):
        load_targets(plan_path, "linux")

    plan["policy_fingerprint"] = "0" * 64
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(ValueError, match="policy fingerprint"):
        load_targets(plan_path, "linux")

    plan["policy_fingerprint"] = ci_validation_policy_fingerprint()
    plan["contract"] = "stagemesh.ci-validation/3"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(ValueError, match="contract is unsupported"):
        load_targets(plan_path, "linux")


def test_selector_catalog_and_acceptance_changes_force_full_ci() -> None:
    for path in (
        "src/stagemesh/validation_components.py",
        "tests/test_validation_component_catalog.py",
    ):
        plan = derive_ci_validation_plan((path,))
        assert plan.mode == FULL
        assert path in " ".join(plan.reasons)


def test_policy_fingerprints_are_stable_sha256_values() -> None:
    assert len(component_catalog_fingerprint()) == 64
    assert len(ci_validation_policy_fingerprint()) == 64
    assert component_catalog_fingerprint() == component_catalog_fingerprint()
    assert ci_validation_policy_fingerprint() == ci_validation_policy_fingerprint()
