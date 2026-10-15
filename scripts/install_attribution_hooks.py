"""Install optional local attribution hooks without replacing existing hooks."""
from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path


def install(repo: Path) -> None:
    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()

    if subprocess.run(
        ["git", "config", "--get", "core.hooksPath"], cwd=repo, capture_output=True, check=False,
    ).returncode == 0:
        raise ValueError("core.hooksPath is already configured; integrate the checker with your existing hooks")
    hooks = Path(git("rev-parse", "--path-format=absolute", "--git-path", "hooks"))
    # Resolve the active worktree at hook execution, rather than baking in a checkout path.
    prefix = "#!/bin/sh\nset -eu\nroot=$(git rev-parse --show-toplevel)\nexec " + shlex.quote(Path(sys.executable).as_posix())
    scripts = {
        "commit-msg": prefix + ' "$root/scripts/check_attribution.py" --repo "$root" --message-file "$1"\n',
        "pre-push": prefix + ' "$root/scripts/check_attribution.py" --repo "$root" --pre-push "$1"\n',
    }
    for name, content in scripts.items():
        path = hooks / name
        if path.exists() and path.read_text(encoding="utf-8") != content:
            raise ValueError(f"existing {name} hook preserved; integrate scripts/check_attribution.py manually")
    hooks.mkdir(parents=True, exist_ok=True)
    for name, content in scripts.items():
        path = hooks / name
        path.write_text(content, encoding="utf-8", newline="\n")
        path.chmod(path.stat().st_mode | 0o111)
    print("Installed commit-msg and pre-push attribution checks. GitHub enforcement is still required.")


if __name__ == "__main__":
    try:
        install(Path.cwd())
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
