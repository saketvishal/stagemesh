"""Candidate provenance (which SHA each piece of evidence is bound to) and workspace ownership (who may move the candidate).

`CandidateProvenance` is read from the durable store: baseline, implementation candidate, validation, review and integration SHAs,
plus the lineage of replacement candidates. Evidence only authorizes integration when validation and review are bound to exactly
the current candidate; a replacement candidate never inherits its original's evidence.

`WorkspaceOwnership` records what the supervisor expects an execution-owned worktree to look like. Any HEAD, tracked-file, candidate
ref or remote ref change that StageMesh did not register is an `EXTERNAL_WORKSPACE_MUTATION`: it is never silently adopted.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..audit import record_audit
from ..domain import EvidenceKind, EvidenceStatus
from ..git import GitError, GitWorkspace
from ..persistence import Store
from .gitfacts import TRUSTED_COMMITTER_EMAILS, GitFacts

LINEAGE_EVENT = "autonomy.candidate_lineage"
OWNERSHIP_EVENT = "autonomy.workspace_ownership"
REVOKED_EVENT = "autonomy.evidence_revoked"


@dataclass(frozen=True)
class CandidateProvenance:
    task_id: str
    baseline_sha: str | None
    candidate_sha: str | None
    validation_sha: str | None  # SHA of the newest PASSED validation evidence (may differ from the candidate: then it is stale)
    review_sha: str | None
    integration_sha: str | None  # the integration ref tip recorded by passed INTEGRATION evidence
    lineage: tuple[str, ...] = ()  # original -> ... -> current candidate, when replacement candidates were created
    revoked: frozenset[str] = frozenset()

    @property
    def original_candidate_sha(self) -> str | None:
        return self.lineage[0] if self.lineage else self.candidate_sha

    def evidence_problems(self, require_review: bool = True) -> list[str]:
        """Why existing evidence cannot authorize integrating the current candidate (empty list means it can)."""
        problems: list[str] = []
        if not self.candidate_sha:
            return ["no candidate"]
        if self.candidate_sha in self.revoked:
            problems.append(f"evidence for candidate {self.candidate_sha[:7]} was revoked (external mutation)")
        if self.validation_sha != self.candidate_sha:
            problems.append(f"validation is bound to {_s(self.validation_sha)}, not candidate {self.candidate_sha[:7]}")
        if require_review and self.review_sha != self.candidate_sha:
            problems.append(f"review is bound to {_s(self.review_sha)}, not candidate {self.candidate_sha[:7]}")
        return problems

    def authorizes_integration(self, require_review: bool = True) -> bool:
        return not self.evidence_problems(require_review)

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "baseline_sha": self.baseline_sha,
            "candidate_sha": self.candidate_sha,
            "validation_sha": self.validation_sha,
            "review_sha": self.review_sha,
            "integration_sha": self.integration_sha,
            "lineage": list(self.lineage),
            "evidence_authorizes_integration": self.authorizes_integration(),
        }


def _s(sha: str | None) -> str:
    return sha[:7] if sha else "none"


def load_provenance(store: Store, task_id: str) -> CandidateProvenance:
    candidate = store.latest_candidate(task_id)
    candidate_sha = str(candidate["sha"]) if candidate is not None else None
    binding = store.contract_binding(task_id, candidate_sha) if candidate_sha else None
    baseline = str(binding["baseline_sha"]) if binding is not None and binding["baseline_sha"] else store.task_baseline(task_id)
    return CandidateProvenance(
        task_id=task_id,
        baseline_sha=baseline,
        candidate_sha=candidate_sha,
        validation_sha=_latest_passed_sha(store, task_id, EvidenceKind.VALIDATION),
        review_sha=_latest_passed_sha(store, task_id, EvidenceKind.REVIEW),
        integration_sha=_integration_sha(store, task_id),
        lineage=candidate_lineage(store, task_id, candidate_sha),
        revoked=revoked_candidates(store, task_id),
    )


def _latest_passed_sha(store: Store, task_id: str, kind: EvidenceKind) -> str | None:
    """The newest PASSED evidence of `kind`; the latest candidate's own evidence wins so stale rows never mask a fresh pass."""
    candidate = store.latest_candidate(task_id)
    if candidate is not None and store.has_evidence(task_id, str(candidate["sha"]), kind, EvidenceStatus.PASSED):
        return str(candidate["sha"])
    row = store.conn.execute(
        "SELECT candidate_sha FROM evidence WHERE task_id=? AND kind=? AND status=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (task_id, kind, EvidenceStatus.PASSED),
    ).fetchone()
    return str(row["candidate_sha"]) if row is not None else None


