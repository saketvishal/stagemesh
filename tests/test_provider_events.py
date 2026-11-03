from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from stagemesh.capabilities import capability_policy_snapshot
from stagemesh.capacity import CapacityKind
from stagemesh.domain import ExecutionStatus
from stagemesh.execution import ExecutionResult
from stagemesh.persistence import Store
from stagemesh.provider_events import (
    CAPABILITY_EVIDENCE_MISSING,
    CAPABILITY_POLICY_VIOLATION,
    ProviderEventError,
    event_stream_command,
    loaded_receipts,
    mcp_invocation_receipts,
    observe_mcp_invocations,
    observe_skill_loads,
    provider_event_response,
    validate_observable_attachments,
)
from stagemesh.provider_pool import IMPLEMENT, REVIEW, PooledExecutor, ProviderPool
from stagemesh.providers import RuntimeCommandAdapter


def _freeze_capability_policy(
    store: Store, project: Path, task_id: str, capability_ids: tuple[str, ...]
) -> None:
    snapshot = capability_policy_snapshot(project, capability_ids)
    with store.conn:
        store.conn.execute(
            "INSERT INTO task_capability_policies(task_id, digest, canonical_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (task_id, snapshot["digest"], snapshot["canonical_json"], 1.0),
        )


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (("codex", "exec"), ("--json", None)),
        (("claude", "-p"), ("--output-format", "stream-json")),
        (("grok",), ("--output-format", "streaming-json")),
        (("agy", "--print"), ("--output-format", "stream-json")),
    ],
)
def test_event_stream_command_is_provider_specific(
    command: tuple[str, ...], expected: tuple[str, str | None]
) -> None:
    configured = event_stream_command(command)

    assert expected[0] in configured
    if expected[1] is not None:
        assert configured[configured.index(expected[0]) + 1] == expected[1]


@pytest.mark.parametrize(
    "event",
    [
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": '{"decision":"PASS"}'},
        },
        {"type": "result", "result": '{"decision":"PASS"}'},
        {"type": "result", "structured_output": {"decision": "PASS"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": '{"decision":"PASS"}'}
                ]
            },
        },
    ],
)
def test_provider_event_response_extracts_supported_final_answers(
    event: dict[str, object],
) -> None:
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "private"}),
            json.dumps(event),
        ]
    )

    assert provider_event_response(stdout) == '{"decision":"PASS"}'


def test_provider_event_response_fails_closed_on_unknown_or_malformed_events() -> None:
    assert provider_event_response('{"type":"turn.completed"}\nnot-json') is None


@pytest.mark.parametrize(
    "event",
    [
        {
            "type": "item.completed",
            "item": {"type": "command_execution", "command": "Get-Content 'C:/test-fixture/SKILL.md'"},
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "C:/test-fixture/SKILL.md"}}
                ]
            },
        },
        {"type": "tool_call", "tool_name": "read_file", "arguments": {"path": "C:/test-fixture/SKILL.md"}},
        {"type": "tool_use", "name": "Read", "input": {"path": "C:\\test-fixture\\SKILL.md"}},
        {
            "type": "tool_call",
            "tool_name": "functions.read_file",
            "arguments": json.dumps({"path": "C:/test-fixture/SKILL.md"}),
        },
    ],
)
def test_provider_events_normalize_exact_skill_reads(event: dict[str, object]) -> None:
    attachments = [
        {
            "capability_id": "review-guidance",
            "content_sha256": "a" * 64,
            "_path": "C:/test-fixture/SKILL.md",
        }
    ]

    observation = observe_skill_loads(json.dumps(event), attachments)

    assert observation.loaded == ("review-guidance",)
    assert observation.missing == ()
    assert loaded_receipts(observation, attachments) == [
        {
            "capability_id": "review-guidance",
            "kind": "skill",
            "content_sha256": "a" * 64,
            "observed_action": "file_read",
            "observed_before_first_recorded_mutation": True,
            "invocation": "observed-load",
        }
    ]


