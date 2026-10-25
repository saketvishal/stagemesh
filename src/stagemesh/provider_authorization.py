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

import os
import re
from collections.abc import Sequence
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


def denial(name: str, argv: Sequence[str], allowed: set[str] | frozenset[str] | tuple[str, ...] | None, *, unattended: bool) -> str | None:
    """Why this provider may not run, or None. `allowed` is None when the project has no allowlist."""
    if allowed is None:
        return _MISSING if unattended else None
    if name not in allowed:
        return f"provider '{name}' is not in this project's authorized providers ({', '.join(sorted(allowed))})"
    program = program_name(argv[0]) if argv else ""
    if name in REAL_CLIS:
        if program != name:
            return f"command for '{name}' runs '{program or '(empty)'}', not the {name} CLI (substitution or wrapper)"
        return None
    wrapped = sorted({program_name(token) for token in argv if program_name(token) in REAL_CLIS})
    if wrapped:
        return f"custom provider '{name}' wraps the real CLI {', '.join(wrapped)}; name it after the CLI and authorize that"
    return None


def refusal(project: Path, name: str, argv: Sequence[str]) -> str | None:
    """The execution-boundary check: re-reads the project's own config so it holds however the adapter was built."""
    from .config import ConfigValidationError, load_config

    mode = unattended()
    try:
        config = load_config(Path(project))
    except (ConfigValidationError, OSError, ValueError) as exc:
        return f"project configuration could not be verified ({type(exc).__name__})" if mode else None
    allowed = config.authorized_providers
    return denial(name, argv, None if allowed is None else set(allowed), unattended=mode)


def pinned_model(argv: Sequence[str]) -> str | None:
    """The model a command pins (`--model X`, `-m X`, `--model=X`, `-c model=X`), or None when the CLI's own default would apply."""
    tokens = list(argv)
    for index, token in enumerate(tokens):
        if token in ("--model", "-m") and index + 1 < len(tokens):
            return tokens[index + 1]
        if token.startswith("--model="):
            return token.split("=", 1)[1]
        if token in ("-c", "--config") and index + 1 < len(tokens) and tokens[index + 1].startswith("model="):
            return tokens[index + 1].split("=", 1)[1].strip("\"'")
    return None
