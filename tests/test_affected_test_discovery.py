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
