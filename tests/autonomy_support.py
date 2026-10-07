"""Deterministic local fixtures for the Founder Hands-Off incident corpus: real git repositories, a real SQLite store, no network."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from stagemesh.contracts import canonical_contract_json, parse_contract
from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage
from stagemesh.persistence import Store

TASK = "TASK-1"
STAGEMESH = ("StageMesh", "stagemesh@stagemesh.invalid")
HUMAN = ("Other Dev", "other.dev@example.com")  # a second writer: not a StageMesh identity
CONTRACT = {"objective": "add the widget", "allowed_files": ["src/**", "tests/**"], "acceptance_criteria": ["widget works"]}


def git(repo: Path, *args: str, who: tuple[str, str] = STAGEMESH, check: bool = True) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": who[0],
        "GIT_AUTHOR_EMAIL": who[1],
        "GIT_COMMITTER_NAME": who[0],
        "GIT_COMMITTER_EMAIL": who[1],
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, env=env, check=False, encoding="utf-8")
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "core.autocrlf", "false")
    return path


def commit(repo: Path, files: dict[str, str | None], message: str, *, who: tuple[str, str] = STAGEMESH) -> str:
    """Write (or delete, with None) files, commit them as `who`, return the new HEAD."""
    for name, content in files.items():
        target = repo / name
        if content is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message, who=who)
    return git(repo, "rev-parse", "HEAD")


def tree_of(repo: Path, rev: str) -> str:
    return git(repo, "rev-parse", f"{rev}^{{tree}}")


def new_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "state" / "stagemesh.sqlite3")
    store.migrate()
    return store


def contract_binding_args(contract: dict | None = None) -> tuple[int, str, str]:
    import hashlib

    canonical = canonical_contract_json(parse_contract(contract or CONTRACT))
    return 1, hashlib.sha256(canonical.encode("utf-8")).hexdigest(), canonical


def seed_candidate(
    store: Store,
    task_id: str,
    *,
    baseline: str,
    candidate: str,
    producer: str = "codex",
    stage: Stage = Stage.VALIDATE,
    contract: dict | None = None,
) -> None:
    """A task with a durable candidate bound to the standard contract, as the coordinator would leave it."""
    store.upsert_task("add the widget", source_id=task_id)
    store.set_task_baseline(task_id, baseline)
    store.add_candidate(task_id, candidate, producer, durable_handoff=True)
    version, digest, canonical = contract_binding_args(contract)
    store.bind_contract(task_id, candidate, baseline, version, digest, canonical)
    store.advance_task(task_id, stage)


def add_passing_evidence(
    store: Store, task_id: str, sha: str, *, baseline: str, independent: bool = True, contract: dict | None = None
) -> None:
    version, digest, _ = contract_binding_args(contract)
    bound = {"contract_version": version, "contract_hash": digest, "baseline_sha": baseline, "candidate_sha": sha}
    store.add_evidence(task_id, sha, EvidenceKind.VALIDATION, EvidenceStatus.PASSED, bound)
    review = {
        **bound,
        "review_provider": "claude" if independent else "codex",
        "implementer_provider": "codex",
        "independent_reviewer": independent,
        "review_execution_invoked": True,
    }
    store.add_evidence(task_id, sha, EvidenceKind.REVIEW, EvidenceStatus.PASSED, review)


def decisions(store: Store, task_id: str | None = None) -> list[dict]:
    rows = store.conn.execute(
        "SELECT payload FROM audit_events WHERE event_type='autonomy.decision' ORDER BY created_at, rowid"
    ).fetchall()
    out = [json.loads(row["payload"]) for row in rows]
    return [d for d in out if task_id is None or d["task_id"] == task_id]
