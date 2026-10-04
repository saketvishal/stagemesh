"""regenerate-contracts: migrate auto-generated broad contracts to narrow ones after the scope map changes, safely."""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

import stagemesh.auto_plan as auto_plan_module
import stagemesh.cli as cli_module
from stagemesh.auto_plan import create_contract
from stagemesh.config import load_config
from stagemesh.contract_binding import bind_task_contract
from stagemesh.domain import EvidenceKind, EvidenceStatus, ExecutionKind, Stage
from stagemesh.git import GitWorkspace
from stagemesh.regenerate import recommendation_for, regenerate_contracts

from test_parallel import Rig, ScriptedExecutor
from test_queue_run import outcomes, queue

GATE = {"name": "project-acceptance-fake", "command": [sys.executable, "-c", "pass"], "timeout_seconds": 60}
AREAS = {
    "schema_version": 1,
    "areas": [
        {"id": "alpha", "labels": ["alpha"], "allowed_files": ["out/A.txt"]},
        {"id": "beta", "labels": ["beta"], "allowed_files": ["out/B.txt"]},
    ],
}


@pytest.fixture(autouse=True)
def fake_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auto_plan_module, "detect_gates", lambda project: [dict(GATE)])


def contract_path(rig: Rig, task_id: str) -> Path:
    return rig.project / ".stagemesh" / "contracts" / f"{task_id}.json"


def read(rig: Rig, task_id: str) -> dict:
    return json.loads(contract_path(rig, task_id).read_text(encoding="utf-8"))


def broad_generated_rig(tmp_path: Path) -> Rig:
    """A is labelled alpha with an auto-generated BROAD contract (made before any scope map existed); B has a hand-written contract."""
    rig = Rig(tmp_path, ["A", "B"], labels={"A": ["alpha"], "B": ["beta"]})
    contract_path(rig, "A").unlink()
    (rig.project / ".stagemesh" / "config.json").write_text(json.dumps({"auto_plan": {"broad_scope": "allow"}}), encoding="utf-8")
    create_contract(rig.store, rig.project, "A")
    assert read(rig, "A")["allowed_files"] == ["**"]
    (rig.project / "stagemesh.scope.json").write_text(json.dumps(AREAS), encoding="utf-8")  # the scope map arrives later (and is committed)
    git = GitWorkspace(rig.project)
    git.run("add", "stagemesh.scope.json")
    git.run("commit", "-q", "-m", "add scope map")
    return rig


