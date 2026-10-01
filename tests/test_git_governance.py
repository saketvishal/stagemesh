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
    TaskWorktreeIsolationError,
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


def test_real_production_path_worktree_isolation(store: Store, repo: tuple[Path, GitWorkspace]) -> None:
    """1. Production worktree isolation: Primary project contains unrelated dirty and untracked files.
    Execution through normal Coordinator path must not leak either unrelated file into canonical candidate,
    and primary checkout must remain unchanged.
    """
    repo_dir, ws = repo
    task_id = store.upsert_task("worktree-isolation-task")
    store.advance_task(task_id, Stage.IMPLEMENT)

    # Dirty primary checkout with an unrelated modification and an untracked file
    unrelated_mod = repo_dir / "README.md"
    unrelated_mod.write_text("# UNRELATED MODIFIED IN PRIMARY CHECKOUT\n", encoding="utf-8")
    unrelated_untracked = repo_dir / "unrelated_untracked.txt"
    unrelated_untracked.write_text("unrelated untracked file in primary\n", encoding="utf-8")

    # Primary checkout is dirty
    primary_status_before = ws.run("status", "--porcelain").stdout
    assert "M README.md" in primary_status_before
    assert "?? unrelated_untracked.txt" in primary_status_before

    # Run through normal Coordinator tick
    coord = Coordinator(store, repo_dir, executor=FakeExecutor())
    assert coord.tick() == 1
    assert store.get_task(task_id)["stage"] == Stage.VALIDATE

    candidate = store.latest_candidate(task_id)
    assert candidate is not None
    canonical_sha = candidate["sha"]

    # Verify canonical candidate files: neither unrelated file must be present or contaminated
    candidate_files = ws.run("ls-tree", "-r", "--name-only", canonical_sha).stdout.splitlines()
    assert "unrelated_untracked.txt" not in candidate_files
    assert f"stagemesh-task-{task_id}.txt" in candidate_files

    # README in candidate must remain at baseline, NOT primary dirty modification
    readme_in_candidate = ws.run("show", f"{canonical_sha}:README.md").stdout
    assert "UNRELATED MODIFIED" not in readme_in_candidate
    assert "Initial baseline" in readme_in_candidate

    # Primary checkout must remain dirty and unchanged
    primary_status_after = ws.run("status", "--porcelain").stdout
    assert "M README.md" in primary_status_after
    assert "?? unrelated_untracked.txt" in primary_status_after


def test_subprocess_executor_structured_result_artifact_exclusion(store: Store, repo: tuple[Path, GitWorkspace]) -> None:
    """2. Fix structured-result artifact contamination: .stagemesh-result-<task_id>.json
    must NEVER become part of the agent result tree.
    """
    import sys
    repo_dir, ws = repo
    task_id = store.upsert_task("structured-result-task")
    store.advance_task(task_id, Stage.IMPLEMENT)
    claim_id = store.acquire_claim(task_id, "worker-sub")

    # Python script executed by SubprocessExecutor
    script = (
        "import os, json, subprocess\n"
        "from pathlib import Path\n"
        "result_path = Path(os.environ['STAGEMESH_RESULT_PATH'])\n"
        "(Path.cwd() / 'feature.py').write_text('def run(): return 42\\n')\n"
        "subprocess.run(['git', 'add', '-A'], check=True)\n"
        "subprocess.run(['git', 'commit', '-m', 'agent temporary commit\\n\\nCo-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>'], check=True)\n"
        "temp_sha = subprocess.check_output(['git', 'rev-parse', 'HEAD']).decode().strip()\n"
        "result_path.write_text(json.dumps({'status': 'SUCCEEDED', 'candidate_sha': temp_sha, 'durable_handoff': True}))\n"
    )

    executor = SubprocessExecutor(command=[sys.executable, "-c", script])
    result = executor.run(store, task_id, claim_id, repo_dir)

    assert result.status is ExecutionStatus.SUCCEEDED
    assert result.candidate_sha is not None

    canonical_sha = result.candidate_sha
    candidate = store.latest_candidate(task_id)
    assert candidate is not None
    assert candidate["sha"] == canonical_sha

    # PROOF 1: .stagemesh-result file is NOT in canonical candidate tree
    candidate_files = ws.run("ls-tree", "-r", "--name-only", canonical_sha).stdout
    assert ".stagemesh-result" not in candidate_files
    assert "feature.py" in candidate_files

    # PROOF 2: Unwanted Claude trailer is absent
    log_text = ws.run("log", "-1", canonical_sha).stdout
    assert "Co-Authored-By" not in log_text
    assert "noreply@anthropic.com" not in log_text

    # PROOF 3: Worktree is clean
    from stagemesh.governance import prepare_task_worktree
    task_wt = prepare_task_worktree(repo_dir, task_id)
    assert GitWorkspace(task_wt).run("status", "--porcelain").stdout.strip() == ""


