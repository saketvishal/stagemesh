"""Review reliability: a reviewer's answer is a strict verdict; anything incomplete is review infrastructure trouble, never a code defect."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_parallel import Rig, ScriptedExecutor
from test_provider_pool import IMPLEMENT, REVIEW
from test_provider_pool import Rig as PoolRig
from test_review_terminalization import TASK as UNIT_TASK
from test_review_terminalization import JsonReviewAdapter, _project_with_candidate, _review_execution
from test_single_task_stale_rebase import TASK, _advance_until

from stagemesh.contract_binding import contract_for_candidate
from stagemesh.coordinator import Coordinator
from stagemesh.domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus, Stage, TaskStatus
from stagemesh.git import GitWorkspace
from stagemesh.integration import _has_exact_bound_evidence
from stagemesh.provider_pool import _infrastructure_reason
from stagemesh.review import (
    INFRASTRUCTURE_FAILURE,
    REVIEW_INCOMPLETE_RETRIES,
    VERDICT_FAIL_WITH_FINDINGS,
    VERDICT_PASS,
    VERDICT_REVIEW_INCOMPLETE,
    Reviewer,
    classify_review_response,
)
from stagemesh.serialized_integration import SerializedIntegrator

FINDING = {"severity": "error", "message": "README.md drops the install section the contract requires", "path": "README.md"}


class ScriptedReviewAdapter:
    """Answers from a fixed script, one entry per call (the last repeats), and counts calls."""

    def __init__(self, *responses: str, name: str = "reviewer") -> None:
        self.name = name
        self.responses = list(responses)
        self.calls = 0

    def review(self, prompt: str) -> str:
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response


def _evidence(store, kind: EvidenceKind, status: EvidenceStatus | None = None) -> list[dict]:
    query, args = "SELECT payload, status FROM evidence WHERE kind=?", [kind]
    if status is not None:
        query, args = query + " AND status=?", [kind, status]
    return [{**json.loads(r["payload"]), "_status": r["status"]} for r in store.conn.execute(query + " ORDER BY created_at", args)]


# ---- the protocol boundary ---------------------------------------------------------------------------------------------------------


def test_complete_pass_is_accepted_and_bound_to_the_exact_candidate_and_contract(tmp_path: Path) -> None:
    project, store, candidate = _project_with_candidate(tmp_path)
    adapter = ScriptedReviewAdapter('{"decision":"PASS"}')

    assert Reviewer(adapter=adapter, require_independent=True).review(store, UNIT_TASK, candidate, project) == EvidenceStatus.PASSED

    (payload,) = _evidence(store, EvidenceKind.REVIEW, EvidenceStatus.PASSED)
    bound = contract_for_candidate(store, UNIT_TASK, candidate, project)
    assert payload["review_verdict"] == VERDICT_PASS and payload["review_attempts"] == 1
    assert (payload["candidate_sha"], payload["contract_hash"], payload["contract_version"]) == (candidate, bound.digest, bound.version)
    assert payload["independent_reviewer"] is True and payload["review_execution_invoked"] is True
    assert store.has_bound_evidence(UNIT_TASK, candidate, EvidenceKind.REVIEW, bound.digest, EvidenceStatus.PASSED)


@pytest.mark.parametrize("decision", [VERDICT_FAIL_WITH_FINDINGS, "FAIL"])
def test_valid_fail_with_findings_records_actionable_findings_against_the_candidate(tmp_path: Path, decision: str) -> None:
    project, store, candidate = _project_with_candidate(tmp_path)
    adapter = ScriptedReviewAdapter(json.dumps({"decision": decision, "findings": [FINDING]}))

    status = Reviewer(adapter=adapter, require_independent=True).review(store, UNIT_TASK, candidate, project)

    assert status == EvidenceStatus.FAILED and adapter.calls == 1
    (finding,) = store.open_findings_for_candidate(UNIT_TASK, candidate)
    assert finding["message"] == FINDING["message"]
    (payload,) = _evidence(store, EvidenceKind.REVIEW, EvidenceStatus.FAILED)
    assert payload["review_verdict"] == VERDICT_FAIL_WITH_FINDINGS and payload["candidate_sha"] == candidate


INCOMPLETE_RESPONSES = {
    "progress": "Review in progress",
    "intent": "I will inspect the candidate and report back.",
    "empty": "",
    "whitespace": "  \n ",
    "truncated": '{"decision":"FAIL","findings":[{"severity":"error","message":"README.md dro',
    "truncated_pass": '{"decision":"PA',
    "fail_without_findings": '{"decision":"FAIL"}',
    "fail_empty_findings": '{"decision":"FAIL_WITH_FINDINGS","findings":[]}',
    "fail_placeholder_finding": '{"decision":"FAIL","findings":[{"severity":"error","message":"Review in progress"}]}',
    "fail_intent_finding": '{"decision":"FAIL_WITH_FINDINGS","findings":["I will inspect the candidate"]}',
    "fail_blank_finding": '{"decision":"FAIL","findings":[{"severity":"error","message":"   "},{"severity":"error"}]}',
    "pass_with_findings": json.dumps({"decision": "PASS", "findings": [FINDING]}),
    "no_decision": '{"verdict":"PASS"}',
    "decision_not_text": '{"decision":true}',
    "unknown_decision": '{"decision":"MAYBE"}',
    "not_json": "looks fine to me, approved",
    "prose_echo_of_the_format": 'I will inspect the candidate, then answer {"decision":"PASS"} or {"decision":"FAIL","findings":[]}.',
    "progress_around_a_verdict": 'Review in progress, I will inspect the candidate now.\n{"decision":"PASS"}',
    "two_conflicting_verdicts": '{"decision":"PASS"}\n{"decision":"FAIL_WITH_FINDINGS","findings":[{"message":"README.md is wrong"}]}',
    "reviewer_declares_incomplete": '{"decision":"REVIEW_INCOMPLETE","reason":"ran out of context"}',
    "json_list": '[{"decision":"PASS"}]',
    "pass_with_dict_findings": '{"decision":"PASS","findings":{"message":"README.md has a real defect"}}',
    "pass_with_text_findings": '{"decision":"PASS","findings":"README.md has a real defect"}',
    "conflict_inside_fence": '```json\n{"decision":"PASS"}\n{"decision":"FAIL","findings":[{"message":"README.md is wrong"}]}\n```',
    "conflict_inside_text_envelope": json.dumps({"text": '{"decision":"PASS"}\n{"decision":"FAIL","findings":[{"message":"README.md is wrong"}]}'}),
    "duplicated_identical_verdict": '{"decision":"PASS"}\n{"decision":"PASS"}',
    "duplicated_inside_text_envelope": json.dumps({"text": '{"decision":"PASS"}\n{"decision":"PASS"}'}),
    "envelope_text_and_structured_conflict": json.dumps(
        {"text": '{"decision":"PASS"}', "structured_output": {"decision": "FAIL_WITH_FINDINGS", "findings": [{"message": "README.md is wrong"}]}}
    ),
    "fenced_progress_around_a_verdict": '```json\nReview in progress.\n{"decision":"PASS"}\n```',
    "preliminary_text_with_structured_pass": json.dumps({"text": "Review in progress", "structured_output": {"decision": "PASS"}}),
    "malformed_text_with_structured_pass": json.dumps({"text": "looks fine", "structured_output": {"decision": "PASS"}}),
    "decision_with_padding": '{"decision":" PASS "}',
    "decision_lowercase": '{"decision":"pass"}',
    "decision_misspelt_case": '{"decision":"fail_with_findings","findings":[{"message":"README.md is wrong"}]}',
    "chatter_around_a_verdict_in_a_text_envelope": json.dumps({"text": 'Review in progress. I will inspect now.\n{"decision":"PASS"}'}),
    "chatter_around_a_fail_in_a_text_envelope": json.dumps(
        {"text": 'I will inspect the candidate.\n{"decision":"FAIL","findings":[{"message":"README.md is wrong"}]}'}
    ),
    "chatter_text_with_matching_structured_output": json.dumps(
        {"text": 'Review in progress.\n{"decision":"PASS"}', "structured_output": {"decision": "PASS"}}
    ),
    "envelope_with_progress_text": json.dumps({"text": "Review in progress"}),
}


@pytest.mark.parametrize("name", sorted(INCOMPLETE_RESPONSES))
def test_incomplete_or_malformed_responses_are_review_infrastructure_failures_not_defects(tmp_path: Path, name: str) -> None:
    project, store, candidate = _project_with_candidate(tmp_path)
    adapter = ScriptedReviewAdapter(INCOMPLETE_RESPONSES[name])

    status = Reviewer(adapter=adapter, require_independent=True).review(store, UNIT_TASK, candidate, project)

    assert classify_review_response(INCOMPLETE_RESPONSES[name]).kind == VERDICT_REVIEW_INCOMPLETE
    assert status == EvidenceStatus.CAPACITY
    assert adapter.calls == 1 + REVIEW_INCOMPLETE_RETRIES  # bounded retry, then give up
    assert store.open_findings_for_candidate(UNIT_TASK, candidate) == []  # nothing for remediation to act on
    (payload,) = _evidence(store, EvidenceKind.REVIEW, EvidenceStatus.CAPACITY)
    assert payload["review_verdict"] == VERDICT_REVIEW_INCOMPLETE and payload["review_infrastructure_failure"]
    assert not _evidence(store, EvidenceKind.REVIEW, EvidenceStatus.FAILED) and not _evidence(store, EvidenceKind.REVIEW, EvidenceStatus.PASSED)
    execution = _review_execution(store)
    assert execution["status"] == ExecutionStatus.FAILED and execution["result"] == payload["review_infrastructure_failure"][:200]


def test_incomplete_reasons_are_specific() -> None:
    assert classify_review_response("Review in progress").reason == "preliminary_review_output"
    assert classify_review_response("").reason == "empty_review_output"
    assert classify_review_response('{"decision":"PA').reason == "malformed_review_output"
    assert classify_review_response('{"decision":"FAIL"}').reason == "fail_without_actionable_findings"
    assert classify_review_response(json.dumps({"decision": "PASS", "findings": [FINDING]})).reason == "contradictory_review_verdict"
    assert classify_review_response('{"decision":"PASS"}\n{"decision":"FAIL","findings":[{"message":"x is broken"}]}').reason == "ambiguous_review_output"


def test_wrapped_verdicts_still_parse() -> None:
    envelope = json.dumps({"text": '```json\n{"decision":"PASS"}\n```'})
    assert classify_review_response(envelope).kind == VERDICT_PASS
    assert classify_review_response('{"structured_output":{"decision":"PASS","findings":[]}}').kind == VERDICT_PASS
    both = {"text": '{"decision":"PASS"}', "structured_output": {"decision": "PASS"}}
    assert classify_review_response(json.dumps(both)).kind == VERDICT_PASS  # the same verdict in both carriers counts once
    assert classify_review_response('Review complete.\n{"decision":"PASS","findings":[]}\nDone.').kind == VERDICT_PASS
    mixed = classify_review_response(json.dumps({"decision": "FAIL", "findings": [FINDING, {"message": "Review in progress"}]}))
    assert mixed.kind == VERDICT_FAIL_WITH_FINDINGS and [f["message"] for f in mixed.findings] == [FINDING["message"]]  # placeholders dropped


def test_concrete_findings_that_mention_progress_words_are_kept() -> None:
    for message in (
        "Spinner remains in progress forever on failed upload.",
        "The migration is in progress when README.md is rendered, so the table is stale.",
        "Pending items are never flushed in sync.py",
        "Let's Encrypt certificate renewal fails because renew.py never reloads the server.",
        "Let me be precise: README.md claims a flag that cli.py does not define.",
    ):
        verdict = classify_review_response(json.dumps({"decision": "FAIL_WITH_FINDINGS", "findings": [{"message": message}]}))
        assert verdict.kind == VERDICT_FAIL_WITH_FINDINGS and verdict.findings[0]["message"] == message
    for filler in ("in progress", "Pending", "TBD.", "n/a", "I'll look at it", "Let me check the diff"):
        assert classify_review_response(json.dumps({"decision": "FAIL", "findings": [{"message": filler}]})).kind == VERDICT_REVIEW_INCOMPLETE


def test_a_later_complete_answer_ends_the_bounded_retry(tmp_path: Path) -> None:
    project, store, candidate = _project_with_candidate(tmp_path)
    adapter = ScriptedReviewAdapter("Review in progress", "", '{"decision":"PASS"}')

    assert Reviewer(adapter=adapter, require_independent=True).review(store, UNIT_TASK, candidate, project) == EvidenceStatus.PASSED

    assert adapter.calls == 3
    assert _evidence(store, EvidenceKind.REVIEW, EvidenceStatus.PASSED)[0]["review_attempts"] == 3


def test_the_retry_budget_is_configurable_and_zero_means_one_attempt(tmp_path: Path) -> None:
    project, store, candidate = _project_with_candidate(tmp_path)
    adapter = ScriptedReviewAdapter("Review in progress")
    Reviewer(adapter=adapter, require_independent=True, max_incomplete_retries=0).review(store, UNIT_TASK, candidate, project)
    assert adapter.calls == 1


# ---- provider failures, independence and fallback ----------------------------------------------------------------------------------


def test_provider_timeout_is_classified_as_infrastructure_and_not_retried(tmp_path: Path) -> None:
    project, store, candidate = _project_with_candidate(tmp_path)
    adapter = ScriptedReviewAdapter(json.dumps({"decision": INFRASTRUCTURE_FAILURE, "reason": "provider_timeout"}))

    status = Reviewer(adapter=adapter, require_independent=True).review(store, UNIT_TASK, candidate, project)

    assert status == EvidenceStatus.CAPACITY and adapter.calls == 1  # the pool already walked every provider; do not hammer it
    (payload,) = _evidence(store, EvidenceKind.REVIEW, EvidenceStatus.CAPACITY)
    assert payload["review_infrastructure_failure"] == "provider_timeout"
    assert store.open_findings_for_candidate(UNIT_TASK, candidate) == []


def test_a_raised_timeout_is_review_infrastructure(tmp_path: Path) -> None:
    project, store, candidate = _project_with_candidate(tmp_path)

    class Timeout:
        name = "reviewer"

        def review(self, prompt: str) -> str:
            raise TimeoutError("review provider timed out")

    assert Reviewer(adapter=Timeout(), require_independent=True).review(store, UNIT_TASK, candidate, project) == EvidenceStatus.CAPACITY
    assert store.open_findings_for_candidate(UNIT_TASK, candidate) == []


def test_the_implementer_can_never_supply_the_required_independent_review(tmp_path: Path) -> None:
    project, store, candidate = _project_with_candidate(tmp_path)  # produced_by "codex"
    adapter = ScriptedReviewAdapter('{"decision":"PASS"}', name="codex")

    status = Reviewer(adapter=adapter, require_independent=True).review(store, UNIT_TASK, candidate, project)

    assert status == EvidenceStatus.CAPACITY and adapter.calls == 0
    assert not _evidence(store, EvidenceKind.REVIEW, EvidenceStatus.PASSED)


def test_a_fallback_that_lands_on_the_implementer_cannot_pass_an_independent_review(tmp_path: Path) -> None:
    project, store, candidate = _project_with_candidate(tmp_path)

    class SwitchingAdapter:
        """Starts as a distinct reviewer, then 'falls back' to the implementer's own provider before answering."""

        def __init__(self) -> None:
            self.name = "claude"

        def review_candidate(self, prompt: str, project: Path, candidate_sha: str) -> str:
            self.name = "codex"
            return '{"decision":"PASS"}'

    status = Reviewer(adapter=SwitchingAdapter(), require_independent=True).review(store, UNIT_TASK, candidate, project)

    assert status == EvidenceStatus.CAPACITY
    assert not _evidence(store, EvidenceKind.REVIEW, EvidenceStatus.PASSED)
    payload = _evidence(store, EvidenceKind.REVIEW, EvidenceStatus.CAPACITY)[0]
    assert payload["independent_reviewer"] is False and payload["review_infrastructure_failure"] == "review_provider_same_as_implementer"


