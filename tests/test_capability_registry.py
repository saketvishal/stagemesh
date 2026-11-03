from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import stagemesh.capability_vendor as capability_vendor_module
import stagemesh.providers as providers_module
from stagemesh.agent_skills import skill_prompt
from stagemesh.capabilities import (
    ATTACHMENT_REQUIRED,
    OBSERVED_LOAD_REQUIRED,
    CapabilityRegistryError,
    capability_policy_report,
    capability_policy_snapshot,
    inventory,
    resolve_attachments,
    set_capability_enabled,
    skill_evidence_mode,
    task_capability_summary,
)
from stagemesh.capability_discovery import discover_capabilities
from stagemesh.capability_vendor import vendor_capability
from stagemesh.capacity import CapacityKind
from stagemesh.cli import main
from stagemesh.contracts import ChangeContract, canonical_contract_json, parse_contract
from stagemesh.git import GitWorkspace
from stagemesh.mcp_capabilities import configure_codex_mcp_command
from stagemesh.persistence import Store
from stagemesh.provider_pool import IMPLEMENT, REVIEW, PooledExecutor, ProviderPool
from stagemesh.providers import (
    RuntimeCommandAdapter,
    _build_task_prompt,
    _record_capability_attachments,
)


def _task(title: str) -> sqlite3.Row:
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        return db.execute("SELECT ? AS title", (title,)).fetchone()


def _skill(project: Path, name: str = "domain-review") -> Path:
    path = project / "agent-skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        f"---\nname: {name}\ndescription: Bounded project guidance.\n---\n\n# Guidance\n",
        encoding="utf-8",
    )
    return path


def _registry(project: Path, capabilities: list[dict[str, object]]) -> None:
    path = project / ".stagemesh" / "capabilities.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"format_version": 1, "capabilities": capabilities}),
        encoding="utf-8",
    )


def _freeze_capability_policy(
    store: Store, project: Path, task_id: str, contract: ChangeContract, baseline: str
) -> None:
    canonical = canonical_contract_json(contract)
    snapshot = capability_policy_snapshot(project, contract.capability_refs)
    store.bind_task_contract(
        task_id,
        baseline,
        1,
        hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        canonical,
        capability_digest=str(snapshot["digest"]),
        capability_json=str(snapshot["canonical_json"]),
    )


def _freeze_preflight_policy(project: Path, task_id: str) -> None:
    contract = parse_contract(
        json.loads((project / ".stagemesh" / "contracts" / f"{task_id}.json").read_text())
    )
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    store.upsert_task("Capability preflight", source_id=task_id)
    _freeze_capability_policy(store, project, task_id, contract, "0" * 40)
    store.close()


def _mcp_manifest(project: Path, name: str = "serena") -> Path:
    path = project / ".stagemesh" / "mcp" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "transport": "stdio",
                "server_name": name,
                "command": sys.executable,
                "args": ["start-mcp-server", "--context", "ide-assistant"],
                "cwd": ".",
                "allowed_tools": ["find_symbol", "search_for_pattern"],
                "startup_timeout_seconds": 15,
                "tool_timeout_seconds": 45,
            }
        ),
        encoding="utf-8",
    )
    return path


def _claude_plugin(project: Path, name: str = "review-kit") -> Path:
    root = project / ".claude" / "plugins" / name
    manifest = root / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "name": name,
                "version": "1.0.0",
                "description": "Local review guidance.",
            }
        ),
        encoding="utf-8",
    )
    for skill_name in ("review", "verify"):
        skill = root / "skills" / skill_name / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text(
            f"---\nname: {skill_name}\ndescription: Bounded guidance.\n---\n\n# {skill_name}\n",
            encoding="utf-8",
        )
    return root


def _entry(
    capability_id: str,
    *,
    kind: str = "skill",
    source: str = "agent-skills/domain-review/SKILL.md",
    enabled: bool = True,
    providers: list[str] | None = None,
    stages: list[str] | None = None,
    permissions: list[str] | None = None,
) -> dict[str, object]:
    return {
        "id": capability_id,
        "kind": kind,
        "source": source,
        "enabled": enabled,
        "providers": providers or ["codex", "claude", "grok", "agy"],
        "stages": stages or ["IMPLEMENT"],
        "permissions": permissions if permissions is not None else ["workspace-read"],
    }


