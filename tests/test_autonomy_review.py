"""Capability 8 and 9: independent review lifecycle and scope discipline (Scenarios H and I)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from autonomy_support import new_store

from stagemesh.autonomy.decisions import Action, Condition, EscalationReason
from stagemesh.autonomy.review_adapter import supervise_reviewer
from stagemesh.autonomy.review_policy import (
    FindingClass,
    ReviewFindingInput,
    ReviewReport,
    assess_review,
    classify_finding,
    required_after_remediation,
)
from stagemesh.autonomy.scope import TaskScope, deferred_items
from stagemesh.autonomy.supervisor import Supervisor
from stagemesh.coordinator import Coordinator
from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage
from stagemesh.execution import SubprocessExecutor
from stagemesh.git import GitWorkspace
from stagemesh.integration import Integrator
from stagemesh.persistence import Store
from stagemesh.remediation import remediation_context
from stagemesh.review import Reviewer, independent_review_verified
from stagemesh.validation import Validator

TASK = "TASK-1"
SCOPE = TaskScope(
    "update the docs",
    acceptance_criteria=("docs say implemented",),
    allowed_files=("docs/**",),
    forbidden_files=("docs/legacy/**",),
)
SHA, OTHER = "a" * 40, "b" * 40


def _report(*findings: ReviewFindingInput, reviewed: str = SHA, reviewer: str = "claude", implementer: str = "codex", invoked: bool = True) -> ReviewReport:
    return ReviewReport(reviewed, reviewer, implementer, tuple(findings), invoked)


# --- policy -----------------------------------------------------------------------------------------------------------------------------


def test_blocking_defect_in_scope_is_remediated_and_forces_new_validation_and_review() -> None:
    finding = ReviewFindingInput("docs/a.md claims a flag that does not exist", "error", "docs/a.md")
    assessment = assess_review(_report(finding), candidate_sha=SHA, scope=SCOPE, task_id=TASK)

    assert assessment.decision.condition is Condition.REVIEW_BLOCKING_IN_SCOPE
    assert assessment.decision.action is Action.REMEDIATE_CANDIDATE and not assessment.decision.requires_human
    assert assessment.remediate == [finding] and not assessment.approved
    assert assessment.decision.detail["invalidates"] == ["VALIDATION", "REVIEW"]
    assert required_after_remediation(SHA, OTHER) == ["VALIDATION", "REVIEW"]  # a new SHA needs new evidence
    assert required_after_remediation(SHA, SHA) == [] and required_after_remediation(SHA, None) == []


def test_unrelated_suggestions_and_nits_are_deferred_and_scope_is_unchanged() -> None:
    nit = ReviewFindingInput("consider renaming the helper", "nit", "docs/a.md")
    suggestion = ReviewFindingInput("while you are here, refactor the CI workflow", "info", ".github/workflows/ci.yml", category="suggestion")
    out_of_scope_major = ReviewFindingInput("the legacy module is a mess", "major", "src/legacy.py")
    assessment = assess_review(_report(nit, suggestion, out_of_scope_major), candidate_sha=SHA, scope=SCOPE, task_id=TASK)

    assert assessment.approved
    assert assessment.decision.condition is Condition.REVIEW_UNRELATED_SUGGESTION
    assert assessment.decision.action is Action.RECORD_AND_DEFER
    assert assessment.remediate == [] and assessment.fix_tests == []
    assert {c.klass for c in assessment.classified} == {FindingClass.NON_BLOCKING, FindingClass.UNRELATED_SUGGESTION, FindingClass.BLOCKING_OUT_OF_SCOPE}
    assert [item.summary for item in assessment.deferred] == [nit.message, suggestion.message, out_of_scope_major.message]
    assert assessment.decision.detail["scope_unchanged"] is True


def test_mixed_review_remediates_only_the_in_scope_blocker() -> None:
    blocker = ReviewFindingInput("broken link in docs/a.md", "major", "docs/a.md")
    unrelated = ReviewFindingInput("src/legacy.py has a lint problem", "major", "src/legacy.py")
    assessment = assess_review(_report(blocker, unrelated), candidate_sha=SHA, scope=SCOPE, task_id=TASK)
    brief = assessment.remediation_brief()
    assert assessment.remediate == [blocker]
    assert "broken link in docs/a.md" in brief and "Explicitly deferred (do NOT fix)" in brief
    assert brief.index("broken link") < brief.index("Explicitly deferred") < brief.index("src/legacy.py has a lint problem")


def test_review_bound_to_another_sha_is_stale_and_requires_rereview() -> None:
    assessment = assess_review(_report(reviewed=OTHER), candidate_sha=SHA, scope=SCOPE, task_id=TASK)
    assert assessment.decision.condition is Condition.REVIEW_STALE_FOR_CANDIDATE
    assert assessment.decision.action is Action.REQUIRE_RE_REVIEW and not assessment.approved


def test_reviewer_must_be_independent_when_required() -> None:
    for report in (_report(reviewer="codex"), _report(reviewer="Codex "), _report(invoked=False)):
        assessment = assess_review(report, candidate_sha=SHA, scope=SCOPE, task_id=TASK)
        assert assessment.decision.action is Action.REQUIRE_INDEPENDENT_REVIEW and not assessment.approved
    assert assess_review(_report(reviewer="codex"), candidate_sha=SHA, scope=SCOPE, task_id=TASK, independent_required=False).approved


def test_test_defect_is_classified_separately_and_fixed_only_when_it_belongs_to_the_objective() -> None:
    in_scope = ReviewFindingInput("the fixture expects success from a no-op provider", "error", "docs/check.py", category="test_defect")
    outside = ReviewFindingInput("an unrelated fixture is wrong", "error", "tests/test_other.py", category="test_defect")
    assert classify_finding(in_scope, SCOPE).klass is FindingClass.TEST_DEFECT
    assessment = assess_review(_report(in_scope, outside), candidate_sha=SHA, scope=SCOPE, task_id=TASK)
    assert assessment.fix_tests == [in_scope]
    assert [item.path for item in assessment.deferred] == ["tests/test_other.py"]
    assert assessment.decision.action is Action.REMEDIATE_CANDIDATE


def test_blocking_finding_needing_an_out_of_scope_file_for_an_acceptance_criterion_escalates_specifically() -> None:
    finding = ReviewFindingInput(
        "the docs criterion cannot be met without editing docs/legacy/index.md", "error", "docs/legacy/index.md",
        acceptance_criterion="docs say implemented",
    )
    assessment = assess_review(_report(finding), candidate_sha=SHA, scope=SCOPE, task_id=TASK)
    escalation = assessment.decision.escalation
    assert assessment.decision.action is Action.ESCALATE_TO_FOUNDER
    assert escalation is not None and escalation.reason is EscalationReason.SCOPE_EXTENSION_REQUIRED_BY_ACCEPTANCE_CRITERION
    assert "docs/legacy/index.md" in escalation.smallest_decision and escalation.smallest_decision.endswith("?")


# --- end to end through the real coordinator -----------------------------------------------------------------------------------------------


class ScriptedReviewer:
    """An independent reviewer adapter that answers from a script, one answer per review call."""

    name = "claude"

    def __init__(self, *responses: dict):
        self.responses = [json.dumps(r) for r in responses]
        self.calls = 0

    def review_candidate(self, prompt: str, project: Path, candidate_sha: str) -> str:
        self.calls += 1
        return self.responses[min(self.calls, len(self.responses)) - 1]


class Rig:
    def __init__(self, tmp_path: Path, adapter: ScriptedReviewer):
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.project = tmp_path / "repo"
        self.project.mkdir()
        git = GitWorkspace(self.project)
        git.init_if_needed()
        git.run("config", "user.email", "test@example.invalid")
        git.run("config", "user.name", "StageMesh Test")
        (self.project / "docs").mkdir()
        (self.project / "docs" / "a.md").write_text("a\n", encoding="utf-8")
        self.base = git.commit_all("base")
        git.run("branch", "integration")
        self.git = git
        contracts = self.project / ".stagemesh" / "contracts"
        contracts.mkdir(parents=True)
        (contracts / f"{TASK}.json").write_text(
            json.dumps(
                {
                    "objective": "update the docs",
                    "allowed_files": ["docs/**"],
                    "required_tests": [{"name": "docs", "command": [sys.executable, "-c", "pass"]}],
                    "acceptance_criteria": ["docs say implemented"],
                }
            ),
            encoding="utf-8",
        )
        (self.project / ".stagemesh" / "autonomy.json").write_text(json.dumps({"enabled": True}), encoding="utf-8")
        self.store = Store(self.project / ".stagemesh" / "stagemesh.sqlite3")
        self.store.migrate()
        self.store.upsert_task("update the docs", source_id=TASK)
        self.store.advance_task(TASK, Stage.IMPLEMENT)
        script = tmp_path / "provider.py"
        script.write_text(
            "import pathlib\n"
            "marker = pathlib.Path('../runs.marker')\n"
            "n = int(marker.read_text()) + 1 if marker.exists() else 1\n"
            "marker.write_text(str(n))\n"
            "pathlib.Path('docs/a.md').write_text(f'implemented v{n}')\n",
            encoding="utf-8",
        )
        self.runs_marker = self.project / ".stagemesh" / "worktrees" / "runs.marker"  # the provider's `../runs.marker`
        self.supervisor = Supervisor(self.store, self.project, integration_ref="refs/heads/integration")
        reviewer = supervise_reviewer(Reviewer(adapter=adapter, require_independent=True), self.supervisor)
        self.coordinator = Coordinator(
            self.store,
            self.project,
            executor=SubprocessExecutor([sys.executable, str(script)], name="codex"),
            validator=Validator(),
            reviewer=reviewer,
            integrator=Integrator("refs/heads/integration", require_independent_review=True),
            require_independent_review=True,
            guard=self.supervisor,
        )

    def drive(self, max_ticks: int = 16) -> int:
        ticks = 0
        while ticks < max_ticks and self.store.get_task(TASK)["stage"] != Stage.DONE:
            self.coordinator.tick()
            ticks += 1
        return ticks

    def candidates(self) -> list[str]:
        return [r["sha"] for r in self.store.conn.execute("SELECT sha FROM candidates WHERE task_id=? ORDER BY created_at, rowid", (TASK,))]

    def evidence(self, sha: str, kind: EvidenceKind, status: EvidenceStatus) -> list[dict]:
        rows = self.store.conn.execute(
            "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? AND status=?", (TASK, sha, kind, status)
        )
        return [json.loads(r["payload"]) for r in rows]


def test_scenario_h_real_defect_triggers_remediation_new_sha_validation_and_independent_rereview(tmp_path: Path) -> None:
    adapter = ScriptedReviewer(
        {"decision": "FAIL", "findings": [{"severity": "major", "message": "docs/a.md does not describe the new flag", "path": "docs/a.md"}]},
        {"decision": "PASS"},
    )
    rig = Rig(tmp_path, adapter)

    rig.drive()

    assert rig.store.get_task(TASK)["stage"] == Stage.DONE
    first, second = rig.candidates()
    assert first != second and adapter.calls == 2
    # review #1 failed on the first SHA; remediation produced a new SHA that was validated and independently re-reviewed
    assert rig.evidence(first, EvidenceKind.REVIEW, EvidenceStatus.FAILED)
    assert not rig.evidence(first, EvidenceKind.REVIEW, EvidenceStatus.PASSED)
    assert rig.evidence(second, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)
    (passed,) = rig.evidence(second, EvidenceKind.REVIEW, EvidenceStatus.PASSED)
    assert independent_review_verified(passed) and passed["review_provider"] == "claude" and passed["implementer_provider"] == "codex"
    assert rig.git.run("rev-parse", "integration").stdout.strip() == second  # only the re-reviewed SHA landed
    context = remediation_context(rig.store, TASK)
    assert context is not None and context["candidate_sha"] == first
    assert [f["message"] for f in context["findings"]] == ["docs/a.md does not describe the new flag"]
    assert rig.runs_marker.read_text() == "2"


def test_scenario_i_unrelated_reviewer_suggestion_is_recorded_not_fixed(tmp_path: Path) -> None:
    adapter = ScriptedReviewer(
        {
            "decision": "FAIL",
            "findings": [
                {"severity": "nit", "message": "rename the helper in docs/a.md", "path": "docs/a.md"},
                {"severity": "major", "message": "src/legacy.py needs a rewrite", "path": "src/legacy.py"},
                {"severity": "info", "message": "consider adding a CI cache", "category": "suggestion"},
            ],
        }
    )
    rig = Rig(tmp_path, adapter)

    rig.drive()

    assert rig.store.get_task(TASK)["stage"] == Stage.DONE
    (only,) = rig.candidates()  # the candidate's scope never changed: no remediation, no second candidate
    assert rig.runs_marker.read_text() == "1" and adapter.calls == 1
    assert not rig.evidence(only, EvidenceKind.REVIEW, EvidenceStatus.FAILED)
    (passed,) = rig.evidence(only, EvidenceKind.REVIEW, EvidenceStatus.PASSED)
    assert independent_review_verified(passed)
    assert "stagemesh-scope-policy" in passed["review_response"] and "src/legacy.py needs a rewrite" in passed["review_response"]
    assert {item.summary for item in deferred_items(rig.store, TASK)} == {
        "rename the helper in docs/a.md",
        "src/legacy.py needs a rewrite",
        "consider adding a CI cache",
    }
    decisions = [d for d in rig.supervisor.trace(TASK) if d["condition"] == "REVIEW_UNRELATED_SUGGESTION"]
    assert decisions and decisions[0]["action"] == "RECORD_AND_DEFER"
    assert rig.git.run("rev-parse", "integration").stdout.strip() == only
    assert rig.store.open_findings_for_candidate(TASK, only) == []


def test_scenario_i_malformed_or_infrastructure_verdicts_are_left_to_the_reviewer(tmp_path: Path) -> None:
    new_store(tmp_path)  # exercise isolated store creation
    rig = Rig(tmp_path / "rig", ScriptedReviewer({"decision": "INFRASTRUCTURE_FAILURE", "reason": "quota"}))
    rig.coordinator.tick(), rig.coordinator.tick(), rig.coordinator.tick()
    assert rig.store.get_task(TASK)["stage"] == Stage.REVIEW  # stays in REVIEW; no remediation, no pass
    assert not rig.evidence(rig.candidates()[0], EvidenceKind.REVIEW, EvidenceStatus.PASSED)