def test_read_after_recorded_mutation_is_not_accepted() -> None:
    events = "\n".join(
        [
            json.dumps({"name": "Edit", "input": {"file_path": "C:/test-fixture/src/app.py"}}),
            json.dumps({"name": "Read", "input": {"file_path": "C:/test-fixture/SKILL.md"}}),
        ]
    )
    attachments = [{"capability_id": "review-guidance", "_path": "C:/test-fixture/SKILL.md"}]

    observation = observe_skill_loads(events, attachments)

    assert observation.loaded == ()
    assert observation.missing == ("review-guidance",)


def test_similar_path_and_duplicate_id_cannot_satisfy_evidence() -> None:
    attachments = [{"capability_id": "skill", "_path": "C:/test-fixture/SKILL.md"}]
    event = json.dumps({"name": "Read", "input": {"file_path": "C:/test-fixture/SKILL.md.bak"}})

    assert observe_skill_loads(event, attachments).missing == ("skill",)
    with pytest.raises(ProviderEventError, match="duplicate selected capability id"):
        validate_observable_attachments(
            [
                *attachments,
                {"capability_id": "skill", "_path": "C:/other-fixture/SKILL.md"},
            ]
        )


def test_claude_plugin_receipt_requires_one_exact_component_read_and_stays_redacted() -> None:
    attachments = [
        {
            "capability_id": "review-kit",
            "kind": "claude-plugin",
            "content_sha256": "a" * 64,
            "_path": "C:/test-fixture/.claude/plugins/review-kit",
            "_plugin": {
                "activation": "skills-only-plugin-dir",
                "skills": [
                    {
                        "name": "review",
                        "path": "C:/test-fixture/.claude/plugins/review-kit/skills/review/SKILL.md",
                    },
                    {
                        "name": "verify",
                        "path": "C:/test-fixture/.claude/plugins/review-kit/skills/verify/SKILL.md",
                    },
                ],
            },
        }
    ]
    event = json.dumps(
        {
            "type": "tool_use",
            "name": "Read",
            "input": {
                "file_path": "C:/test-fixture/.claude/plugins/review-kit/skills/review/SKILL.md"
            },
        }
    )

    validate_observable_attachments(attachments)
    observation = observe_skill_loads(event, attachments)
    assert observation.loaded == ("review-kit",)
    assert observation.missing == ()
    assert dict(observation.components) == {"review-kit": ("review",)}
    receipt = loaded_receipts(observation, attachments)[0]
    assert receipt["kind"] == "claude-plugin"
    assert receipt["components"] == ["review"]
    assert "test-fixture" not in json.dumps(receipt)


def test_parser_does_not_return_raw_event_arguments() -> None:
    event = json.dumps(
        {
            "name": "Read",
            "input": {"file_path": "C:/test-fixture/SKILL.md", "opaque_field": "RAW_EVENT_MARKER"},
        }
    )
    attachments = [
        {
            "capability_id": "domain-skill",
            "content_sha256": "b" * 64,
            "_path": "C:/test-fixture/SKILL.md",
        }
    ]

    receipt = loaded_receipts(observe_skill_loads(event, attachments), attachments)[0]

    assert "test-fixture" not in json.dumps(receipt)
    assert "RAW_EVENT_MARKER" not in json.dumps(receipt)


def test_observation_receipts_preserve_bounded_vendored_tree_identity() -> None:
    skill_attachments = [
        {
            "capability_id": "package-skill",
            "content_sha256": "a" * 64,
            "source_tree_sha256": "b" * 64,
            "_path": "C:/test-fixture/SKILL.md",
        }
    ]
    skill_event = json.dumps(
        {"name": "Read", "input": {"file_path": "C:/test-fixture/SKILL.md"}}
    )
    loaded = loaded_receipts(
        observe_skill_loads(skill_event, skill_attachments), skill_attachments
    )[0]
    assert loaded["source_tree_sha256"] == "b" * 64

    mcp_attachments = [
        {
            "capability_id": "serena",
            "kind": "mcp",
            "content_sha256": "c" * 64,
            "source_tree_sha256": "d" * 64,
            "_mcp": {"server_name": "serena", "allowed_tools": ["find_symbol"]},
        }
    ]
    mcp_event = json.dumps(
        {"type": "mcp_tool_call", "server": "serena", "tool": "find_symbol"}
    )
    invoked = mcp_invocation_receipts(
        observe_mcp_invocations(mcp_event, mcp_attachments), mcp_attachments
    )[0]
    assert invoked["source_tree_sha256"] == "d" * 64


