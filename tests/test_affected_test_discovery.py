"""Wave 4 — VALIDATION-002: affected-test discovery.

Legacy contract (build_coordinator/runner/validation.py):
  - changed source file maps to intended focused tests
  - multiple changed files produce a union
  - unknown file falls back to full baseline validation
  - no affected tests does not silently skip required baseline
  - path normalization works cross-platform (backslash → forward slash)
  - deterministic (sorted) ordering
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stagemesh.validation import AffectedTestDiscovery, ValidationDiscoveryError


BASELINE = ["pytest tests/ -v"]


# ---------------------------------------------------------------------------
# Single changed file → specific test
# ---------------------------------------------------------------------------

def test_known_source_maps_to_specific_tests():
    disc = AffectedTestDiscovery()
    disc.register("src/stagemesh/providers.py", ["pytest tests/test_provider_failover.py -v"])
    result = disc.discover(["src/stagemesh/providers.py"], baseline_commands=BASELINE)
    assert "pytest tests/test_provider_failover.py -v" in result
    # baseline should NOT be included (all files have a mapping)
    assert BASELINE[0] not in result


def test_known_source_with_suffix_match():
    """Pattern matched by path suffix, not full path."""
    disc = AffectedTestDiscovery()
    disc.register("stagemesh/providers.py", ["pytest tests/test_providers.py"])
    result = disc.discover(
        ["/home/user/projects/myproject/src/stagemesh/providers.py"],
        baseline_commands=BASELINE,
    )
    assert "pytest tests/test_providers.py" in result


# ---------------------------------------------------------------------------
# Multiple changed files → union of tests
# ---------------------------------------------------------------------------

def test_multiple_known_files_produce_union():
    disc = AffectedTestDiscovery()
    disc.register("stagemesh/providers.py", ["pytest tests/test_providers.py"])
    disc.register("stagemesh/registry.py", ["pytest tests/test_registry.py"])
    result = disc.discover(
        ["src/stagemesh/providers.py", "src/stagemesh/registry.py"],
        baseline_commands=BASELINE,
    )
    assert "pytest tests/test_providers.py" in result
    assert "pytest tests/test_registry.py" in result


def test_union_contains_no_duplicates():
    disc = AffectedTestDiscovery()
    disc.register("module_a.py", ["pytest tests/test_shared.py"])
    disc.register("module_b.py", ["pytest tests/test_shared.py"])  # same test
    result = disc.discover(
        ["src/module_a.py", "src/module_b.py"],
        baseline_commands=None,
    )
    assert result.count("pytest tests/test_shared.py") == 1


# ---------------------------------------------------------------------------
# Unknown file → fallback to baseline
# ---------------------------------------------------------------------------

def test_unknown_file_falls_back_to_baseline():
    disc = AffectedTestDiscovery()
    disc.register("stagemesh/providers.py", ["pytest tests/test_providers.py"])
    result = disc.discover(
        ["src/stagemesh/completely_new_module.py"],  # no mapping
        baseline_commands=BASELINE,
    )
    assert BASELINE[0] in result


def test_mixed_known_and_unknown_includes_baseline():
    """If ANY changed file has no mapping, baseline is included."""
    disc = AffectedTestDiscovery()
    disc.register("stagemesh/providers.py", ["pytest tests/test_providers.py"])
    result = disc.discover(
        ["src/stagemesh/providers.py", "src/stagemesh/unknown_new.py"],
        baseline_commands=BASELINE,
    )
    assert BASELINE[0] in result
    assert "pytest tests/test_providers.py" in result


# ---------------------------------------------------------------------------
# No changed files → never silently skip baseline
# ---------------------------------------------------------------------------

def test_no_changed_files_still_runs_baseline():
    disc = AffectedTestDiscovery()
    result = disc.discover([], baseline_commands=BASELINE)
    assert result == sorted(BASELINE)


def test_no_changed_files_no_baseline_returns_empty():
    disc = AffectedTestDiscovery()
    result = disc.discover([], baseline_commands=None)
    assert result == []


# ---------------------------------------------------------------------------
# Cross-platform path normalisation
# ---------------------------------------------------------------------------

def test_windows_backslash_paths_are_normalised():
    disc = AffectedTestDiscovery()
    disc.register("stagemesh/providers.py", ["pytest tests/test_providers.py"])
    # Simulate Windows path as the changed file
    result = disc.discover(
        ["src\\stagemesh\\providers.py"],
        baseline_commands=BASELINE,
    )
    assert "pytest tests/test_providers.py" in result


def test_mixed_slash_paths_resolve_correctly():
    disc = AffectedTestDiscovery()
    disc.register("stagemesh/registry.py", ["pytest tests/test_registry.py"])
    result = disc.discover(
        ["src/stagemesh\\registry.py"],  # mixed separators
        baseline_commands=BASELINE,
    )
    assert "pytest tests/test_registry.py" in result


# ---------------------------------------------------------------------------
# Deterministic (sorted) ordering
# ---------------------------------------------------------------------------

def test_output_is_sorted_deterministically():
    disc = AffectedTestDiscovery()
    disc.register("mod_a.py", ["pytest tests/test_z.py", "pytest tests/test_a.py"])
    result = disc.discover(["mod_a.py"])
    assert result == sorted(result)


def test_baseline_and_specific_sorted_together():
    disc = AffectedTestDiscovery()
    disc.register("mod_a.py", ["pytest tests/test_z.py"])
    baseline = ["pytest tests/", "pytest tests/test_m.py"]
    result = disc.discover(["totally_unknown.py"], baseline_commands=baseline)
    assert result == sorted(result)


# ---------------------------------------------------------------------------
# register() validation
# ---------------------------------------------------------------------------

def test_register_empty_pattern_raises():
    disc = AffectedTestDiscovery()
    with pytest.raises(ValidationDiscoveryError, match="non-empty"):
        disc.register("  ", ["pytest tests/"])


def test_register_empty_commands_raises():
    disc = AffectedTestDiscovery()
    with pytest.raises(ValidationDiscoveryError, match="non-empty"):
        disc.register("src/module.py", [])


# ---------------------------------------------------------------------------
# AffectedTestDiscovery can be initialised with a pre-built mapping
# ---------------------------------------------------------------------------

def test_init_with_mapping():
    disc = AffectedTestDiscovery({
        "providers.py": ["pytest tests/test_providers.py"],
        "registry.py": ["pytest tests/test_registry.py"],
    })
    result = disc.discover(["src/stagemesh/registry.py"])
    assert "pytest tests/test_registry.py" in result
    assert "pytest tests/test_providers.py" not in result


# ---------------------------------------------------------------------------
# Path boundary safety (no unsafe substring false matches)
# ---------------------------------------------------------------------------

def test_safe_path_boundary_matching_prevents_false_substring_matches():
    disc = AffectedTestDiscovery()
    disc.register("providers.py", ["pytest tests/test_providers.py"])

    # Matching exact path or path component
    assert disc.discover(["src/stagemesh/providers.py"]) == ["pytest tests/test_providers.py"]

    # Must NOT false-match unrelated filename containing "providers.py" as substring
    fallback_baseline = ["pytest tests/full_suite.py"]
    res = disc.discover(["src/stagemesh/custom_providers.py"], baseline_commands=fallback_baseline)
    assert res == fallback_baseline


def test_default_source_test_mapping_present():
    from stagemesh.validation import DEFAULT_SOURCE_TEST_MAPPING
    disc = AffectedTestDiscovery(use_defaults=True)
    assert any("providers.py" in k for k in DEFAULT_SOURCE_TEST_MAPPING)
    res = disc.discover(["src/stagemesh/providers.py"])
    assert any("provider" in cmd for cmd in res)


def test_validator_validate_wires_affected_tests_integration(tmp_path: Path):
    from stagemesh.persistence import Store
    from stagemesh.validation import Validator

    db = tmp_path / "stagemesh.sqlite3"
    store = Store(db)
    store.migrate()

    # Prepopulate a task
    task_id = store.upsert_task("Test task for validation", source="local")

    # Mock command runner to record executed commands
    executed = []
    def recording_runner(cmd, cwd):
        executed.append(cmd)
        return True, "test passed"

    validator = Validator(command_runner=recording_runner)
    # Validate with no git repo -> safely falls back to baseline validation
    status = validator.validate(store, task_id, "abc1234", tmp_path)
    assert status.name == "PASSED"
    assert len(executed) > 0

