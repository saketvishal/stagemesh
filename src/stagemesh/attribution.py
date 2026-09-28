from __future__ import annotations

from dataclasses import dataclass


class AttributionValidationError(ValueError):
    pass


@dataclass(frozen=True)
class GitAttribution:
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str


def attribution_for_worker(worker_id: str, provider: str) -> GitAttribution:
    worker_id = _display_value(worker_id, "worker id")
    provider = _display_value(provider, "provider")
    safe_worker = _email_token(worker_id, "worker id")
    safe_provider = _email_token(provider, "provider")
    local = f"{safe_provider or 'provider'}+{safe_worker or 'worker'}"
    email = f"{local}@stagemesh.invalid"
    return GitAttribution(
        author_name=f"StageMesh {provider} worker {worker_id}",
        author_email=email,
        committer_name="StageMesh",
        committer_email="stagemesh@stagemesh.invalid",
    )


def _display_value(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise AttributionValidationError(f"{field} must be a string")
    cleaned = " ".join(value.split())
    if not cleaned:
        raise AttributionValidationError(f"{field} must be a non-empty string")
    if len(cleaned) > 100:
        raise AttributionValidationError(f"{field} must be 100 characters or fewer")
    return cleaned


def _email_token(value: str, field: str) -> str:
    token = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value).strip("-")
    if not token:
        raise AttributionValidationError(f"{field} must contain at least one email-safe character")
    return token[:80]