@pytest.mark.parametrize(
    "event",
    [
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "serena",
                "tool": "find_symbol",
                "arguments": {"name_path": "RAW_ARGUMENT_MARKER", "opaque": "RAW_FIELD_MARKER"},
                "result": "RAW_RESULT_MARKER",
            },
        },
        {
            "type": "tool_use",
            "name": "mcp__serena__find_symbol",
            "input": {"name_path": "RAW_ARGUMENT_MARKER"},
        },
    ],
)
def test_mcp_invocation_receipt_is_allowlisted_and_redacted(event: dict[str, object]) -> None:
    attachments = [
        {
            "capability_id": "serena",
            "kind": "mcp",
            "content_sha256": "c" * 64,
            "_mcp": {
                "server_name": "serena",
                "allowed_tools": ["find_symbol", "search_for_pattern"],
            },
        }
    ]

    observation = observe_mcp_invocations(json.dumps(event), attachments)
    receipts = mcp_invocation_receipts(observation, attachments)

    assert observation.invoked == ("serena",)
    assert observation.missing == ()
    assert observation.violations == ()
    assert receipts == [
        {
            "capability_id": "serena",
            "kind": "mcp",
            "content_sha256": "c" * 64,
            "observed_action": "mcp_tool_call",
            "tools": ["find_symbol"],
            "invocation": "observed",
        }
    ]
    assert "RAW_ARGUMENT_MARKER" not in json.dumps(receipts)
    assert "RAW_RESULT_MARKER" not in json.dumps(receipts)


def test_mcp_unallowlisted_tool_is_a_policy_violation() -> None:
    attachments = [
        {
            "capability_id": "serena",
            "kind": "mcp",
            "_mcp": {"server_name": "serena", "allowed_tools": ["find_symbol"]},
        }
    ]
    event = json.dumps({"type": "mcp_tool_call", "server": "serena", "tool": "replace_symbol"})

    observation = observe_mcp_invocations(event, attachments)

    assert observation.invoked == ()
    assert observation.missing == ("serena",)
    assert observation.violations == ("serena:replace_symbol",)


def test_pool_falls_back_without_cooling_when_skill_evidence_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    resets: list[str] = []

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
                    failure_reason=f"{CAPABILITY_EVIDENCE_MISSING}: verification-before-completion",
                )
            return ExecutionResult(ExecutionStatus.SUCCEEDED, candidate_sha="candidate")

    monkeypatch.setattr(
        "stagemesh.provider_pool._reset_worktree",
        lambda *_args, **_kwargs: resets.append("reset"),
    )
    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    store.upsert_task("Use verified skills", source_id="task-1")
    pool = ProviderPool(
        [Adapter("codex", (sys.executable,)), Adapter("claude", (sys.executable,))],
        {IMPLEMENT: ("codex", "claude"), REVIEW: ("claude",)},
        require_independent=False,
    )
    try:
        result = PooledExecutor(pool).run(store, "task-1", None, tmp_path)

        assert result.status == "SUCCEEDED"
        assert calls == ["codex", "claude"]
        assert resets == ["reset"]
        assert not store.conn.execute(
            "SELECT 1 FROM audit_events WHERE event_type='provider.failure'"
        ).fetchall()
    finally:
        store.close()


def test_all_missing_evidence_is_a_policy_failure_not_provider_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
                failure_reason=f"{CAPABILITY_EVIDENCE_MISSING}: required-skill",
            )

    monkeypatch.setattr("stagemesh.provider_pool._reset_worktree", lambda *_args, **_kwargs: None)
    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    store.upsert_task("Use verified skills", source_id="task-1")
    pool = ProviderPool(
        [Adapter("codex", (sys.executable,)), Adapter("grok", (sys.executable,))],
        {IMPLEMENT: ("codex", "grok"), REVIEW: ("codex",)},
        require_independent=False,
    )
    try:
        result = PooledExecutor(pool).run(store, "task-1", None, tmp_path)

        assert result.status == "FAILED"
        assert result.capacity_failure is False
        assert result.failure_reason.startswith("no_provider_with_required_skill_evidence:")
        assert [item["classification"] for item in result.provider_attempts] == [
            "capability_evidence",
            "capability_evidence",
        ]
    finally:
        store.close()