def test_a_fallback_that_lands_on_the_implementer_cannot_create_findings_either(tmp_path: Path) -> None:
    project, store, candidate = _project_with_candidate(tmp_path)

    class SwitchingAdapter:
        def __init__(self) -> None:
            self.name = "claude"

        def review_candidate(self, prompt: str, project: Path, candidate_sha: str) -> str:
            self.name = "codex"
            return json.dumps({"decision": "FAIL_WITH_FINDINGS", "findings": [FINDING]})

    status = Reviewer(adapter=SwitchingAdapter(), require_independent=True).review(store, UNIT_TASK, candidate, project)

    assert status == EvidenceStatus.CAPACITY  # a non-independent verdict is void: no remediation is triggered by it
    assert store.open_findings_for_candidate(UNIT_TASK, candidate) == []
    assert not _evidence(store, EvidenceKind.REVIEW, EvidenceStatus.FAILED)


def test_pool_falls_back_from_an_incomplete_reviewer_to_another_independent_one(tmp_path: Path) -> None:
    rig = PoolRig(
        tmp_path,
        {"codex": "ok", "grok": "progress-review", "claude": "ok"},
        pools={IMPLEMENT: ("codex",), REVIEW: ("grok", "claude")},
    )

    rig.tick(4)  # plan, implement, validate, review

    payload = rig.review_payload()
    assert payload["review_provider"] == "claude" and payload["independent_reviewer"] is True
    assert payload["review_verdict"] == VERDICT_PASS
    assert "fallback: grok failed (preliminary_review_output) -> trying claude" in rig.text()
    assert payload["implementer_provider"] == "codex"


