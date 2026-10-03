"""The shipped Caventra profile (docs/profiles/caventra.profile.json) must load, plan and protect private data."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import stagemesh.cli as cli_module
from stagemesh.auto_plan import create_contract, profile_payload, validate_generated
from stagemesh.config import load_config
from stagemesh.contracts import _matches, parse_contract
from stagemesh.persistence import Store
from stagemesh.profile import expand_gate, parse_profile, resolve_type
from stagemesh.task_selection import select_next_task

from test_run_ready import _project

PROFILE_PATH = Path(__file__).resolve().parents[1] / "docs" / "profiles" / "caventra.profile.json"


@pytest.fixture(scope="module")
def raw() -> dict:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def profile(raw: dict):
    return parse_profile(raw)


def test_the_profile_defines_every_required_task_type_and_level(profile) -> None:
    assert set(profile.types) == {"prep", "frontend", "backend", "schema", "full"}
    assert {t.id: t.level for t in profile.types.values()} == {
        "prep": "light", "frontend": "standard", "backend": "standard", "schema": "standard", "full": "full",
    }
    assert profile.default_type == "full" and profile.escalation_type == "full"


def test_every_type_has_real_validation_and_bounded_scope(profile) -> None:
    for task_type in profile.types.values():
        names = [expand_gate(profile, g)["name"] for g in task_type.gates]
        assert len(names) >= 3, task_type.id  # more than a token check, even for prep
        assert task_type.max_changed_files <= 60 and task_type.max_diff_lines <= 6000
        assert "**" not in task_type.allowed_files  # no type is allowed to touch everything
    prep = profile.types["prep"]
    assert prep.allowed_files == ("docs/**", "README.md") and "apps/**" in prep.forbidden_files
    names = {t: [expand_gate(profile, g)["name"] for g in profile.types[t].gates] for t in profile.types}
    assert "frontend-vitest" in names["frontend"] and "frontend-typecheck" in names["frontend"]
    assert "backend-unit-tests" in names["backend"] and "backend-engineering-known-failures" in names["backend"]
    assert "schema-migration-tests" in names["schema"]
    assert "full-acceptance-regression" in names["full"] and set(names["frontend"] + names["backend"] + names["schema"]) - {"backend-auth-integration"} <= set(names["full"])


def test_gates_are_explicit_about_the_test_database_and_never_touch_production(raw: dict) -> None:
    env = raw["env_sets"]["test_db"]
    assert env["DATABASE_URL"].endswith("/caventra_test") and env["ADMIN_DATABASE_URL"].endswith("/caventra_test")
    db_gates = [g for g in raw["gates"].values() if "env" in g]
    assert db_gates and all(g["env"] == "test_db" for g in db_gates)
    assert all(g["name"] for g in raw["gates"].values())


@pytest.mark.parametrize(
    "path",
    [
        ".env", "apps/api/.env", "apps/api/.env.local", "certs/server.pem", "keys/id_rsa.pub", "data/uploads/matter.pdf",
        "private_matter_inputs/case.pdf", "apps/api/data/matters.sqlite3", ".stagemesh/stagemesh.sqlite3",
        ".stagemesh/stagemesh.sqlite3-wal", ".stagemesh/stagemesh.sqlite3-shm", "local.db", "apps/web-v2/node_modules/x/index.js",
        "apps/web-v2/.npm-cache/_update-notifier-last-checked", "apps/api/app/__pycache__/m.pyc", ".pytest_tmp_quality_change/a",
        ".claude/settings.json", ".codex/config.toml", ".cursor/rules", ".gemini/settings.json", ".codegraph/db", ".mcp.json",
        "AGENTS.md", ".github/workflows/ci.yml", "docs/evaluations/sdd071/local/results.json", "api/credentials.json",
    ],
)
def test_unsafe_paths_are_forbidden_in_every_generated_contract(profile, path: str) -> None:
    for task_type in profile.types.values():
        payload = _payload(profile, task_type.id)
        assert _matches(path, tuple(payload["forbidden_files"])), (task_type.id, path)


@pytest.mark.parametrize(
    "path",
    ["docs/engineering/STAGEMESH_READINESS.md", "apps/web-v2/src/App.tsx", "apps/api/app/services/auth_service.py", "README.md"],
)
def test_ordinary_project_files_are_not_forbidden(profile, path: str) -> None:
    assert not _matches(path, tuple(_payload(profile, "full")["forbidden_files"]))


def _payload(profile, type_id: str) -> dict:
    from stagemesh.profile import TypeDecision, build_contract

    return build_contract(profile, TypeDecision(type_id, "label", ("test",)), "objective", "T", "test")


def test_generated_contracts_for_every_type_parse_and_fit(profile) -> None:
    for type_id in profile.types:
        payload = _payload(profile, type_id)
        assert validate_generated(payload) <= 10000
        contract = parse_contract(payload)
        assert contract.explicit and contract.gates and contract.max_changed_files and contract.max_diff_lines
        assert contract.validation_classification in {"DOCS_ONLY", "LOCALIZED_CODE", "CORE_LIFECYCLE_OR_SCHEMA_SECURITY"}


@pytest.mark.parametrize(
    ("labels", "title", "body", "expected"),
    [
        (("caventra:objective", "status:QUEUED", "stagemesh:ready", "priority:p0"),
         "Prepare Caventra for safe StageMesh execution: contracts, validation gates, and first-task readiness",
         "Keep local databases, secrets, uploaded matter data out of Git. Define validation gates for the backend and frontend.", "prep"),
        (("caventra:objective", "stagemesh:ready"), "Objective: implement approved Caventra UX visual baseline in real product v1",
         "apps/web-v2 shell, themes, Matter Home", "frontend"),
        (("area:backend",), "Add rate limiting", "", "backend"),
        (("area:schema",), "Add trial table", "", "schema"),
        (("area:frontend", "area:backend"), "Wire the new page to the API", "", "full"),
        (("validation:full",), "Anything", "", "full"),
        ((), "Add free-trial anti-abuse and duplicate-account prevention", "A dedicated anti-abuse service boundary and API.", "backend"),
        ((), "Tidy something", "no hints at all", "full"),
    ],
)
def test_real_issue_shapes_resolve_to_the_expected_task_type(profile, labels, title, body, expected) -> None:
    assert resolve_type(profile, labels, title, body).type_id == expected


def test_a_new_prep_and_a_new_product_task_get_valid_generated_contracts(tmp_path: Path) -> None:
    """Dry-run evidence: no hand-written contract, no root-level test config, and still a valid contract with real gates."""
    project = _project(
        tmp_path, ["54", "46"], contracts=[], labels={"54": ["stagemesh:ready", "priority:p0"], "46": ["stagemesh:ready", "area:frontend"]}
    )
    (project / ".stagemesh" / "profile.json").write_text(PROFILE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    cli_module._sync_all_sources(store, project, load_config(project), None)
    store.conn.execute("UPDATE tasks SET title=? WHERE id='54'", ("Prepare the project for safe execution: governance and readiness",))
    store.conn.commit()

    prep = create_contract(store, project, "54")
    product = create_contract(store, project, "46")

    assert prep.profile["task_type"] == "prep" and prep.profile["validation_level"] == "light"
    assert prep.gates == ("docs-static-hygiene", "docs-static-diff-check", "docs-static-boundaries", "docs-static-roadmaps")
    assert product.profile["task_type"] == "frontend"
    assert product.gates[-3:] == ("frontend-vitest", "frontend-ui-lint", "frontend-typecheck")
    for result in (prep, product):
        contract = parse_contract(json.loads(result.path.read_text(encoding="utf-8")))
        assert contract.explicit and ".env" in contract.forbidden_files and "data/**" in contract.forbidden_files
    store.close()


def test_task_selection_prefers_prep_work_and_never_selects_blocked_or_unplannable_tasks(tmp_path: Path) -> None:
    project = _project(
        tmp_path,
        ["1", "2", "3"],
        contracts=[],
        labels={"1": ["priority:p1", "area:backend"], "2": ["priority:p1", "stagemesh:prep"], "3": ["priority:p0", "stagemesh:blocked"]},
    )
    (project / ".stagemesh" / "profile.json").write_text(PROFILE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    config = load_config(project)
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    cli_module._sync_all_sources(store, project, config, None)

    selection = select_next_task(store, project, config.task_selection)

    assert selection.task_id == "2"  # prep beats product at equal priority
    assert {s["task_id"]: s["reason"] for s in selection.skipped}["3"] == "excluded label stagemesh:blocked"
    store.close()


def test_profile_task_selection_defaults_match_the_documented_label_behavior(profile) -> None:
    selection = profile.task_selection
    assert selection["preferred_labels"][0] == "stagemesh:prep"
    assert {"stagemesh:blocked", "stagemesh:deferred"} <= set(selection["excluded_labels"])
    assert selection["priority_labels"] == ["priority:p0", "priority:p1", "priority:p2", "priority:p3"]
    assert profile_payload  # profile_payload is the planning entry point exercised above


FORBIDDEN_TOOLS = {"grep", "egrep", "tail", "head", "sed", "awk", "cat", "sh", "bash", "cmd", "cmd.exe", "powershell", "pwsh", "stagemesh"}


def test_gates_use_only_caventra_native_commands(raw: dict, profile) -> None:
    """No shell wrappers or POSIX utilities, no MSYS-style paths, and nothing that inspects the StageMesh repository."""
    for gate_id, gate in raw["gates"].items():
        command = gate["command"]
        assert Path(command[0]).name.lower().removesuffix(".exe") not in FORBIDDEN_TOOLS, gate_id
        for part in command:
            assert not part.startswith("/c/") and "/mnt/" not in part, (gate_id, part)
            assert "stagemesh-vnext" not in part.lower(), (gate_id, part)
            assert "|" not in part and "&&" not in part, (gate_id, part)  # no shell pipelines inside an argv element
        assert command[0] in {"${PYTHON}", "git", "npm"}, gate_id
        if command[0] == "git":
            assert command[1:3] == ["diff", "--check"], gate_id  # read-only whitespace check of the candidate itself
    for variable in raw["variables"].values():
        text = json.dumps(variable)
        assert "/c/" not in text and "stagemesh-vnext" not in text.lower()