def test_capability_registration_plan_validates_existing_source_without_writes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _skill(tmp_path)

    code = main(
        [
            "--project",
            str(tmp_path),
            "capabilities",
            "register",
            "domain-review",
            "--kind",
            "skill",
            "--source",
            "agent-skills/domain-review/SKILL.md",
            "--provider",
            "codex",
            "--stage",
            "IMPLEMENT",
            "--plan",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["operation"] == "register" and payload["changed"] is True
    assert payload["inventory"]["installed"] is True
    assert payload["inventory"]["available"] is True
    assert payload["inventory"]["enabled"] is False
    assert payload["inventory"]["usable"] is False
    assert payload["planned_writes"] == [".stagemesh/capabilities.json"]
    assert payload["writes"] == [] and payload["provider_calls"] == 0
    assert payload["network_calls"] == 0 and payload["package_installs"] == 0
    assert not (tmp_path / ".stagemesh" / "capabilities.json").exists()
    assert not (tmp_path / ".stagemesh" / "stagemesh.sqlite3").exists()


def test_capability_change_preserves_parent_json_flag_and_refuses_task_scope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _skill(tmp_path)
    base = [
        "--project",
        str(tmp_path),
        "capabilities",
        "--json",
    ]
    change = [
        "register",
        "domain-review",
        "--kind",
        "skill",
        "--source",
        "agent-skills/domain-review/SKILL.md",
        "--provider",
        "codex",
        "--stage",
        "IMPLEMENT",
        "--plan",
    ]

    assert main([*base, *change]) == 0
    assert json.loads(capsys.readouterr().out)["writes"] == []
    assert main([*base[:-1], "--task", "T-1", *change]) == 2
    assert "read-only usage inspection" in capsys.readouterr().err
    assert not (tmp_path / ".stagemesh" / "capabilities.json").exists()


def test_cli_registers_existing_skill_plugin_and_mcp_without_installing_or_invoking(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _skill(tmp_path)
    _claude_plugin(tmp_path)
    _mcp_manifest(tmp_path)
    registry = tmp_path / ".stagemesh" / "capabilities.json"
    registry.write_text(
        json.dumps(
            {
                "format_version": 1,
                "skill_evidence_mode": OBSERVED_LOAD_REQUIRED,
                "capabilities": [],
            }
        ),
        encoding="utf-8",
    )
    commands = [
        [
            "domain-review",
            "--kind",
            "skill",
            "--source",
            "agent-skills/domain-review/SKILL.md",
            "--provider",
            "codex",
            "--stage",
            "IMPLEMENT",
        ],
        [
            "review-kit",
            "--kind",
            "claude-plugin",
            "--source",
            ".claude/plugins/review-kit",
            "--provider",
            "claude",
            "--stage",
            "REVIEW",
        ],
        [
            "serena",
            "--kind",
            "mcp",
            "--source",
            ".stagemesh/mcp/serena.json",
            "--provider",
            "codex",
            "--stage",
            "IMPLEMENT",
        ],
    ]

    for command in commands:
        assert main(["--project", str(tmp_path), "capabilities", "register", *command, "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["written"] is True

    payload = json.loads(registry.read_text(encoding="utf-8"))
    assert payload["skill_evidence_mode"] == OBSERVED_LOAD_REQUIRED
    assert [entry["id"] for entry in payload["capabilities"]] == [
        "domain-review",
        "review-kit",
        "serena",
    ]
    assert all(entry["enabled"] is False for entry in payload["capabilities"])
    assert payload["capabilities"][1]["permissions"] == ["workspace-read"]
    assert payload["capabilities"][2]["permissions"] == ["workspace-read", "subprocess"]
    rows = {row["id"]: row for row in inventory(tmp_path)}
    assert all(rows[name]["available"] is True for name in rows)
    assert all(rows[name]["usable"] is False for name in rows)
    assert not (tmp_path / ".stagemesh" / "stagemesh.sqlite3").exists()


def test_capability_enable_plan_and_disable_are_explicit_and_do_not_create_usage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    skill = _skill(tmp_path)
    _registry(tmp_path, [_entry("domain-review", enabled=False)])
    registry = tmp_path / ".stagemesh" / "capabilities.json"
    before = registry.read_bytes()

    assert main(
        ["--project", str(tmp_path), "capabilities", "enable", "domain-review", "--plan", "--json"]
    ) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["writes"] == [] and plan["inventory"]["usable"] is True
    assert registry.read_bytes() == before

    assert main(["--project", str(tmp_path), "capabilities", "enable", "domain-review", "--json"]) == 0
    enabled = json.loads(capsys.readouterr().out)
    assert enabled["written"] is True and enabled["inventory"]["usable"] is True
    skill.unlink()
    assert main(["--project", str(tmp_path), "capabilities", "disable", "domain-review", "--json"]) == 0
    disabled = json.loads(capsys.readouterr().out)
    assert disabled["written"] is True
    assert disabled["inventory"]["enabled"] is False and disabled["inventory"]["available"] is False
    assert main(["--project", str(tmp_path), "capabilities", "disable", "domain-review", "--json"]) == 0
    unchanged = json.loads(capsys.readouterr().out)
    assert unchanged["written"] is False and unchanged["changed"] is False
    assert not (tmp_path / ".stagemesh" / "stagemesh.sqlite3").exists()


def test_unavailable_enable_and_duplicate_registration_leave_registry_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _skill(tmp_path)
    _registry(tmp_path, [_entry("domain-review", enabled=False)])
    registry = tmp_path / ".stagemesh" / "capabilities.json"
    before = registry.read_bytes()
    duplicate = [
        "--project",
        str(tmp_path),
        "capabilities",
        "register",
        "domain-review",
        "--kind",
        "skill",
        "--source",
        "agent-skills/domain-review/SKILL.md",
        "--provider",
        "codex",
        "--stage",
        "IMPLEMENT",
    ]

    assert main(duplicate) == 2
    assert "already registered" in capsys.readouterr().err
    assert registry.read_bytes() == before

    manifest = _mcp_manifest(tmp_path, "unavailable")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["command"] = "stagemesh-no-such-mcp-runtime"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    assert main(
        [
            "--project",
            str(tmp_path),
            "capabilities",
            "register",
            "unavailable",
            "--kind",
            "mcp",
            "--source",
            ".stagemesh/mcp/unavailable.json",
            "--provider",
            "codex",
            "--stage",
            "IMPLEMENT",
        ]
    ) == 0
    capsys.readouterr()
    registered = registry.read_bytes()
    assert main(["--project", str(tmp_path), "capabilities", "enable", "unavailable"]) == 2
    assert "not available" in capsys.readouterr().err
    assert registry.read_bytes() == registered


def test_explicit_capability_update_preserves_policy_and_unrelated_entries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _skill(tmp_path, "domain-review")
    _skill(tmp_path, "unrelated")
    registry = tmp_path / ".stagemesh" / "capabilities.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    unrelated = _entry("unrelated", source="agent-skills/unrelated/SKILL.md", enabled=False)
    registry.write_text(
        json.dumps(
            {
                "format_version": 1,
                "skill_evidence_mode": OBSERVED_LOAD_REQUIRED,
                "capabilities": [_entry("domain-review", enabled=False), unrelated],
            }
        ),
        encoding="utf-8",
    )

    code = main(
        [
            "--project",
            str(tmp_path),
            "capabilities",
            "register",
            "domain-review",
            "--kind",
            "skill",
            "--source",
            "agent-skills/domain-review/SKILL.md",
            "--provider",
            "claude",
            "--stage",
            "REVIEW",
            "--update",
            "--json",
        ]
    )

    assert code == 0 and json.loads(capsys.readouterr().out)["operation"] == "update"
    payload = json.loads(registry.read_text(encoding="utf-8"))
    assert payload["skill_evidence_mode"] == OBSERVED_LOAD_REQUIRED
    assert payload["capabilities"][1] == unrelated
    assert payload["capabilities"][0]["providers"] == ["claude"]
    assert payload["capabilities"][0]["stages"] == ["REVIEW"]
    assert payload["capabilities"][0]["enabled"] is False


def test_capability_registry_write_refuses_an_existing_symlink(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _skill(tmp_path)
    registry = tmp_path / ".stagemesh" / "capabilities.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-capabilities.json"
    outside.write_text(json.dumps({"format_version": 1, "capabilities": []}), encoding="utf-8")
    try:
        registry.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    before = outside.read_bytes()

    code = main(
        [
            "--project",
            str(tmp_path),
            "capabilities",
            "register",
            "domain-review",
            "--kind",
            "skill",
            "--source",
            "agent-skills/domain-review/SKILL.md",
            "--provider",
            "codex",
            "--stage",
            "IMPLEMENT",
        ]
    )

    assert code == 2 and "refuses a symlink" in capsys.readouterr().err
    assert registry.is_symlink() and outside.read_bytes() == before


def test_inventory_distinguishes_declared_enabled_installed_adapter_and_invocation(
    tmp_path: Path,
) -> None:
    _skill(tmp_path)
    _mcp_manifest(tmp_path)
    _registry(
        tmp_path,
        [
            _entry("domain-review"),
            _entry(
                "serena",
                kind="mcp",
                source=".stagemesh/mcp/serena.json",
                enabled=False,
                providers=["codex", "claude"],
                permissions=["workspace-read", "subprocess"],
            ),
            _entry(
                "claude-review",
                kind="claude-plugin",
                source=".claude/plugins/review",
                enabled=False,
                providers=["claude"],
                permissions=["workspace-read"],
            ),
        ],
    )
    rows = {row["id"]: row for row in inventory(tmp_path)}

    assert rows["domain-review"]["declared"] is True
    assert rows["domain-review"]["enabled"] is True
    assert rows["domain-review"]["installed"] is True
    assert rows["domain-review"]["adapter_supported"] is True
    assert rows["domain-review"]["available"] is True
    assert rows["domain-review"]["usable"] is True
    assert rows["domain-review"]["invocation"] == "not-observed"
    assert rows["serena"]["installed"] is True
    assert rows["serena"]["adapter_supported"] is True
    assert rows["serena"]["supported_providers"] == ["codex"]
    assert rows["serena"]["configuration_valid"] is True
    assert rows["serena"]["runtime_installed"] is True
    assert rows["serena"]["available"] is True
    assert rows["serena"]["usable"] is False
    assert rows["claude-review"]["installed"] is False
    assert rows["claude-review"]["adapter_supported"] is True
    assert rows["claude-review"]["supported_providers"] == ["claude"]


def test_skills_only_claude_plugin_is_attached_without_activating_plugin_runtime(
    tmp_path: Path,
) -> None:
    plugin = _claude_plugin(tmp_path)
    _registry(
        tmp_path,
        [
            _entry(
                "review-kit",
                kind="claude-plugin",
                source=".claude/plugins/review-kit",
                providers=["claude"],
                permissions=["workspace-read"],
            )
        ],
    )
    contract = ChangeContract(objective="Review safely", capability_refs=("review-kit",))
    attachments: list[dict[str, object]] = []

    prompt = _build_task_prompt(
        "task-1",
        _task("Review safely"),
        project=tmp_path,
        contract=contract,
        provider_name="claude",
        capability_audit=attachments,
    )

    assert "Registered Claude plugin skill review-kit/review" in prompt
    assert "Registered Claude plugin skill review-kit/verify" in prompt
    assert "--plugin-dir" not in prompt
    attachment = next(item for item in attachments if item["capability_id"] == "review-kit")
    assert attachment["kind"] == "claude-plugin"
    assert attachment["status"] == "attached"
    assert attachment["invocation"] == "unverified"
    assert attachment["_path"] == str(plugin.resolve())
    assert attachment["_plugin"]["activation"] == "skills-only-import"
    row = inventory(tmp_path)[0]
    assert row["configuration_valid"] is True
    assert row["available"] is True
    assert row["usable"] is True


@pytest.mark.parametrize(
    ("field", "component"),
    [
        ("hooks", None),
        (None, ".mcp.json"),
        (None, "bin"),
        ("agents", None),
        ("userConfig", None),
    ],
)
def test_claude_plugin_runtime_components_fail_closed(
    tmp_path: Path, field: str | None, component: str | None
) -> None:
    plugin = _claude_plugin(tmp_path)
    if field:
        manifest = plugin / ".claude-plugin" / "plugin.json"
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload[field] = {} if field == "userConfig" else "./runtime.json"
        manifest.write_text(json.dumps(payload), encoding="utf-8")
    if component:
        path = plugin / component
        if path.suffix:
            path.write_text("{}", encoding="utf-8")
        else:
            path.mkdir()
    _registry(
        tmp_path,
        [
            _entry(
                "review-kit",
                kind="claude-plugin",
                source=".claude/plugins/review-kit",
                providers=["claude"],
                permissions=["workspace-read"],
            )
        ],
    )

    row = inventory(tmp_path)[0]
    assert row["configuration_valid"] is False
    assert row["available"] is False
    with pytest.raises(CapabilityRegistryError, match="invalid Claude plugin configuration"):
        resolve_attachments(tmp_path, ("review-kit",), provider="claude", stage="IMPLEMENT")


@pytest.mark.parametrize("provider", ["codex", "claude", "grok", "agy"])
def test_registered_skill_is_provider_neutral_prompt_attachment_with_audit_metadata(
    tmp_path: Path, provider: str
) -> None:
    skill = _skill(tmp_path)
    _registry(tmp_path, [_entry("domain-review")])
    contract = ChangeContract(
        objective="Update domain mapping",
        allowed_files=("src/domain.py",),
        capability_refs=("domain-review",),
    )
    attachments: list[dict[str, object]] = []

    prompt = _build_task_prompt(
        "task-1",
        _task("Update domain mapping"),
        project=tmp_path,
        contract=contract,
        provider_name=provider,
        capability_audit=attachments,
    )

    assert f"- Registered skill domain-review: {json.dumps(str(skill.resolve()))}" in prompt
    registered = next(item for item in attachments if item["origin"] == "project-registry")
    assert registered["status"] == "attached"
    assert registered["invocation"] == "unverified"
    assert registered["permissions"] == ["workspace-read"]
    assert len(str(registered["content_sha256"])) == 64


@pytest.mark.parametrize(
    ("entry", "provider", "message"),
    [
        (_entry("off", enabled=False), "codex", "disabled"),
        (_entry("claude-only", providers=["claude"]), "codex", "not compatible"),
        (
            _entry(
                "serena",
                kind="mcp",
                source=".stagemesh/mcp/serena.json",
                providers=["codex", "claude"],
                permissions=["workspace-read", "subprocess"],
            ),
            "claude",
            "no bounded MCP adapter",
        ),
    ],
)
def test_requested_capabilities_fail_closed_before_dispatch(
    tmp_path: Path, entry: dict[str, object], provider: str, message: str
) -> None:
    _skill(tmp_path)
    if entry["kind"] == "mcp":
        _mcp_manifest(tmp_path)
    _registry(tmp_path, [entry])

    with pytest.raises(CapabilityRegistryError, match=message):
        resolve_attachments(tmp_path, (str(entry["id"]),), provider=provider, stage="IMPLEMENT")


def test_registry_skill_routes_only_to_its_declared_lifecycle_stage(tmp_path: Path) -> None:
    skill = _skill(tmp_path)
    _registry(tmp_path, [_entry("domain-review", stages=["REVIEW"])])
    contract = ChangeContract(objective="Review domain mapping", capability_refs=("domain-review",))
    implementation_audit: list[dict[str, object]] = []
    review_audit: list[dict[str, object]] = []

    implementation = _build_task_prompt(
        "task-1",
        _task("Review domain mapping"),
        project=tmp_path,
        contract=contract,
        provider_name="codex",
        capability_audit=implementation_audit,
    )
    review = skill_prompt(
        "Review domain mapping",
        contract,
        stage="review",
        project=tmp_path,
        provider_name="codex",
        capability_audit=review_audit,
    )

    assert "Registered skill domain-review" not in implementation
    assert not any(item.get("capability_id") == "domain-review" for item in implementation_audit)
    assert f"- Registered skill domain-review: {json.dumps(str(skill.resolve()))}" in review
    assert next(item for item in review_audit if item["capability_id"] == "domain-review")[
        "origin"
    ] == "project-registry"


def test_implementation_only_capability_does_not_break_review_prompt(tmp_path: Path) -> None:
    _skill(tmp_path)
    _registry(tmp_path, [_entry("domain-review", stages=["IMPLEMENT"])])
    contract = ChangeContract(objective="Implement only", capability_refs=("domain-review",))

    prompt = skill_prompt(
        "Review the candidate",
        contract,
        stage="review",
        project=tmp_path,
        provider_name="codex",
    )

    assert "Registered skill domain-review" not in prompt


def test_claude_plugin_skills_may_be_scoped_to_review(tmp_path: Path) -> None:
    _claude_plugin(tmp_path)
    _registry(
        tmp_path,
        [
            _entry(
                "review-kit",
                kind="claude-plugin",
                source=".claude/plugins/review-kit",
                providers=["claude"],
                stages=["REVIEW"],
                permissions=["workspace-read"],
            )
        ],
    )
    contract = ChangeContract(objective="Review safely", capability_refs=("review-kit",))

    prompt = skill_prompt(
        "Review safely",
        contract,
        stage="review",
        project=tmp_path,
        provider_name="claude",
    )

    assert "Registered Claude plugin skill review-kit/review" in prompt
    assert "Registered Claude plugin skill review-kit/verify" in prompt


def test_mcp_capability_remains_implementation_only(tmp_path: Path) -> None:
    _mcp_manifest(tmp_path)
    _registry(
        tmp_path,
        [
            _entry(
                "serena",
                kind="mcp",
                source=".stagemesh/mcp/serena.json",
                providers=["codex"],
                stages=["REVIEW"],
                permissions=["workspace-read", "subprocess"],
            )
        ],
    )

    with pytest.raises(CapabilityRegistryError, match="only the IMPLEMENT stage"):
        inventory(tmp_path)


def test_runtime_review_receives_bound_review_capability_and_records_attachment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    git = GitWorkspace(project)
    git.init_if_needed()
    git.run("config", "user.email", "test@example.invalid")
    git.run("config", "user.name", "StageMesh Test")
    _skill(project)
    _registry(project, [_entry("domain-review", providers=["test"], stages=["REVIEW"])])
    (project / "README.md").write_text("candidate\n", encoding="utf-8")
    git.commit_all("candidate")
    candidate = git.head()
    contract = ChangeContract(
        objective="Review the candidate",
        allowed_files=("README.md",),
        capability_refs=("domain-review",),
    )
    canonical = canonical_contract_json(contract)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    command = (
        sys.executable,
        "-c",
        (
            "import sys; p=sys.stdin.read(); "
            "print('{\"decision\":\"PASS\"}' if 'Registered skill domain-review' in p "
            "else '{\"decision\":\"FAIL\",\"findings\":[{\"message\":\"skill missing\"}]}')"
        ),
    )
    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    task_id = store.upsert_task("Review the candidate", source_id="task-1")
    store.add_candidate(task_id, candidate, "implementer", durable_handoff=True)
    store.bind_contract(task_id, candidate, candidate, 1, digest, canonical)
    _freeze_capability_policy(store, project, task_id, contract, candidate)

    def prepare_review(_source: Path, target: Path, _candidate: str) -> None:
        shutil.copytree(project, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git"))

    monkeypatch.setattr(providers_module, "_prepare_review_checkout", prepare_review)
    monkeypatch.setattr(providers_module, "_git_output", lambda *_args: candidate)
    monkeypatch.setattr(providers_module, "_tracked_content_changed", lambda *_args: False)
    try:
        response = RuntimeCommandAdapter("test", command).review_candidate(
            "Review exactly this candidate.",
            project,
            candidate,
            store=store,
            task_id=task_id,
        )

        assert json.loads(response)["decision"] == "PASS", response
        row = store.conn.execute(
            "SELECT payload FROM audit_events WHERE event_type='capability.attached' "
            "AND json_extract(payload, '$.capability_id')='domain-review'"
        ).fetchone()
        payload = json.loads(row["payload"])
        assert payload["stage"] == "REVIEW"
        assert payload["provider"] == "test"
        assert payload["invocation"] == "unverified"
    finally:
        store.close()


@pytest.mark.parametrize("case", ["success", "missing_read", "missing_response"])
def test_observed_load_policy_enforces_review_skill_reads_and_records_redacted_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    git = GitWorkspace(project)
    git.init_if_needed()
    git.run("config", "user.email", "test@example.invalid")
    git.run("config", "user.name", "StageMesh Test")
    _skill(project)
    _registry(
        project,
        [_entry("domain-review", providers=["codex"], stages=["REVIEW"])],
    )
    registry = project / ".stagemesh" / "capabilities.json"
    registry_payload = json.loads(registry.read_text(encoding="utf-8"))
    registry_payload["skill_evidence_mode"] = OBSERVED_LOAD_REQUIRED
    registry.write_text(json.dumps(registry_payload), encoding="utf-8")
    (project / "README.md").write_text("candidate\n", encoding="utf-8")
    git.commit_all("candidate")
    candidate = git.head()
    contract = ChangeContract(
        objective="Review the candidate",
        allowed_files=("README.md",),
        capability_refs=("domain-review",),
    )
    canonical = canonical_contract_json(contract)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    task_id = store.upsert_task("Review the candidate", source_id="task-1")
    store.add_candidate(task_id, candidate, "implementer", durable_handoff=True)
    store.bind_contract(task_id, candidate, candidate, 1, digest, canonical)
    _freeze_capability_policy(store, project, task_id, contract, candidate)
    seen_commands: list[list[str]] = []

    def prepare_review(_source: Path, target: Path, _candidate: str) -> None:
        shutil.copytree(project, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git"))

    def popen(command: list[str], *_args: object, **_kwargs: object) -> SimpleNamespace:
        seen_commands.append(command)
        return SimpleNamespace(returncode=0)

    def communicate(_proc: object, prompt: str, _timeout: float) -> tuple[str, str, bool]:
        paths = [
            json.loads(line.split(": ", 1)[1])
            for line in prompt.splitlines()
            if line.startswith(("- Skill ", "- Project skill ", "- Registered skill "))
        ]
        events = [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": ["Get-Content", path],
                    },
                }
            )
            for path in paths
            if not (
                case == "missing_read"
                and str(path).replace("\\", "/").endswith(
                    "agent-skills/domain-review/SKILL.md"
                )
            )
        ]
        if case != "missing_response":
            events.append(
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": '{"decision":"PASS"}',
                        },
                    },
                )
            )
        return "\n".join(events), "", False

    monkeypatch.setattr(providers_module, "_prepare_review_checkout", prepare_review)
    monkeypatch.setattr(providers_module, "_git_output", lambda *_args: candidate)
    monkeypatch.setattr(providers_module, "_tracked_content_changed", lambda *_args: False)
    monkeypatch.setattr(providers_module, "_authorization_refusal", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(providers_module, "provider_environment", lambda *_args: {})
    monkeypatch.setattr(providers_module.shutil, "which", lambda _command: "codex")
    monkeypatch.setattr(providers_module.subprocess, "Popen", popen)
    monkeypatch.setattr(providers_module, "communicate_bounded", communicate)
    try:
        response = RuntimeCommandAdapter("codex", ("codex", "exec")).review_candidate(
            "Review exactly this candidate.",
            project,
            candidate,
            store=store,
            task_id=task_id,
        )

        assert "--json" in seen_commands[0]
        payload = json.loads(response)
        if case == "missing_read":
            assert payload["decision"] == "INFRASTRUCTURE_FAILURE"
            assert payload["reason"].startswith("capability_evidence_missing:")
            missing = store.conn.execute(
                "SELECT payload FROM audit_events WHERE event_type='capability.evidence_missing' "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
            evidence = json.loads(missing["payload"])
            assert evidence["stage"] == "REVIEW"
            assert "domain-review" in evidence["missing_capabilities"]
        elif case == "missing_response":
            assert payload == {
                "decision": "INFRASTRUCTURE_FAILURE",
                "reason": "provider_event_response_missing",
            }
        else:
            assert payload == {"decision": "PASS"}
            loaded = store.conn.execute(
                "SELECT payload FROM audit_events WHERE event_type='capability.loaded' "
                "AND json_extract(payload, '$.capability_id')='domain-review'"
            ).fetchone()
            evidence = json.loads(loaded["payload"])
            assert evidence["stage"] == "REVIEW"
            assert evidence["invocation"] == "observed-load"
            assert "_path" not in evidence and str(tmp_path) not in json.dumps(evidence)
    finally:
        store.close()


def test_codex_mcp_is_configured_per_invocation_with_no_ambient_servers(tmp_path: Path) -> None:
    manifest = _mcp_manifest(tmp_path)
    _registry(
        tmp_path,
        [
            _entry(
                "serena",
                kind="mcp",
                source=".stagemesh/mcp/serena.json",
                providers=["codex", "claude"],
                permissions=["workspace-read", "subprocess"],
            )
        ],
    )

    attachment = resolve_attachments(
        tmp_path, ("serena",), provider="codex", stage="IMPLEMENT"
    )[0].audit_payload()
    command, permitted = configure_codex_mcp_command(("codex", "exec"), [attachment])

    assert attachment["status"] == "configured"
    assert attachment["invocation"] == "unverified"
    assert attachment["content_sha256"]
    assert attachment["_path"] == str(manifest.resolve())
    assert "--ignore-user-config" in command
    assert "--json" not in command
    assert f"mcp_servers.serena.command={json.dumps(sys.executable)}" in command
    assert 'mcp_servers.serena.enabled_tools=["find_symbol","search_for_pattern"]' in command
    assert "mcp_servers.serena.required=true" in command
    assert "mcp_servers.serena.command" in permitted


def test_mcp_manifest_rejects_ambient_authority_and_invalid_config(tmp_path: Path) -> None:
    manifest = _mcp_manifest(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["env"] = {"TOKEN": "TEST_ONLY_NOT_A_CREDENTIAL"}
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    _registry(
        tmp_path,
        [
            _entry(
                "serena",
                kind="mcp",
                source=".stagemesh/mcp/serena.json",
                providers=["codex"],
                permissions=["workspace-read", "subprocess"],
            )
        ],
    )

    row = inventory(tmp_path)[0]
    assert row["installed"] is True
    assert row["configuration_valid"] is False
    assert row["available"] is False
    with pytest.raises(CapabilityRegistryError, match="invalid MCP configuration"):
        resolve_attachments(tmp_path, ("serena",), provider="codex", stage="IMPLEMENT")


def test_pool_skips_incompatible_provider_without_invoking_or_cooling_it(
    tmp_path: Path,
) -> None:
    from stagemesh.domain import ExecutionStatus
    from stagemesh.execution import ExecutionResult

    calls: list[str] = []

    class Adapter(RuntimeCommandAdapter):
        def check_stage_capacity(self, capability: str) -> str:
            return CapacityKind.AVAILABLE

        def execute(
            self,
            store: Store,
            task_id: str,
            claim_id: str | None,
            project: Path,
        ) -> ExecutionResult:
            calls.append(self.name)
            if self.name == "codex":
                return ExecutionResult(
                    ExecutionStatus.FAILED,
                    failure_reason=(
                        "capability_unavailable: capability claude-only is not compatible "
                        "with provider codex"
                    ),
                )
            return ExecutionResult(ExecutionStatus.SUCCEEDED, candidate_sha="candidate")

    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    store.upsert_task("Use the capability", source_id="task-1")
    pool = ProviderPool(
        [Adapter("codex", (sys.executable,)), Adapter("claude", (sys.executable,))],
        {IMPLEMENT: ("codex", "claude"), REVIEW: ("claude",)},
        require_independent=False,
    )
    try:
        result = PooledExecutor(pool).run(store, "task-1", None, tmp_path)
        assert result.status == "SUCCEEDED"
        assert calls == ["codex", "claude"]
        row = store.conn.execute(
            "SELECT payload FROM audit_events WHERE event_type='capability.provider_skipped'"
        ).fetchone()
        payload = json.loads(row["payload"])
        assert payload["provider"] == "codex"
        assert payload["provider_invoked"] is False
        assert not store.conn.execute(
            "SELECT 1 FROM audit_events WHERE event_type='provider.failure'"
        ).fetchall()
    finally:
        store.close()


def test_pool_reports_all_incompatible_providers_as_contract_failure(tmp_path: Path) -> None:
    from stagemesh.domain import ExecutionStatus
    from stagemesh.execution import ExecutionResult

    class Adapter(RuntimeCommandAdapter):
        def check_stage_capacity(self, capability: str) -> str:
            return CapacityKind.AVAILABLE

        def execute(
            self,
            store: Store,
            task_id: str,
            claim_id: str | None,
            project: Path,
        ) -> ExecutionResult:
            return ExecutionResult(
                ExecutionStatus.FAILED,
                failure_reason=f"capability_unavailable: not compatible with provider {self.name}",
            )

    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    store.upsert_task("Use the capability", source_id="task-1")
    pool = ProviderPool(
        [Adapter("codex", (sys.executable,)), Adapter("grok", (sys.executable,))],
        {IMPLEMENT: ("codex", "grok"), REVIEW: ("codex",)},
        require_independent=False,
    )
    try:
        result = PooledExecutor(pool).run(store, "task-1", None, tmp_path)
        assert result.status == "FAILED"
        assert result.capacity_failure is False
        assert result.failure_reason.startswith("no_compatible_implementation_provider:")
        assert [item["classification"] for item in result.provider_attempts] == [
            "capability_compatibility",
            "capability_compatibility",
        ]
    finally:
        store.close()


def test_contract_persists_capability_refs_without_changing_empty_contracts() -> None:
    plain = parse_contract({"objective": "Ship safely"})
    requested = parse_contract({"objective": "Ship safely", "capability_refs": ["domain-review"]})

    assert requested.capability_refs == ("domain-review",)
    assert "capability_refs" not in json.loads(canonical_contract_json(plain))
    assert json.loads(canonical_contract_json(requested))["capability_refs"] == ["domain-review"]


def test_attachment_audit_records_delivery_but_not_invocation(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    try:
        _record_capability_attachments(
            store,
            "task-1",
            "codex",
            "IMPLEMENT",
            [
                {
                    "capability_id": "domain-review",
                    "kind": "skill",
                    "origin": "project-registry",
                    "source": "agent-skills/domain-review/SKILL.md",
                    "content_sha256": "a" * 64,
                    "permissions": ["workspace-read"],
                    "selection": "contract",
                    "status": "attached",
                    "invocation": "unverified",
                    "_path": "C:/test-fixture/workspace/SKILL.md",
                }
            ],
        )
        row = store.conn.execute(
            "SELECT payload FROM audit_events WHERE event_type='capability.attached'"
        ).fetchone()
        payload = json.loads(row["payload"])
        assert payload["provider"] == "codex"
        assert payload["status"] == "attached"
        assert payload["invocation"] == "unverified"
        assert "absolute_path" not in payload
        assert "_path" not in payload
    finally:
        store.close()


def test_unsupported_requested_adapter_stops_worker_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stagemesh import providers

    _mcp_manifest(tmp_path)
    _registry(
        tmp_path,
        [
            _entry(
                "serena",
                kind="mcp",
                source=".stagemesh/mcp/serena.json",
                providers=["test"],
                permissions=["workspace-read", "subprocess"],
            )
        ],
    )
    bound = ChangeContract(objective="Use Serena", capability_refs=("serena",))
    monkeypatch.setattr(providers, "record_task_baseline", lambda *_: "baseline")
    monkeypatch.setattr(providers, "bind_task_contract", lambda *_: SimpleNamespace(contract=bound))
    monkeypatch.setattr(providers, "remediation_context", lambda *_: None)
    monkeypatch.setattr(providers, "_provider_budget_refusal", lambda *_: None)
    monkeypatch.setattr(
        providers.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("worker launched with unsupported capability"),
    )
    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    store.upsert_task("Use Serena", source_id="task-1")
    lease = SimpleNamespace(path=tmp_path, check=lambda _phase: None)
    try:
        result = RuntimeCommandAdapter("test", (sys.executable,))._execute_owned(
            store, "task-1", None, tmp_path, lease
        )
        assert result.status == "FAILED"
        assert result.failure_reason.startswith("capability_unavailable:")
        assert not list(store.running_executions())
    finally:
        store.close()


def test_capabilities_cli_is_read_only_inventory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _skill(tmp_path)
    _registry(tmp_path, [_entry("domain-review")])

    assert main(["--project", str(tmp_path), "capabilities", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["registry"] == ".stagemesh/capabilities.json"
    assert payload["capabilities"][0]["usable"] is True
    assert payload["capabilities"][0]["invocation"] == "not-observed"
    assert payload["skill_evidence_mode"] == ATTACHMENT_REQUIRED


def test_capabilities_cli_reports_task_scoped_durable_usage_read_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _skill(tmp_path)
    _mcp_manifest(tmp_path)
    _registry(
        tmp_path,
        [
            _entry("domain-review", stages=["IMPLEMENT", "REVIEW"]),
            _entry(
                "serena",
                kind="mcp",
                source=".stagemesh/mcp/serena.json",
                providers=["codex"],
                permissions=["workspace-read", "subprocess"],
            ),
        ],
    )
    current_digests = {
        str(row["id"]): str(row["observed_content_sha256"])
        for row in inventory(tmp_path)
    }
    database = tmp_path / ".stagemesh" / "stagemesh.sqlite3"
    store = Store(database)
    store.migrate()
    task_id = store.upsert_task("Use guidance", source_id="task-1")
    _record_capability_attachments(
        store,
        task_id,
        "codex",
        "IMPLEMENT",
        [
            {
                "capability_id": "domain-review",
                "kind": "skill",
                "origin": "project-registry",
                "source": "agent-skills/domain-review/SKILL.md",
                    "content_sha256": current_digests["domain-review"],
                "permissions": ["workspace-read"],
                "selection": "contract",
                "status": "attached",
                "invocation": "unverified",
            }
        ],
    )
    store.add_audit_event(
        "capability.loaded",
        {
            "task_id": task_id,
            "provider": "codex",
            "stage": "IMPLEMENT",
            "capability_id": "domain-review",
        },
    )
    _record_capability_attachments(
        store,
        task_id,
        "codex",
        "IMPLEMENT",
        [
            {
                "capability_id": "serena",
                "kind": "mcp",
                "origin": "project-registry",
                "source": ".stagemesh/mcp/serena.json",
                "content_sha256": current_digests["serena"],
                "permissions": ["workspace-read", "subprocess"],
                "selection": "contract",
                "status": "configured",
                "invocation": "unverified",
            }
        ],
    )
    store.add_audit_event(
        "capability.invoked",
        {
            "task_id": task_id,
            "provider": "codex",
            "stage": "IMPLEMENT",
            "capability_id": "serena",
        },
    )
    store.close()
    before = database.stat().st_mtime_ns

    assert main(["--project", str(tmp_path), "capabilities", "--task", task_id, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["history_available"] is True
    assert payload["task_exists"] is True
    rows = {row["id"]: row for row in payload["capabilities"]}
    assert rows["domain-review"]["invocation"] == "loaded"
    assert rows["domain-review"]["usage"] == {
        "attached": 1,
        "content_sha256s": [current_digests["domain-review"]],
        "source_tree_sha256s": [],
        "evidence_missing": 0,
        "invoked": 0,
        "last_observed_at": rows["domain-review"]["usage"]["last_observed_at"],
        "loaded": 1,
        "providers": ["codex"],
        "stages": ["IMPLEMENT"],
        "status": "loaded",
    }
    assert rows["serena"]["invocation"] == "invoked"
    assert rows["serena"]["usage"]["attached"] == 1
    assert rows["serena"]["usage"]["loaded"] == 0
    assert rows["serena"]["usage"]["invoked"] == 1
    assert database.stat().st_mtime_ns == before


def test_capabilities_task_query_does_not_create_missing_runtime_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _skill(tmp_path)
    _registry(tmp_path, [_entry("domain-review")])
    database = tmp_path / ".stagemesh" / "stagemesh.sqlite3"

    assert main(["--project", str(tmp_path), "capabilities", "--task", "missing", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["history_available"] is False
    assert payload["task_exists"] is False
    assert payload["capabilities"][0]["invocation"] == "not-observed"
    assert not database.exists()


def test_registry_skill_evidence_mode_is_explicit_and_validated(tmp_path: Path) -> None:
    path = tmp_path / ".stagemesh" / "capabilities.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "skill_evidence_mode": OBSERVED_LOAD_REQUIRED,
                "capabilities": [],
            }
        ),
        encoding="utf-8",
    )

    assert skill_evidence_mode(tmp_path) == OBSERVED_LOAD_REQUIRED
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["skill_evidence_mode"] = "trust-me"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CapabilityRegistryError, match="skill_evidence_mode"):
        skill_evidence_mode(tmp_path)
    path.write_text(
        json.dumps(
            {"format_version": 1, "skill_evidence_mode": OBSERVED_LOAD_REQUIRED}
        ),
        encoding="utf-8",
    )
    with pytest.raises(CapabilityRegistryError, match="capabilities must be a list"):
        skill_evidence_mode(tmp_path)


def test_skill_evidence_policy_plan_is_read_only_and_reports_bounded_effects(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = tmp_path / ".stagemesh" / "capabilities.json"

    assert main(
        [
            "--project",
            str(tmp_path),
            "capabilities",
            "policy",
            OBSERVED_LOAD_REQUIRED,
            "--plan",
            "--json",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload == {
        "changed": True,
        "current_mode": ATTACHMENT_REQUIRED,
        "database_writes": 0,
        "network_calls": 0,
        "operation": "set-skill-evidence-mode",
        "package_installs": 0,
        "planned": True,
        "planned_writes": [".stagemesh/capabilities.json"],
        "provider_calls": 0,
        "registry": ".stagemesh/capabilities.json",
        "skill_evidence_mode": OBSERVED_LOAD_REQUIRED,
        "writes": [],
    }
    assert not registry.exists()


def test_skill_evidence_policy_apply_is_atomic_preserves_entries_and_noop_does_not_rewrite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _skill(tmp_path)
    _registry(tmp_path, [_entry("domain-review")])
    registry = tmp_path / ".stagemesh" / "capabilities.json"

    assert main(
        [
            "--project",
            str(tmp_path),
            "capabilities",
            "policy",
            OBSERVED_LOAD_REQUIRED,
            "--json",
        ]
    ) == 0
    applied = json.loads(capsys.readouterr().out)
    content = registry.read_bytes()
    modified = registry.stat().st_mtime_ns
    persisted = json.loads(content)

    assert applied["written"] is True and applied["writes"] == [
        ".stagemesh/capabilities.json"
    ]
    assert persisted["skill_evidence_mode"] == OBSERVED_LOAD_REQUIRED
    assert persisted["capabilities"] == [_entry("domain-review")]

    assert main(
        [
            "--project",
            str(tmp_path),
            "capabilities",
            "policy",
            OBSERVED_LOAD_REQUIRED,
            "--json",
        ]
    ) == 0
    noop = json.loads(capsys.readouterr().out)
    assert noop["changed"] is False and noop["written"] is False
    assert registry.read_bytes() == content and registry.stat().st_mtime_ns == modified


def test_skill_evidence_policy_refuses_registry_symlink_without_reading_or_writing_outside(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = tmp_path / ".stagemesh" / "capabilities.json"
    registry.parent.mkdir(parents=True)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-policy.json"
    outside.write_text(
        json.dumps({"format_version": 1, "capabilities": []}), encoding="utf-8"
    )
    try:
        registry.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    before = outside.read_bytes()

    assert main(
        [
            "--project",
            str(tmp_path),
            "capabilities",
            "policy",
            OBSERVED_LOAD_REQUIRED,
            "--plan",
        ]
    ) == 2
    assert "read refuses a symlink" in capsys.readouterr().err
    assert outside.read_bytes() == before and registry.is_symlink()


def test_discovery_lists_multiple_vendored_skills_and_previews_registration_without_writes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("brainstorming", "writing-plans", "executing-plans"):
        _skill(tmp_path, name)
    _registry(
        tmp_path,
        [
            _entry(
                "brainstorming",
                source="agent-skills/brainstorming/SKILL.md",
                providers=["codex"],
            )
        ],
    )
    registry = tmp_path / ".stagemesh" / "capabilities.json"
    before = registry.read_bytes()

    assert main(
        [
            "--project",
            str(tmp_path),
            "capabilities",
            "discover",
            "--root",
            "agent-skills",
            "--json",
        ]
    ) == 0
    discovered = json.loads(capsys.readouterr().out)

    rows = {row["id"]: row for row in discovered["candidates"]}
    assert list(sorted(rows)) == ["brainstorming", "executing-plans", "writing-plans"]
    assert rows["brainstorming"]["registered"] is True
    assert rows["brainstorming"]["enabled"] is True
    assert rows["brainstorming"]["usable"] is True
    assert rows["writing-plans"]["registered"] is False
    assert discovered["read_only"] is True
    assert discovered["writes"] == [] and discovered["subprocess_calls"] == 0

    assert main(
        [
            "--project",
            str(tmp_path),
            "capabilities",
            "discover",
            "--root",
            "agent-skills",
            "--registration-plan",
            "writing-plans",
            "--provider",
            "codex",
            "--provider",
            "grok",
            "--stage",
            "IMPLEMENT",
            "--stage",
            "REVIEW",
            "--json",
        ]
    ) == 0
    plan = json.loads(capsys.readouterr().out)
    assert len(plan["entry"].pop("content_sha256")) == 64
    assert plan["entry"] == {
        "enabled": False,
        "id": "writing-plans",
        "kind": "skill",
        "permissions": ["workspace-read"],
        "providers": ["codex", "grok"],
        "source": "agent-skills/writing-plans/SKILL.md",
        "stages": ["IMPLEMENT", "REVIEW"],
    }
    assert plan["planned"] is True and plan["writes"] == []
    assert registry.read_bytes() == before
    assert not (tmp_path / ".stagemesh" / "runtime.sqlite3").exists()


def test_discovery_reuses_plugin_and_mcp_validators_without_activating_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _claude_plugin(tmp_path)
    no_manifest_skill = (
        tmp_path / ".claude" / "plugins" / "no-manifest" / "skills" / "review" / "SKILL.md"
    )
    no_manifest_skill.parent.mkdir(parents=True)
    no_manifest_skill.write_text("# Review\n", encoding="utf-8")
    unsafe = _claude_plugin(tmp_path, "unsafe-kit")
    (unsafe / "commands").mkdir()
    _mcp_manifest(tmp_path)
    bad_mcp = tmp_path / ".stagemesh" / "mcp" / "bad.json"
    bad_mcp.write_text("{}", encoding="utf-8")

    assert main(
        [
            "--project",
            str(tmp_path),
            "capabilities",
            "discover",
            "--root",
            ".",
            "--json",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    rows = {row["id"]: row for row in payload["candidates"]}

    assert rows["review-kit"]["kind"] == "claude-plugin"
    assert rows["review-kit"]["valid"] is True
    assert rows["no-manifest"]["kind"] == "claude-plugin"
    assert rows["no-manifest"]["valid"] is True
    assert rows["unsafe-kit"]["valid"] is False
    assert "runtime component is unsupported" in rows["unsafe-kit"]["reason"]
    assert rows["serena"]["kind"] == "mcp" and rows["serena"]["valid"] is True
    assert rows["bad"]["valid"] is False
    assert payload["provider_calls"] == 0 and payload["subprocess_calls"] == 0
    assert not (tmp_path / ".stagemesh" / "runtime.sqlite3").exists()

    for candidate_id, provider, stage, kind in (
        ("review-kit", "claude", "REVIEW", "claude-plugin"),
        ("serena", "codex", "IMPLEMENT", "mcp"),
    ):
        assert main(
            [
                "--project",
                str(tmp_path),
                "capabilities",
                "discover",
                "--root",
                ".",
                "--registration-plan",
                candidate_id,
                "--provider",
                provider,
                "--stage",
                stage,
                "--json",
            ]
        ) == 0
        plan = json.loads(capsys.readouterr().out)
        assert plan["entry"]["kind"] == kind
        assert plan["entry"]["enabled"] is False and plan["writes"] == []


def test_discovery_fails_closed_on_candidate_collisions_and_bounds(tmp_path: Path) -> None:
    for prefix in ("one", "two"):
        skill = tmp_path / "packages" / prefix / "same" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("# Same\n", encoding="utf-8")

    with pytest.raises(CapabilityRegistryError, match="candidate id collision"):
        discover_capabilities(tmp_path, "packages")
    with pytest.raises(CapabilityRegistryError, match="more than 1 candidates"):
        discover_capabilities(tmp_path, "packages", max_candidates=1)
    with pytest.raises(CapabilityRegistryError, match="entry limit"):
        discover_capabilities(tmp_path, "packages", max_entries=1)

    oversized = tmp_path / "oversized" / "too-large" / "SKILL.md"
    oversized.parent.mkdir(parents=True)
    oversized.write_bytes(b"x" * (256 * 1024 + 1))
    payload = discover_capabilities(tmp_path, "oversized")
    assert payload["candidates"][0]["valid"] is False
    assert "byte discovery limit" in payload["candidates"][0]["reason"]


def test_discovery_refuses_symlinks_without_following_them(tmp_path: Path) -> None:
    root = tmp_path / "agent-skills"
    root.mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-outside-skill.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    link = root / "SKILL.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(CapabilityRegistryError, match="discovery refuses symlink"):
        discover_capabilities(tmp_path, "agent-skills")
    assert outside.read_text(encoding="utf-8") == "# Outside\n"


def _capability_contract_fixture(project: Path) -> Path:
    _skill(project)
    _registry(
        project,
        [
            _entry(
                "domain-review",
                providers=["test"],
                stages=["IMPLEMENT", "REVIEW"],
            )
        ],
    )
    runtime = project / ".stagemesh"
    (runtime / "contracts").mkdir(parents=True, exist_ok=True)
    (runtime / "config.json").write_text(
        json.dumps(
            {
                "providers": {"test": "git"},
                "routing": {
                    "require_independent_review": False,
                    "pools": {"IMPLEMENT": ["test"], "REVIEW": ["test"]},
                },
            }
        ),
        encoding="utf-8",
    )
    contract = runtime / "contracts" / "task-1.json"
    contract.write_text(
        json.dumps(
            {
                "objective": "Use project guidance",
                "allowed_files": ["src/**"],
                "invariants": ["Keep unrelated behavior unchanged"],
            }
        ),
        encoding="utf-8",
    )
    return contract


def test_task_capability_plan_and_apply_are_explicit_atomic_and_noop_safe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    contract = _capability_contract_fixture(tmp_path)
    before = contract.read_bytes()

    assert main(
        [
            "--project",
            str(tmp_path),
            "capabilities",
            "contract",
            "--task",
            "task-1",
            "--add",
            "domain-review",
            "--plan",
            "--json",
        ]
    ) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["changed"] is True
    assert plan["proposed_capability_refs"] == ["domain-review"]
    assert plan["routing"]["status"] == "ready"
    assert plan["writes"] == [] and plan["database_writes"] == 0
    assert plan["provider_calls"] == 0 and contract.read_bytes() == before
    assert not (tmp_path / ".stagemesh" / "stagemesh.sqlite3").exists()

    assert main(
        [
            "--project",
            str(tmp_path),
            "capabilities",
            "contract",
            "--task",
            "task-1",
            "--add",
            "domain-review",
            "--json",
        ]
    ) == 0
    applied = json.loads(capsys.readouterr().out)
    persisted = json.loads(contract.read_text(encoding="utf-8"))
    assert applied["written"] is True and applied["writes"] == [
        ".stagemesh/contracts/task-1.json"
    ]
    assert persisted["capability_refs"] == ["domain-review"]
    assert persisted["allowed_files"] == ["src/**"]
    assert persisted["invariants"] == ["Keep unrelated behavior unchanged"]

    modified = contract.stat().st_mtime_ns
    assert main(
        [
            "--project",
            str(tmp_path),
            "capabilities",
            "contract",
            "--task",
            "task-1",
            "--remove",
            "not-selected",
            "--json",
        ]
    ) == 0
    noop = json.loads(capsys.readouterr().out)
    assert noop["changed"] is False and noop["written"] is False
    assert contract.stat().st_mtime_ns == modified


def test_task_capability_change_reports_frozen_binding_without_mutating_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    contract = _capability_contract_fixture(tmp_path)
    database = tmp_path / ".stagemesh" / "stagemesh.sqlite3"
    store = Store(database)
    store.migrate()
    task_id = store.upsert_task("Use project guidance", source_id="task-1")
    canonical = canonical_contract_json(parse_contract(json.loads(contract.read_text())))
    store.bind_task_contract(task_id, "a" * 40, 1, hashlib.sha256(canonical.encode()).hexdigest(), canonical)
    frozen = dict(store.task_contract(task_id))
    store.close()

    assert main(
        [
            "--project",
            str(tmp_path),
            "capabilities",
            "contract",
            "--task",
            "task-1",
            "--add",
            "domain-review",
            "--plan",
            "--json",
        ]
    ) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["runtime_contract_frozen"] is True
    assert plan["rebind_required"] is True
    assert plan["next_command"] == "stagemesh rebind-contract --task task-1 --plan"
    assert contract.read_text(encoding="utf-8").find("capability_refs") == -1

    reopened = Store(database)
    try:
        assert dict(reopened.task_contract(task_id)) == frozen
    finally:
        reopened.close()


def test_task_capability_preflight_reuses_provider_and_stage_compatibility(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    contract = _capability_contract_fixture(tmp_path)
    payload = json.loads(contract.read_text(encoding="utf-8"))
    payload["capability_refs"] = ["domain-review"]
    contract.write_text(json.dumps(payload), encoding="utf-8")
    _freeze_preflight_policy(tmp_path, "task-1")
    database = tmp_path / ".stagemesh" / "stagemesh.sqlite3"
    database_before = database.read_bytes()

    assert main(
        [
            "--project",
            str(tmp_path),
            "capabilities",
            "preflight",
            "--task",
            "task-1",
            "--json",
        ]
    ) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ready" and report["ready"] is True
    assert report["requested_capabilities"] == ["domain-review"]
    capability = report["capabilities"][0]
    assert capability["registered"] is True
    assert capability["installed"] is True
    assert capability["available"] is True
    assert capability["enabled"] is True
    assert capability["integrity"] == "unpinned"
    assert capability["eligible_routes"] == [
        {"provider": "test", "stage": "IMPLEMENT"},
        {"provider": "test", "stage": "REVIEW"},
    ]
    assert report["provider_calls"] == 0 and report["writes"] == []
    assert [stage["eligible_providers"] for stage in report["stages"]] == [
        ["test"],
        ["test"],
    ]
    assert database.read_bytes() == database_before

    registry = json.loads((tmp_path / ".stagemesh" / "capabilities.json").read_text())
    registry["capabilities"][0]["providers"] = ["claude"]
    (tmp_path / ".stagemesh" / "capabilities.json").write_text(
        json.dumps(registry), encoding="utf-8"
    )
    assert main(
        [
            "--project",
            str(tmp_path),
            "capabilities",
            "preflight",
            "--task",
            "task-1",
            "--json",
        ]
    ) == 2
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["status"] == "blocked" and blocked["ready"] is False
    assert blocked["capabilities"][0]["eligible_routes"] == []
    assert len(blocked["capabilities"][0]["ineligible_routes"]) == 2
    reasons = [
        provider["reason"]
        for stage in blocked["stages"]
        for provider in stage["providers"]
    ]
    assert all("not compatible with provider test" in str(reason) for reason in reasons)


def test_task_capability_selection_rejects_unsafe_adds_but_allows_unavailable_removal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    contract = _capability_contract_fixture(tmp_path)
    registry_path = tmp_path / ".stagemesh" / "capabilities.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["capabilities"][0]["enabled"] = False
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    before = contract.read_bytes()

    command = [
        "--project",
        str(tmp_path),
        "capabilities",
        "contract",
        "--task",
        "task-1",
        "--add",
        "domain-review",
        "--plan",
    ]
    assert main(command) == 2
    assert "disabled capability" in capsys.readouterr().err
    assert contract.read_bytes() == before

    payload = json.loads(contract.read_text(encoding="utf-8"))
    payload["capability_refs"] = ["domain-review"]
    contract.write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "agent-skills" / "domain-review" / "SKILL.md").unlink()
    assert main(
        [
            "--project",
            str(tmp_path),
            "capabilities",
            "contract",
            "--task",
            "task-1",
            "--remove",
            "domain-review",
            "--json",
        ]
    ) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["removed"] == ["domain-review"] and result["written"] is True
    assert "capability_refs" not in json.loads(contract.read_text(encoding="utf-8"))


def test_task_capability_contract_refuses_symlinks_without_touching_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _capability_contract_fixture(tmp_path)
    contract = tmp_path / ".stagemesh" / "contracts" / "task-1.json"
    outside = tmp_path.parent / f"{tmp_path.name}-outside-contract.json"
    outside.write_bytes(contract.read_bytes())
    contract.unlink()
    try:
        contract.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    before = outside.read_bytes()

    assert main(
        [
            "--project",
            str(tmp_path),
            "capabilities",
            "contract",
            "--task",
            "task-1",
            "--add",
            "domain-review",
            "--plan",
        ]
    ) == 2
    assert "refuses a symlink" in capsys.readouterr().err
    assert outside.read_bytes() == before and contract.is_symlink()


def test_capability_preflight_routes_codex_mcp_and_claude_plugin_without_invocation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    command_dir = tmp_path / "provider-bin"
    command_dir.mkdir()
    for name in ("codex", "claude"):
        executable = command_dir / (f"{name}.exe" if os.name == "nt" else name)
        executable.write_text("", encoding="utf-8")
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(command_dir) + os.pathsep + os.environ.get("PATH", ""))
    _mcp_manifest(tmp_path)
    _claude_plugin(tmp_path)
    registry = tmp_path / ".stagemesh" / "capabilities.json"
    registry.write_text(
        json.dumps(
            {
                "format_version": 1,
                "skill_evidence_mode": OBSERVED_LOAD_REQUIRED,
                "capabilities": [
                    _entry(
                        "serena",
                        kind="mcp",
                        source=".stagemesh/mcp/serena.json",
                        providers=["codex"],
                        stages=["IMPLEMENT"],
                        permissions=["workspace-read", "subprocess"],
                    ),
                    _entry(
                        "review-kit",
                        kind="claude-plugin",
                        source=".claude/plugins/review-kit",
                        providers=["claude"],
                        stages=["REVIEW"],
                    ),
                ],
            }
        ),
        encoding="utf-8",
    )
    contracts = tmp_path / ".stagemesh" / "contracts"
    contracts.mkdir(parents=True)
    (contracts / "task-1.json").write_text(
        json.dumps(
            {
                "objective": "Use bounded tools and review guidance",
                "capability_refs": ["serena", "review-kit"],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / ".stagemesh" / "config.json").write_text(
        json.dumps(
            {
                "providers": {"codex": "codex", "claude": "claude"},
                "routing": {
                    "require_independent_review": True,
                    "pools": {"IMPLEMENT": ["codex"], "REVIEW": ["claude"]},
                },
            }
        ),
        encoding="utf-8",
    )
    _freeze_preflight_policy(tmp_path, "task-1")
    database = tmp_path / ".stagemesh" / "stagemesh.sqlite3"
    database_before = database.read_bytes()

    assert main(
        [
            "--project",
            str(tmp_path),
            "capabilities",
            "preflight",
            "--task",
            "task-1",
            "--json",
        ]
    ) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ready"
    assert report["skill_evidence_mode"] == OBSERVED_LOAD_REQUIRED
    assert report["stages"][0]["requested_capabilities"] == ["serena"]
    assert report["stages"][0]["evidence_requirements"] == ["mcp-invocation"]
    assert report["stages"][0]["eligible_providers"] == ["codex"]
    assert report["stages"][1]["requested_capabilities"] == ["review-kit"]
    assert report["stages"][1]["evidence_requirements"] == ["observed-load"]
    assert report["stages"][1]["eligible_providers"] == ["claude"]
    assert report["provider_calls"] == 0 and report["writes"] == []
    assert database.read_bytes() == database_before


def test_capability_preflight_supports_distinct_grok_and_agy_skill_routes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _skill(tmp_path)
    _registry(
        tmp_path,
        [
            _entry(
                "domain-review",
                providers=["grok", "agy"],
                stages=["IMPLEMENT", "REVIEW"],
            )
        ],
    )
    contracts = tmp_path / ".stagemesh" / "contracts"
    contracts.mkdir(parents=True)
    (contracts / "task-1.json").write_text(
        json.dumps(
            {
                "objective": "Use guidance across providers",
                "capability_refs": ["domain-review"],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / ".stagemesh" / "config.json").write_text(
        json.dumps(
            {
                "providers": {"grok": "git", "agy": "git"},
                "routing": {
                    "require_independent_review": True,
                    "pools": {"IMPLEMENT": ["grok"], "REVIEW": ["agy"]},
                },
            }
        ),
        encoding="utf-8",
    )
    _freeze_preflight_policy(tmp_path, "task-1")

    assert main(
        [
            "--project",
            str(tmp_path),
            "capabilities",
            "preflight",
            "--task",
            "task-1",
            "--json",
        ]
    ) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["independent_provider_pair"] is True
    assert report["stages"][0]["eligible_providers"] == ["grok"]
    assert report["stages"][1]["eligible_providers"] == ["agy"]


def test_capability_policy_snapshot_is_deterministic_and_selected_only(
    tmp_path: Path,
) -> None:
    _skill(tmp_path)
    selected = _entry("domain-review", providers=["codex"], stages=["IMPLEMENT"])
    unrelated = _entry("other-review", providers=["claude"], stages=["REVIEW"])
    _registry(tmp_path, [unrelated, selected])

    first = capability_policy_snapshot(tmp_path, ["domain-review", "domain-review"])
    _registry(
        tmp_path,
        [
            selected,
            {**unrelated, "enabled": False, "providers": ["grok"]},
        ],
    )
    second = capability_policy_snapshot(tmp_path, ["domain-review"])

    assert first == second
    assert json.loads(str(first["canonical_json"]))["capabilities"] == [selected]
    assert first["skill_evidence_mode"] == ATTACHMENT_REQUIRED

    registry = tmp_path / ".stagemesh" / "capabilities.json"
    payload = json.loads(registry.read_text(encoding="utf-8"))
    payload["skill_evidence_mode"] = OBSERVED_LOAD_REQUIRED
    registry.write_text(json.dumps(payload), encoding="utf-8")
    observed_mode = capability_policy_snapshot(tmp_path, ["domain-review"])
    assert observed_mode["skill_evidence_mode"] == OBSERVED_LOAD_REQUIRED
    assert observed_mode["digest"] != first["digest"]

    _registry(tmp_path, [{**selected, "permissions": ["workspace-read"]}, unrelated])
    assert capability_policy_snapshot(tmp_path, ["domain-review"])["digest"] == first["digest"]
    _registry(tmp_path, [{**selected, "enabled": False}, unrelated])
    assert capability_policy_snapshot(tmp_path, ["domain-review"])["digest"] != first["digest"]


def test_capability_policy_binding_reports_verified_drifted_and_invalid(
    tmp_path: Path,
) -> None:
    _skill(tmp_path)
    entry = _entry("domain-review", providers=["codex"], stages=["IMPLEMENT"])
    _registry(tmp_path, [entry])
    contract = ChangeContract(
        objective="Use frozen guidance", capability_refs=("domain-review",)
    )
    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    task_id = store.upsert_task("Use frozen guidance", source_id="task-1")

    assert capability_policy_report(
        tmp_path, store.conn, task_id, contract.capability_refs
    )["state"] == "legacy-unbound"
    _freeze_capability_policy(store, tmp_path, task_id, contract, "0" * 40)
    verified = capability_policy_report(
        tmp_path, store.conn, task_id, contract.capability_refs
    )
    assert verified["state"] == "verified"
    assert verified["dispatch_ready"] is True
    assert verified["frozen_skill_evidence_mode"] == ATTACHMENT_REQUIRED
    assert verified["current_skill_evidence_mode"] == ATTACHMENT_REQUIRED

    _registry(tmp_path, [{**entry, "enabled": False}])
    drifted = capability_policy_report(
        tmp_path, store.conn, task_id, contract.capability_refs
    )
    assert drifted["state"] == "drifted"
    assert drifted["rebind_required"] is True
    assert drifted["frozen_digest"] != drifted["current_digest"]

    store.conn.execute(
        "UPDATE task_capability_policies SET digest=? WHERE task_id=?",
        ("f" * 64, task_id),
    )
    store.conn.commit()
    assert capability_policy_report(
        tmp_path, store.conn, task_id, contract.capability_refs
    )["state"] == "invalid"
    store.close()


@pytest.mark.parametrize("drift", ["declaration", "evidence-mode"])
def test_implementation_policy_drift_refuses_before_provider_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    from stagemesh import providers

    _skill(tmp_path)
    entry = _entry("domain-review", providers=["test"], stages=["IMPLEMENT"])
    _registry(tmp_path, [entry])
    bound = ChangeContract(
        objective="Use frozen guidance", capability_refs=("domain-review",)
    )
    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    task_id = store.upsert_task("Use frozen guidance", source_id="task-1")
    _freeze_capability_policy(store, tmp_path, task_id, bound, "0" * 40)
    if drift == "declaration":
        _registry(tmp_path, [{**entry, "enabled": False}])
    else:
        registry = tmp_path / ".stagemesh" / "capabilities.json"
        payload = json.loads(registry.read_text(encoding="utf-8"))
        payload["skill_evidence_mode"] = OBSERVED_LOAD_REQUIRED
        registry.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(providers, "record_task_baseline", lambda *_: "0" * 40)
    monkeypatch.setattr(
        providers, "bind_task_contract", lambda *_: SimpleNamespace(contract=bound)
    )
    monkeypatch.setattr(providers, "remediation_context", lambda *_: None)
    monkeypatch.setattr(providers, "_provider_budget_refusal", lambda *_: None)
    monkeypatch.setattr(
        providers.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("provider launched after capability policy drift"),
    )
    lease = SimpleNamespace(path=tmp_path, check=lambda _phase: None)
    try:
        result = RuntimeCommandAdapter("test", (sys.executable,))._execute_owned(
            store, task_id, None, tmp_path, lease
        )
        assert result.status == "FAILED"
        assert "capability policy drifted" in str(result.failure_reason)
        assert not list(store.running_executions())
        receipt = json.loads(
            store.conn.execute(
                "SELECT payload FROM audit_events WHERE event_type='capability.policy_refused'"
            ).fetchone()["payload"]
        )
        assert receipt["provider_invoked"] is False
        assert receipt["stage"] == "IMPLEMENT"
        assert receipt["capability_ids"] == ["domain-review"]
        assert receipt["policy_state"] == "drifted"
        assert "expected=" in receipt["reason"] and "observed=" in receipt["reason"]
        if drift == "evidence-mode":
            assert receipt["frozen_skill_evidence_mode"] == ATTACHMENT_REQUIRED
            assert receipt["current_skill_evidence_mode"] == OBSERVED_LOAD_REQUIRED
    finally:
        store.close()


@pytest.mark.parametrize("drift", ["declaration", "evidence-mode"])
def test_review_policy_drift_refuses_before_provider_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    git = GitWorkspace(project)
    git.init_if_needed()
    git.run("config", "user.email", "test@example.invalid")
    git.run("config", "user.name", "StageMesh Test")
    _skill(project)
    entry = _entry("domain-review", providers=["test"], stages=["REVIEW"])
    _registry(project, [entry])
    (project / "README.md").write_text("candidate\n", encoding="utf-8")
    git.commit_all("candidate")
    candidate = git.head()
    contract = ChangeContract(
        objective="Review with frozen guidance",
        allowed_files=("README.md",),
        capability_refs=("domain-review",),
    )
    canonical = canonical_contract_json(contract)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    task_id = store.upsert_task("Review with frozen guidance", source_id="task-1")
    store.add_candidate(task_id, candidate, "implementer", durable_handoff=True)
    store.bind_contract(task_id, candidate, candidate, 1, digest, canonical)
    _freeze_capability_policy(store, project, task_id, contract, candidate)
    if drift == "declaration":
        _registry(project, [{**entry, "enabled": False}])
    else:
        registry = project / ".stagemesh" / "capabilities.json"
        payload = json.loads(registry.read_text(encoding="utf-8"))
        payload["skill_evidence_mode"] = OBSERVED_LOAD_REQUIRED
        registry.write_text(json.dumps(payload), encoding="utf-8")

    def prepare_review(_source: Path, target: Path, _candidate: str) -> None:
        shutil.copytree(project, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git"))

    monkeypatch.setattr(providers_module, "_prepare_review_checkout", prepare_review)
    monkeypatch.setattr(providers_module, "_git_output", lambda *_args: candidate)
    monkeypatch.setattr(
        providers_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("review provider launched after policy drift"),
    )
    try:
        response = RuntimeCommandAdapter("test", (sys.executable,)).review_candidate(
            "Review exactly this candidate.",
            project,
            candidate,
            store=store,
            task_id=task_id,
        )
        failure = json.loads(response)
        assert failure["decision"] == "INFRASTRUCTURE_FAILURE"
        assert "capability policy drifted" in failure["reason"]
        receipt = json.loads(
            store.conn.execute(
                "SELECT payload FROM audit_events WHERE event_type='capability.policy_refused'"
            ).fetchone()["payload"]
        )
        assert receipt["provider_invoked"] is False
        assert receipt["stage"] == "REVIEW"
        assert receipt["capability_ids"] == ["domain-review"]
        assert receipt["policy_state"] == "drifted"
        if drift == "evidence-mode":
            assert receipt["frozen_skill_evidence_mode"] == ATTACHMENT_REQUIRED
            assert receipt["current_skill_evidence_mode"] == OBSERVED_LOAD_REQUIRED
    finally:
        store.close()


def test_preflight_exposes_legacy_unbound_without_creating_a_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    contract = _capability_contract_fixture(tmp_path)
    payload = json.loads(contract.read_text(encoding="utf-8"))
    payload["capability_refs"] = ["domain-review"]
    contract.write_text(json.dumps(payload), encoding="utf-8")

    assert main(
        [
            "--project",
            str(tmp_path),
            "capabilities",
            "preflight",
            "--task",
            "task-1",
            "--json",
        ]
    ) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "blocked"
    assert report["policy_binding"]["state"] == "legacy-unbound"
    assert report["policy_binding"]["rebind_required"] is True
    assert not (tmp_path / ".stagemesh" / "stagemesh.sqlite3").exists()


def test_preflight_reports_skill_evidence_mode_drift_without_writes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    contract = _capability_contract_fixture(tmp_path)
    payload = json.loads(contract.read_text(encoding="utf-8"))
    payload["capability_refs"] = ["domain-review"]
    contract.write_text(json.dumps(payload), encoding="utf-8")
    _freeze_preflight_policy(tmp_path, "task-1")
    registry = tmp_path / ".stagemesh" / "capabilities.json"
    registry_payload = json.loads(registry.read_text(encoding="utf-8"))
    registry_payload["skill_evidence_mode"] = OBSERVED_LOAD_REQUIRED
    registry.write_text(json.dumps(registry_payload), encoding="utf-8")
    database = tmp_path / ".stagemesh" / "stagemesh.sqlite3"
    database_before = database.read_bytes()

    assert main(
        [
            "--project",
            str(tmp_path),
            "capabilities",
            "preflight",
            "--task",
            "task-1",
            "--json",
        ]
    ) == 2
    report = json.loads(capsys.readouterr().out)
    binding = report["policy_binding"]
    assert binding["state"] == "drifted"
    assert binding["frozen_skill_evidence_mode"] == ATTACHMENT_REQUIRED
    assert binding["current_skill_evidence_mode"] == OBSERVED_LOAD_REQUIRED
    assert binding["reason"] == "skill evidence mode changed"
    assert database.read_bytes() == database_before


def test_capability_preflight_fails_closed_when_observed_events_are_unsupported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    contract = _capability_contract_fixture(tmp_path)
    payload = json.loads(contract.read_text(encoding="utf-8"))
    payload["capability_refs"] = ["domain-review"]
    contract.write_text(json.dumps(payload), encoding="utf-8")
    registry_path = tmp_path / ".stagemesh" / "capabilities.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["skill_evidence_mode"] = OBSERVED_LOAD_REQUIRED
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    assert main(
        [
            "--project",
            str(tmp_path),
            "capabilities",
            "preflight",
            "--task",
            "task-1",
            "--json",
        ]
    ) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "blocked"
    assert report["capabilities"][0]["evidence_requirements"] == ["observed-load"]
    reasons = [
        route["reason"] for route in report["capabilities"][0]["ineligible_routes"]
    ]
    assert all("observed skill loading is unsupported" in reason for reason in reasons)
    assert report["provider_calls"] == 0 and report["writes"] == []


def test_capability_registration_pins_each_supported_source_kind(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _skill(tmp_path)
    _claude_plugin(tmp_path)
    _mcp_manifest(tmp_path)
    registry = tmp_path / ".stagemesh" / "capabilities.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        json.dumps({"format_version": 1, "capabilities": []}), encoding="utf-8"
    )
    commands = (
        (
            "domain-review",
            "skill",
            "agent-skills/domain-review/SKILL.md",
            "codex",
            "IMPLEMENT",
        ),
        (
            "review-kit",
            "claude-plugin",
            ".claude/plugins/review-kit",
            "claude",
            "REVIEW",
        ),
        (
            "serena",
            "mcp",
            ".stagemesh/mcp/serena.json",
            "codex",
            "IMPLEMENT",
        ),
    )
    for capability_id, kind, source, provider, stage in commands:
        assert main(
            [
                "--project",
                str(tmp_path),
                "capabilities",
                "register",
                capability_id,
                "--kind",
                kind,
                "--source",
                source,
                "--provider",
                provider,
                "--stage",
                stage,
                "--json",
            ]
        ) == 0
        result = json.loads(capsys.readouterr().out)
        assert len(result["entry"]["content_sha256"]) == 64
        assert result["inventory"]["integrity"] == "verified"

    persisted = json.loads(registry.read_text(encoding="utf-8"))
    assert all(len(item["content_sha256"]) == 64 for item in persisted["capabilities"])
    assert all(row["integrity"] == "verified" for row in inventory(tmp_path))
    assert not (tmp_path / ".stagemesh" / "stagemesh.sqlite3").exists()


def test_legacy_capability_pin_plan_apply_and_noop_are_explicit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _skill(tmp_path)
    _registry(tmp_path, [_entry("domain-review")])
    registry = tmp_path / ".stagemesh" / "capabilities.json"
    before = registry.read_bytes()
    legacy = inventory(tmp_path)[0]
    assert legacy["integrity"] == "unpinned"
    assert legacy["available"] is True and legacy["usable"] is True

    assert main(
        [
            "--project",
            str(tmp_path),
            "capabilities",
            "pin",
            "domain-review",
            "--plan",
            "--json",
        ]
    ) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["changed"] is True and plan["writes"] == []
    assert plan["previous_content_sha256"] is None
    assert plan["inventory"]["integrity"] == "verified"
    assert registry.read_bytes() == before

    assert main(
        [
            "--project",
            str(tmp_path),
            "capabilities",
            "pin",
            "domain-review",
            "--json",
        ]
    ) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["written"] is True and applied["inventory"]["integrity"] == "verified"
    modified = registry.stat().st_mtime_ns
    pinned = registry.read_bytes()

    assert main(
        [
            "--project",
            str(tmp_path),
            "capabilities",
            "pin",
            "domain-review",
            "--json",
        ]
    ) == 0
    noop = json.loads(capsys.readouterr().out)
    assert noop["changed"] is False and noop["written"] is False
    assert registry.read_bytes() == pinned and registry.stat().st_mtime_ns == modified
    assert not (tmp_path / ".stagemesh" / "stagemesh.sqlite3").exists()


def test_pinned_capability_drift_is_unavailable_and_refused_before_attachment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    skill = _skill(tmp_path)
    assert main(
        [
            "--project",
            str(tmp_path),
            "capabilities",
            "register",
            "domain-review",
            "--kind",
            "skill",
            "--source",
            "agent-skills/domain-review/SKILL.md",
            "--provider",
            "codex",
            "--stage",
            "IMPLEMENT",
            "--enable",
            "--json",
        ]
    ) == 0
    capsys.readouterr()
    skill.write_text(skill.read_text(encoding="utf-8") + "\nChanged guidance.\n", encoding="utf-8")

    row = inventory(tmp_path)[0]
    assert row["integrity"] == "drifted"
    assert row["available"] is False and row["usable"] is False
    assert row["content_sha256"] != row["observed_content_sha256"]
    with pytest.raises(CapabilityRegistryError, match="source digest drifted"):
        resolve_attachments(
            tmp_path, ("domain-review",), provider="codex", stage="IMPLEMENT"
        )


def test_pinned_plugin_and_mcp_drift_fail_closed(tmp_path: Path) -> None:
    plugin = _claude_plugin(tmp_path)
    manifest = _mcp_manifest(tmp_path)
    _registry(
        tmp_path,
        [
            _entry(
                "review-kit",
                kind="claude-plugin",
                source=".claude/plugins/review-kit",
                providers=["claude"],
                stages=["REVIEW"],
            ),
            _entry(
                "serena",
                kind="mcp",
                source=".stagemesh/mcp/serena.json",
                providers=["codex"],
                stages=["IMPLEMENT"],
                permissions=["workspace-read", "subprocess"],
            ),
        ],
    )
    for capability_id in ("review-kit", "serena"):
        assert main(
            ["--project", str(tmp_path), "capabilities", "pin", capability_id]
        ) == 0

    plugin_skill = plugin / "skills" / "review" / "SKILL.md"
    plugin_skill.write_text(
        plugin_skill.read_text(encoding="utf-8") + "\nChanged.\n", encoding="utf-8"
    )
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + "\n", encoding="utf-8"
    )
    rows = {row["id"]: row for row in inventory(tmp_path)}
    assert rows["review-kit"]["integrity"] == "drifted"
    assert rows["serena"]["integrity"] == "drifted"
    with pytest.raises(CapabilityRegistryError, match="source digest drifted"):
        resolve_attachments(
            tmp_path, ("review-kit",), provider="claude", stage="REVIEW"
        )
    with pytest.raises(CapabilityRegistryError, match="source digest drifted"):
        resolve_attachments(
            tmp_path, ("serena",), provider="codex", stage="IMPLEMENT"
        )


def test_malformed_capability_digest_fails_closed(tmp_path: Path) -> None:
    _skill(tmp_path)
    entry = _entry("domain-review")
    entry["content_sha256"] = "NOT-A-DIGEST"
    _registry(tmp_path, [entry])

    with pytest.raises(CapabilityRegistryError, match="lowercase SHA-256"):
        inventory(tmp_path)


def test_historical_capability_evidence_does_not_satisfy_changed_content(
    tmp_path: Path,
) -> None:
    _skill(tmp_path)
    _registry(tmp_path, [_entry("domain-review")])
    assert main(
        [
            "--project",
            str(tmp_path),
            "capabilities",
            "pin",
            "domain-review",
        ]
    ) == 0
    current = str(inventory(tmp_path)[0]["observed_content_sha256"])
    old = "b" * 64 if current != "b" * 64 else "c" * 64
    store = Store(tmp_path / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    task_id = store.upsert_task("Use guidance", source_id="task-1")
    store.add_audit_event(
        "capability.loaded",
        {
            "task_id": task_id,
            "provider": "codex",
            "stage": "IMPLEMENT",
            "capability_id": "domain-review",
            "content_sha256": old,
        },
    )
    stale = task_capability_summary(
        tmp_path, store.conn, task_id, requested=("domain-review",)
    )["capabilities"][0]
    assert stale["integrity"] == "verified"
    assert stale["invocation"] == "evidence-digest-mismatch"
    assert stale["evidence_matches_current"] is False

    store.add_audit_event(
        "capability.loaded",
        {
            "task_id": task_id,
            "provider": "codex",
            "stage": "IMPLEMENT",
            "capability_id": "domain-review",
            "content_sha256": current,
        },
    )
    fresh = task_capability_summary(
        tmp_path, store.conn, task_id, requested=("domain-review",)
    )["capabilities"][0]
    assert fresh["invocation"] == "loaded"
    assert fresh["evidence_matches_current"] is True
    store.close()


def test_capability_vendor_plan_is_read_only_and_reports_distinct_lifecycle_states(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "reviewed" / "SKILL.md"
    source.parent.mkdir()
    source.write_text("# Reviewed guidance\n", encoding="utf-8")

    code = main(
        [
            "--project",
            str(project),
            "capabilities",
            "vendor",
            "reviewed-guidance",
            "--kind",
            "skill",
            "--source",
            str(source),
            "--provider",
            "codex",
            "--stage",
            "IMPLEMENT",
            "--plan",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["operation"] == "vendor-local-capability"
    assert payload["lifecycle"] == {
        "vendored": False,
        "registered": False,
        "enabled": False,
        "runtime_installed": True,
        "available": False,
        "attached": False,
        "loaded": False,
        "invoked": False,
    }
    assert payload["post_apply"]["registered"] is True
    assert payload["post_apply"]["enabled"] is False
    assert payload["source"]["tree_sha256"] == payload["receipt"]["source_tree_sha256"]
    assert payload["writes"] == []
    assert payload["network_calls"] == payload["package_installs"] == 0
    assert payload["provider_calls"] == payload["subprocess_calls"] == 0
    assert payload["user_home_writes"] == 0
    assert not (project / ".stagemesh").exists()


def test_capability_vendor_apply_copies_registers_and_pins_disabled_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "reviewed" / "SKILL.md"
    source.parent.mkdir()
    source.write_bytes(b"# Reviewed guidance\n")

    code = main(
        [
            "--project",
            str(project),
            "capabilities",
            "vendor",
            "reviewed-guidance",
            "--kind",
            "skill",
            "--source",
            str(source),
            "--provider",
            "codex",
            "--stage",
            "IMPLEMENT",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    target = project / ".stagemesh" / "vendor" / "capabilities" / "reviewed-guidance"
    registry = json.loads((project / ".stagemesh" / "capabilities.json").read_text())
    receipt = json.loads((target / ".stagemesh-vendor.json").read_text())
    assert code == 0 and (target / "SKILL.md").read_bytes() == source.read_bytes()
    assert payload["lifecycle"]["vendored"] is True
    assert payload["lifecycle"]["registered"] is True
    assert payload["lifecycle"]["enabled"] is False
    assert payload["lifecycle"]["available"] is True
    assert payload["lifecycle"]["attached"] is False
    assert payload["lifecycle"]["loaded"] is False
    assert payload["lifecycle"]["invoked"] is False
    assert registry["capabilities"][0]["source"].endswith("reviewed-guidance/SKILL.md")
    assert registry["capabilities"][0]["enabled"] is False
    assert registry["capabilities"][0]["content_sha256"] == hashlib.sha256(
        source.read_bytes()
    ).hexdigest()
    assert receipt["provenance"] == "operator-supplied-local-source"
    assert "reviewed" not in json.dumps(receipt).replace("reviewed-guidance", "")


def test_capability_vendor_rejects_unsupported_plugin_files_and_existing_targets(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    plugin = tmp_path / "plugin"
    skill = plugin / "skills" / "review" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# Review\n", encoding="utf-8")
    (plugin / "README.md").write_text("not imported", encoding="utf-8")

    with pytest.raises(CapabilityRegistryError, match="unsupported file"):
        vendor_capability(
            project,
            capability_id="review-plugin",
            kind="claude-plugin",
            source=plugin,
            providers=["claude"],
            stages=["REVIEW"],
        )
    assert not (project / ".stagemesh").exists()

    (plugin / "README.md").unlink()
    vendor_root = project / ".stagemesh" / "vendor" / "capabilities"
    vendor_root.mkdir(parents=True)
    lock = vendor_root / ".review-plugin.vendor.lock"
    lock.write_text("other operation", encoding="utf-8")
    with pytest.raises(CapabilityRegistryError, match="cannot vendor capability"):
        vendor_capability(
            project,
            capability_id="review-plugin",
            kind="claude-plugin",
            source=plugin,
            providers=["claude"],
            stages=["REVIEW"],
        )
    assert lock.read_text(encoding="utf-8") == "other operation"
    lock.unlink()

    target = vendor_root / "review-plugin"
    target.mkdir(parents=True)
    marker = target / "marker.txt"
    marker.write_text("preserve", encoding="utf-8")
    with pytest.raises(CapabilityRegistryError, match="already exists"):
        vendor_capability(
            project,
            capability_id="review-plugin",
            kind="claude-plugin",
            source=plugin,
            providers=["claude"],
            stages=["REVIEW"],
        )
    assert marker.read_text(encoding="utf-8") == "preserve"
    assert not (project / ".stagemesh" / "capabilities.json").exists()


def test_capability_vendor_accepts_bounded_skills_only_claude_plugin(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    plugin = tmp_path / "plugin"
    manifest = plugin / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": "review-plugin"}), encoding="utf-8")
    skill = plugin / "skills" / "review" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# Review\n", encoding="utf-8")

    payload = vendor_capability(
        project,
        capability_id="review-plugin",
        kind="claude-plugin",
        source=plugin,
        providers=["claude"],
        stages=["REVIEW"],
    )

    target = project / ".stagemesh" / "vendor" / "capabilities" / "review-plugin"
    assert (target / ".claude-plugin" / "plugin.json").is_file()
    assert (target / "skills" / "review" / "SKILL.md").is_file()
    assert (target / ".stagemesh-vendor.json").is_file()
    assert payload["inventory"]["configuration_valid"] is True
    assert payload["lifecycle"]["available"] is True
    assert payload["lifecycle"]["enabled"] is False


def test_capability_vendor_refuses_source_and_destination_symlinks(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "SKILL.md"
    source.write_text("# Reviewed\n", encoding="utf-8")
    source_link = tmp_path / "linked" / "SKILL.md"
    source_link.parent.mkdir()
    try:
        source_link.symlink_to(source)
    except OSError:
        pytest.skip("symlinks are not available")

    with pytest.raises(CapabilityRegistryError, match="must not be a symlink"):
        vendor_capability(
            project,
            capability_id="reviewed",
            kind="skill",
            source=source_link,
            providers=["codex"],
            stages=["IMPLEMENT"],
        )

    outside = tmp_path / "outside"
    outside.mkdir()
    metadata = project / ".stagemesh"
    metadata.mkdir()
    (metadata / "vendor").symlink_to(outside, target_is_directory=True)
    with pytest.raises(CapabilityRegistryError, match="inside the project|symlink directory"):
        vendor_capability(
            project,
            capability_id="reviewed",
            kind="skill",
            source=source,
            providers=["codex"],
            stages=["IMPLEMENT"],
        )
    assert list(outside.iterdir()) == []


def test_capability_vendor_copies_and_pins_bounded_skill_package(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    package = tmp_path / "systematic-debugging"
    package.mkdir()
    (package / "SKILL.md").write_text(
        "# Debugging\n\nRead [root cause](root-cause.md).\n", encoding="utf-8"
    )
    (package / "root-cause.md").write_text("# Root cause tracing\n", encoding="utf-8")
    examples = package / "examples"
    examples.mkdir()
    (examples / "find-polluter.sh").write_text("#!/bin/sh\necho example\n", encoding="utf-8")

    payload = vendor_capability(
        project,
        capability_id="systematic-debugging",
        kind="skill",
        source=package,
        providers=["codex", "claude", "grok"],
        stages=["IMPLEMENT"],
    )

    target = project / ".stagemesh" / "vendor" / "capabilities" / "systematic-debugging"
    row = inventory(project)[0]
    frozen = json.loads(
        capability_policy_snapshot(project, ["systematic-debugging"])["canonical_json"]
    )["capabilities"][0]
    assert (target / "root-cause.md").is_file()
    assert (target / "examples" / "find-polluter.sh").is_file()
    assert payload["source"]["file_count"] == 3
    assert row["integrity"] == "verified"
    assert row["source_tree_sha256"] == row["observed_source_tree_sha256"]
    assert frozen["source_tree_sha256"] == row["source_tree_sha256"]


def test_vendored_skill_package_drift_fails_closed_before_attachment(tmp_path: Path) -> None:
    package = tmp_path / "source"
    package.mkdir()
    (package / "SKILL.md").write_text("# Skill\n\nRead reference.md.\n", encoding="utf-8")
    (package / "reference.md").write_text("original\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    vendor_capability(
        project,
        capability_id="package-skill",
        kind="skill",
        source=package,
        providers=["codex"],
        stages=["IMPLEMENT"],
    )
    set_capability_enabled(project, "package-skill", True)
    (attachment,) = resolve_attachments(
        project, ["package-skill"], provider="codex", stage="IMPLEMENT"
    )
    assert attachment.audit_payload()["source_tree_sha256"] == inventory(project)[0][
        "source_tree_sha256"
    ]

    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    task_id = store.upsert_task("Use package", source_id="package-task")
    store.add_audit_event(
        "capability.loaded",
        {
            "task_id": task_id,
            "provider": "codex",
            "stage": "IMPLEMENT",
            "capability_id": "package-skill",
            "content_sha256": attachment.content_sha256,
            "source_tree_sha256": attachment.source_tree_sha256,
        },
    )
    current = task_capability_summary(
        project, store.conn, task_id, requested=("package-skill",)
    )["capabilities"][0]
    assert current["evidence_matches_current"] is True

    copied = project / ".stagemesh" / "vendor" / "capabilities" / "package-skill"
    (copied / "reference.md").write_text("changed\n", encoding="utf-8")
    row = inventory(project)[0]
    assert row["integrity"] == "drifted"
    assert row["available"] is False and row["usable"] is False
    assert "do not match the receipt" in row["source_tree_error"]
    stale = task_capability_summary(
        project, store.conn, task_id, requested=("package-skill",)
    )["capabilities"][0]
    assert stale["invocation"] == "evidence-digest-mismatch"
    assert stale["evidence_matches_current"] is False
    with pytest.raises(CapabilityRegistryError, match="source tree drifted"):
        resolve_attachments(
            project, ["package-skill"], provider="codex", stage="IMPLEMENT"
        )
    store.close()


def test_vendored_skill_package_refuses_binary_support_files(tmp_path: Path) -> None:
    package = tmp_path / "source"
    package.mkdir()
    (package / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    (package / "payload.bin").write_bytes(b"text\x00payload")
    project = tmp_path / "project"
    project.mkdir()

    with pytest.raises(CapabilityRegistryError, match="NUL bytes"):
        vendor_capability(
            project,
            capability_id="package-skill",
            kind="skill",
            source=package,
            providers=["codex"],
            stages=["IMPLEMENT"],
        )
    assert not (project / ".stagemesh").exists()


def test_capability_vendor_mcp_reports_runtime_separately_without_starting_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    manifest = tmp_path / "reviewed.mcp.json"
    manifest.write_text(
        json.dumps(
            {
                "format_version": 1,
                "transport": "stdio",
                "server_name": "reviewed",
                "command": sys.executable,
                "args": ["-m", "reviewed_server"],
                "cwd": ".",
                "allowed_tools": ["inspect"],
            }
        ),
        encoding="utf-8",
    )

    code = main(
        [
            "--project",
            str(project),
            "capabilities",
            "vendor",
            "reviewed-mcp",
            "--kind",
            "mcp",
            "--source",
            str(manifest),
            "--provider",
            "codex",
            "--stage",
            "IMPLEMENT",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["lifecycle"]["runtime_installed"] is True
    assert payload["lifecycle"]["available"] is True
    assert payload["lifecycle"]["enabled"] is False
    assert payload["subprocess_calls"] == 0 and payload["provider_calls"] == 0
    assert payload["lifecycle"]["invoked"] is False


def test_capability_vendor_rolls_back_copy_when_registration_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "SKILL.md"
    source.write_text("# Reviewed\n", encoding="utf-8")

    def fail_registration(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise CapabilityRegistryError("simulated registry refusal")

    monkeypatch.setattr(capability_vendor_module, "register_capability", fail_registration)
    with pytest.raises(CapabilityRegistryError, match="simulated registry refusal"):
        vendor_capability(
            project,
            capability_id="reviewed",
            kind="skill",
            source=source,
            providers=["codex"],
            stages=["IMPLEMENT"],
        )
    assert not (project / ".stagemesh" / "vendor" / "capabilities" / "reviewed").exists()
    assert not (project / ".stagemesh" / "capabilities.json").exists()
