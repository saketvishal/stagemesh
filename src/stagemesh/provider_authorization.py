"""Which coding CLIs may run unattended in a project. Installing a CLI never authorizes it.

A project authorizes providers explicitly in `.stagemesh/config.json`:

    "authorization": {"unattended_providers": ["codex", "claude"]}

The allowlist is enforced at three layers: selection (an unauthorized provider is never eligible, so it is never a fallback), the
execution boundary (`RuntimeCommandAdapter.execute` / `review_candidate` re-check immediately before spawning anything), and the
command itself (a provider must run its own CLI: no substitution, no wrapper, no custom name that wraps a real CLI).
Unattended mode (`STAGEMESH_UNATTENDED=1`, set by the continuous driver) fails closed: no allowlist, or an unreadable config,
means nothing may run. A project that lists an allowlist is held to it in every mode.
"""
from __future__ import annotations

import contextlib
import contextvars
import os
import shutil
import re
from collections.abc import Iterator, Sequence
from pathlib import Path

UNATTENDED_ENV = "STAGEMESH_UNATTENDED"
REAL_CLIS = ("codex", "claude", "grok", "agy")
_SUFFIX = re.compile(r"\.(exe|cmd|bat|ps1|com)$")
_MISSING = "no explicit unattended provider allowlist (.stagemesh/config.json authorization.unattended_providers)"


def unattended(environ: dict[str, str] | None = None) -> bool:
    return (environ if environ is not None else os.environ).get(UNATTENDED_ENV) == "1"


def program_name(token: str) -> str:
    base = str(token).replace("\\", "/").rsplit("/", 1)[-1].strip().lower()
    return _SUFFIX.sub("", base)


# A provider the caller has already authorized against the REAL project (used by the live smoke, which runs in a temp project).
_PRE_AUTHORIZED: contextvars.ContextVar[frozenset[str] | None] = contextvars.ContextVar("pre_authorized", default=None)
_WORD = re.compile(r"[a-z0-9]+")


@contextlib.contextmanager
def pre_authorized(names: Sequence[str]) -> Iterator[None]:
    token = _PRE_AUTHORIZED.set(frozenset(names))
    try:
        yield
    finally:
        _PRE_AUTHORIZED.reset(token)


def _inside(path: str, project: Path) -> bool:
    try:
        child = os.path.normcase(os.path.realpath(path))
        root = os.path.normcase(os.path.realpath(str(project)))
        return os.path.commonpath([child, root]) == root
    except ValueError:
        return False


def _path_problem(token: str, project: Path | None) -> str | None:
    """The executable must be the installed CLI, not a file the repository (or a relative path, or a network share) supplies."""
    text = str(token)
    if text.startswith(("\\\\", "//")):
        return "network path"
    if "/" not in text and "\\" not in text:
        # A bare name is resolved the way the spawn would: in PATH order, and (on Windows) the working directory first.
        # Relative PATH entries are harmless unless the executable would actually be found through one of them.
        exts = [""] + ([e.lower() for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e] if os.name == "nt" else [])
        root = Path(project) if project is not None else Path.cwd()
        hit: str | None = None
        entries = [e.strip().strip('"') for e in os.environ.get("PATH", "").split(os.pathsep)]
        search = ([""] if os.name == "nt" else []) + entries
        for entry in search:
            if os.name == "nt" and entry == "" and search.index(entry) != 0:
                continue  # Windows ignores empty PATH entries
            absolute = entry != "" and (os.path.isabs(entry) or re.match(r"^[A-Za-z]:[\\/]", entry))
            base = Path(entry) if absolute else root / entry
            for ext in exts:
                candidate = base / (text + ext)
                if candidate.is_file():
                    hit = str(candidate)
                    if not absolute:
                        return "the executable would be found through the working directory or a relative PATH entry, inside the project"
                    break
            if hit:
                break
        if hit is None:
            return None
        text = hit
    if not (os.path.isabs(text) or re.match(r"^[A-Za-z]:[\\/]", text)):
        return "relative path"
    suffix = os.path.splitext(text)[1].lower()
    if suffix not in ("", ".exe", ".cmd"):
        return f"unsupported executable type '{suffix}'"
    if project is not None and _inside(text, project):
        return "executable lives inside the project or its worktrees"
    return None


