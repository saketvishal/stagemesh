from __future__ import annotations

import contextlib
import copy
import io
import json
import sys
from pathlib import Path

import pytest

import stagemesh.cli as cli_module
from stagemesh.auto_plan import AutoPlanError, create_contract, plannable
from stagemesh.config import load_config
from stagemesh.contracts import ContractError, GateCommand, canonical_contract_json, parse_contract, run_gate
from stagemesh.profile import ProfileError, load_profile, parse_profile, resolve_type
from stagemesh.persistence import Store
from stagemesh.task_selection import select_next_task

from test_run_ready import _project

PY = sys.executable


def _profile(**overrides) -> dict:
    base = {
        "schema_version": 1,
        "name": "demo",
        "variables": {"PY": PY, "BASE": "main"},
        "env_sets": {"test_db": {"DATABASE_URL": "postgresql://test/${BASE}_test"}},
        "forbidden_files": [".env", "**/.env", "data/**", "**/*.sqlite3", ".stagemesh/**"],
        "defaults": {"max_changed_files": 30, "max_diff_lines": 3000},
        "gates": {
            "docs": {"name": "docs-static-check", "command": ["${PY}", "-c", "pass"], "timeout_seconds": 60},
            "front": {"name": "frontend-tests", "command": ["${PY}", "-c", "pass"], "timeout_seconds": 60},
            "back": {"name": "backend-tests", "command": ["${PY}", "-c", "pass"], "env": "test_db", "cwd": ".", "timeout_seconds": 60},
            "schema": {"name": "schema-migration-tests", "command": ["${PY}", "-c", "pass"], "timeout_seconds": 60},
            "all": {"name": "full-acceptance-regression", "command": ["${PY}", "-c", "pass"], "timeout_seconds": 60},
        },
        "task_types": {
            "prep": {"labels": ["stagemesh:prep", "documentation"], "keywords": ["readiness", "governance"], "allowed_files": ["docs/**", "stagemesh-task-*.txt"], "validation_level": "light", "gates": ["docs"]},
            "frontend": {"labels": ["area:frontend"], "keywords": ["ui", "frontend"], "allowed_files": ["apps/web/**", "stagemesh-task-*.txt"], "validation_level": "standard", "gates": ["docs", "front"]},
            "backend": {"labels": ["area:backend"], "keywords": ["api", "endpoint"], "allowed_files": ["apps/api/app/**", "stagemesh-task-*.txt"], "validation_level": "standard", "gates": ["docs", "back"]},
            "schema": {"labels": ["area:schema"], "keywords": ["migration", "schema"], "allowed_files": ["apps/api/migrations/**"], "validation_level": "standard", "gates": ["back", "schema"]},
            "full": {"labels": ["validation:full"], "keywords": [], "allowed_files": ["**"], "max_changed_files": 60, "validation_level": "full", "gates": ["docs", "front", "back", "schema", "all"]},
        },
        "type_selection": {"default_type": "full", "escalation_type": "full", "escalate_at_product_types": 2},
        "task_selection": {"preferred_labels": ["stagemesh:prep"], "priority_labels": ["priority:p0", "priority:p1"]},
    }
    base.update(overrides)
    return base


def _write(project: Path, profile: dict) -> None:
    (project / ".stagemesh").mkdir(exist_ok=True)
    (project / ".stagemesh" / "profile.json").write_text(json.dumps(profile), encoding="utf-8")


def _continue(project: Path, *argv: str) -> tuple[int, dict]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = cli_module.main(["--project", str(project), "continue", "--dry-run", "--json", *argv])
    return code, json.loads(out.getvalue())


# --- profile validation ----------------------------------------------------------------------


