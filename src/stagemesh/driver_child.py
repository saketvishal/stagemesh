"""Child wrapper that keeps run-window ownership durable if the scheduler parent exits."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .continuous_driver import clear_child_owner, save_child_owner
from .execution import kill_process_tree
from .process_identity import owner_identity, process_identity


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
        process = subprocess.Popen(command, cwd=project)
        try:
            save_child_owner(project, identity, process_identity(process.pid))
        except OSError:
            pass  # the wrapper record already exists; the child identity only tightens ownership
        try:
            return process.wait()
        except BaseException:
            kill_process_tree(process)  # subprocess.run did this for us; an interrupt must not orphan a running window
            process.wait()
            raise
    finally:
        try:
            clear_child_owner(project, identity, allow_live=True)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
