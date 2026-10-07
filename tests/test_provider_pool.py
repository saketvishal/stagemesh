from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from stagemesh.config import ConfigValidationError, load_config
from stagemesh.coordinator import Coordinator
from stagemesh.domain import EvidenceKind, EvidenceStatus, ExecutionStatus, Stage
from stagemesh.git import GitWorkspace
from stagemesh.integration import Integrator
from stagemesh.persistence import Store
from stagemesh.provider_pool import (
    IMPLEMENT,
    REVIEW,
    PooledExecutor,
    ProviderLog,
    ProviderPool,
    default_pools,
)
from stagemesh.providers import RuntimeCommandAdapter
from stagemesh.review import Reviewer
from stagemesh.workspaces import task_workspace

TASK = "TASK-1"
SCRIPT = (
    "import pathlib, sys\n"
    "mode = sys.argv[1]\n"
    "prompt = sys.stdin.read()\n"
    "if 'Review candidate' in prompt:\n"
    "    if mode == 'review-rate-limit':\n"
    "        sys.stderr.write('rate limit exceeded'); sys.exit(1)\n"
    "    if mode == 'weekly-limit':\n"
    "        sys.stderr.write(\"You've hit your weekly limit \\u00b7 resets 1am (America/Chicago)\"); sys.exit(1)\n"
    "    print('{\"decision\":\"PASS\"}'); sys.exit(0)\n"
    "if mode == 'auth-fail':\n"
    "    sys.stderr.write('authentication_error: not logged in'); sys.exit(1)\n"
    "if mode == 'weekly-limit':\n"
    "    sys.stderr.write(\"You've hit your weekly limit \\u00b7 resets 1am (America/Chicago)\"); sys.exit(1)\n"
    "if mode == 'impl-error':\n"
    "    sys.stderr.write('AssertionError: expected the capacity line once'); sys.exit(1)\n"
    "if mode == 'noop':\n"
    "    sys.exit(0)\n"
    "if mode == 'sleep':\n"
    "    import time; time.sleep(30); sys.exit(0)\n"
    "pathlib.Path('docs/a.md').write_text('by ' + mode + ' ' + (sys.argv[2] if len(sys.argv) > 2 else '') + '\\n')\n"
)


class Rig:
    def __init__(
        self,
        tmp_path: Path,
        modes: dict[str, str | None],
        pools: dict[str, tuple[str, ...]] | None = None,
        contract: dict | None = None,
        **pool_kwargs,
    ):
        """modes: provider name -> behavior ('ok', 'auth-fail', 'review-rate-limit') or None for a missing CLI."""
        self.project = tmp_path / "repo"
        self.project.mkdir(parents=True)
        git = self.git = GitWorkspace(self.project)
        git.init_if_needed()
        git.run("config", "user.email", "t@example.invalid")
        git.run("config", "user.name", "T")
        (self.project / "docs").mkdir()
        (self.project / "docs" / "a.md").write_text("a\n", encoding="utf-8")
        self.base = git.commit_all("base")
        git.run("branch", "integration")
        contracts = self.project / ".stagemesh" / "contracts"
        contracts.mkdir(parents=True)
        (contracts / f"{TASK}.json").write_text(
            json.dumps(
                contract
                or {
                    "objective": "docs only",
                    "allowed_files": ["docs/**"],
                    "required_tests": [{"name": "docs-static", "command": [sys.executable, "-c", "pass"]}],
                }
            ),
            encoding="utf-8",
        )
        script = tmp_path / "provider.py"
        script.write_text(SCRIPT, encoding="utf-8")
        adapters = [
            RuntimeCommandAdapter(
                name,
                (sys.executable, str(script), mode, name) if mode else ("stagemesh-no-such-cli-xyz",),
            )
            for name, mode in modes.items()
        ]
        self.log = ProviderLog(echo=False)
        self.pool = ProviderPool(
            adapters,
            pools or {IMPLEMENT: tuple(modes), REVIEW: tuple(modes)},
            require_independent=True,
            log=self.log,
            **pool_kwargs,
        )
        self.store = Store(self.project / ".stagemesh" / "stagemesh.sqlite3")
        self.store.migrate()
        self.store.upsert_task("t", source_id=TASK)
        self.coordinator = Coordinator(
            self.store,
            self.project,
            executor=PooledExecutor(self.pool),
            reviewer=Reviewer(require_independent=True, review_pool=self.pool),
            integrator=Integrator(integration_ref="refs/heads/integration", require_independent_review=True),
            require_independent_review=True,
        )

    def tick(self, count: int = 1) -> None:
        for _ in range(count):
            self.coordinator.tick()

    @property
    def stage(self) -> str:
        return self.store.get_task(TASK)["stage"]

    def ref(self, name: str) -> str:
        return self.git.run("rev-parse", name).stdout.strip()

    def text(self) -> str:
        return "\n".join(self.log.lines)

    def review_payload(self) -> dict:
        row = self.store.conn.execute(
            "SELECT payload FROM evidence WHERE kind=? ORDER BY created_at DESC", (EvidenceKind.REVIEW,)
        ).fetchone()
        return json.loads(row["payload"])


