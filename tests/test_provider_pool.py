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
    "    print('{\"decision\":\"PASS\"}'); sys.exit(0)\n"
    "if mode == 'auth-fail':\n"
    "    sys.stderr.write('authentication_error: not logged in'); sys.exit(1)\n"
    "pathlib.Path('docs/a.md').write_text('by ' + mode + ' ' + (sys.argv[2] if len(sys.argv) > 2 else '') + '\\n')\n"
)


class Rig:
    def __init__(
        self, tmp_path: Path, modes: dict[str, str | None], pools: dict[str, tuple[str, ...]] | None = None, **pool_kwargs
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
                {
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