def test_a_valid_profile_loads_with_its_task_types_and_gates() -> None:
    profile = parse_profile(_profile())
    assert set(profile.types) == {"prep", "frontend", "backend", "schema", "full"}
    assert profile.types["prep"].level == "light" and profile.types["full"].max_changed_files == 60
    assert profile.types["backend"].max_diff_lines == 3000  # inherited from defaults


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda p: p["gates"]["docs"].update(name="readiness-check"), "light task type prep"),
        (lambda p: p["gates"]["front"].update(name="frontend-acceptance"), "standard task type frontend"),
        (lambda p: p["gates"]["all"].update(name="full-regression"), "needs at least one broad gate"),
        (lambda p: p["task_types"]["prep"].update(gates=["nope"]), "unknown: nope"),
        (lambda p: p["task_types"]["prep"].update(validation_level="heavy"), "validation_level"),
        (lambda p: p["task_types"]["prep"].update(bogus=1), "unsupported keys"),
        (lambda p: p["task_types"]["prep"].pop("allowed_files"), "allowed_files"),
        (lambda p: p["gates"]["docs"]["command"].append("${MISSING}"), "unknown profile variable"),
        (lambda p: p["gates"]["back"].update(env="nope"), "unknown env set"),
        (lambda p: p["type_selection"].update(default_type="nope"), "default_type"),
        (lambda p: p.update(bogus=1), "unsupported keys"),
        (lambda p: p.update(schema_version=2), "schema_version"),
    ],
)
def test_unusable_profiles_are_rejected_at_load_time(mutate, message: str) -> None:
    profile = _profile()
    mutate(profile)
    with pytest.raises(ProfileError, match=message):
        parse_profile(profile)


def test_environment_overrides_profile_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STAGEMESH_PROFILE_PY", "custom-python")
    from stagemesh.profile import expand_gate

    assert expand_gate(parse_profile(_profile()), "docs")["command"][0] == "custom-python"


# --- task type resolution ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("labels", "title", "body", "expected", "source"),
    [
        (("area:frontend",), "anything", "", "frontend", "label"),
        (("stagemesh:prep",), "Fix the API", "", "prep", "label"),  # labels beat keywords
        ((), "Prepare readiness for the pilot", "touches the api endpoint", "prep", "title keyword"),
        ((), "Tidy things", "adds an endpoint", "backend", "description keyword"),
        ((), "Update the UI and the API endpoint", "", "full", "escalation"),  # two product areas
        (("area:frontend", "area:backend"), "x", "", "full", "escalation"),
        (("area:schema",), "x", "", "schema", "label"),
        (("validation:full", "area:frontend"), "x", "", "full", "label"),  # explicit full label forces full
        ((), "Add anti-abuse prevention", "no area keywords", "full", "default"),  # unsure -> most validation
        ((), "Improve governance of the api", "", "backend", "title keyword"),  # product beats prep in the same pass
    ],
)
def test_task_type_resolution_is_deterministic_and_explained(labels, title, body, expected, source) -> None:
    decision = resolve_type(parse_profile(_profile()), labels, title, body)
    assert (decision.type_id, decision.source) == (expected, source)
    assert decision.evidence


# --- generated contracts --------------------------------------------------------------------------