def _integration_sha(store: Store, task_id: str) -> str | None:
    row = store.conn.execute(
        "SELECT payload FROM evidence WHERE task_id=? AND kind=? AND status=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (task_id, EvidenceKind.INTEGRATION, EvidenceStatus.PASSED),
    ).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row["payload"]).get("integration_ref_after")
    except (TypeError, ValueError):
        return None
    return str(value) if value else None


def record_lineage(store: Store, task_id: str, original: str, replacement: str, reason: str, **detail: object) -> None:
    record_audit(
        store,
        LINEAGE_EVENT,
        {"task_id": task_id, "original_candidate": original, "replacement_candidate": replacement, "reason": reason, **detail},
    )


def candidate_lineage(store: Store, task_id: str, candidate_sha: str | None) -> tuple[str, ...]:
    """Chain original -> replacement -> ... ending at `candidate_sha`; empty when the candidate has no recorded ancestry."""
    if not candidate_sha:
        return ()
    parents: dict[str, str] = {}
    for row in store.conn.execute(
        "SELECT payload FROM audit_events WHERE event_type=? ORDER BY created_at, rowid", (LINEAGE_EVENT,)
    ):
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if payload.get("task_id") == task_id:
            parents[str(payload["replacement_candidate"])] = str(payload["original_candidate"])
    chain = [candidate_sha]
    while chain[-1] in parents and parents[chain[-1]] not in chain:
        chain.append(parents[chain[-1]])
    return tuple(reversed(chain)) if len(chain) > 1 else ()


def is_verified_done(store: Store, task_id: str) -> bool:
    """DONE in the store AND an integration SHA recorded by passed INTEGRATION evidence: the only completion the streak accepts."""
    task = store.get_task(task_id)
    return task is not None and str(task["stage"]) == "DONE" and _integration_sha(store, task_id) is not None


def revoke_evidence(store: Store, task_id: str, candidate_sha: str, reason: str) -> None:
    """Mark the candidate's evidence as no longer authorizing integration. The evidence rows themselves are kept."""
    record_audit(store, REVOKED_EVENT, {"task_id": task_id, "candidate_sha": candidate_sha, "reason": reason})


def revoked_candidates(store: Store, task_id: str) -> frozenset[str]:
    out: set[str] = set()
    for row in store.conn.execute("SELECT payload FROM audit_events WHERE event_type=?", (REVOKED_EVENT,)):
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if payload.get("task_id") == task_id:
            out.add(str(payload["candidate_sha"]))
    return frozenset(out)


# --- workspace ownership ------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkspaceOwnership:
    """What StageMesh expects of an execution-owned worktree, recorded when it hands the worktree to an execution."""

    task_id: str
    worktree: str
    expected_head: str
    candidate_ref: str | None = None  # local branch/ref that carries the candidate, if any
    expected_ref_tip: str | None = None
    remote: str | None = None  # remote name and branch the candidate is published on, if any
    remote_branch: str | None = None
    expected_remote_tip: str | None = None
    owner_execution_id: str | None = None
    trusted_committer_emails: tuple[str, ...] = TRUSTED_COMMITTER_EMAILS
    tracked_fingerprint: str = ""

    def to_dict(self) -> dict[str, object]:
        return {**self.__dict__, "trusted_committer_emails": list(self.trusted_committer_emails)}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> WorkspaceOwnership:
        values = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        values["trusted_committer_emails"] = tuple(values.get("trusted_committer_emails", TRUSTED_COMMITTER_EMAILS))  # type: ignore[arg-type]
        return cls(**values)  # type: ignore[arg-type]