def test_pool_treats_a_findings_free_fail_like_an_incomplete_answer(tmp_path: Path) -> None:
    rig = PoolRig(
        tmp_path,
        {"codex": "ok", "grok": "fail-no-findings-review", "claude": "ok"},
        pools={IMPLEMENT: ("codex",), REVIEW: ("grok", "claude")},
    )

    rig.tick(4)

    assert rig.review_payload()["review_provider"] == "claude"
    assert "fallback: grok failed (fail_without_actionable_findings) -> trying claude" in rig.text()
    assert not rig.store.open_findings_for_candidate(TASK_POOL, rig.store.latest_candidate(TASK_POOL)["sha"])


TASK_POOL = "TASK-1"


def test_pool_reasons_match_the_classifier() -> None:
    assert _infrastructure_reason("Review in progress") == "preliminary_review_output"
    assert _infrastructure_reason('{"decision":"FAIL"}') == "fail_without_actionable_findings"
    assert _infrastructure_reason('{"decision":"PASS"}') is None


# ---- the task lifecycle: remediation only for real findings, nothing lost on infrastructure trouble -------------------------------


def _coordinator(rig: Rig, executor: ScriptedExecutor, adapter) -> Coordinator:
    integrator = SerializedIntegrator(rig.ref, False, rig.lock, max_rebases=2)
    return Coordinator(rig.store, rig.project, executor=executor, integrator=integrator, reviewer=Reviewer(adapter=adapter, require_independent=True))


