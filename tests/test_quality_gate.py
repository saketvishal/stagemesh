from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("quality_gate", ROOT / "scripts/quality_gate.py")
assert SPEC and SPEC.loader
quality_gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = quality_gate
SPEC.loader.exec_module(quality_gate)


def test_seeded_unused_import_is_rejected_with_actionable_ruff_output(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(quality_gate, "ROOT", tmp_path)
    (tmp_path / "bad.py").write_text("import os\n\nvalue = 1\n", encoding="utf-8")
    changes = quality_gate.ChangeSet("a" * 40, "b" * 40, ("bad.py",), ("bad.py",))
    with pytest.raises(quality_gate.QualityGateFailure, match="F401"):
        quality_gate._static_checks(changes, 12)


def test_seeded_complexity_violation_is_rejected_for_new_code(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(quality_gate, "ROOT", tmp_path)
    branches = "\n".join(f"    if value == {index}:\n        return {index}" for index in range(14))
    (tmp_path / "complex.py").write_text(
        f"def decide(value: int) -> int:\n{branches}\n    return -1\n",
        encoding="utf-8",
    )
    changes = quality_gate.ChangeSet("a" * 40, "b" * 40, ("complex.py",), ("complex.py",))
    with pytest.raises(quality_gate.QualityGateFailure, match="C901"):
        quality_gate._static_checks(changes, 12)


@pytest.mark.parametrize(
    "source",
    [
        "from typing import Any\nvalue: Any = 1\n",
        "from typing import cast\nvalue = cast(int, 1)\n",
        "value: int = 'x'  # type: ignore[assignment]\n",
    ],
)
def test_new_type_escape_hatches_are_rejected(source: str, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(quality_gate, "ROOT", tmp_path)
    (tmp_path / "unsafe.py").write_text(source, encoding="utf-8")
    assert quality_gate._type_escape_findings(("unsafe.py",))


def test_strict_type_check_is_bounded_to_declared_targets(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(quality_gate, "ROOT", tmp_path)
    (tmp_path / "strict.py").write_text("value: int = 1\n", encoding="utf-8")
    captured: list[str] = []

    def fake_run(command: list[str], name: str) -> dict[str, object]:
        captured.extend(command)
        return {"name": name, "status": "passed"}

    monkeypatch.setattr(quality_gate, "_run", fake_run)
    changes = quality_gate.ChangeSet("a" * 40, "b" * 40, (), ())
    quality_gate._type_checks(changes, ("strict.py",))

    assert "--strict" in captured
    assert "--follow-imports=skip" in captured
    assert captured[-1] == "strict.py"


def test_stale_or_candidate_modified_policy_hash_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("STAGEMESH_QUALITY_POLICY_SHA256", "0" * 64)
    with pytest.raises(quality_gate.QualityGateFailure, match="trusted validation plan"):
        quality_gate.run("fast", None, None)


def test_exact_range_is_required_for_authoritative_modes(monkeypatch) -> None:
    monkeypatch.delenv("STAGEMESH_QUALITY_BASE_SHA", raising=False)
    monkeypatch.delenv("STAGEMESH_CI_BASE_SHA", raising=False)
    with pytest.raises(quality_gate.QualityGateFailure, match="exact non-zero base SHA"):
        quality_gate._changes("ci", None, "HEAD")
