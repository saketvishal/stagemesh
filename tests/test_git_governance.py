from __future__ import annotations

import json
from pathlib import Path
import subprocess
import pytest

from stagemesh.coordinator import Coordinator
from stagemesh.domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus, Stage
from stagemesh.execution import ExecutionResult, Executor, FakeExecutor, SubprocessExecutor
from stagemesh.git import GitWorkspace
from stagemesh.governance import (
    CandidateCanonicalizer,
    CommitMetadataValidator,
    GitGovernancePolicy,
    GovernanceAttributionError,
    GovernanceBaselineMismatchError,
    GovernanceExactTreeMismatchError,
    GovernanceValidationError,
    TaskBaseline,
    capture_agent_result_tree,
    capture_baseline,
    canonicalize_and_record_candidate,
    get_governance_evidence,
)
from stagemesh.persistence import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    db = Store(tmp_path / "governance_state.sqlite3")
    db.migrate()
    yield db
    db.close()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, GitWorkspace]:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    ws = GitWorkspace(repo_dir)
    ws.init_if_needed()
    # Create initial baseline commit
    init_file = repo_dir / "README.md"
    init_file.write_text("# Project\nInitial baseline\n", encoding="utf-8")
    ws.run("add", "-A")
    ws.run("commit", "-m", "Initial commit")
    return repo_dir, ws


def test_scenario_a_claude_style_attribution(store: Store, repo: tuple[Path, GitWorkspace]) -> None:
    """A. Claude-style attribution: Agent creates temporary commit with Claude trailer.
    Canonical candidate must have identical tree, no Claude co-author trailer, and StageMesh author/committer.
    """
    repo_dir, ws = repo
    task_id = store.upsert_task("task-claude")
    baseline = capture_baseline(ws, task_id)
    assert baseline.commit_sha is not None
    assert baseline.tree_sha is not None

    execution_id = store.start_execution(task_id=task_id, claim_id=None, kind=ExecutionKind.IMPLEMENTATION)

    # Agent creates a temporary commit with Claude co-author trailer
    code_file = repo_dir / "claude_work.py"
    code_file.write_text("print('hello from claude')\n", encoding="utf-8")
    ws.run("add", "-A")
    ws.run(
        "commit",
        "-m",
        "Agent temporary commit\n\nCo-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>",
    )
    temp_agent_commit = ws.run("rev-parse", "HEAD").stdout.strip()
    agent_tree = ws.run("rev-parse", "HEAD^{tree}").stdout.strip()

    # StageMesh canonicalizes the result
    canonical_sha = canonicalize_and_record_candidate(
        store=store,
        workspace=ws,
        task_id=task_id,
        execution_id=execution_id,
        claim_id=None,
        provider="claude",
        baseline=baseline,
        agent_result_tree=agent_tree,
        agent_candidate_sha=temp_agent_commit,
    )

    assert canonical_sha != temp_agent_commit

    # Tree equivalence
    canonical_tree = ws.run("rev-parse", f"{canonical_sha}^{{tree}}").stdout.strip()
    assert canonical_tree == agent_tree

    # Parent is baseline
    parents = ws.run("rev-parse", f"{canonical_sha}^@").stdout.splitlines()
    assert parents == [baseline.commit_sha]

    # Commit metadata verification
    log_text = ws.run("log", "-1", canonical_sha).stdout
    assert "Co-Authored-By: Claude" not in log_text
    assert "noreply@anthropic.com" not in log_text
    assert "Author: StageMesh <stagemesh@example.invalid>" in log_text
    assert "StageMesh" in log_text

    # Store verification
    candidate = store.latest_candidate(task_id)
    assert candidate is not None
    assert candidate["sha"] == canonical_sha
    assert candidate["base_sha"] == baseline.commit_sha


