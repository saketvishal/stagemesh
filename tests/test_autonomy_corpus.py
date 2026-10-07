"""The incident corpus is complete, its tests exist, and docs/founder-hands-off.md tells the truth about it."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from stagemesh.autonomy.corpus import (
    CAPABILITY_LEVELS,
    INCIDENT_CORPUS,
    MAX_LEVEL,
    capability_readiness_percent,
    scenario_summary,
)
from stagemesh.autonomy.decisions import REQUIRED_STREAK

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "founder-hands-off.md"


def _test_names(path: Path) -> set[str]:
    return {node.name for node in ast.parse(path.read_text(encoding="utf-8")).body if isinstance(node, ast.FunctionDef)}


def test_every_scenario_a_to_l_is_present_exactly_once() -> None:
    assert [s.id for s in INCIDENT_CORPUS] == list("ABCDEFGHIJKL")


def test_every_referenced_regression_test_exists() -> None:
    for scenario in INCIDENT_CORPUS:
        for test in scenario.tests:
            path, _, name = test.ref.partition("::")
            assert (ROOT / path).is_file(), f"scenario {scenario.id}: {path} is missing"
            assert name in _test_names(ROOT / path), f"scenario {scenario.id}: {test.ref} does not exist"


def test_every_scenario_has_a_success_path_and_a_fail_closed_path() -> None:
    for scenario in INCIDENT_CORPUS:
        kinds = {t.kind for t in scenario.tests}
        assert kinds == {"success", "fail_closed"}, f"scenario {scenario.id} covers only {kinds}"


def test_no_scenario_test_is_referenced_twice() -> None:
    refs = [t.ref for s in INCIDENT_CORPUS for t in s.tests]
    assert len(refs) == len(set(refs))


def test_first_milestone_is_the_four_requested_situations() -> None:
    # external workspace mutation, ordinary main advancement, equivalent-tree rewrite, candidate CI failure vs base CI failure
    assert [s.id for s in INCIDENT_CORPUS if s.milestone_one] == ["A", "B", "C", "E", "F"]


def test_readiness_model_is_well_formed() -> None:
    assert all(0 <= level <= MAX_LEVEL for level, _ in CAPABILITY_LEVELS.values())
    assert not any(level == MAX_LEVEL for level, _ in CAPABILITY_LEVELS.values()), "level 3 needs proof on real tasks"
    assert 0 <= capability_readiness_percent() <= 100


# --- the canonical document -----------------------------------------------------------------------------------------------------------------


def test_document_exists_and_is_the_only_hands_off_roadmap() -> None:
    assert DOC.is_file()
    others = [p.name for p in (ROOT / "docs").glob("*.md") if re.search(r"hands.?off|autonomy.*roadmap", p.name, re.IGNORECASE) and p != DOC]
    assert others == []


def test_document_states_the_same_numbers_as_the_code() -> None:
    text = DOC.read_text(encoding="utf-8")
    summary = scenario_summary()
    assert f"**{capability_readiness_percent()}%**" in text
    assert f"{summary['tests']} mapped tests" in text
    assert f"**{len(INCIDENT_CORPUS)} of 12**" in text
    for scenario in INCIDENT_CORPUS:
        assert re.search(rf"^\| {scenario.id} \| {re.escape(scenario.title)} \|", text, re.MULTILINE), f"scenario {scenario.id} missing from the table"
    for name, (level, _) in CAPABILITY_LEVELS.items():
        label = name.split(" ", 1)[1] if name[0].isdigit() else name
        assert re.search(rf"^\| (\d+|–) \| {re.escape(label.split(' (')[0])}.*\| {level} \|", text, re.MULTILINE), f"capability {name!r} level {level} not in the document"


def _streak_rows(text: str) -> list[list[str]]:
    block = text.split("<!-- streak:begin -->")[1].split("<!-- streak:end -->")[0]
    rows = [[c.strip() for c in line.strip().strip("|").split("|")] for line in block.splitlines() if line.strip().startswith("|")]
    return [row for row in rows[2:] if row and row[0].isdigit()]  # skip the header and separator


def test_document_cannot_claim_the_gate_without_ten_consecutive_clean_tasks() -> None:
    text = DOC.read_text(encoding="utf-8")
    rows = _streak_rows(text)
    streak = 0
    for row in rows:
        streak = streak + 1 if row[3] == "0" and row[4].lower() in {"yes", "true"} else 0
    claims_met = "**Founder Hands-Off gate** | **MET**" in text
    assert claims_met == (streak >= REQUIRED_STREAK)
    assert f"**{streak} / {REQUIRED_STREAK}**" in text, "the documented streak does not match the streak table"


def test_readme_points_to_the_document_instead_of_duplicating_it() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/founder-hands-off.md" in readme
