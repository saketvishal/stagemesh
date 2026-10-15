from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # the StageMesh source checkout this script builds from
# The runtime is a standalone, non-editable install shared by every project: <runtime home>/venv. Projects only get a shim to it.
# `--editable` keeps the old development layout (an editable install in <project>/.stagemesh/tooling/venv).
RUNTIME_HOME_ENV = "STAGEMESH_RUNTIME_HOME"
USER_BIN = Path(os.environ.get("STAGEMESH_USER_BIN", Path.home() / ".stagemesh" / "bin"))
BUILTIN_PROVIDERS = ("codex", "claude", "grok", "agy")
DEFAULT_COMMANDS = {
    "codex": "codex exec",
    "claude": "claude -p",
    "grok": "grok",
    "agy": "agy --mode accept-edits",
}
AGY_UNATTENDED_COMMAND = "agy --mode accept-edits --dangerously-skip-permissions"
DEFAULT_QUEUE_LABELS = ("stagemesh:ready",)
DEFAULT_EXCLUDED_QUEUE_LABELS = (
    "stagemesh:blocked",
    "stagemesh:deferred",
    "status:BLOCKED",
    "status:REMEDIATING",
)


def _runtime_home() -> Path:
    if os.environ.get(RUNTIME_HOME_ENV):
        return Path(os.environ[RUNTIME_HOME_ENV])
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "StageMesh" / "runtime"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "stagemesh" / "runtime"


VENV = _runtime_home() / "venv"
BIN = ROOT / ".stagemesh" / "bin"
PROJECT = ROOT


def _use_project(project: Path, *, editable: bool) -> None:
    global BIN, VENV, PROJECT
    PROJECT = project
    BIN = project / ".stagemesh" / "bin"
    VENV = project / ".stagemesh" / "tooling" / "venv" if editable else _runtime_home() / "venv"


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def _source_version() -> str | None:
    try:
        text = (ROOT / "src" / "stagemesh" / "__init__.py").read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^__version__\s*=\s*[\"']([^\"']+)[\"']", text, re.MULTILINE)
    return match.group(1) if match else None


def _configured_version() -> str | None:
    try:
        import tomllib

        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        proj = data.get("project", {})
        if proj.get("name") != "stagemesh":
            return None
        version = proj.get("version")
    except (OSError, ValueError):
        return None
    return version if isinstance(version, str) else None