def test_scenario_b_codex_style_attribution(store: Store, repo: tuple[Path, GitWorkspace]) -> None:
    """B. Codex-style attribution: Agent creates temporary commit with OpenAI Codex trailer.
    Canonical candidate must have identical tree, no Codex co-author trailer, and StageMesh author/committer.
    """
    repo_dir, ws = repo
    task_id = store.upsert_task("task-codex")
    baseline = capture_baseline(ws, task_id)

    execution_id = store.start_execution(task_id=task_id, claim_id=None, kind=ExecutionKind.IMPLEMENTATION)

    # Agent creates temporary commit with Codex trailer
    code_file = repo_dir / "codex_work.py"
    code_file.write_text("print('hello from codex')\n", encoding="utf-8")
    ws.run("add", "-A")
    ws.run(
        "commit",
        "-m",
        "Implement codex feature\n\nCo-Authored-By: OpenAI Codex <codex@openai.com>",
    )
    temp_agent_commit = ws.run("rev-parse", "HEAD").stdout.strip()
    agent_tree = capture_agent_result_tree(ws, agent_candidate_sha=temp_agent_commit)

    canonical_sha = canonicalize_and_record_candidate(
        store=store,
        workspace=ws,
        task_id=task_id,
        execution_id=execution_id,
        claim_id=None,
        provider="codex",
        baseline=baseline,
        agent_result_tree=agent_tree,
        agent_candidate_sha=temp_agent_commit,
    )

    assert canonical_sha != temp_agent_commit
    canonical_tree = ws.run("rev-parse", f"{canonical_sha}^{{tree}}").stdout.strip()
    assert canonical_tree == agent_tree

    log_text = ws.run("log", "-1", canonical_sha).stdout
    assert "Co-Authored-By" not in log_text
    assert "codex@openai.com" not in log_text
    assert "Author: StageMesh <stagemesh@example.invalid>" in log_text


def test_scenario_c_unknown_provider(store: Store, repo: tuple[Path, GitWorkspace]) -> None:
    """C. Unknown provider: Works without provider-specific code for any future provider."""
    repo_dir, ws = repo
    task_id = store.upsert_task("task-unknown")
    baseline = capture_baseline(ws, task_id)

    execution_id = store.start_execution(task_id=task_id, claim_id=None, kind=ExecutionKind.IMPLEMENTATION)

    code_file = repo_dir / "future_agent.py"
    code_file.write_text("print('future AI agent')\n", encoding="utf-8")
    ws.run("add", "-A")
    ws.run(
        "commit",
        "-m",
        "Future AI work\n\nCo-Authored-By: FutureAgent-4000 <ai@future-agent.example.org>",
    )
    temp_commit = ws.run("rev-parse", "HEAD").stdout.strip()
    agent_tree = capture_agent_result_tree(ws)

    canonical_sha = canonicalize_and_record_candidate(
        store=store,
        workspace=ws,
        task_id=task_id,
        execution_id=execution_id,
        claim_id=None,
        provider="future-agent-4000",
        baseline=baseline,
        agent_result_tree=agent_tree,
        agent_candidate_sha=temp_commit,
    )

    assert canonical_sha != temp_commit
    log_text = ws.run("log", "-1", canonical_sha).stdout
    assert "Co-Authored-By" not in log_text
    assert "future-agent" not in log_text
    assert "Author: StageMesh <stagemesh@example.invalid>" in log_text


def test_scenario_d_multiple_temporary_commits(store: Store, repo: tuple[Path, GitWorkspace]) -> None:
    """D. Multiple temporary commits: Agent produces 3 commits.
    Canonical candidate must be ONE StageMesh candidate commit with parent=baseline and tree=final tree.
    """
    repo_dir, ws = repo
    task_id = store.upsert_task("task-multi-commit")
    baseline = capture_baseline(ws, task_id)

    execution_id = store.start_execution(task_id=task_id, claim_id=None, kind=ExecutionKind.IMPLEMENTATION)

    # 1st agent commit
    (repo_dir / "step1.txt").write_text("step 1\n", encoding="utf-8")
    ws.run("add", "-A")
    ws.run("commit", "-m", "Agent step 1")

    # 2nd agent commit
    (repo_dir / "step2.txt").write_text("step 2\n", encoding="utf-8")
    ws.run("add", "-A")
    ws.run("commit", "-m", "Agent step 2")

    # 3rd agent commit
    (repo_dir / "step3.txt").write_text("step 3\n", encoding="utf-8")
    ws.run("add", "-A")
    ws.run("commit", "-m", "Agent step 3\n\nCo-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>")

    final_temp_commit = ws.run("rev-parse", "HEAD").stdout.strip()
    final_agent_tree = ws.run("rev-parse", "HEAD^{tree}").stdout.strip()

    canonical_sha = canonicalize_and_record_candidate(
        store=store,
        workspace=ws,
        task_id=task_id,
        execution_id=execution_id,
        claim_id=None,
        provider="claude",
        baseline=baseline,
        agent_result_tree=final_agent_tree,
    )

    # Exactly ONE candidate commit
    assert canonical_sha != final_temp_commit
    parents = ws.run("rev-parse", f"{canonical_sha}^@").stdout.splitlines()
    assert parents == [baseline.commit_sha]

    # Tree equals final agent tree
    canonical_tree = ws.run("rev-parse", f"{canonical_sha}^{{tree}}").stdout.strip()
    assert canonical_tree == final_agent_tree

    # Intermediary commits are not part of canonical history
    ancestors = ws.run("rev-list", f"{canonical_sha}").stdout.splitlines()
    assert final_temp_commit not in ancestors
    assert len(ancestors) == 2  # [canonical_sha, baseline.commit_sha]


