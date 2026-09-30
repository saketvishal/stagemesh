from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path

from .domain import ProcessIdentity


def boot_id() -> str:
    path = Path("/proc/sys/kernel/random/boot_id")
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return f"{platform.system()}:{platform.node()}"


def current_process_identity() -> ProcessIdentity:
    return ProcessIdentity(
        pid=os.getpid(),
        create_time=None,
        boot_id=boot_id(),
        executable=None,
    )


def popen_identity(proc: subprocess.Popen[str]) -> ProcessIdentity:
    return ProcessIdentity(pid=proc.pid, create_time=None, boot_id=boot_id(), executable=proc.args[0] if proc.args else None)


def classify_process(saved: ProcessIdentity, observed: ProcessIdentity | None) -> str:
    if not saved.is_known or observed is None or not observed.is_known:
        return "UNKNOWN"
    return "LIVE" if saved.matches(observed) else "DEAD"


def is_pid_alive(pid: int, expected_boot_id: str | None = None) -> bool:
    if pid <= 0:
        return False
    if expected_boot_id and expected_boot_id != boot_id():
        return False
    try:
        if platform.system() == "Windows":
            cmd = ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            return str(pid) in res.stdout
        else:
            os.kill(pid, 0)
            return True
    except (OSError, Exception):
        return False