WEEKLY = "You've hit your weekly limit · resets 1am (America/Chicago)"


def test_weekly_limit_phrase_is_quota_not_an_implementation_defect() -> None:
    from stagemesh.execution import classify_failure

    is_cap, reason = classify_failure(1, stderr=WEEKLY)
    assert is_cap is True and reason == "quota_rate_limit"
    is_cap, reason = classify_failure(1, stderr="AssertionError: expected the capacity line once")
    assert is_cap is False and reason == "implementation_failure"
    is_cap, reason = classify_failure(1, stderr="the diff hit the file limit")
    assert is_cap is False and reason == "implementation_failure"


def test_weekly_limit_falls_through_to_the_next_provider_without_naming_it(tmp_path: Path) -> None:
    rig = Rig(tmp_path, {"provider-a": "weekly-limit", "provider-b": "ok"}, pools={IMPLEMENT: ("provider-a", "provider-b"), REVIEW: ("provider-b",)})

    rig.tick(2)

    candidate = rig.store.latest_candidate(TASK)
    assert candidate["produced_by"] == "provider-b"
    assert "fallback: provider-a failed (quota_rate_limit) -> trying provider-b" in rig.text()
    failure = json.loads(rig.store.conn.execute("SELECT payload FROM audit_events WHERE event_type='provider.failure'").fetchone()["payload"])
    assert failure["provider"] == "provider-a" and failure["reason"] == "quota_rate_limit"
    assert "You've hit your weekly limit" in failure["provider_output"]
    assert "resets 1am" in failure["provider_output"]
    assert "resets 1am" in failure["retry_after"]
    assert failure["next_provider"] == "provider-b"
    assert rig.store.task_remediation_count(TASK, "IMPLEMENT") == 0
    assert rig.store.latest_candidate(TASK)["sha"]


def test_every_provider_weekly_limit_is_one_capacity_result(tmp_path: Path) -> None:
    rig = Rig(tmp_path, {"provider-a": "weekly-limit", "provider-b": "weekly-limit"})

    progressed = rig.coordinator.tick()
    assert progressed == 1  # plan
    progressed = rig.coordinator.tick()
    assert progressed == 0
    assert rig.store.latest_candidate(TASK) is None
    assert rig.stage == Stage.IMPLEMENT
    events = [json.loads(r["payload"]) for r in rig.store.conn.execute("SELECT payload FROM audit_events WHERE event_type='task.capacity_failure'")]
    assert len(events) == 1
    assert events[0]["reason"].startswith("all_implementation_providers_failed")
    assert "provider-a" in events[0]["reason"] and "provider-b" in events[0]["reason"]
    assert rig.store.task_remediation_count(TASK, "IMPLEMENT") == 0
    # cooled down: another tick does not call them again
    rig.coordinator.tick()
    assert rig.store.conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 2


