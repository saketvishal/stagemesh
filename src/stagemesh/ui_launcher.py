from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class UiLaunchError(RuntimeError):
    pass


@dataclass(frozen=True)
class UiLaunch:
    method: str
    executable: str | None = None


def _windows_app_candidates(
    environ: Mapping[str, str],
    which: Callable[[str], str | None],
) -> tuple[Path, ...]:
    relative = (
        ("ProgramFiles(x86)", "Microsoft/Edge/Application/msedge.exe"),
        ("ProgramFiles", "Microsoft/Edge/Application/msedge.exe"),
        ("LOCALAPPDATA", "Microsoft/Edge/Application/msedge.exe"),
        ("ProgramFiles", "Google/Chrome/Application/chrome.exe"),
        ("ProgramFiles(x86)", "Google/Chrome/Application/chrome.exe"),
        ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe"),
    )
    paths = [Path(root) / suffix for key, suffix in relative if (root := environ.get(key))]
    paths.extend(Path(found) for name in ("msedge.exe", "msedge", "chrome.exe", "chrome") if (found := which(name)))
    return tuple(dict.fromkeys(paths))


def launch_ui(
    url: str,
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    exists: Callable[[Path], bool] | None = None,
    popen: Callable[..., Any] = subprocess.Popen,
    startfile: Callable[[str], Any] | None = None,
) -> UiLaunch:
    """Open the loopback UI without printing or persisting its fragment token."""
    platform_name = platform_name or sys.platform
    environ = os.environ if environ is None else environ
    exists = (lambda path: path.is_file()) if exists is None else exists
    if platform_name.startswith("win"):
        for executable in _windows_app_candidates(environ, which):
            if not exists(executable):
                continue
            try:
                popen(
                    [
                        str(executable),
                        "--no-first-run",
                        "--no-default-browser-check",
                        f"--app={url}",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                )
            except OSError:
                continue
            return UiLaunch("browser-app", str(executable))
    if platform_name.startswith("win"):
        opener = startfile or getattr(os, "startfile", None)
        if opener is not None:
            try:
                opener(url)
            except OSError:
                pass
            else:
                return UiLaunch("system-browser")
    else:
        command = "open" if platform_name == "darwin" else "xdg-open"
        executable = which(command)
        if executable:
            child_environment = dict(environ)
            child_environment.pop("BROWSER", None)
            try:
                popen(
                    [executable, url],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    env=child_environment,
                )
            except OSError:
                pass
            else:
                return UiLaunch("system-browser", executable)
    raise UiLaunchError("no usable browser could be opened")
