from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import stagemesh.providers as providers_module
from stagemesh.agent_skills import skill_prompt
from stagemesh.capabilities import (
    ATTACHMENT_REQUIRED,
    OBSERVED_LOAD_REQUIRED,
    CapabilityRegistryError,
    inventory,
    resolve_attachments,
    skill_evidence_mode,
)
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
                "content_sha256": "a" * 64,
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
                "content_sha256": "b" * 64,
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
