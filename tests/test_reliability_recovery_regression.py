from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from stagemesh.domain import ExecutionKind, ExecutionStatus
from stagemesh.execution import classify_failure
from stagemesh.providers import _command_and_input, _diagnose_no_change
from stagemesh.selection_scope import Scope, ScopeError, TransientScopeError, attach
from stagemesh.task_sources import DiscoveredTask, GitHubApiIssueSource


def test_agy_command_and_input_preserves_stdin_and_avoids_winerror_206() -> None:
    # 1. Without --print flag: prompt is passed via stdin
    cmd = ("agy", "--mode", "accept-edits", "--dangerously-skip-permissions")
    argv, stdin_input = _command_and_input(cmd, "prompt content")
    assert argv == ["agy", "--mode", "accept-edits", "--dangerously-skip-permissions"]
    assert stdin_input == "prompt content"

    # 2. With --print flag but short prompt: preserved
    cmd_print = ("agy", "--print", "--mode", "accept-edits")
    argv2, stdin_input2 = _command_and_input(cmd_print, "short prompt")
    assert argv2 == ["agy", "--print", "short prompt", "--mode", "accept-edits"]
    assert stdin_input2 == ""

    # 3. With --print flag but huge prompt: strips flag to avoid WinError 206
    huge_prompt = "x" * 5000
    argv3, stdin_input3 = _command_and_input(cmd_print, huge_prompt)
    assert "--print" not in argv3
    assert stdin_input3 == huge_prompt


def test_classify_failure_distinguishes_winerror_206_from_provider_unavailable() -> None:
    normal_missing = FileNotFoundError("The system cannot find the file specified")
    is_cap, reason = classify_failure(1, exc=normal_missing)
    assert is_cap is True
    assert reason == "provider_unavailable"

    win_206 = FileNotFoundError("[WinError 206] The filename or extension is too long")
    win_206.winerror = 206
    is_cap_206, reason_206 = classify_failure(1, exc=win_206)
    assert is_cap_206 is False
    assert reason_206 == "command_line_too_long"


def test_diagnose_no_implementation_change_classifies_tool_permission_and_contract_scope(tmp_path: Path) -> None:
    # 1. Wildcard tool pattern rejection
    stderr_wildcard = r'Ignoring --allowedTools rule "*\)": Wildcard tool name "*\)" is not supported in allow rules.'
    diag1 = _diagnose_no_change("", stderr_wildcard, tmp_path, tmp_path, None)
    assert "tool_permission_denied" in diag1

    # 2. Findings in forbidden files
    stdout_forbidden = "I made no changes. Everything in the findings is outside what I can edit."
    diag2 = _diagnose_no_change(stdout_forbidden, "", tmp_path, tmp_path, None)
    assert "specification_insufficient" in diag2

    # 3. Default unchanged
    diag3 = _diagnose_no_change("No changes needed.", "", tmp_path, tmp_path, None)
    assert diag3 == "no_implementation_change"


def test_github_api_issue_source_retries_transient_transport_errors() -> None:
    source = GitHubApiIssueSource("owner", "repo", token="secret")

    call_count = 0

    def mock_urlopen(request, timeout):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise urllib.error.URLError("Remote end closed connection without response")
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps([
            {"number": 1, "title": "Issue 1", "state": "open", "labels": []}
        ]).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        return mock_resp

    with patch("urllib.request.urlopen", side_effect=mock_urlopen), patch("time.sleep"):
        tasks, status, retry_after = source.discover(max_retries=3)

    assert status == "OK"
    assert len(tasks) == 1
    assert tasks[0].source_id == "1"
    assert call_count == 3
    assert source.last_successful_sync is not None


