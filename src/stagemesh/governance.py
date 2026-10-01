from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import time
from typing import Any

from .audit import record_audit
from .git import GitWorkspace
from .persistence import Store, StoreValidationError


class GovernanceError(RuntimeError):
    """Base error for StageMesh Git governance failures."""


class GovernanceValidationError(GovernanceError, ValueError):
    """Raised when governance validation bounds or contracts fail."""


class GovernanceExactTreeMismatchError(GovernanceValidationError):
    """Raised when canonical candidate tree does not equal agent result tree."""


class GovernanceBaselineMismatchError(GovernanceValidationError):
    """Raised when baseline commit/parent unexpectedly moves or mismatches."""


class GovernanceAttributionError(GovernanceValidationError):
    """Raised when prohibited or unauthorized attribution trailers are detected."""


def _validate_non_empty_str(value: str, field_name: str, max_length: int = 200) -> str:
    if not isinstance(value, str):
        raise GovernanceValidationError(f"{field_name} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise GovernanceValidationError(f"{field_name} must be a non-empty string")
    if len(cleaned) > max_length:
        raise GovernanceValidationError(f"{field_name} must be {max_length} characters or fewer")
    return cleaned


@dataclass(frozen=True)
class GitGovernancePolicy:
    """Explicit governance policy contract controlling Git commit creation and metadata."""
    canonical_author_name: str = "StageMesh"
    canonical_author_email: str = "stagemesh@example.invalid"
    canonical_committer_name: str = "StageMesh"
    canonical_committer_email: str = "stagemesh@example.invalid"
    prohibit_ai_provider_trailers: bool = True
    allow_human_coauthors: bool = False
    allowed_human_coauthors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.canonical_author_name, "canonical author name")
        _validate_non_empty_str(self.canonical_author_email, "canonical author email")
        _validate_non_empty_str(self.canonical_committer_name, "canonical committer name")
        _validate_non_empty_str(self.canonical_committer_email, "canonical committer email")
        if not isinstance(self.prohibit_ai_provider_trailers, bool):
            raise GovernanceValidationError("prohibit_ai_provider_trailers must be a boolean")
        if not isinstance(self.allow_human_coauthors, bool):
            raise GovernanceValidationError("allow_human_coauthors must be a boolean")
        if not isinstance(self.allowed_human_coauthors, tuple):
            raise GovernanceValidationError("allowed_human_coauthors must be a tuple")
        for author in self.allowed_human_coauthors:
            _validate_non_empty_str(author, "allowed human coauthor")


def default_governance_policy() -> GitGovernancePolicy:
    return GitGovernancePolicy()


@dataclass(frozen=True)
class TaskBaseline:
    """Captured immutable baseline for a task before coding agent execution begins."""
    task_id: str
    commit_sha: str | None
    tree_sha: str | None
    branch: str | None = None
    repo_path: str | None = None


def capture_baseline(project: Path | GitWorkspace, task_id: str) -> TaskBaseline:
    """Capture the baseline commit and tree SHA before handing work to a coding agent."""
    task_id = _validate_non_empty_str(task_id, "task id")
    ws = project if isinstance(project, GitWorkspace) else GitWorkspace(project)
    ws.init_if_needed()

    probe = ws.run("rev-parse", "--verify", "--quiet", "HEAD", check=False)
    if probe.returncode == 0 and probe.stdout.strip():
        commit_sha = probe.stdout.strip()
        tree_sha = ws.run("rev-parse", "--verify", "HEAD^{tree}").stdout.strip()
        branch_probe = ws.run("rev-parse", "--abbrev-ref", "HEAD", check=False)
        branch = branch_probe.stdout.strip() if branch_probe.returncode == 0 else None
        if branch == "HEAD":
            branch = None
    else:
        commit_sha = None
        tree_sha = None
        branch = None

    return TaskBaseline(
        task_id=task_id,
        commit_sha=commit_sha,
        tree_sha=tree_sha,
        branch=branch,
        repo_path=str(ws.path),
    )


