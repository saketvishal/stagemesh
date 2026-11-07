from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from stagemesh.domain import Stage
from stagemesh.objective_governance import (
    CHALLENGE_RECORDED,
    EVALUATION_RECORDED,
    PLAN_RECORDED,
    ObjectiveGovernanceError,
    freeze_objective_challenge,
    freeze_objective_evaluation,
    freeze_objective_plan,
    load_objective_evaluation,
    load_objective_lineage,
    load_objective_plan,
    save_objective_challenge,
    save_objective_evaluation,
    save_objective_plan,
    validate_plan_transition,
)
from stagemesh.persistence import Store


def _plan(*, objective_id: str = "OBJ-1", revision: int = 1) -> dict:
    return {
        "version": 1,
        "objective_id": objective_id,
        "revision": revision,
        "objective": "Ship a restart-safe synthetic workspace feature.",
        "success_criteria": [
            {"id": "feature", "description": "The feature works from the public boundary."},
            {"id": "restart", "description": "A restart preserves accepted state."},
        ],
        "tasks": [
            {
                "id": "implement",
                "title": "Implement the bounded feature",
                "dependencies": [],
                "criterion_ids": ["feature"],
            },
            {
                "id": "verify-restart",
                "title": "Verify restart recovery",
                "dependencies": ["implement"],
                "criterion_ids": ["restart"],
            },
        ],
        "non_goals": ["No production deployment."],
        "previous_plan_hash": None,
        "responds_to_challenge_hash": None,
        "change_reason": None,
        "finding_responses": [],
    }


def _challenge(plan, *, verdict: str = "accept", findings: list[dict] | None = None) -> dict:
    return {
        "version": 1,
        "objective_id": plan.plan.objective_id,
        "plan_hash": plan.digest,
        "plan_revision": plan.plan.revision,
        "planner_identity": "fixture-planner",
        "challenger_identity": "fixture-challenger",
        "verdict": verdict,
        "findings": findings or [],
        "required_decisions": [],
    }


def _accepted(plan):
    return freeze_objective_challenge(_challenge(plan), plan)


def _evaluation(
    plan,
    statuses: dict[str, str] | None = None,
    *,
    accepted_challenge=None,
) -> dict:
    statuses = statuses or {item.id: "PASS" for item in plan.plan.success_criteria}
    accepted_challenge = accepted_challenge or _accepted(plan)
    return {
        "version": 1,
        "objective_id": plan.plan.objective_id,
        "plan_hash": plan.digest,
        "plan_revision": plan.plan.revision,
        "accepted_challenge_hash": accepted_challenge.digest,
        "candidate_sha": "a" * 40,
        "evaluator_identity": "fixture-evaluator",
        "implementation_identities": ["fixture-builder"],
        "runner_identity": "fixture-runner/1",
        "criteria": [
            {
                "criterion_id": item.id,
                "status": statuses[item.id],
                "evidence_refs": [f"evidence:{item.id}"],
            }
            for item in plan.plan.success_criteria
        ],
        "negative_findings": [],
    }


@pytest.fixture
def project_store(tmp_path: Path):
    project = tmp_path / "project"
    runtime = project / ".stagemesh"
    runtime.mkdir(parents=True)
    store = Store(runtime / "stagemesh.sqlite3")
    store.migrate()
    try:
        yield project, store
    finally:
        store.close()


def test_plan_identity_is_deterministic_and_every_task_is_outcome_linked() -> None:
    first = freeze_objective_plan(_plan())
    shuffled = _plan()
    shuffled = {key: shuffled[key] for key in reversed(shuffled)}

    assert freeze_objective_plan(shuffled).digest == first.digest
    assert first.plan.revision == 1
    assert {criterion for task in first.plan.tasks for criterion in task.criterion_ids} == {
        "feature",
        "restart",
    }


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda plan: plan.update(objective_id="../escape"), "portable identifier"),
        (
            lambda plan: plan["tasks"].append(dict(plan["tasks"][0])),
            "task id contains duplicates",
        ),
        (
            lambda plan: plan["tasks"][0].update(dependencies=["missing"]),
            "unknown dependency",
        ),
        (
            lambda plan: (
                plan["tasks"][0].update(dependencies=["verify-restart"]),
                plan["tasks"][1].update(dependencies=["implement"]),
            ),
            "acyclic graph",
        ),
        (
            lambda plan: plan["tasks"][1].update(criterion_ids=["feature"]),
            "not linked to a task: restart",
        ),
    ],
)
def test_invalid_plan_graphs_fail_closed(mutate, message: str) -> None:
    payload = _plan()
    mutate(payload)

    with pytest.raises(ObjectiveGovernanceError, match=message):
        freeze_objective_plan(payload)


