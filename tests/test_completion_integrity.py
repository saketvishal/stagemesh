from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from stagemesh.contract_binding import bind_task_contract, contract_for_candidate
from stagemesh.coordinator import Coordinator
from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage, TaskStatus
from stagemesh.execution import SubprocessExecutor
from stagemesh.git import GitWorkspace
from stagemesh.integration import Integrator
from stagemesh.persistence import Store
from stagemesh.providers import RuntimeCommandAdapter
from stagemesh.review import Reviewer
from stagemesh.validation import Validator

TASK = "TASK-1"


class FakeReviewAdapter:
    def __init__(self, name: str, response: str = '{"decision":"PASS"}'):
        self.name = name
        self.response = response
        self.calls = 0

    def review_candidate(self, prompt: str, project: Path, candidate_sha: str) -> str:
        self.calls += 1
        return self.response


class Rig:
    def __init__(self, tmp_path: Path, adapter, *, integration_ref: str, require: bool = True):
        self.tmp_path = tmp_path
        self.project = tmp_path / "repo"
        self.project.mkdir()
        self.git = GitWorkspace(self.project)
        self.git.init_if_needed()
        self.git.run("config", "user.email", "test@example.invalid")
        self.git.run("config", "user.name", "StageMesh Test")
        (self.project / "docs").mkdir()
        (self.project / "docs" / "a.md").write_text("a\n", encoding="utf-8")
        self.base = self.git.commit_all("base")
        self.branch = self.git.run("symbolic-ref", "HEAD").stdout.strip()
        self.git.run("branch", "integration")
        contracts = self.project / ".stagemesh" / "contracts"
        contracts.mkdir(parents=True)
        (contracts / f"{TASK}.json").write_text(
            json.dumps(
                {
                    "objective": "docs only",
                    "allowed_files": ["docs/**"],
                    "required_tests": [{"name": "docs-static", "command": [sys.executable, "-c", "pass"]}],
                }
            ),
            encoding="utf-8",
        )
        self.store = Store(self.project / ".stagemesh" / "stagemesh.sqlite3")
        self.store.migrate()
        self.store.upsert_task("completion", source_id=TASK)
        self.store.advance_task(TASK, Stage.IMPLEMENT)
        self.adapter = adapter
        self.log = tmp_path / "impl.log"
        script = tmp_path / "provider.py"
        script.write_text(
            "import pathlib\n"
            f"log = pathlib.Path({str(self.log)!r})\n"
            "log.write_text((log.read_text() if log.exists() else '') + 'run\\n')\n"
            "pathlib.Path('docs/a.md').write_text('implemented')\n",
            encoding="utf-8",
        )
        self.integrator = Integrator(integration_ref=integration_ref, require_independent_review=require)
        self.coordinator = Coordinator(
            self.store,
            self.project,
            executor=SubprocessExecutor([sys.executable, str(script)], name="codex"),
            validator=Validator(),
            reviewer=Reviewer(adapter=adapter, require_independent=require),
            integrator=self.integrator,
            require_independent_review=require,
        )

    def tick(self, count: int = 1) -> None:
        for _ in range(count):
            self.coordinator.tick()

    @property
    def stage(self) -> str:
        return self.store.get_task(TASK)["stage"]

    def evidence(self, kind: EvidenceKind) -> list[tuple[str, dict[str, object]]]:
        rows = self.store.conn.execute(
            "SELECT status, payload FROM evidence WHERE kind=? ORDER BY created_at, rowid", (kind,)
        ).fetchall()
        return [(row["status"], json.loads(row["payload"])) for row in rows]

    def ref(self, name: str) -> str:
        return self.git.run("rev-parse", name).stdout.strip()


def test_real_independent_review_succeeds_and_advances_to_integrate(tmp_path: Path) -> None:
    adapter = FakeReviewAdapter("claude")
    rig = Rig(tmp_path, adapter, integration_ref="refs/heads/integration")

    rig.tick(3)  # implement, validate, review

    (status, payload), = rig.evidence(EvidenceKind.REVIEW)
    assert status == EvidenceStatus.PASSED
    assert payload["independent_reviewer"] is True
    assert payload["review_execution_invoked"] is True
    assert payload["implementer_provider"] == "codex" and payload["review_provider"] == "claude"
    assert payload["independent_review_required"] is True
    assert adapter.calls == 1
    assert rig.stage == Stage.INTEGRATE