def resolve_or_capture_baseline(
    store: Store,
    project: Path | GitWorkspace,
    task_id: str,
    execution_id: str | None = None,
) -> TaskBaseline:
    """Recover previously persisted baseline on restart, or capture and persist new baseline."""
    persisted = store.get_baseline(task_id, execution_id=execution_id)
    if persisted:
        return TaskBaseline(
            task_id=task_id,
            commit_sha=persisted["commit_sha"],
            tree_sha=persisted["tree_sha"],
            branch=persisted["branch"],
            repo_path=persisted["repo_path"],
        )
    baseline = capture_baseline(project, task_id)
    if execution_id:
        store.record_baseline(
            task_id=task_id,
            execution_id=execution_id,
            commit_sha=baseline.commit_sha,
            tree_sha=baseline.tree_sha,
            branch=baseline.branch,
            repo_path=baseline.repo_path,
        )
    return baseline


def prepare_task_worktree(project: Path, task_id: str, base_sha: str | None = None) -> Path:
    """Prepare a dedicated StageMesh task worktree isolated from the primary project checkout."""
    from .worktree import ensure_worktree, task_branch_name, is_git_worktree
    ws_path = Path(project).resolve()
    if is_git_worktree(ws_path) and ".stagemesh" in str(ws_path):
        return ws_path

    git_dir = ws_path / ".git"
    if not git_dir.exists():
        return ws_path

    # Ensure .stagemesh/ is in .git/info/exclude of primary repo
    try:
        exclude = (git_dir if git_dir.is_dir() else ws_path) / "info" / "exclude"
        if exclude.parent.exists():
            content = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
            if ".stagemesh/" not in content:
                exclude.write_text(content + "\n.stagemesh/\n", encoding="utf-8")
    except Exception:
        pass

    safe_name = re.sub(r"[^A-Za-z0-9._-]", "-", task_id).strip("./-") or "task"
    worktree_path = ws_path / ".stagemesh" / "worktrees" / safe_name
    try:
        wt = ensure_worktree(
            worktree_path,
            repo_root=ws_path,
            branch_name=task_branch_name(task_id),
            base_sha=base_sha,
        )
        return wt or ws_path
    except Exception:
        return ws_path


def capture_agent_result_tree(workspace: GitWorkspace, agent_candidate_sha: str | None = None) -> str:
    """Determine the actual final repository tree resulting from the agent's work."""
    workspace.init_if_needed()

    # Explicitly remove StageMesh control artifacts before staging
    for ctrl in list(workspace.path.glob(".stagemesh-result-*.json")):
        ctrl.unlink(missing_ok=True)
    workspace.run("rm", "--cached", "--ignore-unmatch", ".stagemesh-result-*.json", check=False)

    # Stage any worktree changes made by the agent
    workspace.run("add", "-A")
    write_res = workspace.run("write-tree", check=False)
    tree_sha = write_res.stdout.strip() if write_res.returncode == 0 else ""

    if not _is_valid_sha(tree_sha):
        if agent_candidate_sha and _is_valid_sha(agent_candidate_sha):
            rev_res = workspace.run("rev-parse", "--verify", f"{agent_candidate_sha}^{{tree}}", check=False)
            if rev_res.returncode == 0:
                tree_sha = rev_res.stdout.strip()

    if not _is_valid_sha(tree_sha):
        raise GovernanceValidationError(
            f"could not deterministically resolve agent result tree: {write_res.stderr or write_res.stdout}"
        )

    return tree_sha


@dataclass(frozen=True)
class MetadataValidationResult:
    is_valid: bool
    author_valid: bool
    committer_valid: bool
    parent_valid: bool
    tree_valid: bool
    trailers_valid: bool
    details: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "is_valid": self.is_valid,
            "author_valid": self.author_valid,
            "committer_valid": self.committer_valid,
            "parent_valid": self.parent_valid,
            "tree_valid": self.tree_valid,
            "trailers_valid": self.trailers_valid,
            "details": self.details,
        }


