"""Is the installed `stagemesh` CLI behind the repo source? Say so, instead of letting an operator hit "invalid choice".

The project-local install (`.stagemesh/tooling/venv`) is a copy that does not follow the checkout, so a command added to source is
missing from `stagemesh.exe` until it is reinstalled. This compares the command sets of the two and reports the difference, and says
which one the current process is running. Nothing here changes the install.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_BOOTSTRAP = "import sys; from stagemesh.cli import main; sys.exit(main(sys.argv[1:]))"
_CHOICES = re.compile(r"\{([A-Za-z0-9_,\-]+)\}")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass
class RuntimeFreshness:
    running_from: str = ""
    running_from_source: bool = False
    installed_cli: str | None = None
    installed_commands: list[str] = field(default_factory=list)
    source_commands: list[str] = field(default_factory=list)
    missing_from_installed: list[str] = field(default_factory=list)
    stale: bool = False
    message: str = ""
    refresh_command: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def installed_cli_path(project: Path) -> Path | None:
    """The project-local install if there is one, else whatever `stagemesh` is on PATH."""
    venv = project / ".stagemesh" / "tooling" / "venv"
    for candidate in (venv / "Scripts" / "stagemesh.exe", venv / "bin" / "stagemesh"):
        if candidate.is_file():
            return candidate
    found = shutil.which("stagemesh")
    return Path(found) if found else None


def _help_commands(argv: list[str], runner: Runner, env: dict[str, str] | None = None) -> list[str] | None:
    try:
        proc = runner(argv, capture_output=True, text=True, check=False, timeout=30, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    groups = _CHOICES.findall((proc.stdout or "") + (proc.stderr or ""))
    # argparse prints the subcommand list as `{a,b,c}`; an option metavar can also be braced, so take the longest group
    return sorted(max(groups, key=len).split(",")) if groups else None


def _python_for(cli: Path) -> str:
    for name in ("python.exe", "python"):
        candidate = cli.parent / name
        if candidate.is_file():
            return str(candidate)
    return "python"


def check_runtime(project: Path, *, runner: Runner = subprocess.run, running_file: str | None = None) -> RuntimeFreshness:
    import stagemesh

    project = Path(project).resolve()
    src = project / "src"
    here = Path(running_file or stagemesh.__file__).resolve()
    result = RuntimeFreshness(running_from=str(here.parent), running_from_source=src.is_dir() and src in here.parents)
    cli = installed_cli_path(project)
    if not (src / "stagemesh" / "cli.py").is_file():
        result.message = "no StageMesh source in this project to compare the install with"
        return result
    env = dict(os.environ)
    env["PYTHONPATH"] = str(src) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    result.source_commands = _help_commands([sys.executable, "-c", _BOOTSTRAP, "--help"], runner, env) or []
    if cli is None:
        result.message = "no installed `stagemesh` found; run from source: PYTHONPATH=src python -c \"from stagemesh.cli import main; main()\" ..."
        return result
    result.installed_cli = str(cli)
    clean = {key: value for key, value in os.environ.items() if key.upper() not in ("PYTHONPATH", "PYTHONHOME")}
    installed = _help_commands([str(cli), "--help"], runner, clean)  # an inherited PYTHONPATH=src would make the install look current
    if installed is None or not result.source_commands:
        result.message = f"could not compare the installed CLI {cli} with source (help output unavailable)"
        return result
    result.installed_commands = installed
    result.missing_from_installed = sorted(set(result.source_commands) - set(installed))
    result.stale = bool(result.missing_from_installed)
    if not result.stale:
        result.message = f"installed CLI {cli} has every command in source"
        return result
    result.refresh_command = f'"{_python_for(cli)}" -m pip install --upgrade "{project}"'
    using = (
        "this process is running from source, so it is not using the stale install"
        if result.running_from_source
        else f"this process is running from {result.running_from}, not from source"
    )
    result.message = (
        f"installed CLI {cli} is stale: it lacks {', '.join(result.missing_from_installed)} (present in source). {using}. "
        f"To run a missing command from source: PYTHONPATH=src python -c \"from stagemesh.cli import main; main()\" <command> ... "
        f"To refresh the install (not done automatically): {result.refresh_command}"
    )
    return result
