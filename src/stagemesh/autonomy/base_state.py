"""Base state: how has the integration base moved since the candidate was built, and what is the deterministic response?

Classification (`classify_base`) is a pure function of git facts and never consults an LLM:

* `BASE_UNCHANGED`             the base tip is the commit the candidate was built on.
* `BASE_ADVANCED`              the old base is an ancestor of the new tip: ordinary forward advancement.
* `BASE_HISTORY_REWRITTEN`     the old base is no longer in the new tip's ancestry (force-push, squash-landed dependency, rewound
                               ref). With identical old/new base *trees* the rewrite is content-neutral (`tree_equivalent`).
* `CANDIDATE_ALREADY_INTEGRATED` the new tip already contains the candidate.

Responses (`plan_base_response`) never rewrite or force-push the original candidate. The original is preserved under a StageMesh
ref and a replacement candidate is built on the new base by transplanting only the candidate's own commits (three-way, no
checkout). Replacement candidates are new SHAs, so none of the original's validation or review evidence applies to them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .decisions import Action, AutonomyDecision, Condition, Escalation, EscalationReason
from .gitfacts import GitFacts

POLICY = "base-state/v1"


@dataclass(frozen=True)
class BaseState:
    condition: Condition
    old_base: str | None
    new_base: str | None
    candidate: str
    tree_equivalent: bool | None = None  # old/new base trees identical (None when it does not apply)
    candidate_stale: bool = False  # the candidate does not contain the new base tip
    dependency_landed: bool = False
    detail: str = ""

    def observed(self) -> dict[str, str]:
        out = {"old_base": self.old_base or "unknown", "new_base": self.new_base or "unknown"}
        if self.tree_equivalent is not None:
            out["tree_equivalent"] = "true" if self.tree_equivalent else "false"
        return out


def classify_base(
    facts: GitFacts, *, candidate: str, old_base: str | None, new_base: str | None, dependency_landed: bool = False
) -> BaseState:
    """Classify how `new_base` relates to the base `candidate` was built on. `dependency_landed` marks a stacked PR's base landing."""
    if new_base is None or old_base is None or not facts.exists(new_base):
        return _unrecoverable(old_base, new_base, candidate)
    if facts.is_ancestor(candidate, new_base):
        return BaseState(Condition.CANDIDATE_ALREADY_INTEGRATED, old_base, new_base, candidate)
    if not facts.exists(old_base):
        return _unrecoverable(old_base, new_base, candidate)
    stale = not facts.is_ancestor(new_base, candidate)
    if old_base == new_base:
        return BaseState(Condition.BASE_UNCHANGED, old_base, new_base, candidate, candidate_stale=False)
    if facts.is_ancestor(old_base, new_base):
        return BaseState(Condition.BASE_ADVANCED, old_base, new_base, candidate, candidate_stale=stale)
    old_tree, new_tree = facts.tree(old_base), facts.tree(new_base)
    equivalent = old_tree is not None and old_tree == new_tree
    condition = Condition.BASE_DEPENDENCY_LANDED if dependency_landed else (
        Condition.BASE_HISTORY_REWRITTEN if equivalent else Condition.BASE_HISTORY_REWRITTEN_CONTENT_CHANGED
    )
    return BaseState(
        condition,
        old_base,
        new_base,
        candidate,
        tree_equivalent=equivalent,
        candidate_stale=stale,
        dependency_landed=dependency_landed,
        detail="old base is not an ancestor of the new base tip" if not facts.is_ancestor(old_base, new_base) else "base moved",
    )


def _unrecoverable(old_base: str | None, new_base: str | None, candidate: str) -> BaseState:
    return BaseState(
        Condition.BASE_PROVENANCE_UNRECOVERABLE,
        old_base,
        new_base,
        candidate,
        detail="the recorded base or the current base tip cannot be resolved in the object store",
    )


