"""End to end through the real coordinator, pool and adapters with stand-in CLIs named codex, claude, grok and agy on PATH.

The stand-ins record every invocation. Whatever the failure pattern, `grok` and `agy` must never be started, and review must stay
independent (an authorized-but-unavailable reviewer is a blocker, not a reason to substitute).
"""
from __future__ import annotations

import json
import os
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
prompt = sys.stdin.read()
mode = os.environ.get(mode_var, "ok")
if mode == "down":
    sys.stderr.write("authentication_error: not logged in"); sys.exit(1)
if "Review candidate" in prompt:
    print('{"decision":"PASS","findings":[]}'); sys.exit(0)
pathlib.Path("docs/a.md").write_text("by " + name + "\n")
'''


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
    (rig.project / ".stagemesh" / "config.json").write_text(
        json.dumps({"authorization": {"unattended_providers": ["codex", "claude"]}}), encoding="utf-8"
    )
    config = load_config(rig.project)
    adapters = adapters_from_config(config)
    pool = ProviderPool(adapters, default_pools([a.name for a in adapters], config.stage_routes, config.provider_pools), require_independent=True, log=rig.log)
    rig.coordinator = Coordinator(
        rig.store, rig.project, executor=PooledExecutor(pool), reviewer=Reviewer(require_independent=True, review_pool=pool),
        integrator=Integrator(integration_ref="refs/heads/integration", require_independent_review=True), require_independent_review=True,
    )
    return rig, calls


def invoked(calls: Path) -> list[str]:
    return [line for line in calls.read_text(encoding="utf-8").splitlines() if line]


def test_codex_down_never_falls_through_to_grok_or_agy_and_review_blocks(world, monkeypatch):
    rig, calls = world
    monkeypatch.setenv("SHIM_MODE_CODEX", "down")
    rig.tick(6)
    used = invoked(calls)
    assert "claude" in used, rig.text()  # the authorized fallback implemented
    assert "grok" not in used and "agy" not in used
    assert rig.stage != Stage.DONE  # nobody independent of claude is available: a blocker, not a substitution
    assert "not_authorized" in rig.text()


def test_authorized_pair_completes_a_task_with_independent_review(world):
    rig, calls = world
    rig.tick(8)
    used = invoked(calls)
    assert rig.stage == Stage.DONE, rig.text()
    assert set(used) <= {"codex", "claude"} and {"codex", "claude"} <= set(used)
    assert rig.store.latest_candidate(TASK)["produced_by"] != "unused"
