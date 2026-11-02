"""Regression suite: private GitHub repository 403 response handling and reporting consolidation."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import stagemesh.fast_forward as ff
from stagemesh.autonomy.github_adapter import GitHubAuthorizationError
from stagemesh.config import load_config
from stagemesh.run_window import Check, resolve_merge_defaults


def _make_project(tmp_path: Path, merging_dict: dict) -> Path:
    proj = tmp_path / "proj"
    stagemesh_dir = proj / ".stagemesh"
    stagemesh_dir.mkdir(parents=True, exist_ok=True)
    cfg = {
        "github": {"owner": "testowner", "repo": "testrepo"},
        "merging": merging_dict,
        "routing": {
            "require_independent_review": True,
            "pools": {"IMPLEMENT": ["claude"], "REVIEW": ["agy"]},
        },
    }
    (stagemesh_dir / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    return proj


def test_resolve_merge_defaults_selects_stagemesh_when_check_app_id_configured(tmp_path: Path) -> None:
    """When --auto-merge-ff is given without --merge-policy, projects with check_app_id default to stagemesh."""
    proj = _make_project(tmp_path, {
        "mode": "pr_only",
        "required_checks": ["backend", "web"],
        "check_app_id": 15368,
    })
    config = load_config(proj)
    args = argparse.Namespace(auto_merge_ff=True, merge_policy=None, pr_only=False, required_checks=None, concurrency=None)
    defaults = resolve_merge_defaults(config, args)
    assert defaults["auto_merge_ff"] is True
    assert defaults["merge_policy"] == "stagemesh"
    assert defaults["check_app_id"] == 15368
    assert defaults["required_checks"] == ("backend", "web")
    assert defaults["concurrency"] == 1


def test_resolve_merge_defaults_respects_configured_stagemesh_mode(tmp_path: Path) -> None:
    """If project config explicitly sets mode to stagemesh, it is honored."""
    proj = _make_project(tmp_path, {
        "mode": "stagemesh",
        "required_checks": ["backend"],
        "check_app_id": 15368,
    })
    config = load_config(proj)
    args = argparse.Namespace(auto_merge_ff=False, merge_policy=None, pr_only=False, required_checks=None, concurrency=None)
    defaults = resolve_merge_defaults(config, args)
    assert defaults["auto_merge_ff"] is True
    assert defaults["merge_policy"] == "stagemesh"


def test_resolve_merge_defaults_explicit_merge_policy_overrides(tmp_path: Path) -> None:
    """Explicit --merge-policy overrides project defaults."""
    proj = _make_project(tmp_path, {
        "mode": "stagemesh",
        "required_checks": ["backend"],
        "check_app_id": 15368,
    })
    config = load_config(proj)
    args = argparse.Namespace(auto_merge_ff=False, merge_policy="hosted", pr_only=False, required_checks=None, concurrency=None)
    defaults = resolve_merge_defaults(config, args)
    assert defaults["merge_policy"] == "hosted"


def test_host_policy_handles_private_repo_403_gracefully() -> None:
    """When GitHub returns 403 asking to upgrade to Pro on private repos, host_policy returns actionable string without unhandled raise."""
    adapter = ff.FastForwardPullRequests("owner", "repo", MagicMock(), MagicMock())
    adapter.configure_ff(Path("."), MagicMock(), "main", ("backend",), MagicMock(), policy="hosted")

    def mock_request(method: str, path: str, *args, **kwargs):
        raise GitHubAuthorizationError(403, "Upgrade to GitHub Pro or make this repository public to enable this feature.")

    adapter._request = mock_request

    error = adapter.host_policy()
    assert error is not None
    assert "hosted_branch_policy_unavailable" in error
    assert "GitHub requires GitHub Pro" in error
    assert "--merge-policy stagemesh" in error


def test_preflight_separates_landing_policy_from_pr_lookup(tmp_path: Path) -> None:
    """A policy error in merge_pr.preflight is recorded under hosted_landing_policy, not prior_pr_lookup."""
    from stagemesh.persistence import Store
    from stagemesh.run_window import WindowOptions, run_window

    store = Store(Path(":memory:"))
    store.migrate()

    mock_merger = MagicMock()
    mock_merger.preflight.side_effect = RuntimeError("simulated policy failure")
    mock_merger.active_recovery.return_value = None
    mock_merger.pending_records.return_value = []

    mock_hooks = MagicMock()
    mock_hooks.repo_state.return_value = None

    options = WindowOptions(
        duration_seconds=60,
        auto_merge_ff=True,
        merge_policy="hosted",
        required_checks=("backend",),
    )

    proj = _make_project(tmp_path, {"mode": "pr_only"})
    config = load_config(proj)

    report = run_window(
        store,
        proj,
        config,
        options,
        mock_hooks,
        base_ref="refs/heads/main",
        merge_pr=mock_merger,
        list_prs=lambda p: [],
    )

    names = {c.name: c for c in report.preflight}
    assert "hosted_landing_policy" in names
    assert names["hosted_landing_policy"].ok is False
    assert "simulated policy failure" in names["hosted_landing_policy"].detail
    # prior_pr_lookup must NOT be marked failed when list_prs succeeded
    assert "prior_pr_lookup" not in names or names["prior_pr_lookup"].ok is True