@dataclass(frozen=True)
class EquivalenceProof:
    """Evidence that the replacement carries exactly the intended work."""

    old_base_tree: str | None
    new_base_tree: str | None
    candidate_tree: str | None
    replacement_tree: str | None
    base_trees_identical: bool
    candidate_trees_identical: bool  # replacement tree == original candidate tree (strongest proof; needs identical base trees)
    original_changed_paths: tuple[str, ...]
    replacement_changed_paths: tuple[str, ...]
    scope_preserved: bool  # the replacement changes only paths the candidate changed (it may change fewer: some may already be on the base)
    patch_id_equal: bool | None  # same diff content relative to each one's own base

    @property
    def proven(self) -> bool:
        """Strongest available proof for the situation: identical trees when bases are identical, otherwise preserved scope."""
        if self.base_trees_identical:
            return self.candidate_trees_identical
        return self.scope_preserved

    def to_dict(self) -> dict[str, object]:
        return {
            "old_base_tree": self.old_base_tree,
            "new_base_tree": self.new_base_tree,
            "candidate_tree": self.candidate_tree,
            "replacement_tree": self.replacement_tree,
            "base_trees_identical": self.base_trees_identical,
            "candidate_trees_identical": self.candidate_trees_identical,
            "scope_preserved": self.scope_preserved,
            "patch_id_equal": self.patch_id_equal,
            "original_changed_paths": list(self.original_changed_paths),
            "replacement_changed_paths": list(self.replacement_changed_paths),
            "proven": self.proven,
        }


@dataclass
class RetargetResult:
    ok: bool
    replacement: str | None = None
    preserved_ref: str | None = None
    replacement_ref: str | None = None
    proof: EquivalenceProof | None = None
    conflicts: tuple[str, ...] = ()
    dropped_commits: tuple[str, ...] = ()
    transplanted: tuple[str, ...] = ()
    reason: str = ""
    refs: dict[str, str] = field(default_factory=dict)
    already_on_base: bool = False  # every change of the candidate is already present on the new base


def _ref_key(task_key: str, sha: str) -> str:
    return f"{task_key}/{sha[:12]}"


def build_replacement_candidate(
    facts: GitFacts, *, task_key: str, candidate: str, old_base: str, new_base: str, reason: str
) -> RetargetResult:
    """Transplant the candidate's own commits (`old_base..candidate`) onto `new_base` as new commits.

    The original candidate is preserved under `refs/stagemesh/preserved/<task>/<sha>` and the replacement is published under
    `refs/stagemesh/candidates/<task>/<sha>`; neither ref can overwrite an existing one. Only linear mechanical transplants are
    accepted: a conflict or a merge commit yields `ok=False` with the conflicting paths so the caller can reconstruct instead.
    """
    own_base = old_base if facts.is_ancestor(old_base, candidate) else facts.merge_base(old_base, candidate)
    if own_base is None:
        return RetargetResult(False, reason="the candidate shares no history with its recorded base; cannot identify its own commits")
    commits = facts.commits_between(own_base, candidate)
    if not commits:
        return RetargetResult(False, reason="the candidate has no commits of its own beyond its base")
    tip = new_base
    dropped: list[str] = []
    transplanted: list[str] = []
    for sha in commits:
        result = facts.transplant(tip, sha)
        if not result.clean or result.tree is None:
            return RetargetResult(False, conflicts=result.conflicts, reason=f"commit {sha[:7]} does not apply mechanically onto {new_base[:7]}")
        if result.tree == facts.tree(tip):
            dropped.append(sha)  # the change is already present on the new base
            continue
        info = facts.commit(sha)
        message = info.message.rstrip("\n") + f"\n\nStageMesh-Retargeted-From: {sha}\nStageMesh-Original-Candidate: {candidate}\n"
        tip = facts.commit_tree(result.tree, tip, message, author=info)
        transplanted.append(sha)
    if tip == new_base:
        return RetargetResult(False, dropped_commits=tuple(dropped), reason="every commit of the candidate is already on the new base", already_on_base=True)

    preserved_ref = f"refs/stagemesh/preserved/{_ref_key(task_key, candidate)}"
    replacement_ref = f"refs/stagemesh/candidates/{_ref_key(task_key, tip)}"
    facts.ensure_ref(preserved_ref, candidate)
    facts.ensure_ref(replacement_ref, tip)
    proof = prove_equivalence(facts, candidate=candidate, old_base=own_base, replacement=tip, new_base=new_base)
    return RetargetResult(
        True,
        replacement=tip,
        preserved_ref=preserved_ref,
        replacement_ref=replacement_ref,
        proof=proof,
        dropped_commits=tuple(dropped),
        transplanted=tuple(transplanted),
        reason=reason,
    )