@dataclass(frozen=True)
class Mutation:
    kind: str  # HEAD_MOVED | HEAD_REWRITTEN | TRACKED_FILES_MODIFIED | CANDIDATE_REF_MOVED | REMOTE_REF_MOVED | WORKTREE_MISSING | CANDIDATE_PROVENANCE_MISMATCH
    expected: str
    observed: str
    foreign_commits: tuple[str, ...] = ()
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {**self.__dict__, "foreign_commits": list(self.foreign_commits)}


@dataclass
class OwnershipCheck:
    ownership: WorkspaceOwnership
    mutations: list[Mutation] = field(default_factory=list)
    adopted_owner_advance: str | None = None  # new expected HEAD when only trusted (StageMesh/owner) commits were added

    @property
    def clean(self) -> bool:
        return not self.mutations


def tracked_fingerprint(worktree: Path) -> str:
    """Digest of the worktree's content: HEAD tree, the status of tracked files, and every untracked file (name and content).

    Untracked files matter: `git add -A` would sweep a second writer's file into the next candidate. The fingerprint is refreshed when
    an owned execution ends, so whatever the owner itself left behind is part of the expectation and anything that changes later,
    while the worktree is idle, is external.
    """
    git = GitWorkspace(worktree)
    status = git.run("status", "--porcelain", "--untracked-files=no", check=False).stdout
    untracked = [name for name in git.run("ls-files", "--others", "--exclude-standard", "-z", check=False).stdout.split("\0") if name]
    digest = hashlib.sha256()
    for name in sorted(untracked):
        path = Path(worktree) / name
        digest.update(name.encode("utf-8", "replace") + b"\0")
        try:
            if path.is_symlink():
                digest.update(os.readlink(path).encode("utf-8", "replace"))
            elif path.is_file():
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1 << 20), b""):
                        digest.update(chunk)
        except OSError:
            digest.update(b"<unreadable>")
    return f"{git.run('rev-parse', 'HEAD^{tree}', check=False).stdout.strip()}|{status.strip()}|{digest.hexdigest()[:24]}"


def claim_workspace(
    store: Store,
    task_id: str,
    worktree: Path,
    *,
    candidate_ref: str | None = None,
    remote: str | None = None,
    remote_branch: str | None = None,
    owner_execution_id: str | None = None,
    trusted_committer_emails: tuple[str, ...] = TRUSTED_COMMITTER_EMAILS,
) -> WorkspaceOwnership:
    """Record the expected state of the worktree (and its candidate/remote refs) as of now."""
    facts = GitFacts(worktree)
    head = facts.resolve("HEAD")
    if head is None:
        raise GitError(f"cannot claim {worktree}: no HEAD")
    ownership = WorkspaceOwnership(
        task_id=task_id,
        worktree=str(Path(worktree).resolve()),
        expected_head=head,
        candidate_ref=candidate_ref,
        expected_ref_tip=facts.resolve(candidate_ref) if candidate_ref else None,
        remote=remote,
        remote_branch=remote_branch,
        expected_remote_tip=facts.resolve(f"refs/remotes/{remote}/{remote_branch}") if remote and remote_branch else None,
        owner_execution_id=owner_execution_id,
        trusted_committer_emails=trusted_committer_emails,
        tracked_fingerprint=tracked_fingerprint(Path(worktree)),
    )
    save_ownership(store, ownership)
    return ownership


def save_ownership(store: Store, ownership: WorkspaceOwnership) -> None:
    record_audit(store, OWNERSHIP_EVENT, ownership.to_dict())


def load_ownership(store: Store, task_id: str) -> WorkspaceOwnership | None:
    row = store.conn.execute(
        "SELECT payload FROM audit_events WHERE event_type=? AND json_extract(payload, '$.task_id')=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (OWNERSHIP_EVENT, task_id),
    ).fetchone()
    if row is None:
        return None
    return WorkspaceOwnership.from_dict(json.loads(row["payload"]))


