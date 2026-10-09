"""Git identity policy: StageMesh uses the repository owner's identity and never writes or rewrites one.

* Identity is *resolved*, never *configured*: StageMesh does not write `user.name` / `user.email` anywhere. A `git worktree` shares the
  repository's config, so a "harmless" per-worktree config write silently replaces the owner's identity in their real checkout.
* Commits and rebases StageMesh runs carry the owner's identity (environment first, then git config). Only when no identity exists at
  all is a clearly synthetic one supplied, and then only through the process environment of that single git command.
* Placeholder GitHub noreply ids (for example the invented `12345678+user@users.noreply.github.com`, which is a different real
  account) are refused outright: a commit with the wrong numeric id is credited to a stranger.
* AI providers are recorded in StageMesh execution metadata (candidate producer, execution actor), never as commit authors or
  `Co-authored-by` contributors. Non-human trailers are stripped from commit messages StageMesh writes.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

SYNTHETIC_NAME = "StageMesh"
SYNTHETIC_EMAIL = "stagemesh@stagemesh.invalid"

# Numeric ids that are known placeholders. 12345678 is a real account; an invented id attributes commits to someone else.
PLACEHOLDER_NOREPLY_IDS = frozenset({"12345678"})
_SIMPLE_EMAIL = re.compile(r"^[^@\s<>]+@[^@\s<>]+$")
_NOREPLY = re.compile(r"^(?P<id>\d+)\+(?P<login>[^@\s]+)@users\.noreply\.github\.com$", re.IGNORECASE)

# AI coding providers. A `Co-authored-by` for one of these credits a GitHub account for tool use; participation belongs in StageMesh metadata.
AI_PROVIDER_EMAIL_DOMAINS = frozenset({"anthropic.com", "openai.com", "x.ai"})

# Addresses that identify tools, not people. They must not become GitHub contributors through a Co-authored-by trailer.
NON_HUMAN_EMAIL_DOMAINS = frozenset({"anthropic.com", "openai.com", "x.ai", "stagemesh.invalid", "example.invalid", "invalid"})
_COAUTHOR_LINE = re.compile(r"^\s*co-authored-by\s*:\s*(?P<who>.*?)\s*$", re.IGNORECASE)
_EMAIL_IN_TRAILER = re.compile(r"<(?P<email>[^<>]*)>")


class GitIdentityError(ValueError):
    """The identity a commit would carry is unusable or would credit the wrong account."""


@dataclass(frozen=True)
class GitIdentity:
    name: str
    email: str
    source: str  # "configured" (environment or git config) or "synthetic"

    def env(self) -> dict[str, str]:
        return {
            "GIT_AUTHOR_NAME": self.name,
            "GIT_AUTHOR_EMAIL": self.email,
            "GIT_COMMITTER_NAME": self.name,
            "GIT_COMMITTER_EMAIL": self.email,
        }


SYNTHETIC_IDENTITY = GitIdentity(SYNTHETIC_NAME, SYNTHETIC_EMAIL, "synthetic")


def validate_identity(name: str, email: str) -> None:
    """Refuse malformed or placeholder identities. Raises GitIdentityError."""
    if not name.strip():
        raise GitIdentityError("git identity name is empty")
    if not _SIMPLE_EMAIL.match(email):
        raise GitIdentityError(
            f"git identity email {email!r} is malformed (no whitespace or angle brackets allowed): fix user.email / GIT_*_EMAIL"
        )
    match = _NOREPLY.match(email.strip())
    if match and match.group("id") in PLACEHOLDER_NOREPLY_IDS:
        raise GitIdentityError(
            f"git identity email {email!r} uses placeholder GitHub id {match.group('id')}, which belongs to a different account; "
            "use the real noreply id from https://api.github.com/users/<login>"
        )


def _git_config(path: Path | None, key: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "config", "--get", key],
            cwd=path if path is not None and Path(path).is_dir() else None,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def configured_identity(path: Path | None = None, environ: dict[str, str] | None = None) -> GitIdentity | None:
    """The identity git itself would commit with (environment, then local/global config), or None when none is set.

    Never guesses from the host name and never writes anything. Raises GitIdentityError for a malformed or placeholder identity.
    """
    env = os.environ if environ is None else environ
    # Exactly git's own committer resolution: GIT_COMMITTER_* then config. GIT_AUTHOR_* describes only the author and is not a fallback.
    name = env.get("GIT_COMMITTER_NAME") or _git_config(path, "user.name")
    email = env.get("GIT_COMMITTER_EMAIL") or _git_config(path, "user.email")
    if not name or not email:
        return None
    validate_identity(name, email)
    return GitIdentity(name.strip(), email.strip(), "configured")


def project_identity(path: Path | None = None) -> GitIdentity:
    """The owner's identity for commits StageMesh creates in `path`, falling back to the synthetic identity when none exists."""
    return configured_identity(path) or SYNTHETIC_IDENTITY


def identity_env_for(path: Path | None, existing: dict[str, str] | None = None) -> dict[str, str]:
    """Environment variables supplying an identity to a single git command, filling only what the caller has not already set.

    The user's own environment is respected (and validated); the identity is passed per command and is never persisted to config.
    """
    present = dict(os.environ)
    present.update(existing or {})
    explicit_name = present.get("GIT_AUTHOR_NAME") or present.get("GIT_COMMITTER_NAME")
    explicit_email = present.get("GIT_AUTHOR_EMAIL") or present.get("GIT_COMMITTER_EMAIL")
    if explicit_name and explicit_email:
        validate_identity(explicit_name, explicit_email)
    identity = project_identity(path)
    fill = {key: value for key, value in identity.env().items() if not present.get(key)}
    return fill


def is_non_human_email(email: str) -> bool:
    cleaned = email.strip().lower()
    if "@" not in cleaned:
        return True
    local, domain = cleaned.rsplit("@", 1)
    if "+local-worker" in local or domain in NON_HUMAN_EMAIL_DOMAINS or domain.endswith(".invalid"):
        return True
    match = _NOREPLY.match(cleaned)
    return bool(match and match.group("id") in PLACEHOLDER_NOREPLY_IDS)


def is_ai_or_placeholder_email(email: str) -> bool:
    """The narrow set used when correcting existing history: AI-provider addresses and placeholder GitHub ids (nothing else)."""
    cleaned = email.strip().lower()
    domain = cleaned.rsplit("@", 1)[-1]
    match = re.search(r"(\d+)\+[^@\s]+@users\.noreply\.github\.com$", cleaned)
    return domain in AI_PROVIDER_EMAIL_DOMAINS or bool(match and match.group(1) in PLACEHOLDER_NOREPLY_IDS)


def sanitize_commit_message(message: str, *, minimal: bool = False) -> str:
    """Drop `Co-authored-by` trailers that name a tool or a placeholder account; trailers for real people are kept untouched.

    `minimal` limits removal to AI-provider and placeholder-id addresses (used when correcting published history).
    """
    judge = is_ai_or_placeholder_email if minimal else is_non_human_email
    kept: list[str] = []
    for line in message.splitlines():
        trailer = _COAUTHOR_LINE.match(line)
        if trailer:
            emails = _EMAIL_IN_TRAILER.findall(trailer.group("who"))
            if emails and any(judge(email) for email in emails):
                continue
            if not minimal and (not emails or any(ch.isspace() for email in emails for ch in email.strip())):
                continue
        kept.append(line)
    return "\n".join(kept).rstrip() + ("\n" if message.endswith("\n") else "")
