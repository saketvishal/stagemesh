from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GitAttribution:
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str


def attribution_for_worker(worker_id: str, provider: str) -> GitAttribution:
    safe_worker = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in worker_id).strip("-")
    safe_provider = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in provider).strip("-")
    local = f"{safe_provider or 'provider'}+{safe_worker or 'worker'}"
    email = f"{local}@stagemesh.invalid"
    return GitAttribution(
        author_name=f"StageMesh {provider} worker {worker_id}",
        author_email=email,
        committer_name="StageMesh",
        committer_email="stagemesh@stagemesh.invalid",
    )
