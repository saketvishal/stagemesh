"""Automated parity evidence integrity test verifying all referenced tests exist and are collected."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_parity_evidence_references_resolve_to_collected_tests():
    """Verify that every test reference of form file.py::test_name exists and is collected by pytest."""
    repo_root = Path(__file__).resolve().parent.parent
    parity_file = repo_root / "docs" / "parity" / "legacy_vnext_parity.json"
    assert parity_file.is_file(), f"Parity ledger not found at {parity_file}"

    with open(parity_file, "r", encoding="utf-8") as f:
        parity_data = json.load(f)

    # Collect all pytest node IDs from the test suite
    cmd = [sys.executable, "-m", "pytest", "--collect-only", "-q"]
    proc = subprocess.run(
        cmd,
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, f"pytest collection failed:\n{proc.stderr}\n{proc.stdout}"

    collected_nodes = set()
    for line in proc.stdout.splitlines():
        line = line.strip()
        if "::" in line:
            # Normalize path separators for comparison
            normalized = line.replace("\\", "/")
            collected_nodes.add(normalized)

    missing_references: list[str] = []
    total_checked = 0

    wave4_items = {
        "OWNERSHIP-004",
        "OBJECTIVES-003",
        "OBJECTIVES-005",
        "OBJECTIVES-006",
        "PROJECTS-005",
        "PROJECTS-006",
        "PERSISTENCE-005",
        "VALIDATION-002",
        "OBSERVABILITY-004",
    }
    checked_wave4: set[str] = set()

    for item in parity_data:
        capability_id = item.get("id", "UNKNOWN")
        vnext_tests = item.get("vnext_tests", [])

        if capability_id in wave4_items:
            assert vnext_tests, f"Wave 4 entry {capability_id} must have non-empty vnext_tests"
            checked_wave4.add(capability_id)

        for ref in vnext_tests:
            if not isinstance(ref, str):
                continue
            normalized_ref = ref.replace("\\", "/")

            if "::" in normalized_ref and ".py::" in normalized_ref:
                file_part, _test_name = normalized_ref.split("::", 1)

                if file_part.startswith("tests/"):
                    target_path = repo_root / file_part
                    total_checked += 1
                    if not target_path.is_file():
                        missing_references.append(
                            f"[{capability_id}] File does not exist: {file_part} (ref: {ref})"
                        )
                    elif normalized_ref not in collected_nodes:
                        missing_references.append(
                            f"[{capability_id}] Test node not collected by pytest: {ref}"
                        )
                elif (repo_root / "tests" / file_part).is_file():
                    total_checked += 1
                    resolved_ref = f"tests/{normalized_ref}"
                    if resolved_ref not in collected_nodes:
                        missing_references.append(
                            f"[{capability_id}] Test node not collected by pytest: {ref} "
                            f"(resolved: {resolved_ref})"
                        )

    assert checked_wave4 == wave4_items, (
        f"Missing Wave 4 items in parity ledger: {wave4_items - checked_wave4}"
    )
    assert total_checked >= 100, f"Expected at least 100 test references checked, got {total_checked}"
    assert not missing_references, (
        f"Found {len(missing_references)} stale or fabricated test reference(s) in "
        f"docs/parity/legacy_vnext_parity.json:\n"
        + "\n".join(f"  - {err}" for err in missing_references)
    )