def cli(rig: Rig, *argv: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = cli_module.main(["--project", str(rig.project), "regenerate-contracts", *argv])
    return code, out.getvalue()


def audit(rig: Rig, event: str) -> list[dict]:
    return [json.loads(r["payload"]) for r in rig.store.conn.execute("SELECT payload FROM audit_events WHERE event_type=? ORDER BY created_at, rowid", (event,))]


def test_broad_generated_contract_becomes_narrow_and_keeps_its_gates(tmp_path: Path) -> None:
    rig = broad_generated_rig(tmp_path)
    old_gates = read(rig, "A")["required_tests"]
    results = {r["task_id"]: r for r in regenerate_contracts(rig.store, rig.project)}
    a = results["A"]
    assert a["status"] == "regenerated" and a["old_scope"]["mode"] == "broad" and a["new_scope"]["mode"] == "narrow"
    assert a["old_scope"]["allowed_files"] == ["**"] and a["new_scope"]["allowed_files"] == ["out/A.txt"]
    assert a["old_digest"] and a["new_digest"] and a["old_digest"] != a["new_digest"]
    contract = read(rig, "A")
    assert contract["allowed_files"] == ["out/A.txt"] and contract["required_tests"] == old_gates and contract["scope"]["areas"] == ["alpha"]
    (event,) = audit(rig, "contract.regenerated")
    assert event["task_id"] == "A" and event["old_digest"] == a["old_digest"] and event["new_digest"] == a["new_digest"]
    assert event["old_scope"]["allowed_files"] == ["**"] and event["new_scope"]["allowed_files"] == ["out/A.txt"]
    assert event["operator_action"] == "REGENERATE_CONTRACT" and event["operator"]
    # idempotent: a second pass has nothing to do
    assert {r["task_id"]: r["status"] for r in regenerate_contracts(rig.store, rig.project)}["A"] == "unchanged"


def test_hand_written_contracts_are_never_touched(tmp_path: Path) -> None:
    rig = broad_generated_rig(tmp_path)
    before = contract_path(rig, "B").read_bytes()
    results = {r["task_id"]: r for r in regenerate_contracts(rig.store, rig.project)}  # B's label matches the scope map too
    assert results["B"]["status"] == "skipped" and "hand-written" in results["B"]["reason"]
    assert contract_path(rig, "B").read_bytes() == before
    forced = regenerate_contracts(rig.store, rig.project, "B", force=True)[0]
    assert forced["status"] == "skipped" and contract_path(rig, "B").read_bytes() == before
    assert all(e["task_id"] != "B" for e in audit(rig, "contract.regenerated"))


def test_a_started_task_is_rebound_and_history_is_kept(tmp_path: Path) -> None:
    rig = broad_generated_rig(tmp_path)
    base = rig.store.set_task_baseline("A", GitWorkspace(rig.project).head())
    bind_task_contract(rig.store, rig.project, "A", base)
    old_digest = rig.store.task_contract("A")["digest"]
    rig.store.add_candidate("A", "c" * 40, "p", durable_handoff=True)
    rig.store.add_evidence("A", "c" * 40, EvidenceKind.VALIDATION, EvidenceStatus.FAILED, {"contract_hash": old_digest, "findings": []})
    before_rows = {t: rig.store.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("candidates", "evidence", "audit_events")}

    (result,) = regenerate_contracts(rig.store, rig.project, "A")

    assert result["status"] == "regenerated" and result["rebound"] is True
    frozen = rig.store.task_contract("A")
    assert frozen["digest"] == result["new_digest"] != old_digest and frozen["version"] == 1 and "out/A.txt" in frozen["canonical_json"]
    assert len(audit(rig, "task.contract_rebound")) == 1 and audit(rig, "contract.regenerated")[0]["rebound"] is True
    after_rows = {t: rig.store.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("candidates", "evidence", "audit_events")}
    assert after_rows["candidates"] == before_rows["candidates"] and after_rows["evidence"] == before_rows["evidence"]
    assert after_rows["audit_events"] > before_rows["audit_events"]  # only ever added to


def test_validate_flag_is_reported_for_a_started_task(tmp_path: Path) -> None:
    rig = broad_generated_rig(tmp_path)
    base = rig.store.set_task_baseline("A", GitWorkspace(rig.project).head())
    bind_task_contract(rig.store, rig.project, "A", base)
    (result,) = regenerate_contracts(rig.store, rig.project, "A", validate=True)
    assert result["status"] == "regenerated" and result["validation"] == {"ran": False, "reason": "task has no candidate"}


def test_active_claim_running_execution_and_done_tasks_are_refused_and_left_alone(tmp_path: Path) -> None:
    rig = broad_generated_rig(tmp_path)
    original = contract_path(rig, "A").read_bytes()

    claim = rig.store.acquire_claim("A", "worker-1")
    (refused,) = regenerate_contracts(rig.store, rig.project, "A")
    assert refused["status"] == "refused" and refused["reason"].startswith("active_claim")
    rig.store.release_claim(claim)

    rig.store.start_execution(task_id="A", claim_id=None, kind=ExecutionKind.VALIDATION)
    (running,) = regenerate_contracts(rig.store, rig.project, "A")
    assert running["status"] == "refused" and running["reason"].startswith("running_execution")
    assert contract_path(rig, "A").read_bytes() == original and not audit(rig, "contract.regenerated")

    rig.store.conn.execute("UPDATE executions SET status='FAILED'")
    rig.store.conn.commit()
    rig.store.advance_task("A", Stage.DONE)
    (done,) = regenerate_contracts(rig.store, rig.project, "A")
    assert done["status"] == "refused" and done["reason"].startswith("task_done")
    assert "A" not in {r["task_id"] for r in regenerate_contracts(rig.store, rig.project)}  # a bulk run skips done tasks entirely
    assert contract_path(rig, "A").read_bytes() == original
    assert regenerate_contracts(rig.store, rig.project, "A", dry_run=True)[0]["status"] == "refused"  # even a dry run reports why


def test_passed_evidence_blocks_regeneration_and_the_file_is_restored(tmp_path: Path) -> None:
    rig = broad_generated_rig(tmp_path)
    base = rig.store.set_task_baseline("A", GitWorkspace(rig.project).head())
    bind_task_contract(rig.store, rig.project, "A", base)
    rig.store.add_candidate("A", "d" * 40, "p", durable_handoff=True)
    rig.store.add_evidence("A", "d" * 40, EvidenceKind.VALIDATION, EvidenceStatus.PASSED, {"contract_hash": rig.store.task_contract("A")["digest"]})
    original = contract_path(rig, "A").read_bytes()
    (result,) = regenerate_contracts(rig.store, rig.project, "A")
    assert result["status"] == "refused" and "history_invalidated" in result["reason"]
    assert contract_path(rig, "A").read_bytes() == original and not audit(rig, "contract.regenerated")
    (forced,) = regenerate_contracts(rig.store, rig.project, "A", force=True)
    assert forced["status"] == "regenerated" and read(rig, "A")["allowed_files"] == ["out/A.txt"]


def test_dry_run_shows_old_and_new_scope_and_digests_and_changes_nothing(tmp_path: Path) -> None:
    rig = broad_generated_rig(tmp_path)
    original = contract_path(rig, "A").read_bytes()
    events_before = rig.store.conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    code, out = cli(rig, "--dry-run", "--json")
    data = json.loads(out)
    assert code == 0 and data["dry_run"] is True
    a = next(r for r in data["results"] if r["task_id"] == "A")
    assert a["status"] == "would_regenerate" and a["old_scope"]["allowed_files"] == ["**"] and a["new_scope"]["allowed_files"] == ["out/A.txt"]
    assert a["old_digest"] and a["new_digest"] and a["old_digest"] != a["new_digest"]
    assert contract_path(rig, "A").read_bytes() == original
    assert rig.store.conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == events_before
    code, text = cli(rig, "--dry-run", "--task", "A")
    assert code == 0 and "WOULD REGENERATE" in text and "old scope: **" in text and "new scope: out/A.txt" in text and "nothing was changed" in text


def test_cli_regenerates_and_reports_refusals_with_a_nonzero_exit(tmp_path: Path) -> None:
    rig = broad_generated_rig(tmp_path)
    code, out = cli(rig, "--json")
    assert code == 0 and next(r for r in json.loads(out)["results"] if r["task_id"] == "A")["status"] == "regenerated"
    assert read(rig, "A")["allowed_files"] == ["out/A.txt"]
    assert cli(rig, "--task", "nope")[0] == 2

    rig2 = broad_generated_rig(tmp_path / "second")
    rig2.store.acquire_claim("A", "worker-1")
    code, text = cli(rig2, "--task", "A")
    assert code == 2 and "refused" in text


def test_unbounded_tasks_keep_their_contract_and_never_get_wider(tmp_path: Path) -> None:
    rig = broad_generated_rig(tmp_path)
    (rig.project / "stagemesh.scope.json").write_text(json.dumps({"schema_version": 1, "areas": [AREAS["areas"][1]]}), encoding="utf-8")  # alpha gone
    before = contract_path(rig, "A").read_bytes()
    (result,) = regenerate_contracts(rig.store, rig.project, "A")
    assert result["status"] == "skipped" and "existing contract is kept" in result["reason"]
    assert contract_path(rig, "A").read_bytes() == before


# --- queue-run recommends it ----------------------------------------------------------------------------------------------------------------------


def test_queue_run_recommends_regeneration_for_a_broad_generated_contract(tmp_path: Path) -> None:
    rig = broad_generated_rig(tmp_path)
    assert recommendation_for(rig.store, rig.project, "A")["command"] == "stagemesh regenerate-contracts --task A"
    assert recommendation_for(rig.store, rig.project, "B") is None  # hand-written
    said: list[tuple[str, str]] = []
    summary = queue(rig, ScriptedExecutor(rig.files), emit=lambda t, text: said.append((t, text))).run()
    assert outcomes(summary) == {"A": "DONE", "B": "DONE"}  # advice only: the broad contract still runs
    assert [r["task_id"] for r in summary.recommendations] == ["A"]
    assert summary.to_dict()["recommendations"][0]["new_scope"] == ["out/A.txt"]
    lines = [text for task, text in said if task == "A" and "regenerate-contracts" in text]
    assert len(lines) == 1 and "broad" in lines[0] and "stagemesh regenerate-contracts --task A" in lines[0]


def test_queue_run_is_quiet_once_the_contract_has_been_regenerated(tmp_path: Path) -> None:
    rig = broad_generated_rig(tmp_path)
    regenerate_contracts(rig.store, rig.project)
    summary = queue(rig, ScriptedExecutor(rig.files)).run()
    assert outcomes(summary) == {"A": "DONE", "B": "DONE"} and summary.recommendations == []
