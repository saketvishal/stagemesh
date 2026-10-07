from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".stagemesh" / "tooling"
VENV = RUNTIME / "venv"
BIN = ROOT / ".stagemesh" / "bin"


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def _script_name() -> str:
    return "stagemesh.exe" if os.name == "nt" else "stagemesh"


def _run(*args: str) -> None:
    subprocess.run(args, cwd=ROOT, check=True)


def main() -> int:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    BIN.mkdir(parents=True, exist_ok=True)
    if not _venv_python().exists():
        _run(sys.executable, "-m", "venv", str(VENV))
    py = _venv_python()
    _run(str(py), "-m", "pip", "install", "--upgrade", "pip")
    _run(str(py), "-m", "pip", "install", f"{ROOT}[dev]")
    installed = py.parent / _script_name()
    if not installed.exists():
        raise SystemExit(f"installed stagemesh entry point not found: {installed}")
    if os.name == "nt":
        shim = BIN / "stagemesh.cmd"
        shim.write_text(f"@echo off\r\n\"{installed}\" %*\r\n", encoding="utf-8")
    else:
        shim = BIN / "stagemesh"
        shim.write_text(f"#!/usr/bin/env sh\nexec \"{installed}\" \"$@\"\n", encoding="utf-8")
        shim.chmod(0o755)
    print(shim)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