def _count(rig: Rig, kind: ExecutionKind) -> int:
    return rig.store.conn.execute("SELECT COUNT(*) FROM executions WHERE task_id=? AND kind=?", (TASK, kind)).fetchone()[0]


@pytest.mark.parametrize("answer", ["Review in progress", "I will inspect the candidate", "", '{"decision":"FAIL"}', "looks fine to me"])
def test_exhausted_review_availability_preserves_candidate_worktree_and_validation(tmp_path: Path, answer: str) -> None:
    rig = Rig(tmp_path, [TASK])
    executor = ScriptedExecutor(rig.files)
    adapter = ScriptedReviewAdapter(answer)
    coord = _coordinator(rig, executor, adapter)

    _advance_until(rig, coord, Stage.REVIEW)
    candidate = rig.store.latest_candidate(TASK)["sha"]
    for _ in range(8):
        coord.tick()

    assert adapter.calls >= 1
    task = rig.store.get_task(TASK)
    assert (task["stage"], task["status"]) == (Stage.REVIEW, TaskStatus.OPEN)  # waiting for a reviewer, not sent back to IMPLEMENT
    assert rig.store.latest_candidate(TASK)["sha"] == candidate
    worktree = executor.worktrees[TASK]
    git = GitWorkspace(worktree)
    assert git.head() == candidate and git.run("status", "--porcelain").stdout.strip() == ""
    assert _count(rig, ExecutionKind.IMPLEMENTATION) == 1  # no remediation / reimplementation
    assert rig.store.open_findings_for_candidate(TASK, candidate) == []
    assert _count(rig, ExecutionKind.VALIDATION) == 1  # valid validation evidence for the unchanged candidate is kept, not redone
    assert rig.store.has_evidence(TASK, candidate, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)
    assert not rig.store.has_evidence(TASK, candidate, EvidenceKind.REVIEW, EvidenceStatus.FAILED)


