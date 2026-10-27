"""Adversarial result/evidence boundary matrix: forged, malformed or stale evidence is refused by the real integration gate.

Existing coverage inventory (not repeated here): review verdict parsing and independence (test_review_reliability),
scope and staleness policy (test_autonomy_review), exact contract/candidate binding of review evidence
(test_review_reliability). This matrix drives the production `Integrator` with hand-forged evidence rows.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_review_terminalization import TASK, _project_with_candidate

from stagemesh.contract_binding import contract_for_candidate
from stagemesh.domain import EvidenceKind, EvidenceStatus
from stagemesh.git import GitWorkspace
from stagemesh.integration import Integrator
from stagemesh.review import independent_review_verified

REF = "refs/heads/integration"
DROP = ...  # sentinel: remove the field from the forged payload


def _rig(tmp_path: Path):
    project, store, candidate = _project_with_candidate(tmp_path)
    git = GitWorkspace(project)
    baseline = git.run("rev-parse", "HEAD~1").stdout.strip()
    git.run("update-ref", REF, baseline)
    bound = contract_for_candidate(store, TASK, candidate, project)
    return project, store, candidate, git, bound


def _review_payload(bound, candidate: str, **overrides) -> dict:
    payload = {
        **bound.evidence_payload(),
        "candidate_sha": candidate,
        "independent_reviewer": True,
        "review_execution_invoked": True,
        "review_provider": "claude",
        "implementer_provider": "codex",
    }
    payload.update(overrides)
    return {k: v for k, v in payload.items() if v is not DROP}


def _validation(store, candidate: str, bound) -> None:
    store.add_evidence(TASK, candidate, EvidenceKind.VALIDATION, EvidenceStatus.PASSED, {**bound.evidence_payload(), "candidate_sha": candidate})


def _integrate(project, store, candidate: str) -> EvidenceStatus:
    return Integrator(REF, require_independent_review=True).integrate(store, TASK, candidate, project)


def _tip(git: GitWorkspace) -> str:
    return git.run("rev-parse", REF).stdout.strip()


def _assert_refused(project, store, candidate, git) -> None:
    before = _tip(git)
    assert _integrate(project, store, candidate) == EvidenceStatus.FAILED
    assert _tip(git) == before  # no SCM mutation
    assert not store.has_evidence(TASK, candidate, EvidenceKind.INTEGRATION, EvidenceStatus.PASSED)


def test_a_well_formed_bound_independent_review_is_accepted_and_lands_only_the_candidate(tmp_path: Path) -> None:
    project, store, candidate, git, bound = _rig(tmp_path)
    _validation(store, candidate, bound)
    store.add_evidence(TASK, candidate, EvidenceKind.REVIEW, EvidenceStatus.PASSED, _review_payload(bound, candidate))

    assert _integrate(project, store, candidate) == EvidenceStatus.PASSED
    assert _tip(git) == candidate


FORGED_INDEPENDENCE = {
    "independent_flag_is_a_string": {"independent_reviewer": "true"},
    "independent_flag_is_one": {"independent_reviewer": 1},
    "independent_flag_false": {"independent_reviewer": False},
    "execution_not_invoked": {"review_execution_invoked": False},
    "execution_flag_is_a_string": {"review_execution_invoked": "true"},
    "execution_flag_missing": {"review_execution_invoked": DROP},
    "reviewer_is_implementer": {"review_provider": "codex"},
    "reviewer_is_implementer_by_case": {"review_provider": "CODEX"},
    "reviewer_is_implementer_by_padding": {"review_provider": " codex "},
    "reviewer_blank": {"review_provider": ""},
    "reviewer_missing": {"review_provider": DROP},
    "implementer_blank": {"implementer_provider": ""},
    "implementer_missing": {"implementer_provider": DROP},
    "reviewer_not_text": {"review_provider": ["claude"]},
    "implementer_not_text": {"implementer_provider": 7},
}


@pytest.mark.parametrize("name", sorted(FORGED_INDEPENDENCE))
def test_forged_independence_claims_never_pass_the_integration_gate(tmp_path: Path, name: str) -> None:
    project, store, candidate, git, bound = _rig(tmp_path)
    payload = _review_payload(bound, candidate, **FORGED_INDEPENDENCE[name])
    assert not independent_review_verified(payload)
    _validation(store, candidate, bound)
    store.add_evidence(TASK, candidate, EvidenceKind.REVIEW, EvidenceStatus.PASSED, payload)

    _assert_refused(project, store, candidate, git)

    (row,) = store.conn.execute("SELECT payload FROM evidence WHERE kind=?", (EvidenceKind.INTEGRATION,))
    integration = json.loads(row["payload"])
    assert integration["independent_review_verified"] is False
    assert any("independent REVIEW" in f["message"] for f in integration["findings"])


STALE_OR_UNBOUND = {
    "other_candidate_in_payload": {"candidate_sha": "c" * 40},
    "contract_hash_changed": {"contract_hash": "0" * 64},
    "contract_hash_missing": {"contract_hash": DROP},
    "contract_version_changed": {"contract_version": 999},
    "contract_version_as_text": {"contract_version": "1"},
    "baseline_changed": {"baseline_sha": "1" * 40},
    "baseline_missing": {"baseline_sha": DROP},
    "candidate_missing": {"candidate_sha": DROP},
}


@pytest.mark.parametrize("name", sorted(STALE_OR_UNBOUND))
def test_stale_or_unbound_review_evidence_is_refused_without_moving_the_ref(tmp_path: Path, name: str) -> None:
    project, store, candidate, git, bound = _rig(tmp_path)
    _validation(store, candidate, bound)
    store.add_evidence(TASK, candidate, EvidenceKind.REVIEW, EvidenceStatus.PASSED, _review_payload(bound, candidate, **STALE_OR_UNBOUND[name]))

    _assert_refused(project, store, candidate, git)


def test_review_without_validation_does_not_satisfy_the_gate(tmp_path: Path) -> None:
    project, store, candidate, git, bound = _rig(tmp_path)
    store.add_evidence(TASK, candidate, EvidenceKind.REVIEW, EvidenceStatus.PASSED, _review_payload(bound, candidate))

    _assert_refused(project, store, candidate, git)


def test_non_passing_review_statuses_never_satisfy_the_gate(tmp_path: Path) -> None:
    project, store, candidate, git, bound = _rig(tmp_path)
    _validation(store, candidate, bound)
    for status in (EvidenceStatus.FAILED, EvidenceStatus.CAPACITY):
        store.add_evidence(TASK, candidate, EvidenceKind.REVIEW, status, _review_payload(bound, candidate))

    _assert_refused(project, store, candidate, git)


# Follow-up (production defect, not fixed here): valid-JSON non-object payloads such as "[]" or "null" make
# integration._has_bound_evidence raise AttributeError (payload.get) instead of being skipped. It still never trusts them.
@pytest.mark.parametrize("raw", ["not json", "", '{"candidate_sha":'])
def test_malformed_stored_review_payloads_are_skipped_not_trusted(tmp_path: Path, raw: str) -> None:
    project, store, candidate, git, bound = _rig(tmp_path)
    _validation(store, candidate, bound)
    store.conn.execute(
        "INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?)", ("forged", TASK, candidate, EvidenceKind.REVIEW, EvidenceStatus.PASSED, raw, 0.0)
    )
    store.conn.commit()

    _assert_refused(project, store, candidate, git)


def test_a_forged_pass_for_another_task_or_sha_does_not_authorize_this_candidate(tmp_path: Path) -> None:
    project, store, candidate, git, bound = _rig(tmp_path)
    store.upsert_task("other task", source="local", source_id="OTHER")
    _validation(store, candidate, bound)
    store.add_evidence("OTHER", candidate, EvidenceKind.REVIEW, EvidenceStatus.PASSED, _review_payload(bound, candidate))
    store.add_evidence(TASK, "d" * 40, EvidenceKind.REVIEW, EvidenceStatus.PASSED, _review_payload(bound, candidate))

    _assert_refused(project, store, candidate, git)


def test_refusal_leaves_the_candidate_set_and_worktree_untouched(tmp_path: Path) -> None:
    project, store, candidate, git, bound = _rig(tmp_path)
    _validation(store, candidate, bound)
    store.add_evidence(TASK, candidate, EvidenceKind.REVIEW, EvidenceStatus.PASSED, _review_payload(bound, candidate, review_provider="codex"))
    head = git.head()
    candidates = store.conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]

    assert _integrate(project, store, candidate) == EvidenceStatus.FAILED

    dirty = [line for line in git.run("status", "--porcelain").stdout.splitlines() if ".stagemesh/" not in line]
    assert git.head() == head and dirty == []
    assert store.conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == candidates
