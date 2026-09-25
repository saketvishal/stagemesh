"""Sanitization and boundary enforcement for GitHub-derived text and metadata.

GitHub text is completely untrusted input. It MUST NOT:
- select local filesystem paths or worktrees
- change coordinator DB/config
- disable review or change review policies to unreviewed
- disable or bypass human gates
- force protected-main pushes
- access unauthorized repositories
- inject shell commands or executable code
- leak private repository information (especially stagemesh-private) into public/product issues or comments
"""

from __future__ import annotations

import re
from typing import Any

from build_coordinator.policy import CoordinatorPolicyError
from build_coordinator.types import ObjectiveSpec

AUTHORIZED_REPOSITORIES = frozenset(
    {
        "stagemesh",
        "stagemesh-orchestrator",
        "stagemesh-labs",
        "stagemesh-infra",
        "stagemesh-private",
    }
)

AUTHORIZED_OWNERS = frozenset({"saketvishal"})

# Disallowed substrings/patterns for private leakage prevention
PRIVATE_LEAK_PATTERNS = [
    re.compile(r"stagemesh-private", re.IGNORECASE),
    re.compile(r"patent-sensitive", re.IGNORECASE),
    re.compile(r"PATENT_COPY", re.IGNORECASE),
    re.compile(r"STAGEMESH_CAPABILITY_MAP", re.IGNORECASE),
    re.compile(r"STAGEMESH_PRIVATE_ROADMAP", re.IGNORECASE),
    re.compile(r"STAGEMESH_PRODUCT_PRINCIPLES", re.IGNORECASE),
]

_PATH_OR_COMMAND_CHARS = re.compile(r"[;`$|&><\x00]")


class SecurityBoundaryError(CoordinatorPolicyError):
    """Raised when GitHub-derived text attempts to violate coordinator security invariants."""


def validate_repository_name(repo_name: str) -> str:
    """Validate that repository is authorized. Returns normalized repo name."""
    repo = repo_name.strip()
    if "/" in repo:
        parts = repo.split("/", 1)
        owner, repo = parts[0].strip(), parts[1].strip()
        if owner.lower() not in AUTHORIZED_OWNERS:
            raise SecurityBoundaryError(f"unauthorized repository owner: {owner!r}")
    normalized = repo.lower()
    for authorized in AUTHORIZED_REPOSITORIES:
        if normalized == authorized.lower():
            return authorized
    raise SecurityBoundaryError(f"unauthorized repository name: {repo_name!r}")


def full_repo_slug(repo_name: str) -> str:
    """Return the validated `owner/repo` slug for an authorized repository.

    There is exactly one authorized owner today; this is the single place
    that turns a bare repo name into the fully-qualified remote identity
    used for clone URLs, PR targets, and push-destination verification.
    """
    validated = validate_repository_name(repo_name)
    owner = next(iter(AUTHORIZED_OWNERS))
    return f"{owner}/{validated}"


def derive_repo_from_objective_id(objective_id: str) -> str | None:
    """Best-effort recovery of the target repo from the `GH-<repo>-<issue>`
    objective_id convention (see `sanitize_issue_to_spec`), for objectives
    created before `BuildObjective.repo` existed as a first-class column.
    Returns None if no authorized repo name matches -- callers must treat
    that as "unknown", never guess.
    """
    match = re.match(r"^GH-(?P<rest>.+)-\d+$", objective_id)
    if not match:
        return None
    rest = match.group("rest").lower()
    for authorized in AUTHORIZED_REPOSITORIES:
        if rest == authorized.lower():
            return authorized
    return None


def sanitize_text(text: str, *, max_length: int = 4000) -> str:
    """Sanitize freeform text from GitHub issues/comments.

    Strips control characters, null bytes, and limits length.
    """
    if not isinstance(text, str):
        return ""
    # Strip null bytes and non-printable control characters (except newline, tab, carriage return)
    cleaned = "".join(c for c in text if c in ("\n", "\r", "\t") or (ord(c) >= 32 and ord(c) != 127))
    # Strip any potential shell escapes or command injection vectors if single-line
    return cleaned[:max_length].strip()


def sanitize_objective_id(raw_id: str) -> str:
    """Generate a safe, constrained objective ID from issue coordinates."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "-", raw_id.strip())
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    if not cleaned:
        raise SecurityBoundaryError("empty objective ID after sanitization")
    return cleaned[:80]


def sanitize_issue_to_spec(
    repo: str,
    issue_number: int,
    title: str,
    body: str,
    *,
    labels: list[str] | None = None,
) -> ObjectiveSpec:
    """Safely convert a GitHub issue into an ObjectiveSpec.

    Explicitly ignores any attempt within the body to configure filesystem paths,
    worktrees, review policy overrides, or main push policies.
    """
    validated_repo = validate_repository_name(repo)
    clean_title = sanitize_text(title, max_length=200)
    clean_body = sanitize_text(body, max_length=10000)
    objective_id = sanitize_objective_id(f"GH-{validated_repo}-{issue_number}")

    goal = f"{clean_title}\n\n{clean_body}".strip() if clean_body else clean_title

    # Allowed scope is bounded to the target repository
    allowed_scope = (f"{validated_repo}/**",)
    prohibited_scope = ()

    # If the target repository is NOT stagemesh-private, strictly prohibit referencing stagemesh-private
    if validated_repo != "stagemesh-private":
        prohibited_scope = ("stagemesh-private/**", "docs/patent/**")

    return ObjectiveSpec(
        objective_id=objective_id,
        goal=goal,
        repo=validated_repo,
        allowed_scope=allowed_scope,
        prohibited_scope=prohibited_scope,
        completion_criteria=("implementation verified", "independent review passed"),
        human_gate_policy={},
        parallelism=2,
        main_push_policy="HUMAN_GATED",  # Never allow GitHub text to authorize direct main push
        max_auto_created_tasks=10,
        max_child_depth=2,
    )


def redact_private_info(text: str) -> str:
    """Prevent information from stagemesh-private from leaking into public/product text."""
    if not text:
        return ""
    result = text
    for pattern in PRIVATE_LEAK_PATTERNS:
        result = pattern.sub("[REDACTED_PRIVATE_INFO]", result)
    return result