def test_true_crash_restart_baseline_recovery(store: Store, repo: tuple[Path, GitWorkspace]) -> None:
    """3. True crash/restart test: Persist baseline BEFORE provider execution.
    On restart after crash before candidate persistence, recover original baseline A,
    do not recapture baseline from moved worktree HEAD, and candidate parent remains A.
    """
    repo_dir, ws = repo
    task_id = store.upsert_task("task-crash-recovery")
    from stagemesh.governance import prepare_task_worktree, resolve_or_capture_baseline

    task_wt = prepare_task_worktree(repo_dir, task_id)
    wt_ws = GitWorkspace(task_wt)

    # A. Persist baseline A before provider starts
    exec_id_1 = store.start_execution(task_id=task_id, claim_id=None, kind=ExecutionKind.IMPLEMENTATION)
    baseline_A = resolve_or_capture_baseline(store, wt_ws, task_id, execution_id=exec_id_1)
    assert baseline_A.commit_sha is not None
    assert store.get_baseline(task_id, exec_id_1) is not None

    # B. Provider modifies files and creates 2 temporary commits in task worktree
    (task_wt / "agent_step1.py").write_text("step 1\n", encoding="utf-8")
    wt_ws.run("add", "-A")
    wt_ws.run("commit", "-m", "agent commit 1")
    (task_wt / "agent_step2.py").write_text("step 2\n", encoding="utf-8")
    wt_ws.run("add", "-A")
    wt_ws.run("commit", "-m", "agent commit 2")

    # Worktree HEAD has now moved away from baseline A
    current_wt_head = wt_ws.run("rev-parse", "HEAD").stdout.strip()
    assert current_wt_head != baseline_A.commit_sha

    # C. Produce agent result tree
    agent_tree = capture_agent_result_tree(wt_ws)

    # D. SIMULATE CRASH: Process dies before Candidate persistence (store.add_candidate never called)
    assert store.latest_candidate(task_id) is None

    # E. RECREATE all runtime objects from store (fresh instances, no in-memory baseline)
    fresh_store = Store(store.db_path)
    fresh_wt_ws = GitWorkspace(task_wt)

    # F. RESUME: Recover baseline from persistence
    recovered_baseline = resolve_or_capture_baseline(fresh_store, fresh_wt_ws, task_id)

    # G. PROOF: Original baseline A is recovered, NOT recaptured from current_wt_head
    assert recovered_baseline.commit_sha == baseline_A.commit_sha
    assert recovered_baseline.commit_sha != current_wt_head

    # H. Canonical candidate parent remains baseline A
    exec_id_2 = fresh_store.start_execution(task_id=task_id, claim_id=None, kind=ExecutionKind.IMPLEMENTATION)
    canonical_sha = canonicalize_and_record_candidate(
        store=fresh_store,
        workspace=fresh_wt_ws,
        task_id=task_id,
        execution_id=exec_id_2,
        claim_id=None,
        provider="agent",
        baseline=recovered_baseline,
        agent_result_tree=agent_tree,
    )

    parents = fresh_wt_ws.run("rev-parse", f"{canonical_sha}^@").stdout.splitlines()
    assert parents == [baseline_A.commit_sha]

    # I. No extra parent chain created
    ancestors = fresh_wt_ws.run("rev-list", canonical_sha).stdout.splitlines()
    assert ancestors == [canonical_sha, baseline_A.commit_sha]
    assert current_wt_head not in ancestors
    fresh_store.close()


