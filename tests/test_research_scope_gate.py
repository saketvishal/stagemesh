"""Exact research scope narrows candidate admission without expanding the contract."""

from __future__ import annotations

import dataclasses
import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from stagemesh.contracts import BoundChangeContract, ChangeContract, canonical_contract_json
from stagemesh.coordinator import Coordinator
from stagemesh.domain import Stage
from stagemesh.persistence import Store
from stagemesh.research_context import (
    ResearchFinding,
    ResearchScopeDecision,
    build_research_evidence,
    research_scope_gate,
)

TASK = "scope-task"


def git(project: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=project, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def rig(tmp_path: Path):
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src/a.py").write_text("A = 1\n", encoding="utf-8")
    (project / "src/b.py").write_text("B = 1\n", encoding="utf-8")
    git(project, "init", "-q")
    git(project, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    git(project, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    baseline = git(project, "rev-parse", "HEAD")
    contract = ChangeContract(objective="Update scoped files", allowed_files=("src/**",))
    canonical = canonical_contract_json(contract)
    bound = BoundChangeContract(
        contract,
        1,
        hashlib.sha256(canonical.encode()).hexdigest(),
        canonical,
        baseline,
    )
    store = Store(tmp_path / "store.sqlite3")
    store.migrate()
    return project, store, bound


def record_scope(
    project: Path,
    store: Store,
    bound: BoundChangeContract,
    *,
    recommended: tuple[str, ...],
    no_touch: tuple[str, ...] = (),
    enforced: bool = True,
) -> None:
    evidence = build_research_evidence(
        project,
        bound,
        inspected_paths=("src/a.py", "src/b.py"),
        findings=(ResearchFinding("src/a.py", "Primary implementation path."),),
        unresolved_scope_gaps=(),
        provenance=("local read-only inspection",),
        recommended_implementation_paths=recommended,
        no_touch_paths=no_touch,
    )
    store.add_audit_event(
        "research_stage",
        {
            "task_id": TASK,
            "outcome": "accepted",
            "baseline_sha": bound.baseline_sha,
            "contract_digest": bound.digest,
            "scope_enforced": enforced,
            "report": dataclasses.asdict(evidence),
        },
    )


def commit(project: Path, path: str, text: str, message: str) -> str:
    (project / path).write_text(text, encoding="utf-8")
    git(project, "add", "--", path)
    git(project, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message)
    return git(project, "rev-parse", "HEAD")


def test_exact_recommendation_allows_candidate_and_drift_fails_closed(rig):
    project, store, bound = rig
    record_scope(project, store, bound, recommended=("src/a.py",))
    first = commit(project, "src/a.py", "A = 2\n", "change a")

    accepted = research_scope_gate(store, TASK, project, bound, first, required=True)

    assert accepted.allowed and accepted.reason == "accepted"
    assert accepted.changed_paths == ("src/a.py",)
    assert accepted.report_digest and len(accepted.report_digest) == 64

    second = commit(project, "src/b.py", "B = 2\n", "change b")
    drift = research_scope_gate(store, TASK, project, bound, second, required=True)
    assert not drift.allowed and drift.reason == "research_scope_drift"
    assert drift.outside_recommendation == ("src/b.py",)


def test_no_touch_path_overrides_recommendation(rig):
    project, store, bound = rig
    record_scope(
        project,
        store,
        bound,
        recommended=("src/a.py",),
        no_touch=("src/b.py",),
    )
    candidate = commit(project, "src/b.py", "B = 2\n", "touch forbidden research path")

    decision = research_scope_gate(store, TASK, project, bound, candidate, required=True)

    assert not decision.allowed and decision.reason == "research_no_touch_violation"
    assert decision.no_touch_violations == ("src/b.py",)


def test_missing_or_non_enforced_scope_cannot_admit_candidate(rig):
    project, store, bound = rig
    candidate = commit(project, "src/a.py", "A = 2\n", "change a")
    assert research_scope_gate(store, TASK, project, bound, candidate, required=False).allowed
    assert research_scope_gate(store, TASK, project, bound, candidate, required=True).reason == "research_scope_missing"

    record_scope(project, store, bound, recommended=("src/a.py",), enforced=False)
    refused = research_scope_gate(store, TASK, project, bound, candidate, required=True)
    assert not refused.allowed and refused.reason == "research_scope_missing"


def test_coordinator_blocks_before_validation_when_required_scope_refuses(tmp_path, monkeypatch):
    from stagemesh import coordinator as coordinator_module

    store = Store(tmp_path / "coordinator.sqlite3")
    store.migrate()
    store.upsert_task("Scoped candidate", source_id=TASK)
    candidate = "c" * 40
    store.add_candidate(TASK, candidate, "provider", durable_handoff=True)
    store.advance_task(TASK, Stage.VALIDATE)
    coordinator = Coordinator(store, tmp_path)
    monkeypatch.setattr(coordinator, "_workspace_intact", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        coordinator_module,
        "load_config",
        lambda _project: SimpleNamespace(research=SimpleNamespace(enforce_scope=True)),
    )
    monkeypatch.setattr(
        coordinator_module,
        "contract_for_candidate",
        lambda *args: SimpleNamespace(digest="d" * 64, baseline_sha="b" * 40),
    )
    monkeypatch.setattr(
        coordinator_module,
        "research_scope_gate",
        lambda *args, **kwargs: ResearchScopeDecision(
            False,
            "research_scope_drift",
            "e" * 64,
            ("src/outside.py",),
            ("src/outside.py",),
        ),
    )

    assert coordinator.tick() == 1

    assert store.get_task(TASK)["status"] == "BLOCKED"
    assert not store.conn.execute("SELECT 1 FROM evidence WHERE task_id=?", (TASK,)).fetchall()
    admission = store.conn.execute(
        "SELECT payload FROM audit_events WHERE event_type='research_scope.admission'",
    ).fetchone()
    assert "research_scope_drift" in admission["payload"]
