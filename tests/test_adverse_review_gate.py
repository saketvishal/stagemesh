"""Adverse review gate: integration is refused without executed, bound adverse review; findings reach remediation and must clear."""
from __future__ import annotations

from types import SimpleNamespace

from stagemesh import run_ready
from stagemesh.config import AdverseReviewPolicy
from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage
from stagemesh.persistence import Store
from stagemesh.remediation import findings_to_verify, unresolved_findings
from stagemesh.review import adverse_gate

TASK, OLD, NEW, DIGEST = "T-1", "a" * 40, "b" * 40, "c" * 64
POLICY = AdverseReviewPolicy()


def _store(tmp_path) -> Store:
    store = Store(tmp_path / "s.sqlite3")
    store.migrate()
    store.upsert_task("gate", source="local", source_id=TASK)
    store.add_candidate(TASK, OLD, produced_by="codex", durable_handoff=True)
    store.add_candidate(TASK, NEW, produced_by="codex", durable_handoff=True)
    return store


def _review(store, sha=NEW, **overrides):
    payload = {
        "contract_hash": DIGEST, "adverse_review_gate": True, "independent_reviewer": True, "review_execution_invoked": True,
        "implementer_provider": "codex", "review_provider": "claude", "adverse_policy_digest": POLICY.digest(), **overrides,
    }
    store.add_evidence(TASK, sha, EvidenceKind.REVIEW, EvidenceStatus.PASSED, payload)


def _gate(store, **kwargs):
    return adverse_gate(store, TASK, NEW, DIGEST, POLICY, **kwargs)


def test_executed_independent_bound_review_is_accepted(tmp_path):
    store = _store(tmp_path)
    _review(store)
    store.upsert_finding("f1", TASK, NEW, "nit", "style only")
    assert _gate(store).allowed


def test_missing_or_weaker_review_blocks(tmp_path):
    store = _store(tmp_path)
    assert "missing required adverse review" in _gate(store).reasons[0]
    for weaker in ({"review_execution_invoked": False}, {"independent_reviewer": False}, {"adverse_review_gate": False},
                   {"adverse_policy_digest": "0" * 64}, {"review_provider": "codex"}):
        store = _store(tmp_path / str(sorted(weaker)))
        _review(store, **weaker)
        assert not _gate(store).allowed, weaker


def test_review_of_another_candidate_does_not_count(tmp_path):
    store = _store(tmp_path)
    _review(store, sha=OLD)
    assert not _gate(store).allowed


def test_high_severity_finding_blocks_even_with_passing_review(tmp_path):
    store = _store(tmp_path)
    _review(store)
    store.upsert_finding("f1", TASK, NEW, "high", "sql injection")
    decision = _gate(store)
    assert not decision.allowed and "sql injection" in decision.reasons[0]


def test_same_candidate_cannot_replace_a_failed_adverse_review_with_a_pass(tmp_path):
    store = _store(tmp_path)
    store.add_evidence(
        TASK,
        NEW,
        EvidenceKind.REVIEW,
        EvidenceStatus.FAILED,
        {"contract_hash": DIGEST, "findings": [{"severity": "major", "message": "needs repair"}]},
    )
    _review(store)
    decision = _gate(store)
    assert not decision.allowed
    assert "must be replaced" in decision.reasons[0]


def test_findings_feed_remediation_and_must_be_resolved_on_new_candidate(tmp_path):
    store = _store(tmp_path)
    store.upsert_finding("f-old", TASK, OLD, "high", "unchecked path")
    store.add_task_remediation(TASK, "REVIEW", OLD)
    _review(store)
    assert [f["message"] for f in findings_to_verify(store, TASK, NEW)] == ["unchecked path"]
    assert unresolved_findings(store, TASK, NEW) == []
    assert _gate(store).allowed
    store.upsert_finding("f-new", TASK, NEW, "low", "unchecked path")  # same defect re-raised at advisory severity
    assert [f["message"] for f in unresolved_findings(store, TASK, NEW)] == ["unchecked path"]
    assert not _gate(store).allowed


def test_override_needs_actor_and_reason_and_is_audited(tmp_path):
    store = _store(tmp_path)
    assert not _gate(store, override={"actor": "op"}).allowed
    assert not _gate(store, override={"actor": " ", "reason": "x"}).allowed
    assert not store.conn.execute("SELECT 1 FROM audit_events WHERE event_type='adverse_review.override'").fetchall()
    decision = _gate(store, override={"actor": "op", "reason": "accepted risk"})
    assert decision.allowed and decision.overridden and decision.reasons
    row = store.conn.execute("SELECT payload FROM audit_events WHERE event_type='adverse_review.override'").fetchone()
    assert "accepted risk" in row["payload"] and "op" in row["payload"]


def test_drive_task_refuses_to_tick_integration_without_adverse_review(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.advance_task(TASK, Stage.INTEGRATE)
    monkeypatch.setattr(run_ready, "contract_for_candidate", lambda *a: SimpleNamespace(digest=DIGEST))
    ticked = []
    coordinator = SimpleNamespace(tick=lambda: ticked.append(1) or 1)
    summary = run_ready.RunSummary(True, "UNSET", task_id=TASK)
    monkeypatch.setattr(run_ready, "workspace_info", lambda *a: {})
    run_ready.drive_task(store, tmp_path, coordinator, TASK, summary, max_steps=1)
    assert ticked == [] and summary.stop_reason == "BLOCKED" and "adverse review" in summary.message
    assert store.conn.execute("SELECT 1 FROM audit_events WHERE event_type='adverse_review.blocked'").fetchone()


def test_run_ready_resolves_the_configured_adverse_policy(tmp_path, monkeypatch):
    configured = AdverseReviewPolicy(("critical",), "hold")
    monkeypatch.setattr(run_ready, "load_config", lambda _project: SimpleNamespace(adverse_review=configured))
    assert run_ready._configured_adverse_policy(tmp_path, TASK, None) is configured
    assert run_ready._configured_adverse_policy(tmp_path, TASK, POLICY) is POLICY


def test_drive_task_fails_closed_when_adverse_policy_config_is_invalid(tmp_path, monkeypatch):
    store = _store(tmp_path)
    ticked = []
    summary = run_ready.RunSummary(True, "UNSET", task_id=TASK)
    monkeypatch.setattr(
        run_ready,
        "load_config",
        lambda _project: (_ for _ in ()).throw(run_ready.ConfigValidationError("bad adverse policy")),
    )
    run_ready.drive_task(
        store,
        tmp_path,
        SimpleNamespace(require_independent_review=True, tick=lambda: ticked.append(1) or 1),
        TASK,
        summary,
        max_steps=1,
    )
    assert ticked == []
    assert summary.stop_reason == "REFUSED:invalid_config"
    assert "adverse review policy unknown" in summary.message