def test_idempotent_governance_provenance_repair(store: Store, repo: tuple[Path, GitWorkspace]) -> None:
    """4. Make governance provenance repairable/idempotent:
    Candidate persisted -> crash before governance audit -> restart ->
    same candidate reused -> exactly one governance evidence record exists.
    """
    repo_dir, ws = repo
    task_id = store.upsert_task("task-repair-provenance")
    baseline = capture_baseline(ws, task_id)
    exec_id = store.start_execution(task_id=task_id, claim_id=None, kind=ExecutionKind.IMPLEMENTATION)

    (repo_dir / "code.txt").write_text("production code\n", encoding="utf-8")
    agent_tree = capture_agent_result_tree(ws)

    # Create canonical candidate commit manually to simulate: candidate created and persisted,
    # but crash occurred before record_audit("governance.candidate_canonicalized")
    canonicalizer = CandidateCanonicalizer()
    canonical_sha = canonicalizer.canonicalize(ws, baseline, agent_tree, task_id)
    store.add_candidate(task_id, canonical_sha, "agent", durable_handoff=True, base_sha=baseline.commit_sha)

    # Pre-condition: candidate exists, but governance evidence is missing
    assert store.latest_candidate(task_id) is not None
    assert get_governance_evidence(store, task_id) is None

    # Restart / Resume: canonicalize_and_record_candidate is invoked
    reused_sha = canonicalize_and_record_candidate(
        store=store,
        workspace=ws,
        task_id=task_id,
        execution_id=exec_id,
        claim_id=None,
        provider="agent",
        baseline=baseline,
        agent_result_tree=agent_tree,
    )

    # Candidate reused
    assert reused_sha == canonical_sha

    # Evidence repaired idempotently
    evidence = get_governance_evidence(store, task_id)
    assert evidence is not None
    assert evidence["canonical_candidate_sha"] == canonical_sha

    # Call again to verify absolute idempotency: exactly ONE audit record exists
    canonicalize_and_record_candidate(
        store=store,
        workspace=ws,
        task_id=task_id,
        execution_id=exec_id,
        claim_id=None,
        provider="agent",
        baseline=baseline,
        agent_result_tree=agent_tree,
    )

    audit_events = [e for e in store.audit_events(limit=500) if e["event_type"] == "governance.candidate_canonicalized"]
    matching = [e for e in audit_events if json.loads(e["payload"]).get("task_id") == task_id]
    assert len(matching) == 1


def test_runtime_command_adapter_production_governance(store: Store, repo: tuple[Path, GitWorkspace]) -> None:
    """5. Real built-in executor path: RuntimeCommandAdapter must enforce Git Governance."""
    import sys
    from stagemesh.providers import RuntimeCommandAdapter

    repo_dir, ws = repo
    task_id = store.upsert_task("runtime-adapter-task")
    store.advance_task(task_id, Stage.IMPLEMENT)
    claim_id = store.acquire_claim(task_id, "worker-adapter")

    script = (
        "from pathlib import Path\n"
        "(Path.cwd() / 'adapter_output.txt').write_text('adapter code\\n')\n"
    )
    adapter = RuntimeCommandAdapter(name="grok", command=(sys.executable, "-c", script))

    result = adapter.execute(store, task_id, claim_id, repo_dir)
    assert result.status is ExecutionStatus.SUCCEEDED
    assert result.candidate_sha is not None

    canonical_sha = result.candidate_sha
    candidate = store.latest_candidate(task_id)
    assert candidate is not None
    assert candidate["sha"] == canonical_sha

    # Verify StageMesh author identity on canonical candidate
    log_text = ws.run("log", "-1", canonical_sha).stdout
    assert "Author: StageMesh <stagemesh@example.invalid>" in log_text
    assert "adapter_output.txt" in ws.run("ls-tree", "-r", "--name-only", canonical_sha).stdout


