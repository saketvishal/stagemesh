"""Print a fail-safe hosted-CI validation plan and optional GitHub step outputs."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stagemesh.ci_validation_plan import (
    COMPONENT,
    DOCS,
    FULL,
    OPERATOR,
    SMOKE,
    derive_ci_validation_plan,
    full_ci_validation_plan,
    post_merge_ci_validation_plan,
)

SHA = re.compile(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?\Z")
FULL_EVENTS = frozenset({"schedule", "workflow_dispatch"})
DIFF_EVENTS = frozenset({"pull_request", "merge_group"})


def changed_paths(repo: Path, base: str, head: str) -> tuple[str, ...]:
    if not SHA.fullmatch(base) or not SHA.fullmatch(head):
        raise ValueError("base and head must be full Git object IDs")
    for revision in (base, head):
        subprocess.run(
            ["git", "cat-file", "-e", f"{revision}^{{commit}}"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMRD", f"{base}...{head}", "--"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    return tuple(line for line in result.stdout.splitlines() if line)


def protected_merge_push(repo: Path, base: str, head: str) -> bool:
    """Prove this push advances main from its old tip to one two-parent merge commit."""
    if not SHA.fullmatch(base) or not SHA.fullmatch(head):
        return False
    result = subprocess.run(
        ["git", "show", "-s", "--format=%P", head],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    parents = result.stdout.split()
    return len(parents) == 2 and parents[0] == base


def plan_from_inputs(
    *,
    repo: Path,
    paths: tuple[str, ...] = (),
    base: str = "",
    head: str = "",
    event: str = "",
    force_full: bool = False,
):
    event = event.strip().casefold()
    if force_full or event in FULL_EVENTS:
        return full_ci_validation_plan(paths, f"{event or 'explicit'} event requires full regression")
    if event == "push":
        try:
            push_paths = paths or changed_paths(repo, base, head)
            trusted_merge = protected_merge_push(repo, base, head)
            return post_merge_ci_validation_plan(push_paths, protected_merge=trusted_merge)
        except (OSError, UnicodeError, ValueError, subprocess.CalledProcessError) as exc:
            return full_ci_validation_plan(
                (),
                "exact post-merge base/head diff is unavailable; full regression selected",
                f"diff planning error: {type(exc).__name__}",
            )
    if paths:
        return derive_ci_validation_plan(paths)
    if event and event not in DIFF_EVENTS:
        return full_ci_validation_plan((), f"unsupported CI event {event!r} requires full regression")
    try:
        return derive_ci_validation_plan(changed_paths(repo, base, head))
    except (OSError, UnicodeError, ValueError, subprocess.CalledProcessError) as exc:
        return full_ci_validation_plan(
            (),
            "exact base/head diff is unavailable; full regression selected",
            f"diff planning error: {type(exc).__name__}",
        )


def _truthy(value: str) -> bool:
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _write_github_outputs(path: Path, mode: str, required_os: tuple[str, ...]) -> None:
    values = {
        "mode": mode,
        "run_full": str(mode == FULL).lower(),
        "run_operator": str(mode == OPERATOR).lower(),
        "run_component": str(mode == COMPONENT).lower(),
        "run_docs": str(mode == DOCS).lower(),
        "run_smoke": str(mode == SMOKE).lower(),
        "required_os": ",".join(required_os),
    }
    with path.open("a", encoding="utf-8", newline="\n") as output:
        for name, value in values.items():
            output.write(f"{name}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--base", default=os.environ.get("STAGEMESH_CI_BASE_SHA", ""))
    parser.add_argument("--head", default=os.environ.get("STAGEMESH_CI_HEAD_SHA", ""))
    parser.add_argument("--event", default=os.environ.get("STAGEMESH_CI_EVENT", ""))
    parser.add_argument("--force-full", action="store_true")
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--plan-file", type=Path)
    args = parser.parse_args(argv)

    forced = args.force_full or _truthy(os.environ.get("STAGEMESH_CI_FORCE_FULL", ""))
    plan = plan_from_inputs(
        repo=args.repo.resolve(),
        paths=tuple(args.path),
        base=args.base,
        head=args.head,
        event=args.event,
        force_full=forced,
    )
    print(json.dumps(plan.to_dict(), indent=2, sort_keys=True))
    if args.plan_file is not None:
        if args.plan_file.is_symlink():
            raise ValueError("plan file must not be a symlink")
        args.plan_file.write_text(
            json.dumps(plan.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    output = args.github_output or (
        Path(os.environ["GITHUB_OUTPUT"]) if os.environ.get("GITHUB_OUTPUT") else None
    )
    if output is not None:
        _write_github_outputs(output, plan.mode, plan.required_os)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