def _installed_version(py: Path) -> str | None:
    result = subprocess.run(
        [str(py), "-c", "import importlib.metadata as m; print(m.version('stagemesh'))"],
        cwd=tempfile.gettempdir(),
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() or None if result.returncode == 0 else None


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
self_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
self="$self_dir/$(basename -- "$0")"
dir="$(pwd)"
while :; do
  if [ -x "$dir/.stagemesh/bin/stagemesh" ]; then
    exec "$dir/.stagemesh/bin/stagemesh" "$@"
  fi
  parent="$(dirname "$dir")"
  if [ "$parent" = "$dir" ]; then
    break
  fi
  dir="$parent"
done
old_ifs=$IFS
IFS=:
for path_dir in $PATH; do
  IFS=$old_ifs
  [ -n "$path_dir" ] || path_dir=.
  candidate_dir="$(CDPATH= cd -- "$path_dir" 2>/dev/null && pwd -P)" || continue
  candidate="$candidate_dir/stagemesh"
  if [ -x "$candidate" ] && [ "$candidate" != "$self" ]; then
    exec "$candidate" "$@"
  fi
  IFS=:
done
IFS=$old_ifs
echo "stagemesh: no project-local runtime or global fallback found from $(pwd); run python scripts/bootstrap_stagemesh.py in the project first." >&2
exit 127
""",
            encoding="utf-8",
        )
        path.chmod(0o755)


def _install_user_dispatcher() -> Path:
    target_dir = _user_owned_path_dir()
    dispatcher = target_dir / ("stagemesh.cmd" if os.name == "nt" else "stagemesh")
    _write_user_dispatcher(dispatcher)
    return dispatcher


def _windows_shim(installed: Path) -> str:
    # PYTHONPATH/PYTHONHOME would shadow the installed package (e.g. a leftover PYTHONPATH=...\src); the shim never honours them.
    lines = ["@echo off", "setlocal", 'set "PYTHONPATH="', 'set "PYTHONHOME="', f'"{installed}" %*', "exit /b %ERRORLEVEL%", ""]
    return "\n".join(lines)  # write_text translates to CRLF on Windows


def _selected_providers(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    selected = [item.strip() for item in raw.split(",") if item.strip()]
    unknown = sorted(set(selected) - set(BUILTIN_PROVIDERS))
    if unknown:
        raise SystemExit(f"unknown provider(s): {', '.join(unknown)}; choose from {', '.join(BUILTIN_PROVIDERS)}")
    if not selected:
        raise SystemExit("--providers must name at least one provider")
    return selected


def _configure_providers(selected: list[str] | None, *, agy_unattended: bool = False) -> None:
    if agy_unattended and (selected is None or "agy" not in selected):
        raise SystemExit("--agy-unattended requires --providers including agy")
    if selected is None:
        return
    path = PROJECT / ".stagemesh" / "config.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    providers = data.setdefault("providers", {})
    for name in selected:
        command = AGY_UNATTENDED_COMMAND if name == "agy" and agy_unattended else DEFAULT_COMMANDS[name]
        provider = providers.setdefault(name, {"command": command, "capabilities": ["IMPLEMENT", "REVIEW"]})
        if name == "agy" and agy_unattended and isinstance(provider, dict):
            provider["command"] = command
    pools = data.setdefault("routing", {}).setdefault("pools", {})
    pools["IMPLEMENT"] = selected
    pools["REVIEW"] = [name for name in ("claude", "grok", "agy", "codex") if name in selected]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"providers: {', '.join(selected)}")
    print(f"config: {path}")


def _configure_queue_source() -> None:
    path = PROJECT / ".stagemesh" / "config.json"
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    sources = data.get("task_sources")
    if not isinstance(sources, list):
        return
    changed = False
    for source in sources:
        if not isinstance(source, dict) or source.get("type") != "github":
            continue
        if source.get("labels") == ["status:QUEUED"]:
            source["labels"] = list(DEFAULT_QUEUE_LABELS)
            changed = True
        if "excluded_labels" not in source:
            source["excluded_labels"] = list(DEFAULT_EXCLUDED_QUEUE_LABELS)
            changed = True
    if changed:
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print("queue source: stagemesh:ready excluding blocked/remediating labels")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--providers",
        help="Comma-separated project provider pool from codex,claude,grok,agy; omitted leaves config unchanged.",
    )
    parser.add_argument(
        "--agy-unattended",
        action="store_true",
        help="Persist Agy with --dangerously-skip-permissions for this project-local runtime.",
    )
    parser.add_argument(
        "--no-queue-config",
        action="store_true",
        help="Leave existing GitHub task-source label filters unchanged.",
    )
    parser.add_argument(
        "--project",
        help="Project whose .stagemesh/bin shim should point at the runtime (default: this checkout). The project is only configured, never installed into.",
    )
    parser.add_argument(
        "--editable",
        action="store_true",
        help="Development layout: editable install of this checkout in <project>/.stagemesh/tooling/venv instead of the standalone runtime.",
    )
    args = parser.parse_args(argv)
    if args.project and not Path(args.project).is_dir():
        raise SystemExit(f"--project {args.project} is not an existing directory")
    _use_project(Path(args.project).resolve() if args.project else ROOT, editable=args.editable)
    VENV.parent.mkdir(parents=True, exist_ok=True)
    BIN.mkdir(parents=True, exist_ok=True)
    if not _venv_python().exists():
        _run(sys.executable, "-m", "venv", str(VENV))
    py = _venv_python()
    _run(str(py), "-m", "pip", "install", "--upgrade", "pip")
    if args.editable:
        _run(str(py), "-m", "pip", "install", "--upgrade", "-e", f"{ROOT}[dev]")
    else:
        # A real copy of the package: the checkout may move, change branch or disappear without affecting this runtime.
        _run(str(py), "-m", "pip", "install", "--upgrade", "--force-reinstall", "--no-deps", str(ROOT))
    installed_version = _installed_version(py)
    for label, expected in (("repository source", _source_version()), ("configured version", _configured_version())):
        if expected and installed_version and expected != installed_version:
            raise SystemExit(
                f"project-local runtime is stale: installed {installed_version}, {label} {expected}; "
                "rerun python scripts/bootstrap_stagemesh.py"
            )
    installed = py.parent / _script_name()
    if not installed.exists():
        raise SystemExit(f"installed stagemesh entry point not found: {installed}")
    if os.name == "nt":
        shim = BIN / "stagemesh.cmd"
        shim.write_text(_windows_shim(installed), encoding="utf-8")
    else:
        shim = BIN / "stagemesh"
        shim.write_text(f"#!/usr/bin/env sh\nunset PYTHONPATH PYTHONHOME\nexec \"{installed}\" \"$@\"\n", encoding="utf-8")
        shim.chmod(0o755)
    dispatcher = _install_user_dispatcher()
    _configure_providers(_selected_providers(args.providers), agy_unattended=args.agy_unattended)
    if not args.no_queue_config:
        _configure_queue_source()
    print(shim)
    print(f"runtime: {VENV} ({'editable, development' if args.editable else 'standalone, installed'}) version {installed_version}")
    print(f"dispatcher: {dispatcher}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
