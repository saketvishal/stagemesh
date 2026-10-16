"""Reading the implementation under test must not grant permission to change it."""

import sqlite3
from pathlib import Path

from stagemesh.contracts import ChangeContract, scope_classification
from stagemesh.providers import _build_task_prompt


def test_test_only_prompt_allows_relevant_source_reading_without_widening_edit_scope(tmp_path: Path) -> None:
    source = tmp_path / "src" / "theme.css"
    source.parent.mkdir()
    source.write_text(":root { --focus: blue; }\n", encoding="utf-8")
    tests = tmp_path / "tests" / "test_theme.py"
    tests.parent.mkdir()
    tests.write_text("# existing regression tests\n", encoding="utf-8")
    contract = ChangeContract(
        objective="Add theme regression tests",
        allowed_files=("tests/test_theme.py",),
        forbidden_files=("src/**",),
    )
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        task = db.execute("SELECT 'Add theme regression tests' AS title").fetchone()
        prompt = _build_task_prompt("theme-test", task, project=tmp_path, contract=contract)

    assert "Allowed files constrain edits, not read-only investigation." in prompt
    assert "Read directly relevant project source, tests, configuration, and specifications" in prompt
    assert "missing a path required for the task, stop" not in prompt
    assert "complete implementation context" not in prompt
    classified = scope_classification(contract, ["src/theme.css", "tests/test_theme.py"])
    assert classified["in_scope"] == ["tests/test_theme.py"]
    assert "src/theme.css" in classified["forbidden"]
    assert source.read_text(encoding="utf-8") == ":root { --focus: blue; }\n"