@pytest.mark.parametrize(
    ("adapter", "reason"),
    [(None, "independent_review_unavailable"), (FakeReviewAdapter("Codex"), "review_provider_same_as_implementer")],
)
def test_fallback_or_same_provider_cannot_satisfy_required_review(tmp_path: Path, adapter, reason: str) -> None:
    rig = Rig(tmp_path, adapter, integration_ref="refs/heads/integration")

    rig.tick(6)

    assert rig.stage == Stage.REVIEW
    assert rig.store.get_task(TASK)["status"] == TaskStatus.OPEN
    assert not [e for e in rig.evidence(EvidenceKind.REVIEW) if e[0] == EvidenceStatus.PASSED]
    status, payload = rig.evidence(EvidenceKind.REVIEW)[0]
    assert status == EvidenceStatus.CAPACITY
    assert payload["independent_reviewer"] is False
    assert payload["review_infrastructure_failure"] == reason
    if adapter is not None:
        assert adapter.calls == 0
    assert rig.evidence(EvidenceKind.INTEGRATION) == []


@pytest.mark.parametrize("real_missing_binary", [False, True])
def test_reviewer_outage_does_not_trigger_code_remediation(tmp_path: Path, real_missing_binary: bool) -> None:
    if real_missing_binary:
        adapter = RuntimeCommandAdapter(name="claude", command=("stagemesh-no-such-reviewer-binary",))
        reason = "provider_unavailable"
    else:
        adapter = FakeReviewAdapter("claude", '{"decision":"INFRASTRUCTURE_FAILURE","reason":"provider_timeout"}')
        reason = "provider_timeout"
    rig = Rig(tmp_path, adapter, integration_ref="refs/heads/integration")

    rig.tick(7)

    assert rig.stage == Stage.REVIEW
    assert rig.log.read_text().count("run") == 1  # the implementation provider was never re-invoked
    assert rig.store.task_remediation_count(TASK, "REVIEW") == 0
    assert rig.store.conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == 0
    statuses = [status for status, _ in rig.evidence(EvidenceKind.REVIEW)]
    assert statuses and set(statuses) == {EvidenceStatus.CAPACITY}
    assert rig.evidence(EvidenceKind.REVIEW)[0][1]["review_infrastructure_failure"] == reason


@pytest.mark.parametrize("checked_out", [True, False])
def test_exact_candidate_fast_forwards_integration_ref_and_reaches_done(tmp_path: Path, checked_out: bool) -> None:
    rig = Rig(tmp_path, FakeReviewAdapter("claude"), integration_ref="refs/heads/integration")
    if checked_out:
        rig.integrator.integration_ref = rig.branch  # the project's checked-out branch: ff via merge --ff-only
    ref = rig.integrator.integration_ref

    rig.tick(4)

    candidate = rig.store.latest_candidate(TASK)["sha"]
    assert rig.stage == Stage.DONE
    assert rig.ref(ref) == candidate != rig.base
    (status, payload), = rig.evidence(EvidenceKind.INTEGRATION)
    assert status == EvidenceStatus.PASSED
    assert payload["integration_ref_before"] == rig.base
    assert payload["integration_ref_after"] == candidate
    assert payload["integration_method"] == ("merge_ff_only" if checked_out else "update_ref_ff_only")
    assert payload["candidate_sha"] == candidate and payload["baseline_sha"] == rig.base
    assert payload["independent_review_required"] is True and payload["independent_review_verified"] is True
    assert payload["contract_hash"] and payload["contract_version"] == 1


def test_non_fast_forward_integration_fails_without_touching_ref(tmp_path: Path) -> None:
    rig = Rig(tmp_path, FakeReviewAdapter("claude"), integration_ref="refs/heads/integration")
    rig.git.run("checkout", "-q", "integration")
    (rig.project / "docs" / "other.md").write_text("other\n", encoding="utf-8")
    rig.git.run("add", "docs/other.md")
    rig.git.run("commit", "-q", "-m", "other")
    other = rig.git.head()
    rig.git.run("checkout", "-q", rig.branch.removeprefix("refs/heads/"))

    rig.tick(4)

    (status, payload), = rig.evidence(EvidenceKind.INTEGRATION)
    assert status == EvidenceStatus.FAILED
    assert [item["code"] for item in payload["findings"]] == ["integration_non_fast_forward"]
    assert rig.ref("refs/heads/integration") == other
    assert rig.store.get_task(TASK)["stage"] != Stage.DONE


