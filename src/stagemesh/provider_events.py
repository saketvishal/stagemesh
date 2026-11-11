"""Normalize provider event streams into privacy-bounded skill-load evidence."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ProviderEventError(RuntimeError):
    """A provider cannot supply the event evidence required by project policy."""


CAPABILITY_EVIDENCE_MISSING = "capability_evidence_missing"
CAPABILITY_POLICY_VIOLATION = "capability_policy_violation"
# A headless review is complete only when the provider finished the turn.
# max_turn_requests, max_tokens, refusal, and cancelled are terminal failures.
SUCCESSFUL_REVIEW_STOP_REASONS = frozenset({"end_turn"})


@dataclass(frozen=True)
class SkillLoadObservation:
    loaded: tuple[str, ...]
    missing: tuple[str, ...]
    components: tuple[tuple[str, tuple[str, ...]], ...]
    event_count: int
    malformed_lines: int


@dataclass(frozen=True)
class McpInvocationObservation:
    invoked: tuple[str, ...]
    missing: tuple[str, ...]
    tools: tuple[tuple[str, tuple[str, ...]], ...]
    violations: tuple[str, ...]
    event_count: int
    malformed_lines: int


_READ_TOOLS = frozenset({"read", "read_file", "readfile", "open_file", "openfile"})
_MUTATION_TOOLS = frozenset(
    {
        "edit",
        "edit_file",
        "editfile",
        "write",
        "write_file",
        "writefile",
        "apply_patch",
        "applypatch",
        "delete_file",
        "deletefile",
        "move_file",
        "movefile",
    }
)
_READ_COMMAND = re.compile(
    r"(?:^|[;&|]\s*)(?:get-content|gc|cat|type|more|sed\b|head\b|tail\b|"
    r"python(?:\.exe)?\b[^\r\n]*(?:read_text|read_bytes|open\s*\())",
    re.IGNORECASE,
)
_MUTATION_COMMAND = re.compile(
    r"(?:apply_patch|set-content|add-content|out-file|remove-item|move-item|copy-item|"
    r"\brm\b|\bmv\b|\bcp\b|\bdel\b|\berase\b|\bwrite_text\b|\bwrite_bytes\b|"
    r"(?:^|\s)(?:>>|>)(?:\s|[^&]))",
    re.IGNORECASE,
)


def event_stream_command(command: tuple[str, ...] | list[str]) -> list[str]:
    """Enable a supported provider's JSON event stream without enabling new authority."""
    args = list(command)
    executable = Path(args[0]).stem.lower() if args else ""
    if executable == "codex":
        return _ensure_flag(args, "--json")
    if executable == "claude":
        args = _ensure_flag(args, "-p")
        return _set_option(args, "--output-format", "stream-json")
    if executable == "grok":
        return _set_option(args, "--output-format", "streaming-json")
    if executable == "agy":
        return _set_option(args, "--output-format", "stream-json")
    raise ProviderEventError(
        f"observed skill loading is unsupported for provider executable: {executable or '(missing)'}"
    )


def provider_event_response(stdout: str) -> str | None:
    """Extract the last provider answer from supported JSONL event shapes.

    The raw stream remains available to the evidence observers but is never
    persisted by this projection. Unknown events cannot masquerade as a review
    verdict: callers fail closed when this returns ``None``. A terminal event
    whose stop reason is not a completed turn discards any earlier answer.
    Grok's session ``turn_ended`` event is terminal: only ``outcome``
    ``completed`` keeps an earlier answer. An ``assistant`` message is
    intermediate, so a stream that ends without a successful terminal event
    is incomplete. Per-response reasons such as ``tool_use`` are not terminal.
    ``result`` and Codex ``item.completed`` agent messages remain final
    answers on their own.
    """
    answer: str | None = None
    answer_needs_terminal = False
    terminal_failure = False
    successful_terminal = False
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if _terminal_review_stop_failed(event):
            terminal_failure = True
        elif _successful_terminal_event(event):
            successful_terminal = True
        candidate = _event_answer(event)
        if candidate:
            answer = candidate.strip()
            answer_needs_terminal = _answer_needs_successful_terminal(event)
            if not answer_needs_terminal:
                successful_terminal = True
    if terminal_failure or (answer_needs_terminal and not successful_terminal):
        return None
    return answer


