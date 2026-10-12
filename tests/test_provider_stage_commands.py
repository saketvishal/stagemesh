"""One logical provider, optionally a different command per stage. No real provider: commands are the fake script from test_provider_pool."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from test_provider_pool import IMPLEMENT, REVIEW, TASK, Rig

import stagemesh.cli as cli_module
from stagemesh import readiness
from stagemesh.config import ConfigValidationError, load_config
from stagemesh.persistence import Store
from stagemesh.providers import RuntimeCommandAdapter, adapters_from_config, stage_command_summary
from stagemesh.workspaces import task_workspace


def write_config(project: Path, data: dict) -> Path:
    path = project / ".stagemesh" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def caventra_like_legacy_config() -> dict:
    return {
        "providers": {
            "claude": {
                "command": "claude -p --permission-mode acceptEdits",
                "priority": 20,
                "max_concurrency": 1,
            },
            "claude-review": {
                "command": "claude -p --permission-mode plan --allowedTools Read Grep",
                "capabilities": ["REVIEW"],
            },
            "grok": "grok --write",
            "grok-review": "grok --read-only",
        },
        "routing": {
            "pools": {
                "IMPLEMENT": ["claude", "grok"],
                "REVIEW": ["claude-review", "grok-review"],
            },
            "stage_routes": {"REVIEW": "claude-review"},
            "single_agent_provider": "claude-review",
            "provider_weights": {"claude": 2, "claude-review": 2},
        },
        "diagnosis": {"provider": "grok-review"},
    }


def adapter_for(project: Path, providers: dict, name: str) -> RuntimeCommandAdapter:
    write_config(project, {"providers": providers})
    return {a.name: a for a in adapters_from_config(load_config(project))}[name]


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    path = tmp_path / "p"
    path.mkdir()
    return path


# --- task source labels --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source_extra",
    [{}, {"excluded_labels": []}, {"excluded_labels": "stagemesh:blocked"}, {"excluded_labels": ["a", "b"]}, {"exclude_labels": ["x"]}],
)
def test_a_github_task_source_validates_with_or_without_excluded_labels(project: Path, source_extra: dict) -> None:
    write_config(project, {"task_sources": [{"name": "gh", "type": "github", "labels": ["stagemesh:ready"], **source_extra}]})

    source = load_config(project).task_sources[0]

    assert source.labels == ("stagemesh:ready",)
    expected = source_extra.get("excluded_labels", source_extra.get("exclude_labels", ()))
    assert source.excluded_labels == ((expected,) if isinstance(expected, str) else tuple(expected))


def test_a_github_task_source_still_rejects_malformed_labels(project: Path) -> None:
    write_config(project, {"task_sources": [{"name": "gh", "type": "github", "labels": [1]}]})
    with pytest.raises(ConfigValidationError, match="labels must be a string or list of strings"):
        load_config(project)


# --- config schema ------------------------------------------------------------------------------------------------------------


def test_a_provider_with_only_command_behaves_exactly_as_before(project: Path) -> None:
    for providers in ({"p": "tool --x"}, {"p": {"command": "tool --x"}}):
        adapter = adapter_for(project, providers, "p")
        assert adapter.command == ("tool", "--x") and adapter.implement_command is None and adapter.review_command is None
        assert adapter.command_for("code") == adapter.command_for("review") == ("tool", "--x")
        sources = {stage: info["source"] for stage, info in stage_command_summary(adapter).items()}
        assert sources == {"IMPLEMENT": "default", "REVIEW": "default"}


def test_review_command_and_commands_object_are_equivalent_ways_to_set_a_stage_command(project: Path) -> None:
    flat = adapter_for(project, {"p": {"command": "write-tool", "review_command": "read-tool --plan"}}, "p")
    grouped = adapter_for(project, {"p": {"command": "write-tool", "commands": {"REVIEW": "read-tool --plan"}}}, "p")

    for adapter in (flat, grouped):
        assert adapter.command_for("code") == ("write-tool",)  # implementation keeps the default command
        assert adapter.command_for("review") == ("read-tool", "--plan")
        assert adapter.name == "p"  # still one provider
    both = adapter_for(project, {"p": {"command": "d", "implement_command": "writer", "commands": {"review": "reader"}}}, "p")
    assert (both.command_for("code"), both.command_for("review")) == (("writer",), ("reader",))


def test_the_default_command_may_be_omitted_only_when_every_served_stage_has_its_own(project: Path) -> None:
    both = adapter_for(project, {"p": {"commands": {"IMPLEMENT": "writer", "REVIEW": "reader"}}}, "p")
    assert (both.command_for("code"), both.command_for("review")) == (("writer",), ("reader",))
    reviewer_only = adapter_for(project, {"p": {"capabilities": ["REVIEW"], "review_command": "reader"}}, "p")
    assert reviewer_only.command_for("review") == ("reader",)

    write_config(project, {"providers": {"p": {"review_command": "reader"}}})  # serves IMPLEMENT too, which has no command
    with pytest.raises(ConfigValidationError, match="command for p must be a non-empty string"):
        load_config(project)


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ({"command": "c", "review_command": "r", "commands": {"REVIEW": "r2"}}, "sets the REVIEW command twice"),
        ({"command": "c", "capabilities": ["IMPLEMENT"], "review_command": "r"}, "a stage it does not serve"),
        ({"command": "c", "commands": {"DEPLOY": "x"}}, "unsupported stage"),
        ({"command": "c", "commands": {}}, "commands must be an object"),
        ({"command": "c", "review_command": " "}, "review_command must be a non-empty string"),
    ],
)
def test_bad_stage_commands_are_rejected_with_a_clear_message(project: Path, spec: dict, message: str) -> None:
    write_config(project, {"providers": {"p": spec}})
    with pytest.raises(ConfigValidationError, match=message):
        load_config(project)


# --- execution -----------------------------------------------------------------------------------------------------------------


def test_the_review_command_runs_for_review_and_the_default_command_runs_for_implementation(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        {"codex": "ok", "claude": "ok"},
        pools={IMPLEMENT: ("codex",), REVIEW: ("claude",)},
        review_modes={"claude": "fail-no-findings-review"},  # claude's review command answers FAIL; its default command would PASS
    )

    rig.tick(4)  # plan, implement, validate, review

    assert rig.store.latest_candidate(TASK)["produced_by"] == "codex"
    payload = rig.review_payload()
    assert payload["review_provider"] == "claude"
    assert '"decision":"FAIL"' in payload["review_response"].replace(" ", "")  # the review command ran, not claude's default one


def test_the_implementation_command_runs_for_implementation_and_the_default_for_review(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        {"claude": "malformed-review", "codex": "ok"},
        pools={IMPLEMENT: ("claude",), REVIEW: ("codex",)},
        implement_modes={"claude": "ok"},  # claude's default command would not write anything; the implement command does
    )

    rig.tick(2)  # plan, implement

    assert rig.store.latest_candidate(TASK)["produced_by"] == "claude"
    assert "by ok claude" in (task_workspace(rig.project, TASK) / "docs" / "a.md").read_text(encoding="utf-8")


def test_a_missing_review_command_makes_the_provider_unavailable_for_review_only(tmp_path: Path) -> None:
    adapter = RuntimeCommandAdapter("p", (sys.executable,), review_command=("stagemesh-no-such-cli-xyz",))

    assert adapter.check_stage_capacity("code") == "AVAILABLE"
    assert adapter.check_stage_capacity("review") != "AVAILABLE"
    summary = stage_command_summary(adapter)
    assert summary["IMPLEMENT"]["available"] is True and summary["REVIEW"]["available"] is False
    assert summary["REVIEW"]["source"] == "override"


# --- independence ----------------------------------------------------------------------------------------------------------------


def test_a_provider_never_reviews_its_own_candidate_even_through_a_different_review_command(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        {"claude": "ok", "codex": "ok"},
        pools={IMPLEMENT: ("claude",), REVIEW: ("claude", "codex")},
        review_modes={"claude": "ok"},
    )

    verdicts = {v.provider: v for v in rig.pool.evaluate(rig.store, REVIEW, TASK, implementer="claude")}

    assert verdicts["claude"].eligible is False and "not_independent" in verdicts["claude"].reason
    assert verdicts["codex"].eligible is True


def test_with_only_one_logical_provider_independent_review_cannot_be_satisfied(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        {"claude": "ok"},
        pools={IMPLEMENT: ("claude",), REVIEW: ("claude",)},
        review_modes={"claude": "ok"},  # a read-only review command does not make it a second, independent provider
    )

    ok, message, _impl, _review = rig.pool.preflight(rig.store, TASK)

    assert ok is False and "independent review is required but cannot be satisfied" in message


def test_two_providers_sharing_a_stage_command_are_not_independent(tmp_path: Path) -> None:
    rig = Rig(tmp_path, {"a": "ok", "b": "ok"}, pools={IMPLEMENT: ("a",), REVIEW: ("b",)})
    shared = rig.pool.adapters["a"].command  # b's review command is a's implementation command: the same underlying agent
    other = rig.pool.adapters["b"]
    rig.pool.adapters["b"] = RuntimeCommandAdapter("b", other.command, review_command=shared)

    verdicts = {v.provider: v for v in rig.pool.evaluate(rig.store, REVIEW, TASK, implementer="a")}

    assert verdicts["b"].eligible is False and "not_independent" in verdicts["b"].reason


# --- operator visibility -----------------------------------------------------------------------------------------------------------


def test_capacity_config_and_doctor_show_which_command_runs_for_each_stage(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    providers = {"claude": {"command": "claude -p --permission-mode acceptEdits", "review_command": "claude -p --allowedTools Read Grep"}}
    write_config(project, {"providers": providers})
    Store(project / ".stagemesh" / "stagemesh.sqlite3").migrate()

    assert cli_module.main(["--project", str(project), "capacity", "--json"]) == 0
    row = next(r for r in json.loads(capsys.readouterr().out)["providers"] if r["provider"] == "claude")
    assert row["stage_commands"]["IMPLEMENT"]["source"] == "default" and row["stage_commands"]["REVIEW"]["source"] == "override"
    assert "allowedTools Read Grep" in row["stage_commands"]["REVIEW"]["command"]

    assert cli_module.main(["--project", str(project), "capacity"]) == 0
    assert "provider claude commands: IMPLEMENT claude [default]; REVIEW claude [override" in capsys.readouterr().out

    assert cli_module.main(["--project", str(project), "config"]) == 0
    assert "provider.claude.REVIEW: claude -p --allowedTools Read Grep (stage override)" in capsys.readouterr().out

    monkeypatch.setattr(readiness, "check_runtime", lambda p: readiness.RuntimeFreshness())
    assert cli_module.main(["--project", str(project), "doctor", "--no-network"]) == 0
    out = capsys.readouterr().out
    assert "provider commands (one logical provider" in out and "claude: IMPLEMENT claude [default]; REVIEW claude [override" in out


def test_config_migrate_dry_run_reports_caventra_like_legacy_review_providers_without_writing(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_config(project, caventra_like_legacy_config())
    before = path.read_text(encoding="utf-8")

    assert cli_module.main(["--project", str(project), "config", "migrate", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["changed"] is True
    assert payload["backup_path"] is None
    assert "claude-review" not in payload["data"]["providers"]
    assert payload["data"]["providers"]["claude"]["command"] == "claude -p --permission-mode acceptEdits"
    assert payload["data"]["providers"]["claude"]["review_command"] == "claude -p --permission-mode plan --allowedTools Read Grep"
    assert payload["data"]["routing"]["pools"]["REVIEW"] == ["claude", "grok"]
    assert path.read_text(encoding="utf-8") == before
    assert not path.with_name(path.name + ".bak").exists()


def test_config_migrate_write_backs_up_preserves_implementation_commands_and_is_idempotent(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_config(project, caventra_like_legacy_config())
    before = path.read_text(encoding="utf-8")

    assert cli_module.main(["--project", str(project), "config", "migrate", "--write", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    backup = Path(payload["backup_path"])
    assert payload["dry_run"] is False and payload["changed"] is True
    assert backup.exists() and backup.read_text(encoding="utf-8") == before
    written = json.loads(path.read_text(encoding="utf-8"))
    assert set(written["providers"]) == {"claude", "grok"}
    assert written["providers"]["claude"]["command"] == "claude -p --permission-mode acceptEdits"
    assert written["providers"]["grok"]["command"] == "grok --write"
    assert written["providers"]["grok"]["review_command"] == "grok --read-only"
    assert written["routing"]["stage_routes"]["REVIEW"] == "claude"
    assert written["routing"]["single_agent_provider"] == "claude"
    assert written["diagnosis"]["provider"] == "grok"

    config = load_config(project)
    adapters = {adapter.name: adapter for adapter in adapters_from_config(config)}
    assert adapters["claude"].name == "claude" and adapters["claude"].command_for("code") == (
        "claude",
        "-p",
        "--permission-mode",
        "acceptEdits",
    )
    assert adapters["claude"].command_for("review") == (
        "claude",
        "-p",
        "--permission-mode",
        "plan",
        "--allowedTools",
        "Read",
        "Grep",
    )
    assert adapters["grok"].name == "grok" and adapters["grok"].command_for("review") == ("grok", "--read-only")

    assert cli_module.main(["--project", str(project), "config", "migrate", "--write", "--json"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["changed"] is False and second["backup_path"] is None
    assert not path.with_name(path.name + ".bak.1").exists()


def test_doctor_points_legacy_review_provider_config_to_the_migration_command(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(project, caventra_like_legacy_config())
    Store(project / ".stagemesh" / "stagemesh.sqlite3").migrate()
    monkeypatch.setattr(readiness, "check_runtime", lambda p: readiness.RuntimeFreshness())

    assert cli_module.main(["--project", str(project), "doctor", "--no-network"]) == 0

    out = capsys.readouterr().out
    assert "legacy provider review config detected" in out
    assert "stagemesh config migrate --write" in out