def test_ordinary_implementation_error_does_not_skip_to_the_next_provider(tmp_path: Path) -> None:
    rig = Rig(tmp_path, {"provider-a": "impl-error", "provider-b": "ok"})

    rig.tick(2)

    assert rig.store.latest_candidate(TASK) is None
    assert "trying provider-b" not in rig.text()
    assert "final implementation provider: provider-a" in rig.text()
    assert not rig.store.conn.execute("SELECT 1 FROM audit_events WHERE event_type='task.capacity_failure'").fetchone()


def test_review_weekly_limit_falls_through_to_the_next_reviewer(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        {"provider-a": "ok", "provider-b": "weekly-limit", "provider-c": "ok"},
        pools={IMPLEMENT: ("provider-a",), REVIEW: ("provider-b", "provider-c")},
    )

    rig.tick(4)

    payload = rig.review_payload()
    assert payload["review_provider"] == "provider-c"
    assert "fallback: provider-b failed (quota_rate_limit) -> trying provider-c" in rig.text()


def test_implementation_falls_back_through_the_pool_automatically(tmp_path: Path) -> None:
    rig = Rig(tmp_path, {"codex": None, "claude": "auth-fail", "grok": "ok"})

    rig.tick(2)  # PLAN->IMPLEMENT, then implementation with fallback

    candidate = rig.store.latest_candidate(TASK)
    assert candidate["produced_by"] == "grok" and rig.stage == Stage.VALIDATE
    text = rig.text()
    assert "provider pool considered: codex, claude, grok" in text
    assert "skipped codex: cli_not_installed" in text
    assert "fallback: claude failed (authentication_failure) -> trying grok" in text
    assert "final implementation provider: grok" in text and candidate["sha"] in text
    failures = rig.store.conn.execute("SELECT payload FROM audit_events WHERE event_type='provider.failure'").fetchall()
    assert [json.loads(r["payload"])["provider"] for r in failures] == ["claude"]


def test_task_completes_with_one_implementer_and_a_distinct_reviewer_without_touching_main(tmp_path: Path) -> None:
    rig = Rig(tmp_path, {"codex": None, "claude": "ok", "grok": "ok"})
    main_before = rig.ref("HEAD")

    rig.tick(2)  # implement

    assert rig.store.latest_candidate(TASK)["produced_by"] == "claude"
    worktree = task_workspace(rig.project, TASK)
    assert worktree.exists() and worktree != rig.project
    assert rig.ref("HEAD") == main_before and rig.ref("refs/heads/integration") == rig.base  # nothing integrated yet
    assert (rig.project / "docs" / "a.md").read_text(encoding="utf-8") == "a\n"  # checkout untouched
    assert "isolated worktree" in rig.text() and str(worktree) in rig.text()

    rig.tick(3)  # validate, review, integrate

    assert rig.stage == Stage.DONE
    payload = rig.review_payload()
    assert payload["implementer_provider"] == "claude" and payload["review_provider"] == "grok"
    assert payload["independent_reviewer"] is True
    assert "skipped claude: not_independent: produced the candidate" in rig.text()
    assert rig.ref("refs/heads/integration") == rig.store.latest_candidate(TASK)["sha"]


def test_review_falls_back_to_the_next_independent_provider(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        {"codex": "ok", "claude": "review-rate-limit", "grok": "ok"},
        pools={IMPLEMENT: ("codex",), REVIEW: ("claude", "grok")},
    )
    rig.tick(4)  # plan, implement, validate, review

    payload = rig.review_payload()
    assert payload["review_provider"] == "grok" and payload["independent_reviewer"] is True
    text = rig.text()
    assert "fallback: claude failed (quota_rate_limit) -> trying grok" in text
    assert "final review provider: grok" in text