def test_a_reviewer_that_recovers_later_completes_the_task_without_revalidating(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [TASK])
    executor = ScriptedExecutor(rig.files)
    flaky = ScriptedReviewAdapter(*(["Review in progress"] * (1 + REVIEW_INCOMPLETE_RETRIES)), '{"decision":"PASS"}')
    coord = _coordinator(rig, executor, flaky)

    _advance_until(rig, coord, Stage.DONE, limit=40)

    assert _count(rig, ExecutionKind.IMPLEMENTATION) == 1 and _count(rig, ExecutionKind.VALIDATION) == 1
    final = rig.store.latest_candidate(TASK)["sha"]
    assert rig.store.has_evidence(TASK, final, EvidenceKind.REVIEW, EvidenceStatus.PASSED)


def test_only_a_valid_review_with_findings_triggers_remediation(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [TASK])
    executor = ScriptedExecutor(rig.files)
    adapter = ScriptedReviewAdapter(json.dumps({"decision": "FAIL_WITH_FINDINGS", "findings": [FINDING]}))
    coord = _coordinator(rig, executor, adapter)

    _advance_until(rig, coord, Stage.REVIEW)
    candidate = rig.store.latest_candidate(TASK)["sha"]
    _advance_until(rig, coord, Stage.IMPLEMENT)

    assert [f["message"] for f in rig.store.open_findings_for_candidate(TASK, candidate)] == [FINDING["message"]]
    assert rig.store.has_evidence(TASK, candidate, EvidenceKind.REVIEW, EvidenceStatus.FAILED)


