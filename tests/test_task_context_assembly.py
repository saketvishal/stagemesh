"""Tests for automated module-bounded progressive task context assembly."""

from pathlib import Path
import pytest
import json

from build_coordinator.prompts.builders import BuilderPromptBuilder
from build_coordinator.task_context import (
    ContextTier,
    assemble_task_context,
    expand_task_context,
    find_module_manifest,
    parse_module_manifest,
)
from build_coordinator.types import ResumeContext


@pytest.fixture
def mock_repo(tmp_path):
    """Create a mock repository structure with modules and manifests."""
    # Module A: primary module
    mod_a = tmp_path / "alpha"
    mod_a.mkdir(parents=True)
    (mod_a / "service.py").write_text(
        "def run_alpha(x: int) -> int:\n    return x * 2\n\nclass AlphaWorker:\n    pass\n",
        encoding="utf-8",
    )
    (mod_a / "MODULE.md").write_text(
        "module: alpha\n"
        "purpose: Core processing module\n"
        "owned_paths:\n"
        "  - alpha/**\n"
        "public_contracts:\n"
        "  - alpha.service\n"
        "allowed_dependencies:\n"
        "  - beta\n"
        "forbidden_dependencies:\n"
        "  - gamma\n"
        "primary_tests:\n"
        "  - tests/test_alpha.py\n",
        encoding="utf-8",
    )

    # Module B: dependency module
    mod_b = tmp_path / "beta"
    mod_b.mkdir(parents=True)
    (mod_b / "service.py").write_text(
        "def run_beta() -> str:\n    return 'beta'\n",
        encoding="utf-8",
    )
    (mod_b / "MODULE.md").write_text(
        "module: beta\n"
        "purpose: Secondary service\n"
        "owned_paths:\n"
        "  - beta/**\n"
        "public_contracts:\n"
        "  - beta.service\n",
        encoding="utf-8",
    )

    # Module C: unexpected dynamic dependency
    mod_c = tmp_path / "gamma"
    mod_c.mkdir(parents=True)
    (mod_c / "MODULE.md").write_text(
        "module: gamma\n"
        "purpose: Dynamic extra service\n",
        encoding="utf-8",
    )

    return tmp_path


def _sample_resume_context(**overrides) -> ResumeContext:
    defaults = {
        "task_id": "task-test-01",
        "title": "Implement alpha factor processing",
        "task_state": "CLAIMED",
        "project": "api",
        "primary_module": "alpha",
        "allowed_modules": ("beta",),
        "allowed_paths": ("alpha/**",),
        "public_dependencies": ("alpha.service",),
        "forbidden_paths": ("forbidden/**",),
        "primary_tests": ("tests/test_alpha.py",),
        "base_sha": "c410d05",
        "migration_allowed": False,
        "review_policy": "INDEPENDENT_EXTERNAL",
        "independent_review_required": True,
        "branch_name": "feature/test",
        "worktree_path": "/tmp/worktree",
        "current_head_sha": "c410d05",
        "last_checkpoint_id": None,
        "last_checkpoint_at": None,
        "current_step": "implementing alpha",
        "completed_work": ("initial setup",),
        "remaining_work": ("testing",),
        "files_changed": ("alpha/service.py",),
        "commits_created": (),
        "last_successful_tests": (),
        "known_failures": (),
        "explicit_decisions": (),
        "blockers": (),
        "previous_worker_id": None,
        "current_claim_id": "claim-01",
        "current_claim_worker_id": "builder-1",
        "current_claim_type": "EXCLUSIVE",
    }
    defaults.update(overrides)
    return ResumeContext(**defaults)


def test_find_module_manifest(mock_repo):
    manifest = find_module_manifest(mock_repo, "alpha")
    assert manifest is not None
    assert manifest.is_file()
    assert manifest.parent.name == "alpha"

    manifest_b = find_module_manifest(mock_repo, "beta")
    assert manifest_b is not None
    assert manifest_b.parent.name == "beta"

    assert find_module_manifest(mock_repo, "nonexistent") is None


def test_parse_module_manifest(mock_repo):
    manifest_path = mock_repo / "alpha" / "MODULE.md"
    contract = parse_module_manifest(manifest_path, repo_root=mock_repo, include_signatures=True)

    assert contract.module_name == "alpha"
    assert contract.purpose == "Core processing module"
    assert "alpha.service" in contract.public_contracts
    assert "alpha/**" in contract.owned_paths
    assert "gamma" in contract.forbidden_dependencies
    assert "alpha.service" in contract.contract_signatures
    sig = contract.contract_signatures["alpha.service"]
    assert "run_alpha" in sig
    assert "AlphaWorker" in sig


def test_progressive_context_retrieval_tiers(mock_repo):
    context = _sample_resume_context()

    # Tier 1: Envelope only
    env_context = assemble_task_context(
        mock_repo,
        context,
        tier=ContextTier.ENVELOPE,
        extra={"objective": {"objective_id": "obj-01", "goal": "Validate alpha"}},
    )
    assert env_context.tier == ContextTier.ENVELOPE
    assert env_context.primary_module is not None
    assert len(env_context.primary_module.contract_signatures) == 0

    # Tier 2: Progressive Interfaces
    prog_context = assemble_task_context(
        mock_repo,
        context,
        tier=ContextTier.PROGRESSIVE_INTERFACES,
        extra={"objective": {"objective_id": "obj-01", "goal": "Validate alpha"}},
    )
    assert prog_context.tier == ContextTier.PROGRESSIVE_INTERFACES
    assert len(prog_context.primary_module.contract_signatures) > 0
    assert prog_context.estimated_context_tokens >= env_context.estimated_context_tokens


def test_dynamic_context_expansion_for_unexpected_dependencies(mock_repo):
    context = _sample_resume_context(
        primary_module="alpha",
        allowed_modules=("beta",),
    )
    base_context = assemble_task_context(mock_repo, context)
    assert len(base_context.dependency_contracts) == 1

    expanded = expand_task_context(
        mock_repo,
        base_context,
        ["gamma"],
    )
    dep_names = [dep.module_name for dep in expanded.dependency_contracts]
    assert "beta" in dep_names
    assert "gamma" in dep_names
    assert "gamma" in expanded.expanded_modules
    assert expanded.estimated_context_tokens > base_context.estimated_context_tokens


def test_prompt_builder_attaches_progressive_context(mock_repo):
    context = _sample_resume_context()
    builder = BuilderPromptBuilder()
    prompt_json = builder.build(
        context,
        repo_root=mock_repo,
        extra={"objective": {"objective_id": "obj-99", "goal": "Optimize boundaries"}},
    )

    payload = json.loads(prompt_json)
    assert "bounded_module_context" in payload["task_envelope"]
    bmc = payload["task_envelope"]["bounded_module_context"]
    assert bmc["primary_module"]["module_name"] == "alpha"
    assert "contract_signatures" in bmc["primary_module"]
    assert bmc["objective_id"] == "obj-99"
