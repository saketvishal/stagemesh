"""End to end through the real coordinator, pool and adapters with recorded stand-in CLI invocations.

The default fixture authorizes Codex and Claude and proves Grok/Antigravity cannot become implicit fallbacks. The no-Codex recovery
case explicitly authorizes Claude and Antigravity, removes Codex, and keeps review independent across a durable restart.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest
from test_provider_pool import TASK, Rig

from stagemesh import provider_authorization as pa
from stagemesh.config import load_config
from stagemesh.coordinator import Coordinator
from stagemesh.domain import Stage
from stagemesh.integration import Integrator
from stagemesh.provider_pool import PooledExecutor, ProviderPool, default_pools
from stagemesh.providers import adapters_from_config
from stagemesh.review import Reviewer

SHIM = r'''
import os, pathlib, sys
name, mode_var = sys.argv[1], "SHIM_MODE_" + sys.argv[1].upper()
with open(os.environ["SHIM_CALLS"], "a", encoding="utf-8") as handle:
    handle.write(name + "\n")
prompt = sys.stdin.read() or " ".join(sys.argv[2:])
mode = os.environ.get(mode_var, "ok")
if mode == "down":
    sys.stderr.write("authentication_error: not logged in"); sys.exit(1)
if "Review candidate" in prompt:
    print('{"decision":"PASS","findings":[]}'); sys.exit(0)
pathlib.Path("docs/a.md").write_text("by " + name + "\n")
'''


def shim_path(bin_dir: Path, name: str) -> Path:
    return bin_dir / (f"{name}.cmd" if os.name == "nt" else name)


def install_shims(bin_dir: Path, calls: Path) -> None:
    bin_dir.mkdir()
    script = bin_dir / "shim.py"
    script.write_text(SHIM, encoding="utf-8")
    for name in ("codex", "claude", "grok", "agy"):
        if os.name == "nt":
            (bin_dir / f"{name}.cmd").write_text(f'@"{sys.executable}" "{script}" {name} %*\r\n', encoding="utf-8")
        else:
            target = bin_dir / name
            target.write_text(f'#!{sys.executable}\nimport sys\nsys.argv = ["{script}", "{name}"] + sys.argv[1:]\nexec(open("{script}").read())\n', encoding="utf-8")
            target.chmod(target.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def world(tmp_path, monkeypatch):
    calls = tmp_path / "calls.txt"
    calls.write_text("", encoding="utf-8")
    install_shims(tmp_path / "bin", calls)
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("SHIM_CALLS", str(calls))
    monkeypatch.setenv(pa.UNATTENDED_ENV, "1")
    for var in ("STAGEMESH_CODEX_CMD", "STAGEMESH_CLAUDE_CMD", "STAGEMESH_GROK_CMD", "STAGEMESH_AGY_CMD"):
        monkeypatch.delenv(var, raising=False)
    rig = Rig(tmp_path, {"unused": "ok"})
    import subprocess as _subprocess

    from stagemesh import providers as _providers

    rig.spawn_errors = []
    rig.spawned_argv = []
    real_popen = _subprocess.Popen

    def recording_popen(*args, **kwargs):
        rig.spawned_argv.append(args[0] if args else kwargs.get("args"))
        try:
            return real_popen(*args, **kwargs)
        except Exception as exc:  # recorded for diagnosis, then re-raised unchanged
            rig.spawn_errors.append(f"{type(exc).__name__}: {exc} argv0={args[0][0] if args and args[0] else None!r} cwd={kwargs.get('cwd')!r}")
            raise

    monkeypatch.setattr(_providers.subprocess, "Popen", recording_popen)
    (rig.project / ".stagemesh" / "config.json").write_text(
        json.dumps({
            "authorization": {"unattended_providers": ["codex", "claude"]},
            # Windows cannot start a bare `codex` that is only a .cmd shim, so the stand-ins are configured by absolute path
            # (the genuine-CLI check still applies: the program must be named codex or claude, outside the project).
            "providers": {
                "codex": {"command": f"{shim_path(tmp_path / 'bin', 'codex').as_posix()} exec", "capabilities": ["IMPLEMENT", "REVIEW"]},
                "claude": {"command": f"{shim_path(tmp_path / 'bin', 'claude').as_posix()} -p", "capabilities": ["IMPLEMENT", "REVIEW"]},
            },
        }),
        encoding="utf-8",
    )
    config = load_config(rig.project)
    adapters = adapters_from_config(config)
    pool = ProviderPool(adapters, default_pools([a.name for a in adapters], config.stage_routes, config.provider_pools), require_independent=True, log=rig.log)
    rig.coordinator = Coordinator(
        rig.store, rig.project, executor=PooledExecutor(pool), reviewer=Reviewer(require_independent=True, review_pool=pool),
        integrator=Integrator(integration_ref="refs/heads/integration", require_independent_review=True), require_independent_review=True,
    )
    return rig, calls


def failures(rig) -> str:
    rows = rig.store.conn.execute("SELECT payload FROM audit_events WHERE event_type IN ('provider.failure', 'provider.call')").fetchall()
    return "\n".join([*(str(r["payload"])[:700] for r in rows), *getattr(rig, "spawn_errors", [])])


def invoked(calls: Path) -> list[str]:
    return [line for line in calls.read_text(encoding="utf-8").splitlines() if line]


def test_codex_down_never_falls_through_to_grok_or_agy_and_review_blocks(world, monkeypatch):
    rig, calls = world
    monkeypatch.setenv("SHIM_MODE_CODEX", "down")
    rig.tick(6)
    used = invoked(calls)
    assert "claude" in used, rig.text() + failures(rig)  # the authorized fallback implemented
    assert "grok" not in used and "agy" not in used
    assert rig.stage != Stage.DONE  # nobody independent of claude is available: a blocker, not a substitution
    assert set(rig.coordinator.executor.pool.adapters) == {"codex", "claude"}


def test_authorized_pair_completes_a_task_with_independent_review(world):
    rig, calls = world
    rig.tick(8)
    used = invoked(calls)
    assert rig.stage == Stage.DONE, rig.text() + failures(rig)
    assert set(used) <= {"codex", "claude"} and {"codex", "claude"} <= set(used)
    assert rig.store.latest_candidate(TASK)["produced_by"] != "unused"


def test_claude_and_agy_complete_after_restart_with_no_codex_available(world, monkeypatch):
    from stagemesh.domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus
    from stagemesh.persistence import Store
    from stagemesh.provider_authorization import program_name

    rig, calls = world
    bin_dir = calls.parent / "bin"
    codex = shim_path(bin_dir, "codex")
    codex.unlink()
    grok = shim_path(bin_dir, "grok")
    grok.unlink()

    def contains_codex(directory: str) -> bool:
        root = Path(directory.strip().strip('"'))
        extensions = [
            "",
            *(item.lower() for item in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if item),
        ]
        return any((root / f"codex{extension}").is_file() for extension in extensions)

    safe_path = os.pathsep.join(
        directory for directory in os.environ["PATH"].split(os.pathsep) if directory and not contains_codex(directory)
    )
    monkeypatch.setenv("PATH", safe_path)
    assert shutil.which("codex") is None

    settings = rig.project / ".stagemesh" / "agy-test.settings.json"
    settings.write_text("{}\n", encoding="utf-8")
    config_path = rig.project / ".stagemesh" / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "authorization": {"unattended_providers": ["claude", "agy"]},
                "providers": {
                    "claude": {
                        "command": f"{shim_path(bin_dir, 'claude').as_posix()} -p",
                        "capabilities": ["IMPLEMENT", "REVIEW"],
                    },
                    "agy": {
                        "command": f"{shim_path(bin_dir, 'agy').as_posix()} --settings {settings.as_posix()}",
                        "capabilities": ["IMPLEMENT", "REVIEW"],
                    },
                },
                "routing": {
                    "pools": {"IMPLEMENT": ["claude", "agy"], "REVIEW": ["agy", "claude"]},
                    "require_independent_review": True,
                },
            }
        ),
        encoding="utf-8",
    )

    def coordinator(store: Store) -> Coordinator:
        config = load_config(rig.project)
        adapters = adapters_from_config(config)
        assert {adapter.name for adapter in adapters} == {"claude", "agy"}
        pool = ProviderPool(
            adapters,
            default_pools([adapter.name for adapter in adapters], config.stage_routes, config.provider_pools),
            require_independent=True,
            log=rig.log,
        )
        return Coordinator(
            store,
            rig.project,
            executor=PooledExecutor(pool),
            reviewer=Reviewer(require_independent=True, review_pool=pool),
            integrator=Integrator(integration_ref="refs/heads/integration", require_independent_review=True),
            require_independent_review=True,
        )

    rig.coordinator = coordinator(rig.store)
    rig.tick(3)
    candidate = rig.store.latest_candidate(TASK)
    assert candidate is not None and candidate["produced_by"] == "claude"
    candidate_sha = candidate["sha"]
    assert rig.stage == Stage.REVIEW
    calls_before_restart = invoked(calls)
    assert calls_before_restart == ["claude"]
    assert rig.store.has_evidence(TASK, candidate_sha, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)
    recovery_task = rig.store.upsert_task("completed recovery sentinel")
    rig.store.advance_task(recovery_task, Stage.DONE)
    interrupted_review = rig.store.start_execution(
        task_id=recovery_task,
        claim_id=None,
        kind=ExecutionKind.REVIEW,
        pid=2_147_483_647,
        process_create_time=1.0,
        boot_id="synthetic-dead-process",
        executable=str(shim_path(bin_dir, "agy")),
        candidate_sha=None,
        actor="agy",
    )

    database = rig.project / ".stagemesh" / "stagemesh.sqlite3"
    rig.store.close()
    rig.store = Store(database)
    rig.store.migrate()
    rig.coordinator = coordinator(rig.store)
    rig.tick(4)

    assert rig.stage == Stage.DONE, rig.text() + failures(rig)
    assert rig.store.latest_candidate(TASK)["sha"] == candidate_sha
    assert invoked(calls).count("claude") == calls_before_restart.count("claude") == 1
    assert invoked(calls).count("agy") == calls_before_restart.count("agy") + 1
    recovered = rig.store.conn.execute(
        "SELECT status, result FROM executions WHERE id=?", (interrupted_review,)
    ).fetchone()
    assert recovered["status"] == ExecutionStatus.FAILED
    assert recovered["result"] == "orphaned"
    recovery_events = [
        json.loads(row["payload"])
        for row in rig.store.conn.execute(
            "SELECT payload FROM audit_events WHERE event_type='recovery.orphan_execution_failed' ORDER BY rowid"
        )
    ]
    recovery_event = next(item for item in recovery_events if item["execution_id"] == interrupted_review)
    assert recovery_event["execution_kind"] == ExecutionKind.REVIEW
    assert recovery_event["reason"] == "TASK_ALREADY_DONE"

    def launched_program(argv: object) -> str:
        token = argv[0] if isinstance(argv, (list, tuple)) and argv else str(argv).split(maxsplit=1)[0]
        return program_name(str(token))

    launched_programs = {launched_program(argv) for argv in rig.spawned_argv if argv}
    assert {"claude", "agy"} <= launched_programs
    assert "codex" not in launched_programs
    for kind in (EvidenceKind.VALIDATION, EvidenceKind.REVIEW, EvidenceKind.INTEGRATION):
        assert rig.store.has_evidence(TASK, candidate_sha, kind, EvidenceStatus.PASSED)
    review_row = rig.store.conn.execute(
        "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? AND status=? "
        "ORDER BY rowid DESC LIMIT 1",
        (TASK, candidate_sha, EvidenceKind.REVIEW, EvidenceStatus.PASSED),
    ).fetchone()
    review = json.loads(review_row["payload"])
    assert review["implementer_provider"] == "claude"
    assert review["review_provider"] == "agy"
    assert review["independent_reviewer"] is True
    assert rig.git.run("merge-base", "--is-ancestor", candidate_sha, "refs/heads/integration", check=False).returncode == 0


def test_stand_in_cli_is_runnable_on_this_platform(world):
    """Diagnostic for the harness itself: the stand-in must start and answer through subprocess exactly as StageMesh starts it."""
    import shutil
    import subprocess

    rig, calls = world
    found = shutil.which("claude")
    assert found, "stand-in claude is not on PATH"
    try:
        done = subprocess.run([found, "-p", "--flag", "a b"], input="Review candidate", capture_output=True, text=True, cwd=rig.project, timeout=60, check=False)
    except Exception as exc:  # the point is to see exactly what fails
        raise AssertionError(f"{found}: {type(exc).__name__}: {exc}") from exc
    assert done.returncode == 0 and "PASS" in done.stdout, f"{found}: rc={done.returncode} out={done.stdout!r} err={done.stderr!r}"
    assert invoked(calls) == ["claude"]


@pytest.mark.parametrize("capability", ["code", "review"])
def test_authorization_revoked_during_workspace_preparation_prevents_spawn(world, monkeypatch, capability):
    from stagemesh import providers

    rig, calls = world
    adapter = rig.coordinator.executor.pool.adapters["codex"]
    original = providers._command_and_input

    def revoke_before_spawn(*args, **kwargs):
        command = original(*args, **kwargs)
        config_path = rig.project / ".stagemesh" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["authorization"]["unattended_providers"] = ["claude"]
        config_path.write_text(json.dumps(config), encoding="utf-8")
        return command

    monkeypatch.setattr(providers, "_command_and_input", revoke_before_spawn)
    if capability == "code":
        result = adapter.execute(rig.store, TASK, None, rig.project)
        assert "provider_not_authorized" in str(result.failure_reason)
    else:
        result = adapter.review_candidate("Review candidate", rig.project, rig.base)
        assert "provider_not_authorized" in result
    assert invoked(calls) == []