# ---- review evidence is bound to one candidate and one contract ---------------------------------------------------------------------


def test_review_evidence_cannot_be_reused_for_another_candidate_or_contract(tmp_path: Path) -> None:
    project, store, first = _project_with_candidate(tmp_path)
    assert Reviewer(adapter=ScriptedReviewAdapter('{"decision":"PASS"}'), require_independent=True).review(store, UNIT_TASK, first, project) == EvidenceStatus.PASSED
    bound = contract_for_candidate(store, UNIT_TASK, first, project)
    assert _has_exact_bound_evidence(store, UNIT_TASK, first, EvidenceKind.REVIEW, bound, require_independent=True)

    (project / "README.md").write_text("changed again\n", encoding="utf-8")
    second = GitWorkspace(project).commit_all("second candidate")
    store.add_candidate(UNIT_TASK, second, produced_by="codex", durable_handoff=True)
    second_bound = contract_for_candidate(store, UNIT_TASK, second, project)

    assert not store.has_bound_evidence(UNIT_TASK, second, EvidenceKind.REVIEW, second_bound.digest, EvidenceStatus.PASSED)
    assert not _has_exact_bound_evidence(store, UNIT_TASK, second, EvidenceKind.REVIEW, second_bound)
    # the first approval does not satisfy a different contract hash, version or baseline either
    assert not _has_exact_bound_evidence(store, UNIT_TASK, first, EvidenceKind.REVIEW, replace(bound, digest="0" * 64))
    assert not _has_exact_bound_evidence(store, UNIT_TASK, first, EvidenceKind.REVIEW, replace(bound, version=bound.version + 1))
    assert not _has_exact_bound_evidence(store, UNIT_TASK, first, EvidenceKind.REVIEW, replace(bound, baseline_sha="1" * 40))
    assert not store.has_bound_evidence(UNIT_TASK, first, EvidenceKind.REVIEW, "0" * 64, EvidenceStatus.PASSED)