class CommitMetadataValidator:
    """Validates that a canonical candidate commit complies strictly with GitGovernancePolicy."""

    def __init__(self, policy: GitGovernancePolicy | None = None) -> None:
        self.policy = policy or GitGovernancePolicy()

    def validate(
        self,
        workspace: GitWorkspace,
        commit_sha: str,
        expected_tree_sha: str,
        expected_parent_sha: str | None,
    ) -> MetadataValidationResult:
        if not _is_valid_sha(commit_sha):
            raise GovernanceValidationError(f"invalid commit SHA: {commit_sha}")
        if not _is_valid_sha(expected_tree_sha):
            raise GovernanceValidationError(f"invalid expected tree SHA: {expected_tree_sha}")
        if expected_parent_sha is not None and not _is_valid_sha(expected_parent_sha):
            raise GovernanceValidationError(f"invalid expected parent SHA: {expected_parent_sha}")

        # Extract commit fields using null-byte delimiters
        raw = workspace.run(
            "log", "-1", "--format=%an%x00%ae%x00%cn%x00%ce%x00%T%x00%P%x00%B", commit_sha
        ).stdout
        parts = raw.split("\x00", 6)
        if len(parts) < 7:
            raise GovernanceValidationError("failed to extract commit metadata from git log")

        author_name, author_email, committer_name, committer_email, tree_sha, parents_raw, message = parts
        parents = parents_raw.split() if parents_raw.strip() else []

        author_valid = (
            author_name == self.policy.canonical_author_name
            and author_email == self.policy.canonical_author_email
        )
        if not author_valid:
            raise GovernanceAttributionError(
                f"canonical author mismatch: expected '{self.policy.canonical_author_name} <{self.policy.canonical_author_email}>', "
                f"got '{author_name} <{author_email}>'"
            )

        committer_valid = (
            committer_name == self.policy.canonical_committer_name
            and committer_email == self.policy.canonical_committer_email
        )
        if not committer_valid:
            raise GovernanceAttributionError(
                f"canonical committer mismatch: expected '{self.policy.canonical_committer_name} <{self.policy.canonical_committer_email}>', "
                f"got '{committer_name} <{committer_email}>'"
            )

        tree_valid = tree_sha == expected_tree_sha
        if not tree_valid:
            raise GovernanceExactTreeMismatchError(
                f"canonical tree mismatch: expected '{expected_tree_sha}', got '{tree_sha}'"
            )

        if expected_parent_sha:
            parent_valid = parents == [expected_parent_sha]
            if not parent_valid:
                raise GovernanceBaselineMismatchError(
                    f"canonical parent mismatch: expected ['{expected_parent_sha}'], got {parents}"
                )
        else:
            parent_valid = len(parents) == 0
            if not parent_valid:
                raise GovernanceBaselineMismatchError(
                    f"canonical root commit expected no parents, got {parents}"
                )

        # Trailer validation
        trailers = _extract_attribution_trailers(message)
        if not self.policy.allow_human_coauthors and trailers:
            raise GovernanceAttributionError(
                f"co-author trailers are prohibited on canonical candidates: {trailers[0]}"
            )

        if self.policy.allow_human_coauthors:
            for trailer_val in trailers:
                if self.policy.prohibit_ai_provider_trailers and _is_ai_provider_trailer(trailer_val):
                    raise GovernanceAttributionError(
                        f"prohibited AI/provider attribution trailer detected: {trailer_val}"
                    )
                if self.policy.allowed_human_coauthors and trailer_val not in self.policy.allowed_human_coauthors:
                    raise GovernanceAttributionError(
                        f"unauthorized co-author trailer detected: {trailer_val}"
                    )

        trailers_valid = True
        return MetadataValidationResult(
            is_valid=True,
            author_valid=author_valid,
            committer_valid=committer_valid,
            parent_valid=parent_valid,
            tree_valid=tree_valid,
            trailers_valid=trailers_valid,
            details={
                "commit_sha": commit_sha,
                "tree_sha": tree_sha,
                "parents": parents,
                "author": f"{author_name} <{author_email}>",
                "committer": f"{committer_name} <{committer_email}>",
                "trailers": trailers,
            },
        )