def test_grok_can_implement_and_is_skipped_from_reviewing_its_own_candidate(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        {"codex": "ok", "claude": "ok", "grok": "ok"},
        pools={IMPLEMENT: ("grok", "codex"), REVIEW: ("grok", "claude")},
    )

    rig.tick(4)  # plan, implement, validate, review

    assert rig.store.latest_candidate(TASK)["produced_by"] == "grok"
    payload = rig.review_payload()
    assert payload["implementer_provider"] == "grok"
    assert payload["review_provider"] == "claude"
    assert payload["independent_reviewer"] is True
    text = rig.text()
    assert "selected implementation provider grok" in text
    assert "skipped grok: not_independent: produced the candidate" in text
    assert "selected review provider claude" in text


def test_grok_can_be_selected_as_an_independent_reviewer(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        {"codex": "ok", "claude": "ok", "grok": "ok"},
        pools={IMPLEMENT: ("codex",), REVIEW: ("grok", "claude")},
    )

    rig.tick(4)  # plan, implement, validate, review

    payload = rig.review_payload()
    assert rig.store.latest_candidate(TASK)["produced_by"] == "codex"
    assert payload["review_provider"] == "grok"
    assert payload["independent_reviewer"] is True
    assert "selected review provider grok" in rig.text()


def test_operator_json_exposes_provider_selection_timeline(tmp_path: Path) -> None:
    import contextlib
    import io

    import stagemesh.cli as cli_module

    rig = Rig(
        tmp_path,
        {"codex": "ok", "claude": "ok", "grok": "ok"},
        pools={IMPLEMENT: ("grok", "codex"), REVIEW: ("claude", "grok")},
        policy="weighted",
        weights={"grok": 2},
    )
    rig.tick(4)
    rig.store.close()

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli_module.main(["--project", str(rig.project), "operator", "--json"])

    assert code == 0
    data = json.loads(out.getvalue())
    section = next(section for section in data["sections"] if section["name"] == "Provider Selections")
    assert {row["stage"] for row in section["rows"]} >= {IMPLEMENT, REVIEW}
    implementation = next(row for row in section["rows"] if row["stage"] == IMPLEMENT)
    assert implementation["policy"] == "weighted"
    assert implementation["order"][0] == "grok"
    assert any(
        "provider_selection task=TASK-1 stage=IMPLEMENT policy=weighted" in line
        for line in data["lines"]
    )


def test_refuses_clearly_when_only_the_implementer_is_available(tmp_path: Path) -> None:
    rig = Rig(tmp_path, {"codex": None, "claude": "ok", "grok": None})

    ok, diagnostic, _, _ = rig.pool.preflight(rig.store)
    assert ok is False
    assert "independent review is required but cannot be satisfied" in diagnostic
    for provider in ("codex", "claude", "grok"):
        assert provider in diagnostic
    assert "cli_not_installed" in diagnostic and "not_independent" in diagnostic

    rig.tick(4)  # plan, implement, validate, review attempt
    assert rig.stage == Stage.REVIEW
    payload = rig.review_payload()
    assert payload["review_infrastructure_failure"] == "independent_review_unavailable"
    reasons = {c["provider"]: c["reason"] for c in payload["review_providers_considered"]}
    assert set(reasons) == {"codex", "claude", "grok"} and reasons["claude"].startswith("not_independent")
    assert "REFUSED: independent review cannot be satisfied" in rig.text()
    status = rig.store.conn.execute("SELECT status FROM evidence WHERE kind=?", (EvidenceKind.REVIEW,)).fetchone()["status"]
    assert status == EvidenceStatus.CAPACITY


def test_no_implementation_provider_available_lists_every_provider(tmp_path: Path) -> None:
    rig = Rig(tmp_path, {"codex": None, "claude": None, "grok": None})
    result = rig.coordinator.executor.run(rig.store, TASK, None, rig.project)
    assert result.status is ExecutionStatus.FAILED and result.capacity_failure
    for provider in ("codex", "claude", "grok"):
        assert f"{provider}: cli_not_installed" in result.failure_reason
    assert rig.pool.preflight(rig.store)[0] is False