def _words(token: str) -> list[str]:
    """Words of a command token; a plain path is judged by its file name only (a directory called 'claude' is not the claude CLI)."""
    if re.search(r"[\\/]", token) and not re.search(r"\s", token):
        return [program_name(re.split(r"[\\/]", token)[-1]).lower()]
    return _WORD.findall(token.lower())


def denial(
    name: str,
    argv: Sequence[str],
    allowed: set[str] | frozenset[str] | tuple[str, ...] | None,
    *,
    unattended: bool,
    project: Path | None = None,
) -> str | None:
    """Why this provider may not run, or None. `allowed` is None when the project has no allowlist."""
    if allowed is None:
        return _MISSING if unattended else None
    if name not in allowed:
        return f"provider '{name}' is not in this project's authorized providers ({', '.join(sorted(allowed))})"
    program = program_name(argv[0]) if argv else ""
    if name in REAL_CLIS:
        if program != name:
            return f"command for '{name}' runs '{program or '(empty)'}', not the {name} CLI (substitution or wrapper)"
        problem = _path_problem(argv[0], project)
        return f"command for '{name}' is not the installed CLI ({problem})" if problem else None
    wrapped = sorted({word for token in argv for word in _words(str(token)) if word in REAL_CLIS})
    if wrapped:
        return f"custom provider '{name}' references the real CLI {', '.join(wrapped)}; name it after the CLI and authorize that"
    return None


def read_allowlist(project: Path) -> tuple[str, ...] | None:
    """The project's allowlist straight from its config file (no git or network): None when none is declared; ValueError if malformed."""
    import json

    path = Path(project) / ".stagemesh" / "config.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("config root must be an object")
    section = data.get("authorization", {})
    if not isinstance(section, dict):
        raise ValueError("authorization must be an object")
    if "unattended_providers" not in section:
        return None
    names = section["unattended_providers"]
    if not isinstance(names, list) or not names or not all(isinstance(n, str) and n.strip() for n in names):
        raise ValueError("authorization.unattended_providers must be a non-empty list of names")
    return tuple(n.strip() for n in names)


def refusal(project: Path, name: str, argv: Sequence[str], *, build_time_allowed: Sequence[str] | None = None) -> str | None:
    """The execution-boundary check: re-reads the project's own config so it holds however the adapter was built.

    `build_time_allowed` is the allowlist the adapter was built under: if the config becomes unreadable later, that list still binds.
    """
    mode = unattended()
    pre = _PRE_AUTHORIZED.get()
    if pre is not None:
        return denial(name, argv, set(pre), unattended=mode, project=Path(project))
    try:
        allowed = read_allowlist(Path(project))
    except (OSError, ValueError) as exc:
        if build_time_allowed is not None:
            return denial(name, argv, set(build_time_allowed), unattended=mode, project=Path(project)) or (
                f"project configuration could not be re-verified ({type(exc).__name__})"
            )
        return f"project configuration could not be verified ({type(exc).__name__})" if mode else None
    return denial(name, argv, None if allowed is None else set(allowed), unattended=mode, project=Path(project))


def pinned_model(argv: Sequence[str]) -> str | None:
    """The model a command pins (`--model X`, `-m X`, `--model=X`, `-c model=X`), or None when the CLI's own default would apply."""
    tokens = list(argv)
    for index, token in enumerate(tokens):
        if token in ("--model", "-m") and index + 1 < len(tokens):
            return tokens[index + 1].strip() or None
        if token.startswith("--model="):
            return token.split("=", 1)[1].strip() or None
        if token in ("-c", "--config") and index + 1 < len(tokens) and tokens[index + 1].startswith("model="):
            return tokens[index + 1].split("=", 1)[1].strip("\"' ") or None
    return None