def test_scenario_e_no_agent_commit(store: Store, repo: tuple[Path, GitWorkspace]) -> None:
    """E. No agent commit: Agent modifies files in worktree but creates zero commits.
    Canonicalization still succeeds from final worktree tree.
    """
    repo_dir, ws = repo
    task_id = store.upsert_task("task-no-commit")
    baseline = capture_baseline(ws, task_id)

    execution_id = store.start_execution(task_id=task_id, claim_id=None, kind=ExecutionKind.IMPLEMENTATION)

    # Agent creates files but runs NO git commit
    (repo_dir / "uncommitted_work.py").write_text("x = 42\n", encoding="utf-8")

    agent_tree = capture_agent_result_tree(ws)
    assert agent_tree != baseline.tree_sha

    canonical_sha = canonicalize_and_record_candidate(
        store=store,
        workspace=ws,
        task_id=task_id,
        execution_id=execution_id,
        claim_id=None,
        provider="agent",
        baseline=baseline,
        agent_result_tree=agent_tree,
    )

    canonical_tree = ws.run("rev-parse", f"{canonical_sha}^{{tree}}").stdout.strip()
    assert canonical_tree == agent_tree

    parents = ws.run("rev-parse", f"{canonical_sha}^@").stdout.splitlines()
    assert parents == [baseline.commit_sha]


def test_scenario_f_tree_mismatch(store: Store, repo: tuple[Path, GitWorkspace]) -> None:
    """F. Tree mismatch: Deliberately altered expected tree during canonicalization must fail closed."""
    repo_dir, ws = repo
    task_id = store.upsert_task("task-tree-mismatch")
    baseline = capture_baseline(ws, task_id)

    canonicalizer = CandidateCanonicalizer()

    # Pass an invalid or non-existent tree SHA
    fake_tree = "0000000000000000000000000000000000000000"
    with pytest.raises(GovernanceExactTreeMismatchError):
        canonicalizer.canonicalize(
            workspace=ws,
            baseline=baseline,
            agent_result_tree=fake_tree,
            task_id=task_id,
        )