def test_pool_falls_back_without_cooling_after_capability_policy_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
                        f"{CAPABILITY_POLICY_VIOLATION}: unallowlisted MCP tool invocation"
                    ),
                )
            return ExecutionResult(ExecutionStatus.SUCCEEDED, candidate_sha="candidate")

    monkeypatch.setattr("stagemesh.provider_pool._reset_worktree", lambda *_args, **_kwargs: None)
    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    store.upsert_task("Use bounded MCP", source_id="task-1")
    pool = ProviderPool(
        [Adapter("codex", (sys.executable,)), Adapter("claude", (sys.executable,))],
        {IMPLEMENT: ("codex", "claude"), REVIEW: ("claude",)},
        require_independent=False,
    )
    try:
        result = PooledExecutor(pool).run(store, "task-1", None, tmp_path)

        assert result.status == "SUCCEEDED"
        assert calls == ["codex", "claude"]
        assert not store.conn.execute(
            "SELECT 1 FROM audit_events WHERE event_type='provider.failure'"
        ).fetchall()
    finally:
        store.close()


def test_runtime_records_redacted_observed_load_receipt_before_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stagemesh import agent_skills, providers
    from stagemesh.contracts import ChangeContract

    registry = tmp_path / ".stagemesh" / "capabilities.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        json.dumps(
            {
                "format_version": 1,
                "skill_evidence_mode": "observed-load-required",
                "capabilities": [],
            }
        ),
        encoding="utf-8",
    )
    selected = (
        agent_skills.SKILL_ROOT
        / "superpowers"
        / "verification-before-completion"
        / "SKILL.md"
    ).resolve()
    captured: list[list[str]] = []

    def popen(command: list[str], **_kwargs: object) -> SimpleNamespace:
        captured.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(providers, "record_task_baseline", lambda *_: "baseline")
    monkeypatch.setattr(
        providers,
        "bind_task_contract",
        lambda *_: SimpleNamespace(
            contract=ChangeContract(objective="Update docs", allowed_files=("docs/guide.md",))
        ),
    )
    monkeypatch.setattr(providers, "remediation_context", lambda *_: None)
    monkeypatch.setattr(providers, "_provider_budget_refusal", lambda *_: None)
    monkeypatch.setattr(providers, "_authorization_refusal", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(providers, "provider_environment", lambda *_: {})
    monkeypatch.setattr(providers.subprocess, "Popen", popen)
    monkeypatch.setattr(
        providers,
        "popen_identity",
        lambda *_: SimpleNamespace(pid=123, create_time=1.0, boot_id="boot", executable="codex"),
    )
    monkeypatch.setattr(
        providers,
        "communicate_bounded",
        lambda *_: (
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": f"Get-Content '{selected}'",
                    },
                }
            ),
            "",
            False,
        ),
    )
    monkeypatch.setattr(providers, "commit_implementation_candidate", lambda *_args, **_kwargs: "candidate")

    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    store.upsert_task("Update docs", source_id="task-1")
    lease = SimpleNamespace(
        path=tmp_path,
        check=lambda _phase: None,
        bind_execution=lambda _execution_id: None,
        after_agent=lambda: None,
        seal=lambda _sha: None,
    )
    try:
        result = RuntimeCommandAdapter("codex", ("codex", "exec"))._execute_owned(
            store, "task-1", None, tmp_path, lease
        )

        assert result.status == "SUCCEEDED"
        assert "--json" in captured[0]
        row = store.conn.execute(
            "SELECT payload FROM audit_events WHERE event_type='capability.loaded'"
        ).fetchone()
        payload = json.loads(row["payload"])
        assert payload["capability_id"] == "verification-before-completion"
        assert payload["invocation"] == "observed-load"
        assert str(selected) not in row["payload"]
    finally:
        store.close()