def test_adversarial_task_worktree_isolation_fails_closed(
    store: Store,
    repo: tuple[Path, GitWorkspace],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1. Task worktree isolation MUST fail closed:
    Deliberately make worktree creation fail, invoke normal Coordinator implementation path,
    assert provider was never launched, primary checkout unchanged, no candidate exists,
    and failure is surfaced rather than falling back.
    """
    repo_dir, ws = repo
    task_id = store.upsert_task("fail-closed-task")
    store.advance_task(task_id, Stage.IMPLEMENT)

    provider_launched = False

    class TrackingExecutor(Executor):
        name = "tracking"

        def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
            nonlocal provider_launched
            provider_launched = True
            return ExecutionResult(ExecutionStatus.SUCCEEDED, "dummy", durable_handoff=True)

    from stagemesh.worktree import WorktreeValidationError
    import stagemesh.worktree

    def failing_ensure_worktree(*args: object, **kwargs: object) -> None:
        raise WorktreeValidationError("simulated worktree isolation failure")

    monkeypatch.setattr(stagemesh.worktree, "ensure_worktree", failing_ensure_worktree)

    primary_status_before = ws.run("status", "--porcelain").stdout
    primary_head_before = ws.run("rev-parse", "HEAD").stdout.strip()

    coord = Coordinator(store, repo_dir, executor=TrackingExecutor())

    with pytest.raises(TaskWorktreeIsolationError, match="failed to provision isolated task worktree"):
        coord.tick()

    assert provider_launched is False

    primary_status_after = ws.run("status", "--porcelain").stdout
    primary_head_after = ws.run("rev-parse", "HEAD").stdout.strip()
    assert primary_status_after == primary_status_before
    assert primary_head_after == primary_head_before

    assert store.latest_candidate(task_id) is None


def test_real_production_restart_durable_baseline_and_new_execution_id(
    store: Store,
    repo: tuple[Path, GitWorkspace],
) -> None:
    """2. Make baseline durable and task-stable BEFORE provider launch.
    A new execution_id after restart/retry must reuse the already-persisted baseline
    for that task rather than recapturing current task-worktree HEAD.
    Real production restart test:
    1. first execution persists baseline A;
    2. provider starts and moves task-worktree HEAD;
    3. execution dies;
    4. new Coordinator/runtime objects are created;
    5. a NEW execution_id is generated;
    6. real executor path resumes;
    7. it still uses baseline A;
    8. canonical candidate parent is A.
    """
    import sys
    repo_dir, ws = repo
    task_id = store.upsert_task("task-restart-stable-baseline")
    store.advance_task(task_id, Stage.IMPLEMENT)

    primary_head = ws.run("rev-parse", "HEAD").stdout.strip()

    state_file = repo_dir / "simulated_attempt.txt"
    state_file.write_text("1\n", encoding="utf-8")

    script = (
        "import os, sys, subprocess, json\n"
        "from pathlib import Path\n"
        "state_f = Path(r'" + str(state_file).replace("\\", "/") + "')\n"
        "attempt = int(state_f.read_text().strip())\n"
        "(Path.cwd() / 'feature_run.py').write_text(f'# attempt {attempt}\\n')\n"
        "subprocess.run(['git', 'add', '-A'], check=True)\n"
        "subprocess.run(['git', 'commit', '-m', f'agent commit attempt {attempt}'], check=True)\n"
        "if attempt == 1:\n"
        "    state_f.write_text('2\\n')\n"
        "    sys.exit(1)\n"
        "temp_sha = subprocess.check_output(['git', 'rev-parse', 'HEAD']).decode().strip()\n"
        "res_path = Path(os.environ['STAGEMESH_RESULT_PATH'])\n"
        "res_path.write_text(json.dumps({'status': 'SUCCEEDED', 'candidate_sha': temp_sha, 'durable_handoff': True}))\n"
    )

    executor1 = SubprocessExecutor(command=[sys.executable, "-c", script])
    coord1 = Coordinator(store, repo_dir, executor=executor1)

    coord1.tick()

    persisted_base = store.get_baseline(task_id)
    assert persisted_base is not None
    assert persisted_base["commit_sha"] == primary_head
    first_exec_id = persisted_base["execution_id"]

    from stagemesh.governance import prepare_task_worktree
    task_wt = prepare_task_worktree(repo_dir, task_id)
    wt_ws = GitWorkspace(task_wt)
    wt_head_after_crash = wt_ws.run("rev-parse", "HEAD").stdout.strip()
    assert wt_head_after_crash != primary_head

    assert store.latest_candidate(task_id) is None

    fresh_store = Store(store.db_path)
    executor2 = SubprocessExecutor(command=[sys.executable, "-c", script])
    coord2 = Coordinator(fresh_store, repo_dir, executor=executor2)

    fresh_store.advance_task(task_id, Stage.IMPLEMENT)
    progressed = coord2.tick()
    assert progressed == 1

    candidate = fresh_store.latest_candidate(task_id)
    assert candidate is not None
    canonical_sha = candidate["sha"]
    parents = wt_ws.run("rev-parse", f"{canonical_sha}^@").stdout.splitlines()
    assert parents == [primary_head]

    exec_rows = list(
        fresh_store.conn.execute("SELECT * FROM executions WHERE task_id=? ORDER BY started_at ASC", (task_id,))
    )
    assert len(exec_rows) >= 2
    second_exec_id = exec_rows[-1]["id"]
    assert second_exec_id != first_exec_id
    second_base = fresh_store.get_baseline(task_id, second_exec_id)
    assert second_base is not None
    assert second_base["commit_sha"] == primary_head

    fresh_store.close()


def test_crash_recovery_after_canonical_commit_before_candidate_persistence(
    store: Store,
    repo: tuple[Path, GitWorkspace],
) -> None:
    """3. Cover the exact remaining crash window:
    1. baseline A persisted;
    2. agent result tree T exists;
    3. StageMesh canonical commit C is created and task-worktree HEAD points to C;
    4. simulate crash BEFORE Store.add_candidate();
    5. recreate Store/Coordinator/governance objects;
    6. resume;
    7. detect that HEAD C is already a valid StageMesh canonical candidate for:
       - baseline A;
       - tree T;
       - canonical metadata;
    8. reuse C;
    9. persist one candidate row;
    10. create one governance provenance record;
    11. do not create a second canonical commit.
    """
    repo_dir, ws = repo
    task_id = store.upsert_task("task-crash-after-canonical-commit")
    from stagemesh.governance import (
        CandidateCanonicalizer,
        canonicalize_and_record_candidate,
        get_governance_evidence,
        prepare_task_worktree,
        resolve_or_capture_baseline,
    )

    baseline_A = resolve_or_capture_baseline(store, ws, task_id)
    task_wt = prepare_task_worktree(repo_dir, task_id, base_sha=baseline_A.commit_sha)
    wt_ws = GitWorkspace(task_wt)

    (task_wt / "agent_code.py").write_text("def work(): return 100\n", encoding="utf-8")
    wt_ws.run("add", "-A")
    agent_tree_T = wt_ws.run("write-tree").stdout.strip()

    canonicalizer = CandidateCanonicalizer()
    canonical_commit_C = canonicalizer.canonicalize(
        workspace=wt_ws,
        baseline=baseline_A,
        agent_result_tree=agent_tree_T,
        task_id=task_id,
    )
    assert wt_ws.run("rev-parse", "HEAD").stdout.strip() == canonical_commit_C

    # Simulate crash before candidate persistence
    assert store.latest_candidate(task_id) is None
    assert get_governance_evidence(store, task_id, candidate_sha=canonical_commit_C) is None

    fresh_store = Store(store.db_path)
    fresh_wt_ws = GitWorkspace(task_wt)
    exec_id_resume = fresh_store.start_execution(task_id=task_id, claim_id=None, kind=ExecutionKind.IMPLEMENTATION)

    resumed_sha = canonicalize_and_record_candidate(
        store=fresh_store,
        workspace=fresh_wt_ws,
        task_id=task_id,
        execution_id=exec_id_resume,
        claim_id=None,
        provider="agent",
        baseline=baseline_A,
        agent_result_tree=agent_tree_T,
    )

    # 7 & 8: Reuses C
    assert resumed_sha == canonical_commit_C

    # 9: Persist one candidate row
    candidate = fresh_store.latest_candidate(task_id)
    assert candidate is not None
    assert candidate["sha"] == canonical_commit_C
    all_candidates = list(fresh_store.conn.execute("SELECT * FROM candidates WHERE task_id=?", (task_id,)))
    assert len(all_candidates) == 1

    # 10: Exactly one governance provenance record
    gov_evidence = get_governance_evidence(fresh_store, task_id, candidate_sha=canonical_commit_C)
    assert gov_evidence is not None
    assert gov_evidence["canonical_candidate_sha"] == canonical_commit_C
    all_audit = [
        e for e in fresh_store.audit_events(limit=500) if e["event_type"] == "governance.candidate_canonicalized"
    ]
    matching_audit = [
        e for e in all_audit if json.loads(e["payload"]).get("canonical_candidate_sha") == canonical_commit_C
    ]
    assert len(matching_audit) == 1

    # 11: Do not create a second canonical commit
    assert fresh_wt_ws.run("rev-parse", "HEAD").stdout.strip() == canonical_commit_C

    fresh_store.close()