class CandidateCanonicalizer:
    """Creates StageMesh-controlled canonical candidate commits from agent-produced trees."""

    def __init__(self, policy: GitGovernancePolicy | None = None) -> None:
        self.policy = policy or GitGovernancePolicy()
        self.validator = CommitMetadataValidator(self.policy)

    def canonicalize(
        self,
        workspace: GitWorkspace,
        baseline: TaskBaseline,
        agent_result_tree: str,
        task_id: str,
        *,
        message: str | None = None,
        author_date: str | None = None,
        committer_date: str | None = None,
    ) -> str:
        task_id = _validate_non_empty_str(task_id, "task id")
        if not _is_valid_sha(agent_result_tree):
            raise GovernanceExactTreeMismatchError(f"invalid agent result tree SHA: {agent_result_tree}")

        # Verify agent_result_tree is a valid tree object
        tree_probe = workspace.run("cat-file", "-t", agent_result_tree, check=False)
        if tree_probe.returncode != 0 or tree_probe.stdout.strip() != "tree":
            raise GovernanceExactTreeMismatchError(
                f"agent result tree '{agent_result_tree}' is not a valid git tree object: {tree_probe.stderr or tree_probe.stdout}"
            )

        if baseline.commit_sha:
            probe = workspace.run("rev-parse", "--verify", "--quiet", f"{baseline.commit_sha}^{{commit}}", check=False)
            if probe.returncode != 0:
                raise GovernanceBaselineMismatchError(
                    f"baseline commit {baseline.commit_sha} no longer exists in repository"
                )

        # Build StageMesh deterministic message
        canonical_message = message or f"feat({task_id}): canonical implementation for {task_id}"
        if self.policy.allow_human_coauthors and self.policy.allowed_human_coauthors:
            for author in self.policy.allowed_human_coauthors:
                canonical_message += f"\n\nCo-Authored-By: {author}"

        # Create the canonical commit object using git commit-tree
        args = ["commit-tree", agent_result_tree]
        if baseline.commit_sha:
            args.extend(["-p", baseline.commit_sha])
        args.extend(["-m", canonical_message])

        env: dict[str, str] = {
            "GIT_AUTHOR_NAME": self.policy.canonical_author_name,
            "GIT_AUTHOR_EMAIL": self.policy.canonical_author_email,
            "GIT_COMMITTER_NAME": self.policy.canonical_committer_name,
            "GIT_COMMITTER_EMAIL": self.policy.canonical_committer_email,
        }
        if author_date:
            env["GIT_AUTHOR_DATE"] = author_date
        if committer_date:
            env["GIT_COMMITTER_DATE"] = committer_date

        try:
            commit_res = workspace.run(*args, env=env)
        except Exception as exc:
            raise GovernanceValidationError(f"commit-tree failed: {exc}") from exc

        canonical_sha = commit_res.stdout.strip()
        if not _is_valid_sha(canonical_sha):
            raise GovernanceValidationError(
                f"commit-tree failed to return valid SHA: {commit_res.stderr or commit_res.stdout}"
            )

        # MANDATORY Exact-Tree Invariant
        candidate_tree = workspace.run("rev-parse", f"{canonical_sha}^{{tree}}").stdout.strip()
        if candidate_tree != agent_result_tree:
            raise GovernanceExactTreeMismatchError(
                f"canonical candidate tree {candidate_tree} != agent result tree {agent_result_tree}"
            )

        # MANDATORY Metadata Validation
        self.validator.validate(
            workspace=workspace,
            commit_sha=canonical_sha,
            expected_tree_sha=agent_result_tree,
            expected_parent_sha=baseline.commit_sha,
        )

        # Align worktree HEAD to the canonical candidate commit and ensure clean index
        workspace.run("update-ref", "HEAD", canonical_sha)
        workspace.run("reset", "--mixed", canonical_sha, check=False)

        return canonical_sha