def test_runtime_activates_claude_plugin_skill_and_requires_load_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stagemesh import agent_skills, providers
    from stagemesh.contracts import ChangeContract

    plugin = tmp_path / ".claude" / "plugins" / "review-kit"
    manifest = plugin / ".claude-plugin" / "plugin.json"
    skill = plugin / "skills" / "review" / "SKILL.md"
    manifest.parent.mkdir(parents=True)
    skill.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": "review-kit"}), encoding="utf-8")
    skill.write_text(
        "---\nname: review\ndescription: Review carefully.\n---\n\n# Review\n",
        encoding="utf-8",
    )
    verification_skill = (
        agent_skills.SKILL_ROOT
        / "superpowers"
        / "verification-before-completion"
        / "SKILL.md"
    ).resolve()
    registry = tmp_path / ".stagemesh" / "capabilities.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        json.dumps(
            {
                "format_version": 1,
                "skill_evidence_mode": "observed-load-required",
                "capabilities": [
                    {
                        "id": "review-kit",
                        "kind": "claude-plugin",
                        "source": ".claude/plugins/review-kit",
                        "enabled": True,
                        "providers": ["claude"],
                        "stages": ["IMPLEMENT"],
                        "permissions": ["workspace-read"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    captured: list[list[str]] = []

    def popen(command: list[str], **_kwargs: object) -> SimpleNamespace:
        captured.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(providers, "record_task_baseline", lambda *_: "baseline")
    monkeypatch.setattr(
        providers,
        "bind_task_contract",
        lambda *_: SimpleNamespace(
            contract=ChangeContract(
                objective="Apply review guidance",
                allowed_files=("docs/guide.md",),
                capability_refs=("review-kit",),
            )
        ),
    )
    monkeypatch.setattr(providers, "remediation_context", lambda *_: None)
    monkeypatch.setattr(providers, "_provider_budget_refusal", lambda *_: None)
    monkeypatch.setattr(providers, "_authorization_refusal", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(providers, "provider_environment", lambda *_: {})
    monkeypatch.setattr(providers.subprocess, "Popen", popen)
    monkeypatch.setattr(
        providers,
        "popen_identity",
        lambda *_: SimpleNamespace(pid=123, create_time=1.0, boot_id="boot", executable="claude"),
    )
    monkeypatch.setattr(
        providers,
        "communicate_bounded",
        lambda *_: (
            "\n".join(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "Read",
                                    "input": {"file_path": str(path)},
                                }
                            ]
                        },
                    }
                )
                for path in (verification_skill, skill.resolve())
            ),
            "",
            False,
        ),
    )
    monkeypatch.setattr(
        providers, "commit_implementation_candidate", lambda *_args, **_kwargs: "candidate"
    )

    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    store.upsert_task("Apply review guidance", source_id="task-1")
    _freeze_capability_policy(store, tmp_path, "task-1", ("review-kit",))
    lease = SimpleNamespace(
        path=tmp_path,
        check=lambda _phase: None,
        bind_execution=lambda _execution_id: None,
        after_agent=lambda: None,
        seal=lambda _sha: None,
    )
    try:
        result = RuntimeCommandAdapter("claude", ("claude",))._execute_owned(
            store, "task-1", None, tmp_path, lease
        )

        assert result.status == "SUCCEEDED", result.failure_reason
        assert "--output-format" in captured[0] and "stream-json" in captured[0]
        assert captured[0][captured[0].index("--plugin-dir") + 1] == str(plugin.resolve())
        activation_row = store.conn.execute(
            "SELECT payload FROM audit_events WHERE event_type='capability.activated' "
            "AND payload LIKE '%review-kit%'"
        ).fetchone()
        activation_payload = json.loads(activation_row["payload"])
        assert activation_payload == {
            "activation": "skills-only-plugin-dir",
            "activation_evidence": "process-started",
            "capability_id": "review-kit",
            "content_sha256": activation_payload["content_sha256"],
            "kind": "claude-plugin",
            "provider": "claude",
            "stage": "IMPLEMENT",
            "status": "activated",
            "task_id": "task-1",
        }
        assert len(activation_payload["content_sha256"]) == 64
        assert str(plugin.resolve()) not in activation_row["payload"]
        assert "_path" not in activation_payload and "_plugin" not in activation_payload
        row = store.conn.execute(
            "SELECT payload FROM audit_events WHERE event_type='capability.loaded' "
            "AND payload LIKE '%review-kit%'"
        ).fetchone()
        payload = json.loads(row["payload"])
        assert payload["kind"] == "claude-plugin"
        assert payload["components"] == ["review"]
        assert str(skill.resolve()) not in row["payload"]
    finally:
        store.close()