def test_new_task_without_a_contract_gets_a_profile_contract_and_completes(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1"], contracts=[], labels={"T-1": ["stagemesh:prep"]})
    _write(project, _profile())

    code, result = _continue(project, "--task", "T-1")

    assert code == 0 and result["stop_reason"] == "DONE"
    assert result["auto_plan"]["occurred"] is True and result["auto_plan"]["gates"] == ["docs-static-check"]
    assert result["auto_plan"]["profile"]["task_type"] == "prep"
    contract = json.loads((project / ".stagemesh" / "contracts" / "T-1.json").read_text(encoding="utf-8"))
    assert contract["allowed_files"] == ["docs/**", "stagemesh-task-*.txt"]
    assert contract["validation_classification"] == "DOCS_ONLY"
    assert contract["profile"]["selection"]["evidence"] == ["prep: stagemesh:prep"]
    for unsafe in (".env", "**/.env", "data/**", "**/*.sqlite3", ".stagemesh/**"):
        assert unsafe in contract["forbidden_files"]  # global privacy exclusions are always present
    assert contract["max_changed_files"] == 30 and contract["max_diff_lines"] == 3000


def test_profile_removes_the_no_validation_gate_failure(tmp_path: Path) -> None:
    # No package.json / pytest config anywhere: root-level detection finds nothing, the profile still plans.
    project = _project(tmp_path, ["T-1"], contracts=[])
    code, result = _continue(project, "--task", "T-1")
    assert code == 2 and result["detail"]["auto_plan_reason"] == "no_validation_gates"

    _write(project, _profile())
    code, result = _continue(project, "--task", "T-1")
    assert code == 0 and result["auto_plan"]["profile"]["task_type"] == "full"  # nothing matched -> default (full)


def test_each_task_type_gets_its_own_gates_scope_and_level(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1"], contracts=[])
    _write(project, _profile())
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    task = store.upsert_task("work", source="local-backlog", source_id="T-1")
    expected = {
        "prep": (["docs-static-check"], "DOCS_ONLY"),
        "frontend": (["docs-static-check", "frontend-tests"], "LOCALIZED_CODE"),
        "backend": (["docs-static-check", "backend-tests"], "LOCALIZED_CODE"),
        "schema": (["backend-tests", "schema-migration-tests"], "LOCALIZED_CODE"),
        "full": (["docs-static-check", "frontend-tests", "backend-tests", "schema-migration-tests", "full-acceptance-regression"], "CORE_LIFECYCLE_OR_SCHEMA_SECURITY"),
    }
    labels = {"prep": "stagemesh:prep", "frontend": "area:frontend", "backend": "area:backend", "schema": "area:schema", "full": "validation:full"}
    from stagemesh.auto_plan import profile_payload

    for type_id, (gates, level) in expected.items():
        store.cache_source("local-backlog", "T-1", {"labels": [labels[type_id]]}, "OPEN")
        payload, selection = profile_payload(store, project, task)
        assert selection["task_type"] == type_id
        assert [g["name"] for g in payload["required_tests"]] == gates and payload["validation_classification"] == level
    store.close()


def test_gate_environment_and_cwd_come_from_the_profile(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1"], contracts=[], labels={"T-1": ["area:backend"]})
    _write(project, _profile())
    code, result = _continue(project, "--task", "T-1")
    assert code == 0
    contract = json.loads((project / ".stagemesh" / "contracts" / "T-1.json").read_text(encoding="utf-8"))
    back = next(g for g in contract["required_tests"] if g["name"] == "backend-tests")
    assert back["cwd"] == "." and back["env"] == {"DATABASE_URL": "postgresql://test/main_test"}


def test_oversized_objectives_are_trimmed_to_fit_but_gates_are_kept(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1"], contracts=[], labels={"T-1": ["validation:full"]})
    _write(project, _profile())
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    store.upsert_task("t" * 100, source="local-backlog", source_id="T-1")
    store.cache_source("local-backlog", "T-1", {"labels": ["validation:full"], "objective": "long " * 3000}, "OPEN")
    result = create_contract(store, project, "T-1")
    payload = json.loads(result.path.read_text(encoding="utf-8"))
    assert len(payload["required_tests"]) == 5 and result.digest_chars <= 10000
    store.close()


def test_invalid_profile_fails_closed_with_a_clear_next_action(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1"], contracts=[])
    (project / ".stagemesh" / "profile.json").write_text("{not json", encoding="utf-8")
    code, result = _continue(project, "--task", "T-1")
    assert code == 2 and result["detail"]["auto_plan_reason"] == "invalid_profile"
    assert "stagemesh profile" in result["detail"]["next_action"] and not (project / ".stagemesh" / "contracts" / "T-1.json").exists()


# --- task selection with a profile ------------------------------------------------------------------


def test_prep_work_is_preferred_over_product_work_and_selection_defaults_come_from_the_profile(tmp_path: Path) -> None:
    project = _project(
        tmp_path,
        ["P-1", "P-2"],
        contracts=[],
        labels={"P-1": ["area:frontend", "priority:p1"], "P-2": ["stagemesh:prep", "priority:p1"]},
    )
    _write(project, _profile())
    assert load_config(project).task_selection.preferred_labels == ("stagemesh:prep",)

    code, result = _continue(project)

    assert code == 0 and result["task_id"] == "P-2"
    assert "preferred label stagemesh:prep" in result["selection"]["reason"]


def test_config_task_selection_overrides_the_profile(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1"], config={"task_selection": {"preferred_labels": ["mine"]}})
    _write(project, _profile())
    assert load_config(project).task_selection.preferred_labels == ("mine",)


def test_product_tasks_need_a_plannable_contract_to_be_selected(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1", "T-2"], contracts=["T-2"], labels={"T-1": ["priority:p0"]})
    (project / ".stagemesh" / "profile.json").write_text(json.dumps(_profile(task_types={})), encoding="utf-8")
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    cli_module._sync_all_sources(store, project, load_config(project), None)
    why = plannable(store, project, "T-1")
    assert why is not None and "project profile is unusable" in why
    selection = select_next_task(store, project, load_config(project).task_selection)
    assert selection.task_id == "T-2"  # T-1 cannot be planned, so the contracted task wins despite lower priority
    store.close()


# --- gate cwd / env -----------------------------------------------------------------------------------


def test_gate_runs_in_its_cwd_with_its_env(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    code = "import os,sys; print(os.getcwd().replace(chr(92), '/').rsplit('/', 1)[-1], os.environ['GATE_VAR']); sys.exit(0 if os.environ['GATE_VAR'] == 'on' else 1)"
    gate = GateCommand("g", [PY, "-c", code], 30, cwd="sub", env=(("GATE_VAR", "on"),))
    result = run_gate(tmp_path, gate)
    assert result.status == "PASSED" and result.stdout.strip() == "sub on"


@pytest.mark.parametrize("cwd", ["../escape", "/abs", "C:/abs", "a/../../b"])
def test_gate_cwd_must_stay_inside_the_checkout(cwd: str) -> None:
    with pytest.raises(ContractError, match="relative path inside the checkout"):
        parse_contract({"objective": "o", "required_tests": [{"name": "g", "command": ["x"], "cwd": cwd}]})


def test_gates_without_cwd_or_env_keep_their_canonical_form() -> None:
    plain = parse_contract({"objective": "o", "required_tests": [{"name": "g", "command": ["x"], "timeout_seconds": 5}]})
    assert '"cwd"' not in canonical_contract_json(plain) and '"env"' not in canonical_contract_json(plain)
    scoped = parse_contract({"objective": "o", "required_tests": [{"name": "g", "command": ["x"], "cwd": "a", "env": {"K": "v"}}]})
    assert '"cwd":"a"' in canonical_contract_json(scoped) and '"env":{"K":"v"}' in canonical_contract_json(scoped)


# --- operator command -----------------------------------------------------------------------------------


def test_profile_command_explains_the_plan_for_a_task(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = _project(tmp_path, ["T-1"], contracts=[], labels={"T-1": ["area:backend"]})
    _write(project, _profile())
    assert cli_module.main(["--project", str(project), "profile", "--task", "T-1", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["task"]["selection"]["type"] == "backend" and report["task"]["gates"] == ["docs-static-check", "backend-tests"]
    assert set(report["task_types"]) == {"prep", "frontend", "backend", "schema", "full"}
    assert not (project / ".stagemesh" / "contracts" / "T-1.json").exists()  # read-only


def test_profile_command_reports_missing_and_invalid_profiles(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = _project(tmp_path, ["T-1"])
    assert cli_module.main(["--project", str(project), "profile"]) == 1
    _write(project, _profile(gates={}))
    assert cli_module.main(["--project", str(project), "profile"]) == 2
    assert "profile invalid" in capsys.readouterr().err
    assert copy and AutoPlanError and load_profile  # imported for readability of the failure modes above
