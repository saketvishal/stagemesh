from __future__ import annotations

import pytest

from stagemesh.diagnosis import TOOL_FAILURE_EVENT, tool_failure_evidence
from stagemesh.execution import SubprocessExecutor


@pytest.mark.parametrize(
    ("kind", "stderr"),
    [
        ("missing_tool", "bash: pnpm: command not found"),
        ("missing_dependency", "ModuleNotFoundError: No module named 'requests'"),
        ("cwd_path", "FileNotFoundError: [Errno 2] No such file or directory: 'web'"),
        ("permission", "EACCES: permission denied, open '/x'"),
        ("service_unavailable", "Error: connect ECONNREFUSED 127.0.0.1:5432"),
        ("browser_startup", "browserType.launch: Executable doesn't exist; run playwright install"),
        ("assertion_failure", "E   AssertionError: assert 1 == 2"),
        ("transient", "request failed: ETIMEDOUT"),
    ],
)
def test_each_tool_failure_kind_is_typed(kind: str, stderr: str) -> None:
    ev = tool_failure_evidence(["tool", "run"], "/work", 1, "", stderr)
    assert ev is not None and ev["kind"] == kind
    assert ev["cwd"] == "/work" and ev["status"] == "exit 1" and ev["next_action"]
    assert ev["command"]["executable"] == "tool"


def test_timeout_is_typed_from_the_flag() -> None:
    ev = tool_failure_evidence(["pytest"], "/w", -9, timed_out=True)
    assert ev is not None and ev["kind"] == "timeout" and ev["status"] == "timed_out"


@pytest.mark.parametrize("reason", ["quota_rate_limit", "authentication_failure", "provider_unavailable"])
def test_provider_failures_are_rejected(reason: str) -> None:
    assert tool_failure_evidence(["x"], "/w", 1, "", "command not found", reason=reason) is None


def test_unrecognised_output_yields_no_evidence() -> None:
    assert tool_failure_evidence(["x"], "/w", 1, "all good", "") is None


def test_excerpt_is_redacted_and_bounded() -> None:
    noise = "\n".join(f"error line {i} " + "x" * 300 for i in range(50))
    err = f"command not found\nAuthorization: Bearer abc123\nGITHUB token=ghp_SECRET99\n{noise}"
    ev = tool_failure_evidence(["run", "--password=hunter2"], "/w", 127, "", err)
    assert ev is not None
    blob = str(ev)
    assert "abc123" not in blob and "ghp_SECRET99" not in blob and "hunter2" not in blob
    assert len(ev["excerpt"]) <= 600


class _Store:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def add_audit_event(self, event_type: str, payload: dict) -> str:
        self.events.append((event_type, payload))
        return "id"


def test_executor_persists_evidence_only_for_tool_failures() -> None:
    store = _Store()
    ex = SubprocessExecutor(["tool"])
    ex._record_tool_evidence(store, "t1", "e1", "/w", 1, "", "npm ERR! Missing script: e2e", "tool_execution_failure", False)
    ex._record_tool_evidence(store, "t1", "e2", "/w", 1, "", "command not found", "authentication_failure", False)
    assert [e[0] for e in store.events] == [TOOL_FAILURE_EVENT]
    assert store.events[0][1]["evidence"]["kind"] == "missing_dependency"