def canonicalize_and_record_candidate(
    store: Store,
    workspace: GitWorkspace,
    task_id: str,
    execution_id: str,
    claim_id: str | None,
    provider: str,
    baseline: TaskBaseline,
    agent_result_tree: str,
    *,
    policy: GitGovernancePolicy | None = None,
    durable_handoff: bool = True,
    agent_candidate_sha: str | None = None,
) -> str:
    """Canonicalize candidate with crash-recovery / idempotency and record internal provenance audit."""
    policy = policy or GitGovernancePolicy()
    validator = CommitMetadataValidator(policy)

    # 1. Idempotency check: see if a valid canonical candidate for this task & baseline already exists
    existing = store.latest_candidate(task_id)
    if existing and existing["base_sha"] == baseline.commit_sha:
        existing_sha = str(existing["sha"])
        if _is_valid_sha(existing_sha):
            tree_probe = workspace.run("rev-parse", "--verify", "--quiet", f"{existing_sha}^{{tree}}", check=False)
            if tree_probe.returncode == 0 and tree_probe.stdout.strip() == agent_result_tree:
                try:
                    val_result = validator.validate(
                        workspace=workspace,
                        commit_sha=existing_sha,
                        expected_tree_sha=agent_result_tree,
                        expected_parent_sha=baseline.commit_sha,
                    )
                    workspace.run("update-ref", "HEAD", existing_sha)
                    workspace.run("reset", "--mixed", existing_sha, check=False)
                    # Provenance repair: if governance audit evidence was missing due to crash, recreate it idempotently
                    if not get_governance_evidence(store, task_id):
                        record_audit(
                            store,
                            "governance.candidate_canonicalized",
                            {
                                "task_id": task_id,
                                "execution_id": execution_id,
                                "provider": provider,
                                "agent_candidate_sha": agent_candidate_sha,
                                "baseline_sha": baseline.commit_sha,
                                "baseline_tree_sha": baseline.tree_sha,
                                "agent_result_tree_sha": agent_result_tree,
                                "canonical_candidate_sha": existing_sha,
                                "canonical_candidate_tree_sha": agent_result_tree,
                                "tree_match": True,
                                "metadata_validation": val_result.to_dict(),
                                "timestamp": time.time(),
                            },
                        )
                    return existing_sha
                except GovernanceValidationError:
                    pass

    # 2. Canonicalize fresh candidate
    canonicalizer = CandidateCanonicalizer(policy)
    canonical_sha = canonicalizer.canonicalize(
        workspace=workspace,
        baseline=baseline,
        agent_result_tree=agent_result_tree,
        task_id=task_id,
    )

    # 3. Store candidate (public publishable candidate is StageMesh-controlled)
    store.add_candidate(
        task_id=task_id,
        sha=canonical_sha,
        produced_by=provider,
        durable_handoff=durable_handoff,
        base_sha=baseline.commit_sha,
    )

    # 4. Record internal provenance audit (agent identity stays internal)
    val_result = validator.validate(
        workspace=workspace,
        commit_sha=canonical_sha,
        expected_tree_sha=agent_result_tree,
        expected_parent_sha=baseline.commit_sha,
    )

    record_audit(
        store,
        "governance.candidate_canonicalized",
        {
            "task_id": task_id,
            "execution_id": execution_id,
            "provider": provider,
            "agent_candidate_sha": agent_candidate_sha,
            "baseline_sha": baseline.commit_sha,
            "baseline_tree_sha": baseline.tree_sha,
            "agent_result_tree_sha": agent_result_tree,
            "canonical_candidate_sha": canonical_sha,
            "canonical_candidate_tree_sha": agent_result_tree,
            "tree_match": True,
            "metadata_validation": val_result.to_dict(),
            "timestamp": time.time(),
        },
    )

    return canonical_sha


def get_governance_evidence(store: Store, task_id: str) -> dict[str, Any] | None:
    """Retrieve structured Git Governance evidence record for a task."""
    for event in store.audit_events(limit=500):
        if event["event_type"] == "governance.candidate_canonicalized":
            try:
                payload = json.loads(event["payload"])
                if payload.get("task_id") == task_id:
                    return payload
            except (json.JSONDecodeError, TypeError):
                continue
    return None


def _is_valid_sha(val: str | None) -> bool:
    return bool(val and len(val) == 40 and all(c in "0123456789abcdefABCDEF" for c in val))


def _extract_attribution_trailers(message: str) -> list[str]:
    trailers: list[str] = []
    for line in message.splitlines():
        match = re.match(r"(?i)^\s*(?:co-authored-by|co-author|assisted-by|ai-assisted-by)\s*:\s*(.+)$", line.strip())
        if match:
            trailers.append(match.group(1).strip())
    return trailers


def _is_ai_provider_trailer(val: str) -> bool:
    lower = val.lower()
    markers = [
        "claude", "anthropic", "openai", "codex", "grok",
        "copilot", "cursor", "antigravity", "gemini", "chatgpt",
        "noreply@anthropic.com", "codex@openai.com",
    ]
    return any(m in lower for m in markers)
