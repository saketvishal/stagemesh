"""Operator-run maintenance tool: scrub AI-provider Git attribution from history.

This is NOT invoked by StageMesh itself. GitHub surfaces `Co-authored-by`
trailers (and similar provider-attribution lines) as additional commit
contributors; this script removes only that metadata from commit messages,
preserving every tree and the parent topology exactly, so repository content
is unchanged and history remains a valid rewrite of the original.

Usage (run manually, from a clean checkout, after StageMesh is idle):

    python scripts/rewrite_history_remove_ai_attribution.py inventory
    python scripts/rewrite_history_remove_ai_attribution.py backup
    python scripts/rewrite_history_remove_ai_attribution.py rewrite --range <old-base>..main
    python scripts/rewrite_history_remove_ai_attribution.py verify --old <ref> --new <ref>

Each step is a separate, explicit command: nothing here force-pushes or
touches remote refs on its own. The operator reviews the SHA mapping and
verification output, then pushes and repairs open PRs by hand.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime, timezone

# Provider names/domains that must never appear as commit attribution.
_PROVIDER_MARKERS = (
    "anthropic",
    "claude",
    "openai",
    "codex",
    "xai",
    "grok",
    "noreply@anthropic.com",
)

_TRAILER_LINE = re.compile(
    r"^(co-authored-by|generated[ -]with|signed-off-by)\s*:.*$",
    re.IGNORECASE,
)
_GENERATED_WITH_LINK = re.compile(r"^\s*🤖?\s*generated with \[.*\].*$", re.IGNORECASE)


def strip_ai_attribution(message: str) -> str:
    """Remove AI-provider attribution lines from a commit message, verbatim otherwise.

    Removes `Co-authored-by:` / `Generated with ...` / `Signed-off-by:` trailer
    lines whose content names an AI provider, plus any resulting run of blank
    trailer lines at the end of the message. Non-provider trailers (a human
    co-author, for instance) are left untouched.
    """
    lines = message.splitlines()
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        is_trailer = bool(_TRAILER_LINE.match(stripped)) or bool(_GENERATED_WITH_LINK.match(stripped))
        if is_trailer and any(marker in stripped.lower() for marker in _PROVIDER_MARKERS):
            continue
        kept.append(line)
    while kept and kept[-1].strip() == "":
        kept.pop()
    return "\n".join(kept) + ("\n" if message.endswith("\n") else "")


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False, **kwargs)


def cmd_inventory(_args: argparse.Namespace) -> int:
    """List open PRs / pushed branches whose merge base could be affected by a rewrite."""
    branches = _run(["git", "branch", "-r"])
    print("Remote branches (rewrite affects any whose merge-base predates the rewrite range):")
    print(branches.stdout)
    gh = _run(["gh", "pr", "list", "--state", "open", "--json", "number,title,headRefName,baseRefName"])
    if gh.returncode == 0:
        print("Open PRs:")
        print(gh.stdout)
    else:
        print("gh unavailable or not authenticated; inventory open PRs manually.", file=sys.stderr)
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    """Create a durable tag pointing at the current tip before any rewrite."""
    tip = _run(["git", "rev-parse", args.ref])
    if tip.returncode != 0:
        print(tip.stderr, file=sys.stderr)
        return 1
    sha = tip.stdout.strip()
    tag = args.tag or f"pre-ai-attribution-cleanup-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    created = _run(["git", "tag", "-a", tag, sha, "-m", f"Backup of {args.ref} ({sha}) before AI-attribution history cleanup"])
    if created.returncode != 0:
        print(created.stderr, file=sys.stderr)
        return 1
    print(f"Backup tag {tag} -> {sha}")
    return 0


def cmd_rewrite(args: argparse.Namespace) -> int:
    """Rewrite commit messages in `range` to strip AI-provider attribution, preserving trees/topology.

    Uses `--partial` because `--refs` scopes this rewrite to less than full
    history: without it, filter-repo treats the run as a fresh-start rewrite
    and prunes the `origin` remote plus any local branches/tags outside
    `range`, which would destroy refs backing other open PRs. `--partial`
    keeps the `origin` remote and leaves out-of-range branches/tags intact.
    """
    filter_repo = _run(["git", "filter-repo", "--version"])
    if filter_repo.returncode != 0:
        print("git-filter-repo is required (pip install git-filter-repo) and was not found.", file=sys.stderr)
        return 1

    script = (
        "import re\n"
        f"_TRAILER_LINE = re.compile(r'{_TRAILER_LINE.pattern}', re.IGNORECASE | re.MULTILINE)\n"
        f"_GENERATED_WITH_LINK = re.compile(r'{_GENERATED_WITH_LINK.pattern}', re.IGNORECASE | re.MULTILINE)\n"
        f"_PROVIDER_MARKERS = {_PROVIDER_MARKERS!r}\n"
        "message = commit.message.decode('utf-8', errors='replace')\n"
        "lines = message.splitlines()\n"
        "kept = []\n"
        "for line in lines:\n"
        "    stripped = line.strip()\n"
        "    is_trailer = bool(_TRAILER_LINE.match(stripped)) or bool(_GENERATED_WITH_LINK.match(stripped))\n"
        "    if is_trailer and any(marker in stripped.lower() for marker in _PROVIDER_MARKERS):\n"
        "        continue\n"
        "    kept.append(line)\n"
        "while kept and kept[-1].strip() == '':\n"
        "    kept.pop()\n"
        "commit.message = ('\\n'.join(kept) + '\\n').encode('utf-8')\n"
    )
    result = _run(
        [
            "git",
            "filter-repo",
            "--force",
            "--partial",
            "--refs",
            args.range,
            "--commit-callback",
            script,
        ]
    )
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        return 1
    print("Rewrite complete. Inspect `git log` output and run the `verify` command before pushing.")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Confirm the rewritten ref is tree-identical, commit-for-commit, to the original."""
    old_commits = _run(["git", "rev-list", "--reverse", args.old]).stdout.split()
    new_commits = _run(["git", "rev-list", "--reverse", args.new]).stdout.split()
    if len(old_commits) != len(new_commits):
        print(f"commit count mismatch: old={len(old_commits)} new={len(new_commits)}", file=sys.stderr)
        return 1
    mismatches = []
    for old_sha, new_sha in zip(old_commits, new_commits):
        old_tree = _run(["git", "rev-parse", f"{old_sha}^{{tree}}"]).stdout.strip()
        new_tree = _run(["git", "rev-parse", f"{new_sha}^{{tree}}"]).stdout.strip()
        if old_tree != new_tree:
            mismatches.append((old_sha, new_sha, old_tree, new_tree))
        print(f"{old_sha} -> {new_sha}")
    if mismatches:
        print(f"TREE MISMATCH in {len(mismatches)} commit(s):", file=sys.stderr)
        for old_sha, new_sha, old_tree, new_tree in mismatches:
            print(f"  {old_sha} (tree {old_tree}) -> {new_sha} (tree {new_tree})", file=sys.stderr)
        return 1
    print(f"OK: {len(old_commits)} commits, all trees identical.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    inv = sub.add_parser("inventory", help="list open PRs / branches a rewrite would affect")
    inv.set_defaults(func=cmd_inventory)

    backup = sub.add_parser("backup", help="tag the current tip before rewriting")
    backup.add_argument("--ref", default="main")
    backup.add_argument("--tag")
    backup.set_defaults(func=cmd_backup)

    rewrite = sub.add_parser("rewrite", help="strip AI-provider attribution from commit messages in range")
    rewrite.add_argument("--range", required=True, help="e.g. <old-base>..main")
    rewrite.set_defaults(func=cmd_rewrite)

    verify = sub.add_parser("verify", help="confirm old and new refs are tree-identical commit-for-commit")
    verify.add_argument("--old", required=True)
    verify.add_argument("--new", required=True)
    verify.set_defaults(func=cmd_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
