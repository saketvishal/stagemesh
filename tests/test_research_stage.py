"""Opt-in read-only research stage: accepted and rejected cases."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from stagemesh.config import ConfigValidationError, load_config
from stagemesh.contracts import BoundChangeContract, ChangeContract, canonical_contract_json
from stagemesh.persistence import Store
from stagemesh.providers import ResearchRole, RuntimeCommandAdapter, _build_task_prompt, adapters_from_config
from stagemesh.research_context import ResearchBlocked

RESEARCHER = r'''
import json, re, sys
mode = sys.argv[1]
prompt = sys.stdin.read()
sha = re.search(r"baseline_sha=([0-9a-f]+)", prompt).group(1)
digest = re.search(r"contract_digest=([0-9a-f]+)", prompt).group(1)
report = {"baseline_sha": sha, "contract_digest": digest, "inspected_paths": ["src/service.py"],
          "findings": [{"path": "src/service.py", "summary": "entry point"}],
          "unresolved_scope_gaps": [], "provenance": ["test researcher"]}
if mode == "gap":
    report["unresolved_scope_gaps"] = ["needs src/other.py"]
elif mode == "stale":
    report["baseline_sha"] = "b" * 40
elif mode == "badpath":
    report["inspected_paths"] = ["../etc/passwd"]
elif mode == "mutate":
    open("src/service.py", "a").write("# changed\n")
elif mode == "untracked":
    open("new.txt", "w").write("x")
elif mode == "fail":
    sys.exit(3)
elif mode == "garbage":
    print("not json"); sys.exit(0)
print(json.dumps(report))
'''


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def rig(tmp_path: Path):
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "service.py").write_text("x = 1\n", encoding="utf-8")
    git(project, "init", "-q")
    git(project, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    git(project, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    script = tmp_path / "researcher.py"
    script.write_text(RESEARCHER, encoding="utf-8")
    contract = ChangeContract(objective="Change service", allowed_files=("src/service.py",))
    canonical = canonical_contract_json(contract)
    bound = BoundChangeContract(
        contract, 1, hashlib.sha256(canonical.encode()).hexdigest(), canonical, git(project, "rev-parse", "HEAD"),
    )
    store = Store(tmp_path / "s.sqlite3")
    store.migrate()

    def adapter(mode: str) -> RuntimeCommandAdapter:
        return RuntimeCommandAdapter(
            "impl", ("true",), research=ResearchRole("researcher", (sys.executable, str(script), mode)), timeout_seconds=30,
        )

    return project, bound, store, adapter


def events(store: Store) -> list[dict]:
    return [json.loads(r["payload"]) for r in store.audit_events() if r["event_type"] == "research_stage"]


def test_accepted_research_feeds_bounded_findings_and_audits(rig) -> None:
    project, bound, store, adapter = rig
    text = adapter("ok")._run_research(store, "t1", project, bound)
    assert "src/service.py: entry point" in text
    assert events(store)[0]["outcome"] == "accepted"
    assert events(store)[0]["finding_count"] == 1
    assert git(project, "status", "--porcelain") == ""


def test_findings_appear_in_implementation_prompt_only_when_supplied(rig) -> None:
    project, bound, _store, _adapter = rig
    task = {"title": "Change service"}
    base = _build_task_prompt("t1", task, project, contract=bound.contract)
    assert "Research findings" not in base
    with_research = _build_task_prompt("t1", task, project, contract=bound.contract, research_findings="Research findings: x\n\n")
    assert "Research findings: x" in with_research


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("gap", "research_unresolved_scope"),
        ("stale", "research_stale"),
        ("badpath", "research_invalid"),
        ("garbage", "research_invalid"),
        ("fail", "research_failed"),
        ("mutate", "research_mutated_source"),
        ("untracked", "research_mutated_source"),
    ],
)
def test_rejected_research_blocks_implementation(rig, mode: str, reason: str) -> None:
    project, bound, store, adapter = rig
    with pytest.raises(ResearchBlocked) as caught:
        adapter(mode)._run_research(store, "t1", project, bound)
    assert caught.value.reason == reason


def test_later_attempt_head_still_uses_the_frozen_research_identity(rig) -> None:
    project, bound, store, adapter = rig
    first = adapter("ok")._run_research(store, "t1", project, bound)
    git(project, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-qm", "later")
    second = adapter("ok")._run_research(store, "t1", project, bound)
    assert "src/service.py: entry point" in first
    assert "src/service.py: entry point" in second
    assert len(events(store)) == 2
    assert all(event["baseline_sha"] == bound.baseline_sha for event in events(store))
    assert all(event["contract_digest"] == bound.digest for event in events(store))


def test_research_is_opt_in_in_config(tmp_path: Path) -> None:
    (tmp_path / ".stagemesh").mkdir()
    config_path = tmp_path / ".stagemesh" / "config.json"
    config_path.write_text(json.dumps({"providers": {"claude": "claude -p"}}), encoding="utf-8")
    assert load_config(tmp_path).research is None
    assert all(a.research is None for a in adapters_from_config(load_config(tmp_path)))
    config_path.write_text(
        json.dumps({"providers": {"claude": "claude -p"}, "research": {"provider": "claude", "command": "claude -p --read-only"}}),
        encoding="utf-8",
    )
    adapters = adapters_from_config(load_config(tmp_path))
    assert all(a.research is not None and a.research.command == ("claude", "-p", "--read-only") for a in adapters)
    config_path.write_text(json.dumps({"research": {"provider": "nobody", "command": "x"}}), encoding="utf-8")
    with pytest.raises(ConfigValidationError):
        load_config(tmp_path)