def test_runtime_configures_codex_mcp_and_requires_invocation_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stagemesh import providers
    from stagemesh.contracts import ChangeContract

    manifest = tmp_path / ".stagemesh" / "mcp" / "serena.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "format_version": 1,
                "transport": "stdio",
                "server_name": "serena",
                "command": sys.executable,
                "args": ["start-mcp-server"],
                "cwd": ".",
                "allowed_tools": ["find_symbol"],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / ".stagemesh" / "capabilities.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "capabilities": [
                    {
                        "id": "serena",
                        "kind": "mcp",
                        "source": ".stagemesh/mcp/serena.json",
                        "enabled": True,
                        "providers": ["codex", "claude"],
                        "stages": ["IMPLEMENT"],
                        "permissions": ["workspace-read", "subprocess"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    captured: list[list[str]] = []

    def popen(command: list[str], **_kwargs: object) -> SimpleNamespace:
        captured.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(providers, "record_task_baseline", lambda *_: "baseline")
    monkeypatch.setattr(
        providers,
        "bind_task_contract",
        lambda *_: SimpleNamespace(
            contract=ChangeContract(
                objective="Use Serena",
                allowed_files=("docs/guide.md",),
                capability_refs=("serena",),
            )
        ),
    )
    monkeypatch.setattr(providers, "remediation_context", lambda *_: None)
    monkeypatch.setattr(providers, "_provider_budget_refusal", lambda *_: None)
    monkeypatch.setattr(providers, "_authorization_refusal", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(providers, "provider_environment", lambda *_: {})
    monkeypatch.setattr(providers.subprocess, "Popen", popen)
    monkeypatch.setattr(
        providers,
        "popen_identity",
        lambda *_: SimpleNamespace(pid=123, create_time=1.0, boot_id="boot", executable="codex"),
    )
    monkeypatch.setattr(
        providers,
        "communicate_bounded",
        lambda *_: (
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "mcp_tool_call",
                        "server": "serena",
                        "tool": "find_symbol",
                        "arguments": {"name_path": "RAW_ARGUMENT_MARKER"},
                        "result": "RAW_RESULT_MARKER",
                    },
                }
            ),
            "",
            False,
        ),
    )
    monkeypatch.setattr(
        providers, "commit_implementation_candidate", lambda *_args, **_kwargs: "candidate"
    )
    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    store.upsert_task("Use Serena", source_id="task-1")
    _freeze_capability_policy(store, tmp_path, "task-1", ("serena",))
    lease = SimpleNamespace(
        path=tmp_path,
        check=lambda _phase: None,
        bind_execution=lambda _execution_id: None,
        after_agent=lambda: None,
        seal=lambda _sha: None,
    )
    try:
        result = RuntimeCommandAdapter("codex", ("codex", "exec"))._execute_owned(
            store, "task-1", None, tmp_path, lease
        )

        assert result.status == "SUCCEEDED"
        assert "--ignore-user-config" in captured[0]
        assert "--json" in captured[0]
        assert 'mcp_servers.serena.enabled_tools=["find_symbol"]' in captured[0]
        row = store.conn.execute(
            "SELECT payload FROM audit_events WHERE event_type='capability.invoked'"
        ).fetchone()
        payload = json.loads(row["payload"])
        assert payload["capability_id"] == "serena"
        assert payload["tools"] == ["find_symbol"]
        assert payload["invocation"] == "observed"
        assert "RAW_ARGUMENT_MARKER" not in row["payload"]
        assert "RAW_RESULT_MARKER" not in row["payload"]
    finally:
        store.close()