def _event_type(event: dict[str, Any]) -> str:
    return str(event.get("type") or "").casefold().replace("-", "_")


def _terminal_review_stop_failed(event: dict[str, Any]) -> bool:
    """True when this event closes the run without a completed turn."""
    event_type = _event_type(event)
    if event_type == "error":
        return True
    if event_type == "turn_ended":
        return str(event.get("outcome") or "").casefold() != "completed"
    if event_type not in {"end", "result"}:
        return False
    reason = event.get("stopReason", event.get("stop_reason"))
    return isinstance(reason, str) and bool(reason) and reason not in SUCCESSFUL_REVIEW_STOP_REASONS


def _successful_terminal_event(event: dict[str, Any]) -> bool:
    """True when this event closes a turn that the provider finished."""
    event_type = _event_type(event)
    if event_type == "turn_ended":
        return str(event.get("outcome") or "").casefold() == "completed"
    if event_type != "end":
        return False
    reason = event.get("stopReason", event.get("stop_reason"))
    return reason in SUCCESSFUL_REVIEW_STOP_REASONS


def _answer_needs_successful_terminal(event: dict[str, Any]) -> bool:
    """Assistant text is not final until a later successful terminal event."""
    return _event_type(event) == "assistant"


def _event_answer(event: object) -> str | None:
    if not isinstance(event, dict):
        return None
    structured = event.get("structured_output")
    if isinstance(structured, dict):
        return json.dumps(structured, separators=(",", ":"))
    event_type = str(event.get("type") or "").casefold().replace("-", "_")
    if event_type == "item.completed":
        item = event.get("item")
        if isinstance(item, dict) and str(item.get("type") or "").casefold() in {
            "agent_message",
            "assistant_message",
        }:
            text = item.get("text")
            return text if isinstance(text, str) else None
    if event_type in {"result", "final", "completed"}:
        result = event.get("result")
        if isinstance(result, dict):
            return json.dumps(result, separators=(",", ":"))
        if isinstance(result, str):
            return result
    if event_type == "assistant":
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else event.get("content")
        if isinstance(content, list):
            parts = [
                str(item["text"])
                for item in content
                if isinstance(item, dict)
                and item.get("type") in {"text", "output_text"}
                and isinstance(item.get("text"), str)
            ]
            return "".join(parts) or None
    return None


def validate_observable_attachments(attachments: Iterable[dict[str, object]]) -> None:
    """Require stable, unique ids so a load receipt cannot be credited to another resource."""
    _skill_target_groups(attachments)


def observe_skill_loads(
    stdout: str,
    attachments: Iterable[dict[str, object]],
) -> SkillLoadObservation:
    """Find exact selected skill reads that occurred before the first recorded mutation."""
    targets = _skill_target_groups(attachments)
    loaded: set[str] = set()
    components: dict[str, set[str]] = {capability_id: set() for capability_id in targets}
    mutation_seen = False
    event_count = 0
    malformed = 0
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        event_count += 1
        all_paths = tuple(
            path
            for paths, _digest, _kind in targets.values()
            for path in paths
        )
        reads, mutates = _event_actions(event, all_paths)
        if not mutation_seen and not mutates:
            for capability_id, (paths, _digest, _kind) in targets.items():
                matched = reads.intersection(paths)
                if matched:
                    loaded.add(capability_id)
                    components[capability_id].update(paths[path] for path in matched)
        mutation_seen = mutation_seen or mutates
    ordered = tuple(targets)
    return SkillLoadObservation(
        loaded=tuple(item for item in ordered if item in loaded),
        missing=tuple(item for item in ordered if item not in loaded),
        components=tuple(
            (item, tuple(sorted(components[item]))) for item in ordered if item in loaded
        ),
        event_count=event_count,
        malformed_lines=malformed,
    )