def test_revision_one_cannot_claim_history_and_later_revisions_require_it() -> None:
    first = _plan()
    first["previous_plan_hash"] = "a" * 64
    with pytest.raises(ObjectiveGovernanceError, match="revision 1 cannot"):
        freeze_objective_plan(first)

    later = _plan(revision=2)
    with pytest.raises(ObjectiveGovernanceError, match="previous_plan_hash"):
        freeze_objective_plan(later)


def test_challenge_requires_independence_exact_plan_and_typed_verdict() -> None:
    plan = freeze_objective_plan(_plan())
    challenge = _challenge(plan)
    challenge["challenger_identity"] = challenge["planner_identity"].upper()
    with pytest.raises(ObjectiveGovernanceError, match="must be different"):
        freeze_objective_challenge(challenge, plan)

    stale = _challenge(plan)
    stale["plan_hash"] = "0" * 64
    with pytest.raises(ObjectiveGovernanceError, match="does not match"):
        freeze_objective_challenge(stale, plan)

    revise_without_findings = _challenge(plan, verdict="revise")
    with pytest.raises(ObjectiveGovernanceError, match="requires at least one finding"):
        freeze_objective_challenge(revise_without_findings, plan)


def test_challenge_findings_have_stable_ids_and_known_criterion_links() -> None:
    plan = freeze_objective_plan(_plan())
    duplicate = _challenge(
        plan,
        verdict="revise",
        findings=[
            {"id": "F-1", "kind": "scope-risk", "summary": "one", "criterion_id": "feature"},
            {"id": "F-1", "kind": "scope-risk", "summary": "two", "criterion_id": "restart"},
        ],
    )
    with pytest.raises(ObjectiveGovernanceError, match="finding id contains duplicates"):
        freeze_objective_challenge(duplicate, plan)

    unknown = _challenge(
        plan,
        verdict="revise",
        findings=[{"id": "F-2", "kind": "scope-risk", "summary": "x", "criterion_id": "unknown"}],
    )
    with pytest.raises(ObjectiveGovernanceError, match="unknown criterion"):
        freeze_objective_challenge(unknown, plan)


def test_revision_transition_preserves_original_criteria_and_every_finding_response() -> None:
    first = freeze_objective_plan(_plan())
    challenge = freeze_objective_challenge(
        _challenge(
            first,
            verdict="revise",
            findings=[
                {
                    "id": "F-ACCESS",
                    "kind": "missing-criterion",
                    "summary": "The objective omits keyboard accessibility.",
                    "criterion_id": None,
                }
            ],
        ),
        first,
    )
    revised_payload = _plan(revision=2)
    revised_payload.update(
        previous_plan_hash=first.digest,
        responds_to_challenge_hash=challenge.digest,
        change_reason="Add the planted missing accessibility requirement.",
        finding_responses=[
            {
                "finding_id": "F-ACCESS",
                "disposition": "adopted",
                "rationale": "Added a stable criterion and verifier task.",
            }
        ],
    )
    revised_payload["success_criteria"].append(
        {"id": "keyboard", "description": "The feature is operable by keyboard."}
    )
    revised_payload["tasks"].append(
        {
            "id": "verify-keyboard",
            "title": "Verify keyboard operation",
            "dependencies": ["implement"],
            "criterion_ids": ["keyboard"],
        }
    )
    revised = freeze_objective_plan(revised_payload)
    assert validate_plan_transition(first, challenge, revised) == ()

    dropped = dict(revised_payload, finding_responses=[])
    with pytest.raises(ObjectiveGovernanceError, match="finding_responses"):
        freeze_objective_plan(dropped)

    changed = json.loads(revised.canonical_json)
    changed["success_criteria"][0]["description"] = "quietly rewritten"
    problems = validate_plan_transition(first, challenge, freeze_objective_plan(changed))
    assert any("original success criterion changed" in problem for problem in problems)


@pytest.mark.parametrize(
    ("statuses", "verdict"),
    [
        ({"feature": "PASS", "restart": "PASS"}, "ACHIEVED"),
        ({"feature": "FAIL", "restart": "UNKNOWN"}, "NOT_ACHIEVED"),
        ({"feature": "PASS", "restart": "UNKNOWN"}, "INCONCLUSIVE"),
    ],
)
def test_evaluation_verdict_is_derived_from_complete_original_criteria(statuses, verdict) -> None:
    plan = freeze_objective_plan(_plan())
    accepted = _accepted(plan)
    evaluation = freeze_objective_evaluation(
        _evaluation(plan, statuses, accepted_challenge=accepted), plan, accepted
    )
    assert evaluation.evaluation.verdict == verdict


