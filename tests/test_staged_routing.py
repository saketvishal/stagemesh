from __future__ import annotations

from pathlib import Path
import pytest

from stagemesh.domain import Stage
from stagemesh.routing import Provider, Router, RoutingMode


def test_staged_routing_honors_configured_stage_providers():
    p_impl = Provider("codex-builder", frozenset({"code"}), priority=10)
    p_rev = Provider("claude-reviewer", frozenset({"review"}), priority=20)
    router = Router(
        providers=[p_impl, p_rev],
        mode=RoutingMode.STAGED,
        stage_routes={
            str(Stage.IMPLEMENT): "codex-builder",
            str(Stage.REVIEW): "claude-reviewer",
        },
    )

    impl_provider = router.choose_for_stage(Stage.IMPLEMENT, "code")
    assert impl_provider is not None
    assert impl_provider.name == "codex-builder"

    rev_provider = router.choose_for_stage(Stage.REVIEW, "review")
    assert rev_provider is not None
    assert rev_provider.name == "claude-reviewer"


def test_staged_routing_missing_stage_provider_returns_none():
    p_impl = Provider("codex-builder", frozenset({"code"}))
    router = Router(
        providers=[p_impl],
        mode=RoutingMode.STAGED,
        stage_routes={
            str(Stage.REVIEW): "non-existent-reviewer",
        },
    )

    rev_provider = router.choose_for_stage(Stage.REVIEW, "review")
    assert rev_provider is None