def loaded_receipts(
    observation: SkillLoadObservation,
    attachments: Iterable[dict[str, object]],
) -> list[dict[str, object]]:
    """Return redacted receipts; never include raw provider events or absolute paths."""
    by_id = {str(item.get("capability_id")): item for item in attachments}
    components = dict(observation.components)
    receipts: list[dict[str, object]] = []
    for capability_id in observation.loaded:
        receipt: dict[str, object] = {
            "capability_id": capability_id,
            "kind": str(by_id[capability_id].get("kind") or "skill"),
            "content_sha256": str(by_id[capability_id].get("content_sha256") or ""),
            "observed_action": "file_read",
            "observed_before_first_recorded_mutation": True,
            "invocation": "observed-load",
        }
        source_tree_sha256 = by_id[capability_id].get("source_tree_sha256")
        if isinstance(source_tree_sha256, str) and source_tree_sha256:
            receipt["source_tree_sha256"] = source_tree_sha256
        if receipt["kind"] == "claude-plugin":
            receipt["components"] = list(components.get(capability_id, ()))
        receipts.append(receipt)
    return receipts


def _skill_target_groups(
    attachments: Iterable[dict[str, object]],
) -> dict[str, tuple[dict[str, str], str, str]]:
    """Map one capability to one or more acceptable SKILL.md reads."""
    targets: dict[str, tuple[dict[str, str], str, str]] = {}
    for attachment in attachments:
        kind = str(attachment.get("kind") or "skill")
        if kind not in {"skill", "claude-plugin"}:
            continue
        capability_id = str(attachment.get("capability_id") or "")
        paths: dict[str, str] = {}
        if kind == "skill":
            path = str(attachment.get("_path") or "")
            if path:
                paths[_normalize_path(path)] = capability_id
        else:
            runtime = attachment.get("_plugin")
            skills = runtime.get("skills") if isinstance(runtime, dict) else None
            if isinstance(skills, list):
                for skill in skills:
                    if not isinstance(skill, dict):
                        continue
                    name = str(skill.get("name") or "")
                    path = str(skill.get("path") or "")
                    if name and path:
                        paths[_normalize_path(path)] = name
        if not capability_id or not paths:
            raise ProviderEventError("selected skill is missing observable identity metadata")
        if capability_id in targets:
            raise ProviderEventError(
                f"duplicate selected capability id cannot produce unambiguous evidence: {capability_id}"
            )
        targets[capability_id] = (
            paths,
            str(attachment.get("content_sha256") or ""),
            kind,
        )
    return targets


def observe_mcp_invocations(
    stdout: str,
    attachments: Iterable[dict[str, object]],
) -> McpInvocationObservation:
    """Observe allowlisted MCP tool calls without retaining arguments or results."""
    targets: dict[str, tuple[str, frozenset[str]]] = {}
    for attachment in attachments:
        if attachment.get("kind") != "mcp":
            continue
        runtime = attachment.get("_mcp")
        if not isinstance(runtime, dict):
            continue
        capability_id = str(attachment.get("capability_id") or "")
        server = str(runtime.get("server_name") or "").casefold()
        tools = runtime.get("allowed_tools")
        if capability_id and server and isinstance(tools, list):
            targets[capability_id] = (server, frozenset(str(tool).casefold() for tool in tools))
    observed: dict[str, set[str]] = {capability_id: set() for capability_id in targets}
    violations: set[str] = set()
    event_count = 0
    malformed = 0
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        event_count += 1
        for item in _objects(event):
            call = _mcp_tool_call(item, tuple(server for server, _tools in targets.values()))
            if call is None:
                continue
            server, tool = call
            matches = [
                capability_id
                for capability_id, (expected_server, _allowed) in targets.items()
                if expected_server == server
            ]
            for capability_id in matches:
                allowed = targets[capability_id][1]
                if tool in allowed:
                    observed[capability_id].add(tool)
                else:
                    violations.add(f"{capability_id}:{tool}")
    invoked = tuple(capability_id for capability_id in targets if observed[capability_id])
    return McpInvocationObservation(
        invoked=invoked,
        missing=tuple(capability_id for capability_id in targets if capability_id not in invoked),
        tools=tuple(
            (capability_id, tuple(sorted(observed[capability_id]))) for capability_id in invoked
        ),
        violations=tuple(sorted(violations)),
        event_count=event_count,
        malformed_lines=malformed,
    )