def test_evaluation_rejects_self_review_missing_criteria_and_claimed_verdict() -> None:
    plan = freeze_objective_plan(_plan())
    accepted = _accepted(plan)
    self_review = _evaluation(plan)
    self_review["evaluator_identity"] = "FIXTURE-BUILDER"
    with pytest.raises(ObjectiveGovernanceError, match="must differ"):
        freeze_objective_evaluation(self_review, plan, accepted)

    incomplete = _evaluation(plan)
    incomplete["criteria"].pop()
    with pytest.raises(ObjectiveGovernanceError, match="no evaluation result"):
        freeze_objective_evaluation(incomplete, plan, accepted)

    claimed = _evaluation(plan)
    claimed["verdict"] = "ACHIEVED"
    with pytest.raises(ObjectiveGovernanceError, match="unknown objective evaluation field"):
        freeze_objective_evaluation(claimed, plan, accepted)


def test_evaluation_requires_an_accepted_challenge_for_the_exact_plan() -> None:
    plan = freeze_objective_plan(_plan())
    revise = freeze_objective_challenge(
        _challenge(
            plan,
            verdict="revise",
            findings=[
                {
                    "id": "F-OPEN",
                    "kind": "missing-criterion",
                    "summary": "An acceptance criterion remains unresolved.",
                    "criterion_id": None,
                }
            ],
        ),
        plan,
    )
    payload = _evaluation(plan, accepted_challenge=revise)

    with pytest.raises(ObjectiveGovernanceError, match="requires an accepted plan challenge"):
        freeze_objective_evaluation(payload, plan, revise)


def test_persistence_rejects_dependencies_that_exist_only_in_memory(project_store) -> None:
    project, store = project_store
    unpersisted = freeze_objective_plan(_plan(objective_id="OBJ-IN-MEMORY"))

    with pytest.raises(ObjectiveGovernanceError, match="dependency is not persisted"):
        save_objective_challenge(store, project, _challenge(unpersisted), unpersisted)

    first, _ = save_objective_plan(store, project, _plan())
    revise = freeze_objective_challenge(
        _challenge(
            first,
            verdict="revise",
            findings=[
                {
                    "id": "F-OPEN",
                    "kind": "missing-criterion",
                    "summary": "An acceptance criterion remains unresolved.",
                    "criterion_id": None,
                }
            ],
        ),
        first,
    )
    revised_payload = _plan(revision=2)
    revised_payload["previous_plan_hash"] = first.digest
    revised_payload["responds_to_challenge_hash"] = revise.digest
    revised_payload["change_reason"] = "Address the independent challenge."
    revised_payload["finding_responses"] = [
        {
            "finding_id": "F-OPEN",
            "disposition": "rejected-with-rationale",
            "rationale": "The original success criteria already cover this behavior.",
        }
    ]

    with pytest.raises(ObjectiveGovernanceError, match="dependency is not persisted"):
        save_objective_plan(
            store,
            project,
            revised_payload,
            previous=first,
            challenge=revise,
        )


def test_artifacts_are_immutable_idempotent_and_audited_without_content(project_store) -> None:
    project, store = project_store
    plan, path = save_objective_plan(store, project, _plan())
    same, same_path = save_objective_plan(store, project, _plan())
    challenge_artifact, challenge_path = save_objective_challenge(
        store, project, _challenge(plan), plan
    )
    evaluation, evaluation_path = save_objective_evaluation(
        store,
        project,
        _evaluation(plan, accepted_challenge=challenge_artifact),
        plan,
        challenge_artifact,
    )

    assert same.digest == plan.digest and same_path == path
    assert load_objective_plan(path) == plan
    assert load_objective_evaluation(evaluation_path, plan, challenge_artifact) == evaluation
    assert challenge_path.is_file() and evaluation_path.is_file()
    rows = store.audit_events()
    assert [row["event_type"] for row in rows].count(PLAN_RECORDED) == 1
    assert {row["event_type"] for row in rows} >= {
        PLAN_RECORDED,
        CHALLENGE_RECORDED,
        EVALUATION_RECORDED,
    }
    serialized = "\n".join(row["payload"] for row in rows)
    assert plan.plan.objective not in serialized
    assert "success_criteria" not in serialized and "negative_findings" not in serialized

    evaluation_payload = json.loads(evaluation_path.read_text(encoding="utf-8"))
    evaluation_payload["verdict"] = "INCONCLUSIVE"
    evaluation_path.write_text(json.dumps(evaluation_payload), encoding="utf-8")
    with pytest.raises(ObjectiveGovernanceError, match="recorded verdict does not match"):
        load_objective_evaluation(evaluation_path, plan, challenge_artifact)

    path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ObjectiveGovernanceError, match="collision or tampering"):
        save_objective_plan(store, project, _plan())


