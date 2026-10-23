"""Child wrapper that keeps run-window ownership durable if the scheduler parent exits."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .continuous_driver import clear_child_owner, save_child_owner
from .process_identity import owner_identity


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 2:
        print("usage: python -m stagemesh.driver_child PROJECT COMMAND...", file=sys.stderr)
        return 2
    project = Path(arguments[0]).resolve()
    command = arguments[1:]
    identity = owner_identity()
    save_child_owner(project, identity)
    try:
        return subprocess.run(command, cwd=project, check=False).returncode
    finally:
        clear_child_owner(project, identity, allow_live=True)


if __name__ == "__main__":
    raise SystemExit(main())
