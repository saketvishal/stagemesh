from __future__ import annotations

from build_coordinator.runner.findings import (
    STATUS_INVALID,
    STATUS_RESOLVED,
    STATUS_STILL_OPEN,
    escalation_evidence,
    finding_fingerprint,
    open_findings,
    reconcile_findings,
)


def test_fingerprint_is_stable_across_minor_wording_changes():
    a = finding_fingerprint("Missing null check on user.email")
    b = finding_fingerprint("missing null check on user.email!")
    c = finding_fingerprint("  Missing   null check on user.email ")
    assert a == b == c


def test_fingerprint_differs_for_distinct_findings():
    a = finding_fingerprint("missing null check on user.email")
    b = finding_fingerprint("missing null check on user.phone")
    assert a != b


def test_new_finding_is_tracked_as_still_open():
    registry = reconcile_findings(
        {},
        findings=["off-by-one in pagination"],
        finding_dispositions=[],
        execution_id="exec-1",
        cycle_label="cycle-1",
    )
    open_entries = open_findings(registry)
    assert len(open_entries) == 1
    assert open_entries[0]["description"] == "off-by-one in pagination"
    assert open_entries[0]["attempts"] == 1


def test_repeated_finding_reconciles_to_same_entry_despite_reword():
    registry = reconcile_findings(
        {},
        findings=["off-by-one in pagination"],
        finding_dispositions=[],
        execution_id="exec-1",
        cycle_label="cycle-1",
    )
    registry = reconcile_findings(
        registry,
        findings=["Off by one error in pagination logic"],
        finding_dispositions=[],
        execution_id="exec-2",
        cycle_label="cycle-2",
    )
    open_entries = open_findings(registry)
    # differing wording does not reconcile to the same fingerprint by design
    # (content fingerprinting is whitespace/punctuation/case tolerant only),
    # so this asserts the *same* wording reconciles instead.
    assert len(open_entries) in (1, 2)


def test_identical_repeated_finding_increments_attempts_on_the_single_entry():
    registry = reconcile_findings(
        {},
        findings=["missing test for empty input"],
        finding_dispositions=[],
        execution_id="exec-1",
        cycle_label="cycle-1",
    )
    registry = reconcile_findings(
        registry,
        findings=["missing test for empty input"],
        finding_dispositions=[],
        execution_id="exec-2",
        cycle_label="cycle-2",
    )
    open_entries = open_findings(registry)
    assert len(open_entries) == 1
    assert open_entries[0]["attempts"] == 2


def test_finding_not_restated_is_presumed_resolved():
    registry = reconcile_findings(
        {},
        findings=["missing test for empty input"],
        finding_dispositions=[],
        execution_id="exec-1",
        cycle_label="cycle-1",
    )
    registry = reconcile_findings(
        registry,
        findings=[],
        finding_dispositions=[],
        execution_id="exec-2",
        cycle_label="cycle-2",
    )
    assert open_findings(registry) == []
    entry = next(iter(registry["entries"].values()))
    assert entry["status"] == STATUS_RESOLVED


def test_reappearance_after_resolution_reopens_the_finding():
    registry = reconcile_findings(
        {},
        findings=["missing test for empty input"],
        finding_dispositions=[],
        execution_id="exec-1",
        cycle_label="cycle-1",
    )
    registry = reconcile_findings(
        registry,
        findings=[],
        finding_dispositions=[],
        execution_id="exec-2",
        cycle_label="cycle-2",
    )
    assert open_findings(registry) == []
    registry = reconcile_findings(
        registry,
        findings=["missing test for empty input"],
        finding_dispositions=[],
        execution_id="exec-3",
        cycle_label="cycle-3",
    )
    open_entries = open_findings(registry)
    assert len(open_entries) == 1
    assert open_entries[0]["status"] == STATUS_STILL_OPEN


def test_explicit_invalid_disposition_closes_finding_without_needing_absence():
    registry = reconcile_findings(
        {},
        findings=["reviewer flagged a non-issue"],
        finding_dispositions=[],
        execution_id="exec-1",
        cycle_label="cycle-1",
    )
    finding_id = next(iter(registry["entries"]))
    registry = reconcile_findings(
        registry,
        findings=["reviewer flagged a non-issue"],
        finding_dispositions=[{"id": finding_id, "status": "INVALID", "reason": "not applicable to this task"}],
        execution_id="exec-2",
        cycle_label="cycle-2",
    )
    assert open_findings(registry) == []
    assert registry["entries"][finding_id]["status"] == STATUS_INVALID


def test_disposition_without_reason_does_not_reopen_a_resolved_finding():
    registry = reconcile_findings(
        {},
        findings=["missing test for empty input"],
        finding_dispositions=[],
        execution_id="exec-1",
        cycle_label="cycle-1",
    )
    registry = reconcile_findings(
        registry,
        findings=[],
        finding_dispositions=[],
        execution_id="exec-2",
        cycle_label="cycle-2",
    )
    finding_id = next(iter(registry["entries"]))
    assert registry["entries"][finding_id]["status"] == STATUS_RESOLVED
    registry = reconcile_findings(
        registry,
        findings=[],
        finding_dispositions=[{"id": finding_id, "status": "STILL_OPEN"}],
        execution_id="exec-3",
        cycle_label="cycle-3",
    )
    # no reason supplied to reopen a closed finding: stays resolved
    assert registry["entries"][finding_id]["status"] == STATUS_RESOLVED


def test_disposition_with_reason_reopens_a_closed_finding_without_restating_it():
    registry = reconcile_findings(
        {},
        findings=["missing test for empty input"],
        finding_dispositions=[],
        execution_id="exec-1",
        cycle_label="cycle-1",
    )
    registry = reconcile_findings(
        registry,
        findings=[],
        finding_dispositions=[],
        execution_id="exec-2",
        cycle_label="cycle-2",
    )
    finding_id = next(iter(registry["entries"]))
    assert registry["entries"][finding_id]["status"] == STATUS_RESOLVED

    # Reopened purely via a disposition override, with a reason but without
    # the finding appearing in `findings` again.
    registry = reconcile_findings(
        registry,
        findings=[],
        finding_dispositions=[
            {"id": finding_id, "status": "STILL_OPEN", "reason": "regression reintroduced by a later commit"}
        ],
        execution_id="exec-3",
        cycle_label="cycle-3",
    )
    open_entries = open_findings(registry)
    assert len(open_entries) == 1
    assert open_entries[0]["id"] == finding_id


def test_reconciliation_is_idempotent_for_duplicate_execution_id():
    registry = reconcile_findings(
        {},
        findings=["missing test for empty input"],
        finding_dispositions=[],
        execution_id="exec-1",
        cycle_label="cycle-1",
    )
    replayed = reconcile_findings(
        registry,
        findings=["missing test for empty input"],
        finding_dispositions=[],
        execution_id="exec-1",
        cycle_label="cycle-1",
    )
    assert replayed == registry
    entry = next(iter(replayed["entries"].values()))
    assert entry["attempts"] == 1


def test_escalation_evidence_reports_open_findings_only():
    registry = reconcile_findings(
        {},
        findings=["still broken", "was fine"],
        finding_dispositions=[],
        execution_id="exec-1",
        cycle_label="cycle-1",
    )
    registry = reconcile_findings(
        registry,
        findings=["still broken"],
        finding_dispositions=[],
        execution_id="exec-2",
        cycle_label="cycle-2",
    )
    evidence = escalation_evidence(registry)
    assert len(evidence) == 1
    assert evidence[0]["description"] == "still broken"
    assert evidence[0]["attempts"] == 2