def test_recently_failed_provider_is_skipped_for_the_same_task_and_stage_only(tmp_path: Path) -> None:
    rig = Rig(tmp_path, {"codex": "ok", "claude": "ok"})
    rig.pool.record_failure(rig.store, IMPLEMENT, TASK, "codex", "quota_rate_limit")

    same = {v.provider: v for v in rig.pool.evaluate(rig.store, IMPLEMENT, TASK)}
    assert not same["codex"].eligible and same["codex"].reason.startswith("recent_failure: quota_rate_limit")
    assert same["claude"].eligible
    assert all(v.eligible for v in rig.pool.evaluate(rig.store, IMPLEMENT, "OTHER-TASK"))
    assert all(v.eligible for v in rig.pool.evaluate(rig.store, REVIEW, TASK))


def test_default_pools_try_routed_provider_first_then_every_other_provider() -> None:
    pools = default_pools(["claude", "codex", "grok"], {"IMPLEMENT": "claude"}, {})
    assert pools == {"IMPLEMENT": ("claude", "codex", "grok"), "REVIEW": ("codex", "claude", "grok")}
    assert default_pools(["claude", "codex"], {"REVIEW": "codex"}, {"IMPLEMENT": ("grok",)})["IMPLEMENT"] == ("grok",)


def test_routing_pools_config_is_validated(tmp_path: Path) -> None:
    runtime = tmp_path / ".stagemesh"
    runtime.mkdir()
    config = runtime / "config.json"
    config.write_text(
        json.dumps({"routing": {"pools": {"IMPLEMENT": ["codex", "claude"], "REVIEW": ["grok"]}}}), encoding="utf-8"
    )
    assert load_config(tmp_path).provider_pools == {"IMPLEMENT": ("codex", "claude"), "REVIEW": ("grok",)}
    for bad in ({"VALIDATE": ["codex"]}, {"IMPLEMENT": []}, {"IMPLEMENT": ["codex", "codex"]}):
        config.write_text(json.dumps({"routing": {"pools": bad}}), encoding="utf-8")
        with pytest.raises(ConfigValidationError):
            load_config(tmp_path)


def test_grok_provider_pool_example_config_loads() -> None:
    example = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "examples"
        / "grok-provider-pools.config.json"
    )
    config = load_config(example.parent, config_path=example)
    assert config.provider_pools[IMPLEMENT] == ("grok", "codex", "claude")
    assert config.provider_pools[REVIEW] == ("claude", "grok", "codex")
    assert config.provider_specs["grok"].capabilities == frozenset({IMPLEMENT, REVIEW})
    assert config.provider_selection_policy == "weighted"
    assert config.provider_weights["grok"] == 2


