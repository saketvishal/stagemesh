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
    origin: str = ""  # where a configured identity came from: "environment" or "git config"

    def env(self) -> dict[str, str]:
        return {
            "GIT_AUTHOR_NAME": self.name,
            "GIT_AUTHOR_EMAIL": self.email,
            "GIT_COMMITTER_NAME": self.name,
            "GIT_COMMITTER_EMAIL": self.email,
        }


SYNTHETIC_IDENTITY = GitIdentity(SYNTHETIC_NAME, SYNTHETIC_EMAIL, "synthetic", "fallback")


def validate_identity(name: str, email: str) -> None:
    """Refuse malformed or placeholder identities. Raises GitIdentityError."""
    if not name.strip():
        raise GitIdentityError("git identity name is empty")
    if not _SIMPLE_EMAIL.match(email):
        raise GitIdentityError(
            f"git identity email {email!r} is malformed (no whitespace or angle brackets allowed): fix user.email / GIT_*_EMAIL"
        )
    if is_tool_email(email):
        raise GitIdentityError(
            f"git identity email {email!r} belongs to an AI provider or StageMesh worker; commits must carry the repository owner's "
            "identity (providers are recorded in StageMesh execution metadata, not as contributors): fix user.email / GIT_*_EMAIL"
        )
    match = _NOREPLY.match(email.strip())
    if match and match.group("id") in PLACEHOLDER_NOREPLY_IDS:
        raise GitIdentityError(
            f"git identity email {email!r} uses placeholder GitHub id {match.group('id')}, which belongs to a different account; "
            "use the real noreply id from https://api.github.com/users/<login>"
        )


def _is_synthetic(name: str | None = None, email: str | None = None) -> bool:
    """StageMesh's own placeholder identity, including the legacy `stagemesh@example.invalid` older versions wrote into git config.

    Such a value is never an approved owner identity: it must not outrank a real identity configured at another level.
    """
    if name is not None and name.strip() == SYNTHETIC_NAME:
        return True
    return email is not None and email.strip().lower().startswith("stagemesh@") and email.strip().lower().endswith(".invalid")


def _git_config_values(path: Path | None, key: str) -> list[str]:
    """Every value of `key` across system/global/local config, lowest precedence first (git itself uses the last)."""
    try:
        result = subprocess.run(
            ["git", "config", "--get-all", key],
            cwd=path if path is not None and Path(path).is_dir() else None,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _resolve_identity(path: Path | None, environ: dict[str, str] | None) -> tuple[GitIdentity | None, str | None]:
    """(approved identity or None, legacy synthetic value that was ignored, if any). Raises GitIdentityError for a bad identity."""
    env = os.environ if environ is None else environ
    ignored: str | None = None

    def pick(env_key: str, config_key: str, is_synth) -> tuple[str | None, str | None]:
        nonlocal ignored
        # git's own committer resolution (GIT_COMMITTER_* then config, last level wins), skipping StageMesh's placeholder identity so a
        # legacy `StageMesh <stagemesh@example.invalid>` in the local config cannot hide the owner's identity configured elsewhere.
        # GIT_AUTHOR_* describes only the author and is not a fallback.
        candidates = [(env.get(env_key), "environment")] + [(v, "git config") for v in reversed(_git_config_values(path, config_key))]
        for value, origin in candidates:
            if not value:
                continue
            if is_synth(value):
                ignored = ignored or value
                continue
            return value, origin
        return None, None

    name, name_origin = pick("GIT_COMMITTER_NAME", "user.name", lambda v: _is_synthetic(name=v))
    email, email_origin = pick("GIT_COMMITTER_EMAIL", "user.email", lambda v: _is_synthetic(email=v))
    if not name or not email:
        return None, ignored
    validate_identity(name, email)
    return GitIdentity(name.strip(), email.strip(), "configured", email_origin or name_origin or "git config"), ignored


def configured_identity(path: Path | None = None, environ: dict[str, str] | None = None) -> GitIdentity | None:
    """The approved owner identity git would commit with (environment, then local/global config), or None when none is set.

    StageMesh's own placeholder identity (including the legacy `stagemesh@example.invalid`) is never approved and is skipped.
    Never guesses from the host name and never writes anything. Raises GitIdentityError for a malformed or placeholder identity.
    """
    return _resolve_identity(path, environ)[0]


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


def is_tool_email(email: str) -> bool:
    """AI-provider addresses and StageMesh provider-worker addresses: never an acceptable author, committer or co-author."""
    cleaned = email.strip().lower()
    local, _, domain = cleaned.rpartition("@")
    return domain in AI_PROVIDER_EMAIL_DOMAINS or "+local-worker" in local


def provider_environment(path: Path | None = None, base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a provider subprocess: pinned to the project identity so a commit the provider makes itself is never credited
    to the provider's own git configuration. A placeholder or tool identity is left for StageMesh's own commit path to refuse."""
    env = dict(os.environ if base is None else base)
    try:
        identity = project_identity(path)
    except GitIdentityError:
        return env
    env.update(identity.env())  # forced, not filled: a provider's own GIT_* variables must not outrank the approved identity
    return env


def identity_report(path: Path | None = None, environ: dict[str, str] | None = None) -> dict[str, object]:
    """What StageMesh would commit as in `path`, and whether that is acceptable. Never writes anything.

    status: "ok" (owner identity), "synthetic" (none configured; StageMesh's per-command fallback is used), or "unapproved"
    (an AI-provider, worker, placeholder or malformed identity that StageMesh refuses to commit with).
    """
    try:
        identity, ignored = _resolve_identity(path, environ)
    except GitIdentityError as exc:
        return {"status": "unapproved", "name": None, "email": None, "source": "configured", "origin": None, "problem": str(exc)}
    if identity is None:
        problem = "no approved git identity configured; commits use the synthetic StageMesh identity (set user.name/user.email to the owner)"
        if ignored:
            problem += f"; ignored placeholder {ignored!r} in git config"
        return {
            "status": "synthetic", "name": SYNTHETIC_NAME, "email": SYNTHETIC_EMAIL, "source": "synthetic", "origin": "fallback",
            "problem": problem,
        }
    note = f"ignored placeholder {ignored!r} in favour of the configured owner identity" if ignored else None
    return {"status": "ok", "name": identity.name, "email": identity.email, "source": identity.source, "origin": identity.origin, "problem": note}


def attribution_offences(commits: list[tuple[str, str, str, str]]) -> list[str]:
    """Offences in (sha, author, committer, message) tuples: tool/worker/placeholder identities or AI/placeholder co-author trailers."""
    offences: list[str] = []
    for sha, author, committer, message in commits:
        for role, who in (("author", author), ("committer", committer)):
            email = who.rsplit("<", 1)[-1].rstrip(">").strip()
            if is_tool_email(email) or is_ai_or_placeholder_email(email):
                offences.append(f"{sha[:10]} {role} {who}")
        for line in message.splitlines():
            trailer = _COAUTHOR_LINE.match(line)
            if trailer and any(
                is_tool_email(e) or is_ai_or_placeholder_email(e) for e in _EMAIL_IN_TRAILER.findall(trailer.group("who"))
            ):
                offences.append(f"{sha[:10]} trailer {line.strip()[:120]}")
    return offences