def check_ownership(
    ownership: WorkspaceOwnership,
    *,
    fetch: bool = False,
    recorded_candidate: str | None = None,
    execution_running: bool = False,
) -> OwnershipCheck:
    """Compare the world with what was recorded. Never adopts a commit that its committer identity does not vouch for.

    Commits added on top of the expected HEAD whose committer is a trusted identity (StageMesh itself, or the owning provider) are a
    legitimate owner advance and are reported as `adopted_owner_advance`. Anything else - another committer, a non-fast-forward
    move, a moved candidate ref or remote branch, or tracked-file edits while no execution owns the worktree - is a mutation.
    """
    result = OwnershipCheck(ownership)
    worktree = Path(ownership.worktree)
    if not (worktree / ".git").exists():
        result.mutations.append(Mutation("WORKTREE_MISSING", ownership.worktree, "absent"))
        return result
    facts = GitFacts(worktree)
    head = facts.resolve("HEAD")
    if head != ownership.expected_head:
        result.mutations.extend(_head_mutations(facts, ownership, head))
        if not result.mutations:
            result.adopted_owner_advance = head
    if ownership.candidate_ref and ownership.expected_ref_tip:
        tip = facts.resolve(ownership.candidate_ref)
        if tip != ownership.expected_ref_tip:
            # Unlike HEAD (which StageMesh itself advances), a candidate ref only moves by registration: any other move is a mutation,
            # whoever committed it (`foreign` lists the untrusted commits purely as information).
            foreign = _foreign_commits(facts, ownership, ownership.expected_ref_tip, tip)
            result.mutations.append(
                Mutation("CANDIDATE_REF_MOVED", ownership.expected_ref_tip, tip or "missing", tuple(foreign), ownership.candidate_ref)
            )
    if ownership.remote and ownership.remote_branch and ownership.expected_remote_tip:
        if fetch:
            facts.fetch_branch(ownership.remote, ownership.remote_branch)
        tip = facts.resolve(f"refs/remotes/{ownership.remote}/{ownership.remote_branch}")
        if tip != ownership.expected_remote_tip:
            foreign = _foreign_commits(facts, ownership, ownership.expected_remote_tip, tip)
            result.mutations.append(
                Mutation(
                    "REMOTE_REF_MOVED",
                    ownership.expected_remote_tip,
                    tip or "missing",
                    tuple(foreign),
                    f"{ownership.remote}/{ownership.remote_branch}",
                )
            )
    if not execution_running and not result.mutations and ownership.tracked_fingerprint:
        current = tracked_fingerprint(worktree)
        if head == ownership.expected_head and current != ownership.tracked_fingerprint:
            result.mutations.append(Mutation("TRACKED_FILES_MODIFIED", ownership.tracked_fingerprint, current))
    mismatch = bool(recorded_candidate and head and head != recorded_candidate)
    if mismatch and not result.mutations and not execution_running and result.adopted_owner_advance is None and ownership.expected_head != recorded_candidate:
        result.mutations.append(Mutation("CANDIDATE_PROVENANCE_MISMATCH", str(recorded_candidate), str(head)))
    return result


def _head_mutations(facts: GitFacts, ownership: WorkspaceOwnership, head: str | None) -> list[Mutation]:
    if head is None:
        return [Mutation("HEAD_REWRITTEN", ownership.expected_head, "missing")]
    if not facts.exists(ownership.expected_head) or not facts.is_ancestor(ownership.expected_head, head):
        return [Mutation("HEAD_REWRITTEN", ownership.expected_head, head, detail="HEAD is not a descendant of the expected commit")]
    foreign = _foreign_commits(facts, ownership, ownership.expected_head, head)
    if foreign:
        return [Mutation("HEAD_MOVED", ownership.expected_head, head, tuple(foreign))]
    return []


def _foreign_commits(facts: GitFacts, ownership: WorkspaceOwnership, old: str, new: str | None) -> list[str]:
    """Commits in old..new not committed by a trusted identity; `[]` when all are trusted; the new tip when it is not a descendant."""
    if new is None or not facts.exists(old) or not facts.is_ancestor(old, new):
        return [new or "missing"]
    trusted = {email.casefold() for email in ownership.trusted_committer_emails}
    return [
        sha for sha in facts.commits_between(old, new) if facts.commit(sha).committer_email.casefold() not in trusted
    ]
