from __future__ import annotations

import json
import sys
from pathlib import Path

from stagemesh.coordinator import Coordinator
from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage, TaskStatus
from stagemesh.execution import SubprocessExecutor
from stagemesh.git import GitWorkspace
from stagemesh.persistence import Store

TASK = "TASK-1"


def _project(tmp_path: Path, contract: dict[str, object] | None = None) -> tuple[Path, GitWorkspace]:
    project = tmp_path / "repo"
    project.mkdir()
    workspace = GitWorkspace(project)
    workspace.init_if_needed()
    workspace.run("config", "user.email", "test@example.invalid")
    workspace.run("config", "user.name", "StageMesh Test")
    (project / "src").mkdir()
    (project / "docs").mkdir()
    (project / "src" / "forbidden.py").write_text("X = 1\n", encoding="utf-8")
    (project / "docs" / "a.md").write_text("a\n", encoding="utf-8")
    workspace.commit_all("base")
    if contract is not None:
        (project / ".stagemesh" / "contracts").mkdir(parents=True)
        (project / ".stagemesh" / "contracts" / f"{TASK}.json").write_text(json.dumps(contract), encoding="utf-8")
    return project, workspace


def _coordinator(tmp_path: Path, project: Path, script: str) -> tuple[Store, Coordinator]:
    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    store.upsert_task("integrity", source_id=TASK)
    store.advance_task(TASK, Stage.IMPLEMENT)
    path = tmp_path / "provider.py"
    path.write_text(script, encoding="utf-8")
    return store, Coordinator(store, project, executor=SubprocessExecutor([sys.executable, str(path)], name="codex"))


def test_noop_provider_creates_no_candidate_and_stays_in_implement(tmp_path: Path) -> None:
    project, _ = _project(tmp_path)
    store, coordinator = _coordinator(tmp_path, project, "pass\n")

    assert coordinator.tick() == 0

    task = store.get_task(TASK)
    assert task["stage"] == Stage.IMPLEMENT
    assert task["status"] == TaskStatus.OPEN
    assert store.latest_candidate(TASK) is None
    event = store.audit_events()[0]
    assert event["event_type"] == "task.implementation_unsuccessful"
    assert json.loads(event["payload"])["reason"] == "no_implementation_change"


def test_genuine_implementation_creates_candidate(tmp_path: Path) -> None:
    project, workspace = _project(tmp_path)
    store, coordinator = _coordinator(
        tmp_path, project, "from pathlib import Path\nPath('docs/a.md').write_text('changed')\n"
    )

    assert coordinator.tick() == 1

    candidate = store.latest_candidate(TASK)
    assert candidate is not None and candidate["durable_handoff"] == 1
    assert candidate["sha"] != workspace.head()
    assert store.get_task(TASK)["stage"] == Stage.VALIDATE
    assert store.task_baseline(TASK) == workspace.head()


_REMEDIATION_PROVIDER = """
from pathlib import Path
marker = Path('../attempt.marker')
first = not marker.exists()
marker.write_text('x')
if first:
    Path('src/forbidden.py').write_text('X = 2')
else:
    Path('docs/a.md').write_text('allowed edit')
"""


def test_remediation_cannot_hide_forbidden_change_and_baseline_is_immutable(tmp_path: Path) -> None:
    project, workspace = _project(
        tmp_path, {"objective": "docs only", "allowed_files": ["docs/**"], "forbidden_files": ["src/forbidden.py"]}
    )
    base = workspace.head()
    store, coordinator = _coordinator(tmp_path, project, _REMEDIATION_PROVIDER)

    for _ in range(4):  # implement, validate-fail, remediate, implement/validate
        coordinator.tick()

    shas = [row["sha"] for row in store.conn.execute("SELECT sha FROM candidates WHERE task_id=? ORDER BY created_at", (TASK,))]
    assert len(shas) == 2
    bindings = store.conn.execute("SELECT candidate_sha, baseline_sha FROM contract_bindings WHERE task_id=?", (TASK,)).fetchall()
    assert {row["candidate_sha"] for row in bindings} == set(shas)
    assert {row["baseline_sha"] for row in bindings} == {base}
    for sha in shas:
        assert store.has_evidence(TASK, sha, EvidenceKind.VALIDATION, EvidenceStatus.FAILED)
    second = json.loads(
        store.conn.execute(
            "SELECT payload FROM evidence WHERE candidate_sha=? AND kind=?", (shas[1], EvidenceKind.VALIDATION)
        ).fetchone()["payload"]
    )
    assert second["baseline_sha"] == base
    assert "src/forbidden.py" in second["changed_files"]
    assert store.get_task(TASK)["stage"] != Stage.DONE