def test_done_requires_candidate_on_integration_ref(tmp_path: Path) -> None:
    rig = Rig(tmp_path, FakeReviewAdapter("claude"), integration_ref="refs/heads/integration")
    rig.tick(3)
    assert rig.stage == Stage.INTEGRATE
    sha = rig.store.latest_candidate(TASK)["sha"]
    bound = contract_for_candidate(rig.store, TASK, sha, rig.project)
    rig.store.add_evidence(
        TASK, sha, EvidenceKind.INTEGRATION, EvidenceStatus.PASSED, {**bound.evidence_payload(), "findings": []}
    )  # fabricated: the ref was never moved

    rig.tick(2)

    assert rig.stage == Stage.INTEGRATE
    assert rig.store.get_task(TASK)["status"] != TaskStatus.DONE
    assert rig.ref("refs/heads/integration") == rig.base


def _commit_docs(git: GitWorkspace, project: Path, text: str) -> str:
    (project / "docs" / "a.md").write_text(text, encoding="utf-8")
    git.run("add", "docs/a.md")
    git.run("commit", "-q", "-m", text)
    return git.head()


def test_review_evidence_for_one_candidate_cannot_authorize_another(tmp_path: Path) -> None:
    rig = Rig(tmp_path, FakeReviewAdapter("claude"), integration_ref="refs/heads/integration")
    store, project = rig.store, rig.project
    store.set_task_baseline(TASK, rig.base)
    bind_task_contract(store, project, TASK, rig.base)
    sha_a = _commit_docs(rig.git, project, "candidate a")
    sha_b = _commit_docs(rig.git, project, "candidate b")
    store.add_candidate(TASK, sha_a, "codex", True)
    store.add_candidate(TASK, sha_b, "codex", True)
    assert Validator().validate(store, TASK, sha_a, project) is EvidenceStatus.PASSED
    assert Reviewer(adapter=FakeReviewAdapter("claude"), require_independent=True).review(
        store, TASK, sha_a, project
    ) is EvidenceStatus.PASSED

    assert rig.integrator.integrate(store, TASK, sha_b, project) is EvidenceStatus.FAILED
    assert rig.ref("refs/heads/integration") == rig.base
    payload = rig.evidence(EvidenceKind.INTEGRATION)[0][1]
    assert payload["findings"][0]["code"] == "missing_required_bound_evidence"

    assert rig.integrator.integrate(store, TASK, sha_a, project) is EvidenceStatus.PASSED
    assert rig.ref("refs/heads/integration") == sha_a


def test_integration_rejects_non_independent_review_when_required(tmp_path: Path) -> None:
    rig = Rig(tmp_path, None, integration_ref="refs/heads/integration")
    store, project = rig.store, rig.project
    store.set_task_baseline(TASK, rig.base)
    bind_task_contract(store, project, TASK, rig.base)
    sha = _commit_docs(rig.git, project, "candidate")
    store.add_candidate(TASK, sha, "codex", True)
    assert Validator().validate(store, TASK, sha, project) is EvidenceStatus.PASSED
    assert Reviewer(provider_name="builtin-deterministic-fallback").review(store, TASK, sha, project) is (
        EvidenceStatus.PASSED
    )

    assert rig.integrator.integrate(store, TASK, sha, project) is EvidenceStatus.FAILED
    assert rig.ref("refs/heads/integration") == rig.base


def test_cli_refuses_to_start_when_required_review_provider_is_not_distinct(tmp_path: Path) -> None:
    import argparse

    import stagemesh.cli as cli_module

    project = tmp_path / "proj"
    project.mkdir()
    git = GitWorkspace(project)
    git.init_if_needed()
    (project / "a.txt").write_text("a\n", encoding="utf-8")
    git.commit_all("base")
    runtime = project / ".stagemesh"
    runtime.mkdir()
    marker = tmp_path / "ran.marker"
    script = tmp_path / "provider.py"
    script.write_text(f"import pathlib\npathlib.Path({str(marker)!r}).write_text('x')\n", encoding="utf-8")
    command = f'"{sys.executable}" "{script}"'
    (runtime / "backlog.json").write_text(json.dumps({"tasks": [{"id": "t1", "title": "t"}]}), encoding="utf-8")
    (runtime / "config.json").write_text(
        json.dumps(
            {
                "routing": {"mode": "STAGED", "stage_routes": {"IMPLEMENT": "codex", "REVIEW": "claude"}},
                "providers": {"codex": command, "claude": command},  # same underlying command: not independent
            }
        ),
        encoding="utf-8",
    )

    args = argparse.Namespace(project=str(project), once=True, json=True, provider=None, dry_run=False, task=None)

    assert cli_module.command_continue(args) == 2
    assert not marker.exists()