def test_same_revision_with_different_content_is_refused(project_store) -> None:
    project, store = project_store
    save_objective_plan(store, project, _plan())
    changed = _plan()
    changed["tasks"][0]["title"] = "Different title"

    with pytest.raises(ObjectiveGovernanceError, match="already has a different"):
        save_objective_plan(store, project, changed)


def test_long_portable_objective_id_uses_a_bounded_collision_resistant_directory(
    project_store,
) -> None:
    project, store = project_store
    plan, path = save_objective_plan(store, project, _plan(objective_id="O" * 100))

    assert plan.plan.objective_id == "O" * 100
    assert len(path.parent.parent.name) <= 45
    assert path.is_file()


def test_artifact_boundary_rejects_unsafe_ids_and_symlink_escape(
    project_store, tmp_path: Path
) -> None:
    project, store = project_store
    unsafe = _plan(objective_id="../outside")
    with pytest.raises(ObjectiveGovernanceError, match="portable identifier"):
        save_objective_plan(store, project, unsafe)

    outside = tmp_path / "outside"
    outside.mkdir()
    objective_root = project / ".stagemesh" / "objectives"
    try:
        os.symlink(outside, objective_root, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")
    with pytest.raises(ObjectiveGovernanceError, match="outside workspace|symlink"):
        save_objective_plan(store, project, _plan())
    assert not list(outside.iterdir())


def test_synthetic_gate1_control_preserves_revision_unknown_and_unrelated_objective(
    project_store,
) -> None:
    project, store = project_store
    first, _ = save_objective_plan(store, project, _plan())
    unrelated, unrelated_path = save_objective_plan(
        store, project, _plan(objective_id="OBJ-UNRELATED")
    )
    unrelated_bytes = unrelated_path.read_bytes()
    revise, _ = save_objective_challenge(
        store,
        project,
        _challenge(
            first,
            verdict="revise",
            findings=[
                {
                    "id": "F-PLANTED",
                    "kind": "missing-criterion",
                    "summary": "The planted requirement for empty state behavior is missing.",
                    "criterion_id": None,
                }
            ],
        ),
        first,
    )
    revised_payload = _plan(revision=2)
    revised_payload.update(
        previous_plan_hash=first.digest,
        responds_to_challenge_hash=revise.digest,
        change_reason="Respond to the independent planted-requirement finding.",
        finding_responses=[
            {
                "finding_id": "F-PLANTED",
                "disposition": "adopted",
                "rationale": "Added an empty-state criterion and dependent verifier.",
            }
        ],
    )
    revised_payload["success_criteria"].append(
        {"id": "empty", "description": "The empty state remains actionable."}
    )
    revised_payload["tasks"].append(
        {
            "id": "verify-empty",
            "title": "Verify the empty state",
            "dependencies": ["implement"],
            "criterion_ids": ["empty"],
        }
    )
    revised, _ = save_objective_plan(
        store,
        project,
        revised_payload,
        previous=first,
        challenge=revise,
    )
    accepted, _ = save_objective_challenge(store, project, _challenge(revised), revised)

    for task in revised.plan.tasks:
        store.upsert_task(task.title, source_id=task.id)
        store.advance_task(task.id, Stage.DONE)
    statuses = {item.id: "PASS" for item in revised.plan.success_criteria}
    statuses["empty"] = "UNKNOWN"
    evaluation, _ = save_objective_evaluation(
        store,
        project,
        _evaluation(revised, statuses, accepted_challenge=accepted),
        revised,
        accepted,
    )
    assert accepted.challenge.verdict == "accept"
    assert evaluation.evaluation.verdict == "INCONCLUSIVE"
    assert all(store.get_task(task.id)["status"] == "DONE" for task in revised.plan.tasks)

    store.close()
    reopened = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    try:
        lineage = load_objective_lineage(project, "OBJ-1")
        assert [item.plan.revision for item in lineage] == [1, 2]
        assert lineage[1].plan.previous_plan_hash == lineage[0].digest
        assert load_objective_lineage(project, "OBJ-UNRELATED") == (unrelated,)
        assert unrelated_path.read_bytes() == unrelated_bytes
        assert {row["event_type"] for row in reopened.audit_events()} >= {
            PLAN_RECORDED,
            CHALLENGE_RECORDED,
            EVALUATION_RECORDED,
        }
    finally:
        reopened.close()