def test_cli_refuses_at_startup_when_only_the_implementer_is_available(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import stagemesh.cli as cli_module

    rig = Rig(tmp_path, {"codex": "ok"})
    rig.store.close()
    script = tmp_path / "provider.py"
    only = f'"{sys.executable}" "{script}" ok claude'
    missing = "stagemesh-no-such-cli-xyz"
    (rig.project / ".stagemesh" / "config.json").write_text(
        json.dumps({"providers": {"codex": missing, "claude": only, "grok": missing}, "routing": {"mode": "STAGED"}}),
        encoding="utf-8",
    )

    code = cli_module.main(["--project", str(rig.project), "continue", "--task", TASK])

    err = capsys.readouterr().err
    assert code == 2
    assert "independent review is required but cannot be satisfied" in err
    for provider in ("codex", "claude", "grok"):
        assert provider in err
    assert "cli_not_installed" in err
    assert Store(rig.project / ".stagemesh" / "stagemesh.sqlite3").latest_candidate(TASK) is None  # nothing ran


def _no_progress_events(rig: Rig) -> list[dict]:
    return [
        json.loads(row["payload"])
        for row in rig.store.conn.execute("SELECT payload FROM audit_events WHERE event_type='provider.no_progress' ORDER BY created_at, rowid")
    ]


def test_noop_provider_falls_through_when_the_task_is_still_unresolved(tmp_path: Path) -> None:
    """Real Task #158 shape: provider A exits 0 with no diff and the objective is not proven satisfied."""
    rig = Rig(
        tmp_path,
        {"provider-a": "noop", "provider-b": "ok"},
        pools={IMPLEMENT: ("provider-a", "provider-b"), REVIEW: ("provider-b",)},
    )

    rig.tick(2)

    candidate = rig.store.latest_candidate(TASK)
    assert candidate is not None and candidate["produced_by"] == "provider-b"
    assert "final implementation provider: provider-a" not in rig.text()
    assert "made no progress (no_implementation_change; task unresolved) -> trying provider-b" in rig.text()
    events = _no_progress_events(rig)
    assert events[0]["provider"] == "provider-a"
    assert events[0]["result"] == "no_implementation_change"
    assert events[0]["task_unresolved"] is True
    assert events[0]["next_provider"] == "provider-b"
    assert events[0]["task_id"] == TASK
    assert rig.store.task_remediation_count(TASK, "IMPLEMENT") == 0
    assert "--provider" not in rig.text()


def test_noop_completes_when_acceptance_gates_already_pass_on_the_baseline(tmp_path: Path) -> None:
    gate = [sys.executable, "-c", "from pathlib import Path; assert Path('docs/a.md').read_text(encoding='utf-8')=='done\\n'"]
    rig = Rig(
        tmp_path,
        {"provider-a": "noop", "provider-b": "ok"},
        pools={IMPLEMENT: ("provider-a", "provider-b"), REVIEW: ("provider-b",)},
        contract={
            "objective": "docs already say done",
            "allowed_files": ["docs/**"],
            "acceptance_criteria": ["docs-already-done"],
            "required_tests": [{"name": "docs-already-done", "command": gate}],
        },
    )
    (rig.project / "docs" / "a.md").write_text("done\n", encoding="utf-8")
    rig.git.commit_all("objective already present")

    rig.tick(2)

    task = rig.store.get_task(TASK)
    assert task["stage"] == Stage.DONE and task["status"] == "DONE"
    assert rig.store.latest_candidate(TASK) is None
    assert _no_progress_events(rig) == []
    assert "trying provider-b" not in rig.text()
    proof = json.loads(rig.store.conn.execute("SELECT payload FROM audit_events WHERE event_type='task.already_satisfied'").fetchone()["payload"])
    assert proof["provider"] == "provider-a"
    assert proof["gates"][0]["name"] == "docs-already-done" and proof["gates"][0]["status"] == "PASSED"
    assert rig.store.task_remediation_count(TASK, "IMPLEMENT") == 0


def test_every_provider_no_progress_is_one_bounded_unresolved_result(tmp_path: Path) -> None:
    from stagemesh.diagnosis import diagnose

    rig = Rig(tmp_path, {"provider-a": "noop", "provider-b": "noop"})

    assert rig.coordinator.tick() == 1  # plan
    assert rig.coordinator.tick() == 0
    assert rig.store.latest_candidate(TASK) is None
    assert rig.stage == Stage.IMPLEMENT
    assert rig.store.conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 2
    reason = json.loads(
        rig.store.conn.execute("SELECT payload FROM audit_events WHERE event_type='task.implementation_unsuccessful'").fetchone()["payload"]
    )["reason"]
    assert reason.startswith("all_implementation_providers_no_progress")
    assert "provider-a: no_implementation_change" in reason and "provider-b: no_implementation_change" in reason
    diagnosis = diagnose(rig.store, TASK, rig.project)
    assert diagnosis is not None
    assert "--provider" not in diagnosis.recommendation
    assert rig.store.task_remediation_count(TASK, "IMPLEMENT") == 0
    assert not rig.store.conn.execute("SELECT 1 FROM evidence").fetchone()


def test_capacity_then_no_progress_then_success_walks_the_pool(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        {"provider-a": "weekly-limit", "provider-b": "noop", "provider-c": "ok"},
        pools={IMPLEMENT: ("provider-a", "provider-b", "provider-c"), REVIEW: ("provider-c",)},
    )

    rig.tick(2)

    assert rig.store.latest_candidate(TASK)["produced_by"] == "provider-c"
    text = rig.text()
    assert "fallback: provider-a failed (quota_rate_limit) -> trying provider-b" in text
    assert "fallback: provider-b made no progress (no_implementation_change; task unresolved) -> trying provider-c" in text


def test_no_progress_then_capacity_then_success_walks_the_pool(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        {"provider-a": "noop", "provider-b": "weekly-limit", "provider-c": "ok"},
        pools={IMPLEMENT: ("provider-a", "provider-b", "provider-c"), REVIEW: ("provider-c",)},
    )

    rig.tick(2)

    assert rig.store.latest_candidate(TASK)["produced_by"] == "provider-c"
    text = rig.text()
    assert "fallback: provider-a made no progress (no_implementation_change; task unresolved) -> trying provider-b" in text
    assert "fallback: provider-b failed (quota_rate_limit) -> trying provider-c" in text


def test_no_progress_does_not_consume_remediation_budget(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        {"provider-a": "noop", "provider-b": "ok"},
        pools={IMPLEMENT: ("provider-a", "provider-b"), REVIEW: ("provider-b",)},
    )
    before = rig.store.task_remediation_count(TASK, "IMPLEMENT")

    rig.tick(2)

    assert rig.store.latest_candidate(TASK)["produced_by"] == "provider-b"
    assert rig.store.task_remediation_count(TASK, "IMPLEMENT") == before
    assert not rig.store.conn.execute("SELECT 1 FROM audit_events WHERE event_type='task.remediation_queued'").fetchone()
    assert not rig.store.conn.execute("SELECT 1 FROM evidence").fetchone()


def _unsuccessful_reason(rig: Rig) -> str:
    row = rig.store.conn.execute(
        "SELECT event_type, payload FROM audit_events WHERE event_type IN ('task.implementation_unsuccessful', 'task.capacity_failure') ORDER BY created_at, rowid"
    ).fetchall()[-1]
    payload = json.loads(row["payload"])
    assert payload["candidate_produced"] is False
    assert payload["pool_exhausted"] is True
    assert payload["no_further_provider"]
    return str(payload["reason"])


def test_no_progress_then_timeout_exhausts_without_asking_for_a_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from stagemesh.diagnosis import diagnose

    monkeypatch.setattr("stagemesh.providers.provider_timeout_seconds", lambda seconds=None: 0.2)
    rig = Rig(
        tmp_path,
        {"provider-a": "noop", "provider-b": "sleep"},
        pools={IMPLEMENT: ("provider-a", "provider-b"), REVIEW: ("provider-b",)},
    )

    assert rig.coordinator.tick() == 1
    assert rig.coordinator.tick() == 0

    reason = _unsuccessful_reason(rig)
    assert reason.startswith("all_implementation_providers_exhausted")
    assert reason.index("provider-a: no_implementation_change") < reason.index("provider-b: provider_timeout")
    payload = json.loads(
        rig.store.conn.execute(
            "SELECT payload FROM audit_events WHERE event_type='task.implementation_unsuccessful'"
        ).fetchone()["payload"]
    )
    assert payload["provider_sequence"] == ["provider-a", "provider-b"]
    assert [item["classification"] for item in payload["provider_outcomes"]] == ["no_progress", "timeout"]
    diagnosis = diagnose(rig.store, TASK, rig.project)
    assert diagnosis is not None
    assert "--provider" not in diagnosis.recommendation
    assert "choose another provider" not in diagnosis.recommendation.lower()
    assert rig.store.latest_candidate(TASK) is None
    assert rig.store.task_remediation_count(TASK, "IMPLEMENT") == 0


def test_quota_then_timeout_exhausts_without_operator_provider_selection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from stagemesh.diagnosis import diagnose

    monkeypatch.setattr("stagemesh.providers.provider_timeout_seconds", lambda seconds=None: 0.2)
    rig = Rig(tmp_path, {"provider-a": "weekly-limit", "provider-b": "sleep"})

    assert rig.coordinator.tick() == 1
    assert rig.coordinator.tick() == 0

    reason = _unsuccessful_reason(rig)
    assert "provider-a" in reason and "provider-b: provider_timeout" in reason
    diagnosis = diagnose(rig.store, TASK, rig.project)
    assert "--provider" not in diagnosis.recommendation
    assert "choose another provider" not in diagnosis.recommendation.lower()
    assert rig.store.task_remediation_count(TASK, "IMPLEMENT") == 0


def test_no_progress_then_quota_is_one_bounded_exhausted_result(tmp_path: Path) -> None:
    from stagemesh.diagnosis import diagnose

    rig = Rig(tmp_path, {"provider-a": "noop", "provider-b": "weekly-limit"})

    assert rig.coordinator.tick() == 1
    assert rig.coordinator.tick() == 0
    assert rig.store.conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 2
    reason = _unsuccessful_reason(rig)
    assert reason.startswith("all_implementation_providers_exhausted")
    diagnosis = diagnose(rig.store, TASK, rig.project)
    assert "--provider" not in diagnosis.recommendation
    assert rig.store.latest_candidate(TASK) is None


def test_all_capacity_exhaustion_does_not_ask_for_a_provider(tmp_path: Path) -> None:
    from stagemesh.diagnosis import diagnose

    rig = Rig(tmp_path, {"provider-a": "weekly-limit", "provider-b": "weekly-limit"})
    rig.coordinator.tick()
    rig.coordinator.tick()
    reason = _unsuccessful_reason(rig)
    assert reason.startswith("all_implementation_providers_failed")
    diagnosis = diagnose(rig.store, TASK, rig.project)
    assert "--provider" not in diagnosis.recommendation


def test_authentication_exhaustion_is_classified_as_capacity(tmp_path: Path) -> None:
    rig = Rig(tmp_path, {"provider-a": "auth-fail", "provider-b": "weekly-limit"})
    rig.coordinator.tick()
    rig.coordinator.tick()
    payload = json.loads(
        rig.store.conn.execute("SELECT payload FROM audit_events WHERE event_type='task.capacity_failure'").fetchone()["payload"]
    )
    assert payload["provider_sequence"] == ["provider-a", "provider-b"]
    assert [item["classification"] for item in payload["provider_outcomes"]] == ["capacity", "capacity"]


def test_a_remaining_provider_is_still_attempted(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        {"provider-a": "noop", "provider-b": "weekly-limit", "provider-c": "ok"},
        pools={IMPLEMENT: ("provider-a", "provider-b", "provider-c"), REVIEW: ("provider-c",)},
    )
    rig.tick(2)
    assert rig.store.latest_candidate(TASK)["produced_by"] == "provider-c"


def test_explicit_provider_pool_does_not_fall_through(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        {"provider-a": "noop", "provider-b": "ok"},
        pools={IMPLEMENT: ("provider-a",), REVIEW: ("provider-b",)},
    )
    rig.tick(2)
    assert rig.store.latest_candidate(TASK) is None
    assert "trying provider-b" not in rig.text()
    actors = [row["actor"] for row in rig.store.conn.execute("SELECT actor FROM executions")]
    assert actors == ["provider-a"]


def test_no_progress_fallback_does_not_depend_on_provider_names(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        {"north": "noop", "south": "ok"},
        pools={IMPLEMENT: ("north", "south"), REVIEW: ("south",)},
    )

    rig.tick(2)

    assert rig.store.latest_candidate(TASK)["produced_by"] == "south"
    assert _no_progress_events(rig)[0]["provider"] == "north"
    assert "trying south" in rig.text()
