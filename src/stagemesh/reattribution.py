"""Re-attribute a candidate whose own commits credit a StageMesh worker, AI provider or placeholder account.

Candidates committed by StageMesh before the identity policy carry `StageMesh <provider> worker ...@stagemesh.invalid` authors. They
cannot be integrated (see integration._attribution_findings) and rebasing preserves authors, so the offence would survive. The fix keeps
every tree and every genuine author, date and subject: only offending commits and their descendants are recreated, authored and
committed as the project owner with tool trailers stripped. The new SHA is a new candidate, so validation and review run again.
"""

from __future__ import annotations

from pathlib import Path

from .autonomy.gitfacts import GitFacts
from .git import GitWorkspace
from .git_identity import (
    GitIdentityError,
    attribution_offences,
    is_ai_or_placeholder_email,
    is_tool_email,
    project_identity,
    sanitize_commit_message,
)

_FIELDS = "%H%x1f%P%x1f%T%x1f%an%x1f%ae%x1f%ad%x1f%cn%x1f%ce%x1f%B%x1e"


def reattribute_range(path: Path, baseline: str, candidate: str) -> str | None:
    """Recreate `baseline..candidate` with offending identities replaced by the owner's; the new head SHA, or None.

    None means nothing to fix or not safely fixable (merge commits, a non-linear range, an unapproved owner identity, a changed tree).
    """
    git = GitWorkspace(path)
    log = git.run(
        "log", "--reverse", "--topo-order", "--date=raw", f"--format={_FIELDS}", f"{baseline}..{candidate}", check=False, encoding="utf-8"
    )
    if log.returncode != 0:
        return None
    commits = [f for f in (r.strip("\n").split("\x1f", 8) for r in log.stdout.split("\x1e")) if len(f) == 9]
    if not commits:
        return None
    try:
        owner = project_identity(path)
    except GitIdentityError:
        return None
    facts = GitFacts(path)
    original_previous = baseline  # the original SHA the next commit must be a child of
    new_parent = baseline  # where the recreated chain currently ends
    changed = False
    for sha, parents, tree, author_name, author_email, author_date, committer_name, committer_email, message in commits:
        if parents.split() != [original_previous]:
            return None  # a merge commit or a range that is not a simple chain
        offended = bool(attribution_offences([(sha, f"{author_name} <{author_email}>", f"{committer_name} <{committer_email}>", message)]))
        if not offended and not changed:
            new_parent = original_previous = sha  # untouched history keeps its exact SHA
            continue
        replace_author = is_tool_email(author_email) or is_ai_or_placeholder_email(author_email)
        env = {
            "GIT_AUTHOR_NAME": owner.name if replace_author else author_name,
            "GIT_AUTHOR_EMAIL": owner.email if replace_author else author_email,
            "GIT_AUTHOR_DATE": author_date,
            "GIT_COMMITTER_NAME": owner.name,
            "GIT_COMMITTER_EMAIL": owner.email,
        }
        new_parent = facts._commit_tree_with_message(  # noqa: SLF001 - the stdin-fed commit-tree the supervisor already uses
            ["commit-tree", tree, "-p", new_parent, "-F", "-"], sanitize_commit_message(message.rstrip("\n")), env
        )
        original_previous = sha
        changed = True
    if not changed:
        return None
    new_tree = git.run("rev-parse", f"{new_parent}^{{tree}}", check=False).stdout.strip()
    old_tree = git.run("rev-parse", f"{candidate}^{{tree}}", check=False).stdout.strip()
    return new_parent if new_tree and new_tree == old_tree else None
