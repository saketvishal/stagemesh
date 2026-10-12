"""Skill selection and delivery at the provider and installed-wheel boundaries."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from stagemesh.contracts import ChangeContract
from stagemesh.providers import RuntimeCommandAdapter, _build_task_prompt, _command_and_input

VERIFY = "verification-before-completion"
DEBUG = "systematic-debugging"
TDD = "test-driven-development"
FRONTEND = "frontend-design"
SOURCE = Path(__file__).resolve().parents[1]


def contract(objective: str, *paths: str) -> ChangeContract:
    return ChangeContract(objective=objective, allowed_files=paths or ("**",))


def task_row(title: str) -> sqlite3.Row:
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        return db.execute("SELECT ? AS title", (title,)).fetchone()


def resources(prompt: str) -> dict[str, Path]:
    return {
        name: Path(json.loads(path))
        for line in prompt.splitlines()
        if line.startswith("- Skill ")
        for name, path in [line.removeprefix("- Skill ").split(": ", 1)]
    }


@pytest.mark.parametrize(
    ("title", "paths", "expected"),
    [
        ("Add intake navigation", ("web/Intake.tsx",), (FRONTEND, TDD, VERIFY)),
        ("Fix keyboard navigation regression", ("web/Intake.tsx",), (FRONTEND, DEBUG, TDD, VERIFY)),
        ("Fix provider timeout", ("src/providers.py",), (DEBUG, TDD, VERIFY)),
        ("Add retry policy", ("src/policy.py",), (TDD, VERIFY)),
        ("Design cache component", ("app/services/cache.py",), (TDD, VERIFY)),
        ("Add web API route", ("app/routes.py",), (TDD, VERIFY)),
        ("Update UI documentation", ("docs/ui.md",), (VERIFY,)),
        ("Adjust spacing and typography", ("web/theme.css",), (FRONTEND, VERIFY)),
        ("Fix button label typo", ("web/Button.tsx",), (FRONTEND, VERIFY)),
        ("Implement frontend navigation", ("**",), (FRONTEND, TDD, VERIFY)),
        ("Complete intake navigation", ("apps/web/**",), (FRONTEND, TDD, VERIFY)),
        ("Complete parser", ("src/**",), (TDD, VERIFY)),
        ("Investigate flaky tests", ("tests/test_parser.py",), (DEBUG, TDD, VERIFY)),
    ],
)
def test_only_relevant_skills_are_selected(
    title: str, paths: tuple[str, ...], expected: tuple[str, ...]
) -> None:
    from stagemesh.agent_skills import select_skills

    assert select_skills(title, contract(title, *paths)) == expected


def test_remediation_selects_debugging_even_without_a_bug_keyword() -> None:
    from stagemesh.agent_skills import select_skills

    assert select_skills(
        "Complete parser", contract("Complete parser", "src/parser.py"), remediation=True
    ) == (DEBUG, TDD, VERIFY)


def test_review_receives_verification_only() -> None:
    from stagemesh.agent_skills import select_skills

    assert select_skills(
        "Fix UI regression", contract("Fix UI regression", "web/Intake.tsx"), stage="review"
    ) == (VERIFY,)


def test_referenced_frontend_spec_selects_design_without_losing_project_spec_content(
    tmp_path: Path,
) -> None:
    specs = tmp_path / "docs"
    specs.mkdir()
    (specs / "frontend.md").write_text(
        "Approved palette: green. Keep contextual menus.", encoding="utf-8"
    )
    bound = ChangeContract(
        objective="Complete the project task",
        allowed_files=("**",),
        spec_refs=("docs/frontend.md",),
    )

    prompt = _build_task_prompt(
        "skill-task", task_row("Complete the project task"), project=tmp_path, contract=bound
    )

    assert "Approved palette: green. Keep contextual menus." in prompt
    assert FRONTEND in resources(prompt)


def test_missing_review_skill_returns_infrastructure_failure_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stagemesh import agent_skills, providers

    monkeypatch.setattr(agent_skills, "SKILL_ROOT", tmp_path / "missing-pack")
    monkeypatch.setattr(providers, "_prepare_review_checkout", lambda *_: "")
    monkeypatch.setattr(providers, "_git_output", lambda *_: "candidate")
    monkeypatch.setattr(
        providers.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("reviewer launched without skills"),
    )

    result = json.loads(
        RuntimeCommandAdapter("test", (sys.executable,)).review_candidate(
            "Return JSON only.", tmp_path, "candidate"
        )
    )

    assert result["decision"] == "INFRASTRUCTURE_FAILURE"
    assert result["reason"].startswith("skill_pack_unavailable:")


def test_title_and_loaded_contract_both_drive_worker_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "stagemesh.contracts.load_contract", lambda *_: contract("Add navigation", "web/Intake.tsx")
    )
    prompt = _build_task_prompt("skill-task", task_row("Fix regression"), project=tmp_path)

    assert tuple(resources(prompt)) == (FRONTEND, DEBUG, TDD, VERIFY)
    assert "Add navigation" in prompt
    assert "StageMesh owns all Git commits" in prompt


@pytest.mark.parametrize("provider", ["codex", "claude", "grok", "agy"])
def test_each_provider_receives_readable_selected_skill_files(provider: str) -> None:
    prompt = _build_task_prompt(
        "skill-task",
        task_row("Fix UI regression"),
        contract=contract("Fix UI regression", "web/Intake.tsx"),
    )
    command, stdin = _command_and_input((provider,), prompt)
    delivered = stdin or command[command.index("--print" if provider == "agy" else "-p") + 1]

    assert tuple(resources(delivered)) == (FRONTEND, DEBUG, TDD, VERIFY)
    for path in resources(delivered).values():
        assert path.is_absolute()
        assert path.read_text(encoding="utf-8").startswith("---\nname:")
    # Full skill bodies are read from disk, keeping single-turn CLI arguments bounded.
    assert len(delivered) < 12_000


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_damaged_selected_resources_fail_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    from stagemesh import agent_skills

    pack = tmp_path / "skills"
    shutil.copytree(agent_skills.SKILL_ROOT, pack)
    path = pack / "superpowers/systematic-debugging/root-cause-tracing.md"
    if damage == "missing":
        path.unlink()
    else:
        path.write_text("corrupted resource", encoding="utf-8")
    monkeypatch.setattr(agent_skills, "SKILL_ROOT", pack)

    with pytest.raises(agent_skills.SkillPackError, match="root-cause-tracing.md"):
        agent_skills.skill_prompt("Fix parser", contract("Fix parser", "src/parser.py"))
    # Damage to an unrelated skill does not block a documentation-only task.
    assert tuple(
        resources(
            agent_skills.skill_prompt("Update docs", contract("Update docs", "docs/guide.md"))
        )
    ) == (VERIFY,)


def test_missing_pack_stops_worker_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stagemesh import agent_skills, providers

    monkeypatch.setattr(agent_skills, "SKILL_ROOT", tmp_path / "missing-pack")
    monkeypatch.setattr(providers, "record_task_baseline", lambda *_: "baseline")
    monkeypatch.setattr(
        providers,
        "bind_task_contract",
        lambda *_: SimpleNamespace(contract=contract("Update docs", "docs/guide.md")),
    )
    monkeypatch.setattr(providers, "remediation_context", lambda *_: None)
    monkeypatch.setattr(
        providers.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("worker launched without skills"),
    )
    store = SimpleNamespace(get_task=lambda _: task_row("Update docs"))
    lease = SimpleNamespace(path=tmp_path)

    result = RuntimeCommandAdapter("test", (sys.executable,))._execute_owned(
        store, "skill-task", None, tmp_path, lease
    )

    assert result.status == "FAILED"
    assert result.failure_reason.startswith("skill_pack_unavailable:")


def test_reviewer_receives_skill_guidance_and_keeps_structured_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stagemesh import providers

    captured: list[str] = []
    monkeypatch.setattr(providers, "_prepare_review_checkout", lambda *_: "")
    monkeypatch.setattr(providers, "_git_output", lambda *_: "candidate")
    monkeypatch.setattr(providers, "_tracked_content_changed", lambda *_: False)
    monkeypatch.setattr(providers, "provider_environment", lambda *_: {})
    monkeypatch.setattr(
        providers.subprocess, "Popen", lambda *_args, **_kwargs: SimpleNamespace(returncode=0)
    )

    def communicate(_proc: object, prompt: str, _timeout: float) -> tuple[str, str, bool]:
        captured.append(prompt)
        return '{"decision":"PASS"}', "", False

    monkeypatch.setattr(providers, "communicate_bounded", communicate)
    response = RuntimeCommandAdapter("test", (sys.executable,)).review_candidate(
        "Return JSON only.", tmp_path, "candidate"
    )

    assert response == '{"decision":"PASS"}'
    assert tuple(resources(captured[0])) == (VERIFY,)
    assert "Return JSON only." in captured[0]
    assert "read-only" in captured[0]


def test_built_wheel_delivers_all_resources_without_source_checkout(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("skill_test_backend", SOURCE / "build_backend.py")
    assert spec and spec.loader
    backend = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backend)
    wheel = tmp_path / backend.build_wheel(str(tmp_path))
    installed = tmp_path / "installed"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(installed)
    consumer = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from stagemesh.agent_skills import skill_prompt
from stagemesh.contracts import ChangeContract
prompt = skill_prompt('Fix UI regression', ChangeContract(objective='Fix UI regression', allowed_files=('web/Page.tsx',)))
paths = [Path(json.loads(line.split(': ', 1)[1])) for line in prompt.splitlines() if line.startswith('- Skill ')]
assert len(paths) == 4
assert all(p.is_relative_to(Path(sys.argv[1])) and p.read_text(encoding='utf-8') for p in paths)
print(json.dumps([p.parent.name for p in paths]))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", consumer, str(installed)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == [FRONTEND, DEBUG, TDD, VERIFY]