def test_github_api_issue_source_honors_ratelimit_reset_and_distinguishes_auth_failure() -> None:
    source = GitHubApiIssueSource("owner", "repo", token="secret", now=1000.0)

    # 1. Rate limited via HTTP 403 with x-ratelimit-remaining: 0
    headers_ratelimit = {
        "x-ratelimit-remaining": "0",
        "x-ratelimit-reset": "1060",
    }
    err_ratelimit = urllib.error.HTTPError(
        "https://api.github.com", 403, "rate limit exceeded", headers_ratelimit, io.BytesIO()
    )

    with patch("urllib.request.urlopen", side_effect=err_ratelimit):
        tasks, status, retry_after = source.discover(max_retries=1)

    assert status == "UNKNOWN"
    assert retry_after is not None
    assert retry_after >= 1060.0

    # 2. Permission failure via HTTP 403 with positive remaining quota
    headers_auth = {
        "x-ratelimit-remaining": "4999",
    }
    err_auth = urllib.error.HTTPError(
        "https://api.github.com", 403, "Forbidden", headers_auth, io.BytesIO()
    )

    with patch("urllib.request.urlopen", side_effect=err_auth):
        tasks2, status2, retry_after2 = source.discover(max_retries=1)

    assert status2 == "STALE"
    assert retry_after2 is None


def test_selection_scope_raises_transient_scope_error_on_unknown(tmp_path: Path) -> None:
    class MockSource:
        name = "mock-github"
        last_status = "UNKNOWN"
        last_retry_after = 1500.0

        def discover(self):
            return []

    class MockBacklog:
        name = "local-backlog"
        last_status = "OK"

        def discover(self):
            return []

    mock_store = MagicMock()
    mock_config = MagicMock()
    scope = Scope("scope-1", "lane:court-intelligence")

    with patch("stagemesh.selection_scope.LocalBacklogSource", return_value=MockBacklog()), \
         patch("stagemesh.selection_scope.task_sources_from_config", return_value=[MockSource()]):
        attach(mock_store, tmp_path, mock_config, scope)
        with pytest.raises(TransientScopeError) as exc_info:
            mock_store.refresh_selection_scope()

        assert "source sync temporarily unavailable: mock-github (UNKNOWN)" in str(exc_info.value)
        assert exc_info.value.retry_after == 1500.0
        assert mock_store.selection_fresh is False


def test_bind_candidate_spec_manifest_fail_closed_on_scope_drift(tmp_path: Path) -> None:
    from stagemesh.spec_integrity import bind_candidate_spec_manifest

    project = tmp_path
    (project / ".stagemesh" / "contracts").mkdir(parents=True)
    task_manifest = project / ".stagemesh" / "contracts" / "424.spec_manifest.json"
    task_manifest.write_text("{}", encoding="utf-8")

    store = MagicMock()
    contract = MagicMock()
    contract.allowed_files = ["src/**"]
    contract.forbidden_files = [".git/**"]

    prereq = {
        "assessment": "structural-only",
        "adapter": {"name": "openspec", "version": "layout-v1"},
        "owners": ["owner_a"],
        "requirements": ["req_1"],
    }

    # Discovering different owners must fail closed
    with patch("stagemesh.spec_integrity.resolve_pinned_adapter") as mock_resolve, \
         patch("stagemesh.contracts.candidate_workspace") as mock_ws, \
         patch("stagemesh.spec_integrity.structural_impact_manifest") as mock_impact:
        mock_impact.return_value = {
            "adapter": {"name": "openspec", "version": "layout-v1"},
            "owners": ["owner_b"],  # DRIFT
            "requirements": ["req_1"],
            "unresolved_references": [],
            "candidate_revision": "cand123",
            "base_revision": "base123",
            "changed_files": ["src/app.py"],
            "unmapped_files": ["src/app.py"],
        }
        bound, errors = bind_candidate_spec_manifest(
            store, project, "424", "cand123", "base123", contract, ["src/app.py"], prereq
        )
        assert bound is None
        assert any("owners" in e for e in errors)


def test_scoped_review_pool_forwards_kwargs_to_underlying_pool() -> None:
    from stagemesh.autonomy.review_adapter import ScopedReviewPool

    mock_pool = MagicMock()
    mock_pool.review_adapter.return_value = (None, [])
    scoped = ScopedReviewPool(mock_pool, None)

    store = MagicMock()
    scoped.review_adapter(
        store, "424", "cand123", "claude",
        excluded_providers=("agy",),
        excluded_command_digests=("digest1",),
    )

    mock_pool.review_adapter.assert_called_once_with(
        store, "424", "cand123", "claude",
        excluded_providers=("agy",),
        excluded_command_digests=("digest1",),
    )