def mcp_invocation_receipts(
    observation: McpInvocationObservation,
    attachments: Iterable[dict[str, object]],
) -> list[dict[str, object]]:
    """Create redacted MCP invocation receipts without arguments, results, or paths."""
    by_id = {str(item.get("capability_id")): item for item in attachments}
    tools = dict(observation.tools)
    receipts: list[dict[str, object]] = []
    for capability_id in observation.invoked:
        receipt: dict[str, object] = {
            "capability_id": capability_id,
            "kind": "mcp",
            "content_sha256": str(by_id[capability_id].get("content_sha256") or ""),
            "observed_action": "mcp_tool_call",
            "tools": list(tools[capability_id]),
            "invocation": "observed",
        }
        source_tree_sha256 = by_id[capability_id].get("source_tree_sha256")
        if isinstance(source_tree_sha256, str) and source_tree_sha256:
            receipt["source_tree_sha256"] = source_tree_sha256
        receipts.append(receipt)
    return receipts


def _mcp_tool_call(item: dict[str, Any], servers: tuple[str, ...]) -> tuple[str, str] | None:
    item_type = str(item.get("type") or "").casefold().replace("-", "_")
    raw_server = item.get("server", item.get("server_name", item.get("mcp_server")))
    raw_tool = item.get("tool", item.get("tool_name", item.get("name")))
    if isinstance(raw_server, str) and isinstance(raw_tool, str) and (
        "mcp" in item_type or raw_server.casefold() in servers
    ):
        return raw_server.casefold(), raw_tool.casefold()
    if not isinstance(raw_tool, str):
        return None
    normalized = raw_tool.casefold().replace("-", "_").replace("/", "__").replace(".", "__")
    for server in servers:
        prefixes = (f"mcp__{server}__", f"{server}__")
        for prefix in prefixes:
            if normalized.startswith(prefix) and len(normalized) > len(prefix):
                return server, normalized[len(prefix) :]
    return None


def _event_actions(event: object, targets: tuple[str, ...]) -> tuple[set[str], bool]:
    reads: set[str] = set()
    mutates = False
    for item in _objects(event):
        tool = _tool_name(item)
        arguments = item.get("input", item.get("arguments", item.get("params", {})))
        strings = tuple(_strings(arguments))
        normalized_strings = tuple(_normalize_path(value) for value in strings)
        if tool in _READ_TOOLS:
            reads.update(path for path in targets if path in normalized_strings)
        if tool in _MUTATION_TOOLS:
            mutates = True
        command = item.get("command", item.get("cmd"))
        if isinstance(command, list):
            command = " ".join(str(part) for part in command)
        if isinstance(command, str):
            normalized_command = _normalize_path(command)
            if _READ_COMMAND.search(command):
                reads.update(path for path in targets if path in normalized_command)
            if _MUTATION_COMMAND.search(command):
                mutates = True
    return reads, mutates


def _objects(value: object) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _objects(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _objects(nested)


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
        if value.lstrip().startswith(("{", "[")):
            try:
                nested = json.loads(value)
            except json.JSONDecodeError:
                return
            yield from _strings(nested)
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _strings(nested)


def _tool_name(item: dict[str, Any]) -> str:
    for key in ("tool_name", "name", "tool"):
        value = item.get(key)
        if isinstance(value, str):
            normalized = value.lower().replace("-", "_").replace("/", ".")
            return normalized.rsplit(".", 1)[-1]
    return ""


def _normalize_path(value: str) -> str:
    normalized = value.strip().strip("\"'").replace("\\", "/").casefold()
    return normalized.removeprefix("//?/")


def _ensure_flag(args: list[str], flag: str) -> list[str]:
    return args if flag in args else [*args, flag]


def _set_option(args: list[str], flag: str, value: str) -> list[str]:
    if flag not in args:
        return [*args, flag, value]
    index = args.index(flag)
    if index == len(args) - 1:
        return [*args, value]
    updated = list(args)
    updated[index + 1] = value
    return updated