def prove_equivalence(facts: GitFacts, *, candidate: str, old_base: str, replacement: str, new_base: str) -> EquivalenceProof:
    old_tree, new_tree = facts.tree(old_base), facts.tree(new_base)
    candidate_tree, replacement_tree = facts.tree(candidate), facts.tree(replacement)
    original_paths = tuple(facts.changed_paths(old_base, candidate))
    replacement_paths = tuple(facts.changed_paths(new_base, replacement))
    return EquivalenceProof(
        old_base_tree=old_tree,
        new_base_tree=new_tree,
        candidate_tree=candidate_tree,
        replacement_tree=replacement_tree,
        base_trees_identical=old_tree is not None and old_tree == new_tree,
        candidate_trees_identical=candidate_tree is not None and candidate_tree == replacement_tree,
        original_changed_paths=original_paths,
        replacement_changed_paths=replacement_paths,
        scope_preserved=bool(replacement_paths) and set(replacement_paths) <= set(original_paths),
        patch_id_equal=_patch_ids_equal(facts, old_base, candidate, new_base, replacement),
    )


def _patch_ids_equal(facts: GitFacts, old_base: str, candidate: str, new_base: str, replacement: str) -> bool | None:
    left, right = facts.patch_id(old_base, candidate), facts.patch_id(new_base, replacement)
    return None if left is None or right is None else left == right


def plan_base_response(state: BaseState, *, task_id: str | None, reconstruct_attempts_left: int = 1) -> AutonomyDecision | None:
    """Pure policy: the action implied by a classified base state, before any commit is built. None means nothing to do."""
    shas = {"original_candidate": state.candidate}
    if state.condition is Condition.BASE_UNCHANGED:
        return None
    if state.condition is Condition.CANDIDATE_ALREADY_INTEGRATED:
        return AutonomyDecision(state.condition, POLICY, Action.PROCEED, task_id, state.observed(), shas, {"note": "candidate already on the new base"})
    if state.condition is Condition.BASE_PROVENANCE_UNRECOVERABLE:
        return AutonomyDecision(
            state.condition,
            POLICY,
            Action.ESCALATE_TO_FOUNDER,
            task_id,
            state.observed(),
            shas,
            {"detail": state.detail},
            Escalation(
                EscalationReason.BASE_PROVENANCE_UNRECOVERABLE,
                attempted=("resolved the recorded base and current base tip in the object store", "looked for the candidate's merge-base"),
                why_undeterminable="the commit this candidate was built on no longer exists locally, so its own changes cannot be separated from its history",
                smallest_decision="Confirm the intended base commit (or approve rebuilding the objective from scratch on the current base).",
            ),
        )
    if state.condition is Condition.BASE_ADVANCED:
        action = Action.REFRESH_CANDIDATE if state.candidate_stale else Action.PROCEED
        return AutonomyDecision(state.condition, POLICY, action, task_id, state.observed(), shas, {"candidate_stale": state.candidate_stale})
    # Rewritten history (or a landed dependency): never reuse the original; build a clean replacement on the new base.
    return AutonomyDecision(
        state.condition,
        POLICY,
        Action.CREATE_RETARGETED_CANDIDATE,
        task_id,
        state.observed(),
        shas,
        {"reconstruct_attempts_left": reconstruct_attempts_left},
    )


def conflict_decision(
    state: BaseState, result: RetargetResult, *, task_id: str | None, reconstruct_attempts_left: int
) -> AutonomyDecision:
    """A mechanical transplant failed: reconstruct the work on the new base, or escalate only when that budget is spent."""
    observed = {**state.observed(), "conflicts": ",".join(result.conflicts[:5]) or "none"}
    shas = {"original_candidate": state.candidate}
    if reconstruct_attempts_left > 0:
        return AutonomyDecision(
            state.condition,
            POLICY,
            Action.RECONSTRUCT_ON_NEW_BASE,
            task_id,
            observed,
            shas,
            {"reason": result.reason, "reconstruct_attempts_left": reconstruct_attempts_left},
        )
    return AutonomyDecision(
        state.condition,
        POLICY,
        Action.ESCALATE_TO_FOUNDER,
        task_id,
        observed,
        shas,
        {"reason": result.reason},
        Escalation(
            EscalationReason.CONFLICT_REQUIRES_SEMANTIC_PRODUCT_DECISION,
            attempted=(
                "transplanted the candidate's commits onto the new base mechanically",
                "reconstructed the work on the new base with a fresh implementation attempt",
            ),
            why_undeterminable=f"the candidate's change conflicts with the new base in {', '.join(result.conflicts[:5]) or 'unknown paths'} and both versions are plausible product behavior",
            smallest_decision="Which behavior should win for the conflicting paths: the candidate's change or what is on the base?",
        ),
    )