def test_an_incomplete_review_never_satisfies_the_integration_gate(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [TASK])
    executor = ScriptedExecutor(rig.files)
    coord = _coordinator(rig, executor, ScriptedReviewAdapter("Review in progress"))
    _advance_until(rig, coord, Stage.REVIEW)
    candidate = rig.store.latest_candidate(TASK)["sha"]
    for _ in range(4):
        coord.tick()
    tip = GitWorkspace(rig.project).run("rev-parse", rig.ref).stdout.strip()

    assert coord.integrator.integrate(rig.store, TASK, candidate, rig.project) == EvidenceStatus.FAILED

    assert GitWorkspace(rig.project).run("rev-parse", rig.ref).stdout.strip() == tip


def test_a_provider_outage_is_not_retried_by_the_incomplete_answer_retry_loop(tmp_path: Path) -> None:
    rig = PoolRig(
        tmp_path,
        {"codex": "ok", "grok": "review-rate-limit", "claude": "progress-review"},
        pools={IMPLEMENT: ("codex",), REVIEW: ("grok", "claude")},
    )

    rig.tick(4)  # plan, implement, validate, review: grok is down, claude only ever sends progress text

    failures = [
        json.loads(r["payload"])
        for r in rig.store.conn.execute("SELECT payload FROM audit_events WHERE event_type='provider.failure'")
    ]
    assert [f["provider"] for f in failures].count("grok") == 1  # the outage was recorded once, not once per retry
    assert [f["provider"] for f in failures].count("claude") == 1 + REVIEW_INCOMPLETE_RETRIES
    assert rig.store.open_findings_for_candidate(TASK_POOL, rig.store.latest_candidate(TASK_POOL)["sha"]) == []


def test_scope_policy_never_reinterprets_a_pass_that_carries_findings() -> None:
    from stagemesh.autonomy.review_adapter import ScopedReviewAdapter

    adapter = object.__new__(ScopedReviewAdapter)  # a PASS must be returned untouched, so no supervisor/store is consulted
    contradictory = json.dumps({"decision": "PASS", "findings": [FINDING]})
    assert adapter._scoped(contradictory, "a" * 40) == contradictory
    assert classify_review_response(contradictory).kind == VERDICT_REVIEW_INCOMPLETE
