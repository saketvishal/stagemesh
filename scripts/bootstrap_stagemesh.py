from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".stagemesh" / "tooling"
VENV = RUNTIME / "venv"
BIN = ROOT / ".stagemesh" / "bin"
USER_BIN = Path(os.environ.get("STAGEMESH_USER_BIN", Path.home() / ".stagemesh" / "bin"))


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def _script_name() -> str:
    return "stagemesh.exe" if os.name == "nt" else "stagemesh"


def _run(*args: str) -> None:
    subprocess.run(args, cwd=ROOT, check=True)


def _path_entries() -> list[Path]:
    return [Path(entry.strip('"')) for entry in os.environ.get("PATH", "").split(os.pathsep) if entry.strip('"')]


def _writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path, prefix=".stagemesh-write-", delete=True):
            return True
    except OSError:
        return False


def _user_owned_path_dir() -> Path:
    path_entries = [entry.resolve() for entry in _path_entries() if entry.exists()]
    home = Path.home().resolve()
    local_app_data = Path(os.environ.get("LOCALAPPDATA", "")).resolve() if os.environ.get("LOCALAPPDATA") else None
    preferred: list[Path] = []
    stable_names = (
        home / ".local" / "bin",
        home / ".stagemesh" / "bin",
        Path(os.environ.get("APPDATA", "")) / "Python" if os.environ.get("APPDATA") else None,
        local_app_data / "Microsoft" / "WindowsApps" if local_app_data is not None else None,
    )
    stable_prefixes = tuple(str(path.resolve()).casefold() for path in stable_names if path is not None)
    for entry in path_entries:
        entry_text = str(entry).casefold()
        home_owned = str(home).casefold() in entry_text
        local_owned = local_app_data is not None and str(local_app_data).casefold() in entry_text
        stable = any(entry_text.startswith(prefix) for prefix in stable_prefixes)
        if not stable and any(part in entry_text for part in ("\\temp\\", "\\tmp\\", "\\.codex\\tmp\\", "\\codex-runtimes\\")):
            continue
        if (home_owned or local_owned) and stable:
            preferred.append(entry)
    for entry in preferred:
        if _writable_dir(entry):
            return entry
    USER_BIN.mkdir(parents=True, exist_ok=True)
    return USER_BIN


def _write_user_dispatcher(path: Path) -> None:
    if os.name == "nt":
        path.write_text(
            """@echo off
setlocal
set "DIR=%CD%"
:find
if exist "%DIR%\\.stagemesh\\bin\\stagemesh.cmd" (
  call "%DIR%\\.stagemesh\\bin\\stagemesh.cmd" %*
  exit /b %ERRORLEVEL%
)
for %%I in ("%DIR%\\..") do set "PARENT=%%~fI"
if /I "%PARENT%"=="%DIR%" goto notfound
set "DIR=%PARENT%"
goto find
:notfound
echo stagemesh: no project-local runtime found from "%CD%"; run python scripts\\bootstrap_stagemesh.py in the project first. 1>&2
exit /b 9009
""",
            encoding="utf-8",
        )
    else:
        path.write_text(
            """#!/usr/bin/env sh
dir="$(pwd)"
while :; do
  if [ -x "$dir/.stagemesh/bin/stagemesh" ]; then
    exec "$dir/.stagemesh/bin/stagemesh" "$@"
  fi
  parent="$(dirname "$dir")"
  if [ "$parent" = "$dir" ]; then
    echo "stagemesh: no project-local runtime found from $(pwd); run python scripts/bootstrap_stagemesh.py in the project first." >&2
    exit 127
  fi
  dir="$parent"
done
""",
            encoding="utf-8",
        )
        path.chmod(0o755)


def _install_user_dispatcher() -> Path:
    target_dir = _user_owned_path_dir()
    dispatcher = target_dir / ("stagemesh.cmd" if os.name == "nt" else "stagemesh")
    _write_user_dispatcher(dispatcher)
    return dispatcher


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
    dispatcher = _install_user_dispatcher()
    print(shim)
    print(f"dispatcher: {dispatcher}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
