"""Is a task's recorded baseline stale relative to the integration ref?

The baseline is the SHA every candidate diff is measured against. It is recorded once, when the task first runs. If another task
integrates in the meantime and the candidate is then built on the newer integration tip, `git diff baseline candidate` contains the
other task's files too, and the contract's scope check blames this task for them. The candidate's own change is its diff against the
merge-base with the integration ref, so that is the baseline a rebaseline moves to. Read-only: nothing here writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import changed_files
from .git import GitError, GitWorkspace
from .persistence import Store


@dataclass
class BaselineAnalysis:
    task_id: str
    integration_ref: str
    candidate_sha: str | None = None
    baseline_sha: str | None = None
    integration_sha: str | None = None
    merge_base: str | None = None
    stale: bool = False
    refusal: str | None = None  # why the baseline cannot be safely recomputed (code), when it cannot
    detail: str = ""
    changed_before: list[str] = field(default_factory=list)
    changed_after: list[str] = field(default_factory=list)

    @property
    def unrelated(self) -> list[str]:
        """Files that appear in the candidate's diff only because the baseline is stale."""
        after = set(self.changed_after)
        return [p for p in self.changed_before if p not in after]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "integration_ref": self.integration_ref,
            "integration_sha": self.integration_sha,
            "candidate_sha": self.candidate_sha,
            "baseline_sha": self.baseline_sha,
            "proposed_baseline_sha": self.merge_base,
            "stale": self.stale,
            "refusal": self.refusal,
            "detail": self.detail,
            "changed_files_before": self.changed_before,
            "changed_files_after": self.changed_after,
            "unrelated_files": self.unrelated,
        }


def resolve_integration_ref(project: Path, configured: str | None = None) -> str | None:
    if configured:
        return configured
    out = GitWorkspace(project).run("symbolic-ref", "-q", "HEAD", check=False).stdout.strip()
    return out or None


def analyze_baseline(store: Store, project: Path, task_id: str, integration_ref: str) -> BaselineAnalysis:
    analysis = BaselineAnalysis(task_id, integration_ref)
    candidate = store.latest_candidate(task_id)
    baseline = store.task_baseline(task_id)
    if candidate is None:
        analysis.refusal, analysis.detail = "no_candidate", "task has no candidate to measure a baseline against"
        return analysis
    if baseline is None:
        analysis.refusal, analysis.detail = "no_baseline", "task has no recorded baseline"
        return analysis
    analysis.candidate_sha, analysis.baseline_sha = str(candidate["sha"]), baseline
    git = GitWorkspace(project)
    try:
        analysis.integration_sha = git.run("rev-parse", "--verify", f"{integration_ref}^{{commit}}").stdout.strip()
        git.run("cat-file", "-e", f"{analysis.candidate_sha}^{{commit}}")
        git.run("cat-file", "-e", f"{baseline}^{{commit}}")
        analysis.changed_before = changed_files(project, analysis.candidate_sha, baseline)
    except (GitError, OSError) as exc:
        analysis.refusal, analysis.detail = "unresolvable", f"cannot resolve refs: {str(exc).strip()[:200]}"
        return analysis
    if git.run("merge-base", "--is-ancestor", analysis.candidate_sha, analysis.integration_sha, check=False).returncode == 0:
        analysis.refusal = "candidate_already_integrated"
        analysis.detail = f"candidate is already contained in {integration_ref}; its diff against any newer baseline would be empty"
        return analysis
    if git.run("merge-base", "--is-ancestor", baseline, analysis.candidate_sha, check=False).returncode != 0:
        analysis.refusal = "candidate_not_based_on_baseline"
        analysis.detail = "the candidate does not descend from the recorded baseline; the right baseline is ambiguous"
        return analysis
    base = git.run("merge-base", analysis.candidate_sha, analysis.integration_sha, check=False)
    merge_base = base.stdout.strip()
    if base.returncode != 0 or not merge_base:
        analysis.refusal, analysis.detail = "no_merge_base", "the candidate shares no history with the integration ref"
        return analysis
    analysis.merge_base = merge_base
    if merge_base == baseline:
        analysis.detail = "baseline already equals the candidate's merge-base with the integration ref"
        analysis.changed_after = list(analysis.changed_before)
        return analysis
    if git.run("merge-base", "--is-ancestor", baseline, merge_base, check=False).returncode != 0:
        analysis.refusal = "baseline_diverged"
        analysis.detail = "the recorded baseline is not an ancestor of the candidate's merge-base with the integration ref"
        return analysis
    try:
        analysis.changed_after = changed_files(project, analysis.candidate_sha, merge_base)
    except (GitError, OSError) as exc:
        analysis.refusal, analysis.detail = "unresolvable", f"cannot diff against the merge-base: {str(exc).strip()[:200]}"
        return analysis
    analysis.stale = bool(analysis.unrelated)
    analysis.detail = (
        f"{len(analysis.unrelated)} file(s) in the candidate diff were integrated by other work after the baseline"
        if analysis.stale
        else "the baseline is behind the integration ref but the candidate diff is unaffected"
    )
    return analysis
