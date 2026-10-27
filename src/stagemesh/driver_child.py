"""Child wrapper that keeps run-window ownership durable if the scheduler parent exits."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .continuous_driver import (
    begin_child_cleanup,
    clear_child_owner,
    finish_child_cleanup,
    save_child_owner,
)
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
    cleanup_owner = identity
    release_owner = True
    save_child_owner(project, identity)
    try:
        process = subprocess.Popen(command, cwd=project)
        release_owner = False  # any failure after launch must retain ownership until cleanup is confirmed
        child_identity = process_identity(process.pid)
        try:
            save_child_owner(project, identity, child_identity)
        except OSError:
            pass  # the wrapper record already exists; the child identity only tightens ownership
        try:
            result = process.wait()
            release_owner = True
            return result
        except BaseException:
            # A failed tree kill cannot be turned into automatic recovery merely because both recorded roots later die.
            # Retain the known run-window as the sole owner: LIVE still blocks, DEAD requires explicit orphan release.
            begin_child_cleanup(project, identity)
            cleanup_owner = child_identity if child_identity is not None and child_identity.is_known else identity
            try:
                save_child_owner(project, cleanup_owner)
            except OSError:
                pass  # the cleanup fence survives failed owner publication; still attempt to stop the child
            tree_killed = kill_process_tree(process)
            process.wait()
            release_owner = tree_killed
            if tree_killed:
                finish_child_cleanup(project)
            raise
    finally:
        if release_owner:
            try:
                clear_child_owner(project, cleanup_owner, allow_live=True)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