def test_scenario_g_parent_baseline_mismatch(store: Store, repo: tuple[Path, GitWorkspace]) -> None:
    """G. Parent/baseline mismatch: Unexpected baseline movement or missing parent must fail closed."""
    repo_dir, ws = repo
    task_id = store.upsert_task("task-baseline-mismatch")

    # Baseline pointing to a non-existent commit SHA
    fake_baseline = TaskBaseline(
        task_id=task_id,
        commit_sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        tree_sha="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )

    (repo_dir / "test.txt").write_text("test\n", encoding="utf-8")
    agent_tree = capture_agent_result_tree(ws)

    canonicalizer = CandidateCanonicalizer()
    with pytest.raises(GovernanceBaselineMismatchError):
        canonicalizer.canonicalize(
            workspace=ws,
            baseline=fake_baseline,
            agent_result_tree=agent_tree,
            task_id=task_id,
        )


def test_scenario_h_human_coauthor_preservation(store: Store, repo: tuple[Path, GitWorkspace]) -> None:
    """H. Human co-author preservation:
    1. By default, v1 prohibits all co-author trailers.
    2. When explicitly permitted by policy, approved human co-authors are attached and validated,
       while AI provider trailers remain strictly prohibited.
    """
    repo_dir, ws = repo
    task_id = store.upsert_task("task-human-coauthor")
    baseline = capture_baseline(ws, task_id)

    (repo_dir / "work.txt").write_text("collaborative work\n", encoding="utf-8")
    agent_tree = capture_agent_result_tree(ws)

    # 1. Custom policy explicitly allowing a human co-author
    human_policy = GitGovernancePolicy(
        allow_human_coauthors=True,
        allowed_human_coauthors=("Alice Engineer <alice@example.com>",),
        prohibit_ai_provider_trailers=True,
    )
    canonicalizer = CandidateCanonicalizer(policy=human_policy)
    canonical_sha = canonicalizer.canonicalize(
        workspace=ws,
        baseline=baseline,
        agent_result_tree=agent_tree,
        task_id=task_id,
    )
    log_text = ws.run("log", "-1", canonical_sha).stdout
    assert "Co-Authored-By: Alice Engineer <alice@example.com>" in log_text

    # 2. An unauthorized human or AI co-author must fail validation
    validator = CommitMetadataValidator(policy=human_policy)
    # Manually create commit with Claude trailer under this policy
    bad_commit = ws.run(
        "commit-tree",
        agent_tree,
        "-p",
        baseline.commit_sha,
        "-m",
        "Test commit\n\nCo-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>",
    ).stdout.strip()
    with pytest.raises(GovernanceAttributionError) as exc_info:
        validator.validate(ws, bad_commit, agent_tree, baseline.commit_sha)
    assert "prohibited AI/provider attribution trailer" in str(exc_info.value)

    # 3. Default policy (allow_human_coauthors=False) rejects any co-author trailer
    default_validator = CommitMetadataValidator(policy=GitGovernancePolicy())
    human_commit = ws.run(
        "commit-tree",
        agent_tree,
        "-p",
        baseline.commit_sha,
        "-m",
        "Test commit\n\nCo-Authored-By: Alice Engineer <alice@example.com>",
    ).stdout.strip()
    with pytest.raises(GovernanceAttributionError) as exc_info2:
        default_validator.validate(ws, human_commit, agent_tree, baseline.commit_sha)
    assert "co-author trailers are prohibited" in str(exc_info2.value)


def test_scenario_i_restart_idempotency(store: Store, repo: tuple[Path, GitWorkspace]) -> None:
    """I. Restart/idempotency: StageMesh recovers and reuses existing canonical candidate on restart without duplicates."""
    repo_dir, ws = repo
    task_id = store.upsert_task("task-idempotent")
    baseline = capture_baseline(ws, task_id)
    execution_id = store.start_execution(task_id=task_id, claim_id=None, kind=ExecutionKind.IMPLEMENTATION)

    (repo_dir / "idempotent.txt").write_text("data\n", encoding="utf-8")
    agent_tree = capture_agent_result_tree(ws)

    # First canonicalization
    sha1 = canonicalize_and_record_candidate(
        store=store,
        workspace=ws,
        task_id=task_id,
        execution_id=execution_id,
        claim_id=None,
        provider="fake",
        baseline=baseline,
        agent_result_tree=agent_tree,
    )

    # Second canonicalization (simulating restart after crash)
    sha2 = canonicalize_and_record_candidate(
        store=store,
        workspace=ws,
        task_id=task_id,
        execution_id=execution_id,
        claim_id=None,
        provider="fake",
        baseline=baseline,
        agent_result_tree=agent_tree,
    )

    assert sha1 == sha2

    # Verify no duplicate candidate records exist
    rows = list(store.conn.execute("SELECT * FROM candidates WHERE task_id=?", (task_id,)))
    assert len(rows) == 1
    assert rows[0]["sha"] == sha1


def test_scenario_j_downstream_sha_proof(store: Store, repo: tuple[Path, GitWorkspace]) -> None:
    """J. Downstream SHA proof: Prove validation, review, and integration all operate
    on the canonical StageMesh candidate SHA rather than the temporary provider commit SHA.
    """
    repo_dir, ws = repo
    task_id = store.upsert_task("task-downstream")
    store.advance_task(task_id, Stage.IMPLEMENT)

    class AdversarialAgentExecutor(Executor):
        name = "adversarial-claude"

        def run(self, s: Store, t_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
            workspace = GitWorkspace(project)
            workspace.init_if_needed()
            baseline = capture_baseline(workspace, t_id)
            exec_id = s.start_execution(task_id=t_id, claim_id=claim_id, kind=ExecutionKind.IMPLEMENTATION)

            # Adversarial agent makes temporary commit with Claude attribution
            f = project / "adversarial.txt"
            f.write_text("agent content\n", encoding="utf-8")
            workspace.run("add", "-A")
            workspace.run(
                "commit",
                "-m",
                "Temporary provider commit\n\nCo-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>",
            )
            temp_sha = workspace.run("rev-parse", "HEAD").stdout.strip()
            agent_tree = capture_agent_result_tree(workspace, agent_candidate_sha=temp_sha)

            # StageMesh governance canonicalizes
            canon_sha = canonicalize_and_record_candidate(
                store=s,
                workspace=workspace,
                task_id=t_id,
                execution_id=exec_id,
                claim_id=claim_id,
                provider=self.name,
                baseline=baseline,
                agent_result_tree=agent_tree,
                durable_handoff=True,
                agent_candidate_sha=temp_sha,
            )
            s.finish_execution(exec_id, ExecutionStatus.SUCCEEDED, canon_sha)
            return ExecutionResult(ExecutionStatus.SUCCEEDED, canon_sha, durable_handoff=True)

    coord = Coordinator(store, repo_dir, executor=AdversarialAgentExecutor())

    # 1. Tick through IMPLEMENT
    assert coord.tick() == 1
    assert store.get_task(task_id)["stage"] == Stage.VALIDATE

    candidate = store.latest_candidate(task_id)
    assert candidate is not None
    canonical_sha = candidate["sha"]

    # 2. Tick through VALIDATE
    assert coord.tick() == 1
    assert store.get_task(task_id)["stage"] == Stage.REVIEW
    assert store.has_evidence(task_id, canonical_sha, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)

    # 3. Tick through REVIEW
    assert coord.tick() == 1
    assert store.get_task(task_id)["stage"] == Stage.INTEGRATE
    assert store.has_evidence(task_id, canonical_sha, EvidenceKind.REVIEW, EvidenceStatus.PASSED)

    # 4. Tick through INTEGRATE
    assert coord.tick() == 1
    assert store.get_task(task_id)["stage"] == Stage.DONE
    assert store.has_evidence(task_id, canonical_sha, EvidenceKind.INTEGRATION, EvidenceStatus.PASSED)

    # STRICT PROOF: Verify EVERY evidence row references canonical_sha and NEVER the temporary agent commit
    all_evidence = list(store.conn.execute("SELECT candidate_sha, kind, status FROM evidence WHERE task_id=?", (task_id,)))
    assert len(all_evidence) == 3
    for ev in all_evidence:
        assert ev["candidate_sha"] == canonical_sha
        assert ev["candidate_sha"] != "temp"

    # Governance Evidence / Audit structured check
    gov_evidence = get_governance_evidence(store, task_id)
    assert gov_evidence is not None
    assert gov_evidence["task_id"] == task_id
    assert gov_evidence["provider"] == "adversarial-claude"
    assert gov_evidence["canonical_candidate_sha"] == canonical_sha
    assert gov_evidence["tree_match"] is True
    assert gov_evidence["metadata_validation"]["is_valid"] is True


def test_ordinary_commit_text_mentioning_ai_is_not_rejected() -> None:
    """Ordinary commit message text mentioning Claude/Codex in documentation or subject
    is NOT rejected; only attribution trailers are targeted.
    """
    policy = GitGovernancePolicy()
    validator = CommitMetadataValidator(policy)

    message = (
        "feat(providers): improve Claude and OpenAI Codex integration\n\n"
        "This commit adds support for parsing Claude outputs and Grok classification.\n"
        "It discusses Anthropic and OpenAI APIs without attributing commits to them."
    )
    # Ensure ordinary text extract has no trailers
    from stagemesh.governance import _extract_attribution_trailers
    assert _extract_attribution_trailers(message) == []
