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


def test_split_command_and_build_executable_argv_safety():
    import sys
    from stagemesh.validation import build_executable_argv, split_command

    # Never invoke .py directly
    cmd_py = "tests/test_foo.py"
    argv = build_executable_argv(cmd_py)
    assert argv[0] == sys.executable
    assert argv[1:3] == ["-m", "pytest"]
    assert argv[3] == "tests/test_foo.py"

    # Pytest command transformed to sys.executable -m pytest
    cmd_pytest = "pytest tests/test_foo.py -q -k 'my_test'"
    argv_pytest = build_executable_argv(cmd_pytest)
    assert argv_pytest[0] == sys.executable
    assert argv_pytest[1:3] == ["-m", "pytest"]
    assert argv_pytest[3] == "tests/test_foo.py"

    # Robust legacy shlex splitting handles quotes without empty strings
    parts = split_command('pytest "tests/my file.py" -q')
    assert parts == ["pytest", "tests/my file.py", "-q"]


def test_real_subprocess_validation_executes_pytest(tmp_path: Path):
    import subprocess
    import sys
    from stagemesh.domain import EvidenceStatus
    from stagemesh.persistence import Store
    from stagemesh.validation import AffectedTestDiscovery, Validator

    # Initialize a real git repo
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)

    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True)
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir(parents=True)

    calc_file = src_dir / "calc.py"
    calc_file.write_text("def add(a, b): return a + b\n", encoding="utf-8")
    test_file = tests_dir / "test_calc.py"
    test_file.write_text("from src.calc import add\ndef test_add(): assert add(2, 3) == 5\n", encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=tmp_path, check=True, capture_output=True)
    base_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True).stdout.strip()

    # Modify calc.py to trigger candidate change
    calc_file.write_text("def add(a, b): return a + b + 0\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "candidate change"], cwd=tmp_path, check=True, capture_output=True)
    candidate_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True).stdout.strip()

    db = tmp_path / "stagemesh.sqlite3"
    store = Store(db)
    store.migrate()
    task_id = store.upsert_task("Test task for real validation", source="local")
    store.add_candidate(task_id, candidate_sha, "test-builder", durable_handoff=True, base_sha=base_sha)

    discovery = AffectedTestDiscovery({
        "src/calc.py": ["pytest tests/test_calc.py -q"],
    })
    # Real validator with NO mock runner
    validator = Validator(discovery=discovery)
    status = validator.validate(store, task_id, candidate_sha, tmp_path, base_sha=base_sha)

    assert status == EvidenceStatus.PASSED
    # Check that execution and evidence were recorded in store
    evidence = store.has_evidence(task_id, candidate_sha, kind=from_str_kind("VALIDATION"))
    assert evidence is True


def test_multi_commit_candidate_range_discovers_all_changed_files(tmp_path: Path):
    import subprocess
    from stagemesh.domain import EvidenceStatus
    from stagemesh.persistence import Store
    from stagemesh.validation import AffectedTestDiscovery, Validator

    # Initialize a real git repo
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)

    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True)
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir(parents=True)

    file_a = src_dir / "mod_a.py"
    file_a.write_text("x = 1\n", encoding="utf-8")
    file_b = src_dir / "mod_b.py"
    file_b.write_text("y = 2\n", encoding="utf-8")

    test_a = tests_dir / "test_a.py"
    test_a.write_text("def test_a(): pass\n", encoding="utf-8")
    test_b = tests_dir / "test_b.py"
    test_b.write_text("def test_b(): pass\n", encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial baseline"], cwd=tmp_path, check=True, capture_output=True)
    base_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True).stdout.strip()

    # Multi-commit candidate:
    # Commit 1: modifies mod_a.py
    file_a.write_text("x = 10\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/mod_a.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "commit 1: mod_a"], cwd=tmp_path, check=True, capture_output=True)

    # Commit 2: modifies mod_b.py (this is candidate_sha)
    file_b.write_text("y = 20\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/mod_b.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "commit 2: mod_b"], cwd=tmp_path, check=True, capture_output=True)
    candidate_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True).stdout.strip()

    executed_commands = []
    def recording_runner(cmd, cwd):
        executed_commands.append(cmd)
        return True, "ok"

    discovery = AffectedTestDiscovery({
        "src/mod_a.py": ["pytest tests/test_a.py -q"],
        "src/mod_b.py": ["pytest tests/test_b.py -q"],
    })
    validator = Validator(discovery=discovery, command_runner=recording_runner)

    db = tmp_path / "stagemesh.sqlite3"
    store = Store(db)
    store.migrate()
    task_id = store.upsert_task("Multi-commit validation task", source="local")

    # When base_sha is passed or resolved from baseline, BOTH test_a and test_b must be discovered!
    status = validator.validate(store, task_id, candidate_sha, tmp_path, base_sha=base_sha)
    assert status == EvidenceStatus.PASSED
    assert "pytest tests/test_a.py -q" in executed_commands
    assert "pytest tests/test_b.py -q" in executed_commands


def from_str_kind(name: str):
    from stagemesh.domain import EvidenceKind
    return EvidenceKind[name]


