"""Reject prohibited commit identities before publication; never rewrite commits."""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from stagemesh.git_identity import attribution_offences

SHA = re.compile(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?\Z")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    ).stdout


def check_commits(repo: Path, revisions: list[str]) -> list[str]:
    # Enumerate hashes separately: arbitrary commit bodies cannot forge record separators.
    hashes = git(repo, "rev-list", *revisions).splitlines()
    findings: list[str] = []
    for sha in hashes:
        author, committer, message = git(
            repo, "show", "-s", "--format=%an <%ae>%x00%cn <%ce>%x00%B", sha,
        ).split("\0", 2)
        findings.extend(attribution_offences([(sha, author, committer, message)]))
    return findings


def check_message(repo: Path, path: Path) -> list[str]:
    return attribution_offences([(
        "pending", git(repo, "var", "GIT_AUTHOR_IDENT").rsplit(">", 1)[0] + ">",
        git(repo, "var", "GIT_COMMITTER_IDENT").rsplit(">", 1)[0] + ">",
        path.read_text(encoding="utf-8"),
    )])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--message-file", type=Path)
    parser.add_argument("--pre-push", metavar="REMOTE")
    args = parser.parse_args(argv)
    try:
        modes = int(args.message_file is not None) + int(args.pre_push is not None) + int(args.base is not None or args.head is not None)
        if modes != 1:
            parser.error("choose --base/--head, --message-file, or --pre-push")
        if args.message_file:
            findings = check_message(args.repo, args.message_file)
        elif args.pre_push:
            findings = []
            for line in sys.stdin:
                _local_ref, head, _remote_ref, base = line.split()
                if not SHA.fullmatch(head) or not SHA.fullmatch(base):
                    raise ValueError("invalid pre-push object ID")
                if set(head) == {"0"}:  # deletion introduces no commits
                    continue
                revisions = [head, "--not", f"--remotes={args.pre_push}"] if set(base) == {"0"} else [f"{base}..{head}"]
                findings.extend(check_commits(args.repo, revisions))
        else:
            if not args.base or not args.head or not SHA.fullmatch(args.base) or not SHA.fullmatch(args.head):
                raise ValueError("--base and --head must be full Git object IDs")
            git(args.repo, "cat-file", "-e", f"{args.base}^{{commit}}")
            git(args.repo, "cat-file", "-e", f"{args.head}^{{commit}}")
            findings = check_commits(args.repo, [f"{args.base}..{args.head}"])
        if findings:
            print("Attribution rejected:\n" + "\n".join(findings), file=sys.stderr)
            print("Correct these unpublished commits; preserve legitimate human contributors.", file=sys.stderr)
            return 1
        print("Attribution policy passed.")
        return 0
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"Attribution check could not complete: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
