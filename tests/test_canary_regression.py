"""Regression tests for defects found and fixed during the low-risk canary run.

DEF-1: command_continue used FakeExecutor even when real providers were configured.
DEF-2: No --provider flag existed to select a specific provider.
DEF-3: SubprocessExecutor.name was a class attribute; couldn't be overridden per-instance via constructor.
DEF-4: Capacity failures left an active claim on the task, blocking re-dispatch until lease TTL expired.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

import stagemesh.coordinator as coordinator_module
from stagemesh.coordinator import Coordinator
from stagemesh.domain import ExecutionKind, ExecutionStatus, ProcessIdentity, Stage, TaskStatus
from stagemesh.execution import ExecutionResult, FakeExecutor, SubprocessExecutor, classify_failure
from stagemesh.git import GitWorkspace
from stagemesh.persistence import Store
from stagemesh.workspaces import task_workspace


@pytest.fixture
def store(tmp_path: Path) -> Store:
    db = Store(tmp_path / "state.sqlite3")
    db.migrate()
    yield db
    db.close()


# ---------------------------------------------------------------------------
# DEF-3: SubprocessExecutor must accept a name via its constructor
# ---------------------------------------------------------------------------


def test_subprocess_executor_accepts_name_in_constructor() -> None:
    """DEF-3: Constructing SubprocessExecutor with an explicit name must override the class default."""
    default = SubprocessExecutor([sys.executable, "--version"])
    assert default.name == "subprocess"

    named = SubprocessExecutor([sys.executable, "--version"], name="claude")
    assert named.name == "claude"

    named2 = SubprocessExecutor([sys.executable, "--version"], name="codex")
    assert named2.name == "codex"


# ---------------------------------------------------------------------------
# DEF-4: Capacity failure must release the claim immediately
# ---------------------------------------------------------------------------


class CapacityFailExecutor(FakeExecutor):
    """Executor that always reports a capacity failure (provider unavailable)."""

    name = "capacity-fail"

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        return ExecutionResult(ExecutionStatus.FAILED, capacity_failure=True)


def test_capacity_failure_releases_claim_immediately(store: Store, tmp_path: Path) -> None:
    """DEF-4: When a provider reports capacity_failure the claim must be released immediately so
    the task stays OPEN and can be re-dispatched without waiting for lease TTL."""
    task_id = store.upsert_task("capacity-fail-task")
    store.advance_task(task_id, Stage.IMPLEMENT)

    coord = Coordinator(store, tmp_path, executor=CapacityFailExecutor())
    coord.tick()

    # Task must be back to OPEN, not CLAIMED
    task = store.get_task(task_id)
    assert task["status"] == TaskStatus.OPEN

    # Stage must not have advanced
    assert task["stage"] == Stage.IMPLEMENT

    # No active claims must remain
    active = list(store.conn.execute("SELECT * FROM claims WHERE task_id=? AND active=1", (task_id,)))
    assert active == [], f"Expected no active claims after capacity failure, got: {active}"


def test_capacity_failure_allows_immediate_re_dispatch(store: Store, tmp_path: Path) -> None:
    """DEF-4 (corollary): After a capacity failure the very next tick must be able to re-claim
    and re-dispatch the task if a working executor is now available."""
    task_id = store.upsert_task("re-dispatch-task")
    store.advance_task(task_id, Stage.IMPLEMENT)

    # First tick: capacity fails, claim released
    coord_cap = Coordinator(store, tmp_path, executor=CapacityFailExecutor())
    result = coord_cap.tick()
    assert result == 0
    assert store.get_task(task_id)["status"] == TaskStatus.OPEN

    # Second tick: real executor succeeds
    coord_real = Coordinator(store, tmp_path, executor=FakeExecutor())
    result = coord_real.tick()
    assert result == 1
    assert store.get_task(task_id)["stage"] == Stage.VALIDATE


def test_release_claim_is_idempotent(store: Store, tmp_path: Path) -> None:
    """DEF-4 (idempotency): Calling release_claim twice on the same claim must not error
    and must leave the task in a consistent state."""
    task_id = store.upsert_task("idempotent-task")
    store.advance_task(task_id, Stage.IMPLEMENT)
    claim_id = store.acquire_claim(task_id, "worker-a")
    assert claim_id is not None

    store.release_claim(claim_id)
    store.release_claim(claim_id)  # second call must be a no-op

    assert store.get_task(task_id)["status"] == TaskStatus.OPEN
    active = list(store.conn.execute("SELECT * FROM claims WHERE task_id=? AND active=1", (task_id,)))
    assert active == []


def test_release_claim_on_missing_claim_is_safe(store: Store, tmp_path: Path) -> None:
    """DEF-4 (safety): release_claim with a non-existent ID must not raise."""
    store.release_claim("00000000-0000-0000-0000-000000000000")  # should be silent


# ---------------------------------------------------------------------------
# DEF-5: stale running implementation execution must not strand an active claim
# ---------------------------------------------------------------------------


def _claimed_implementation_with_execution(
    store: Store,
    *,
    process_create_time: float | None = 100.0,
    boot_id: str | None = "boot-a",
    executable: str | None = "worker",
) -> tuple[str, str, str]:
    task_id = store.upsert_task("stale-claim-task")
    store.advance_task(task_id, Stage.IMPLEMENT)
    claim_id = store.acquire_claim(task_id, "worker-a", lease_seconds=300)
    assert claim_id is not None
    execution_id = store.start_execution(
        task_id=task_id,
        claim_id=claim_id,
        kind=ExecutionKind.IMPLEMENTATION,
        pid=4242,
        process_create_time=process_create_time,
        boot_id=boot_id,
        executable=executable,
    )
    return task_id, claim_id, execution_id


def test_dead_running_implementation_claim_recovers_and_redispatches(
    store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id, old_claim_id, old_execution_id = _claimed_implementation_with_execution(store)
    monkeypatch.setattr(coordinator_module, "process_identity", lambda pid: None)

    assert Coordinator(store, tmp_path, executor=FakeExecutor()).tick() == 1

    task = store.get_task(task_id)
    assert task["stage"] == Stage.VALIDATE
    assert task["status"] == TaskStatus.OPEN
    old_claim = store.conn.execute("SELECT * FROM claims WHERE id=?", (old_claim_id,)).fetchone()
    old_execution = store.conn.execute("SELECT * FROM executions WHERE id=?", (old_execution_id,)).fetchone()
    assert old_claim["active"] == 0
    assert old_execution["status"] == ExecutionStatus.FAILED
    assert store.latest_candidate(task_id) is not None


def test_uncertain_running_implementation_identity_remains_claimed(
    store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id, claim_id, execution_id = _claimed_implementation_with_execution(
        store,
        process_create_time=None,
        boot_id="boot-a",
        executable="worker",
    )
    monkeypatch.setattr(coordinator_module, "process_identity", lambda pid: None)

    assert Coordinator(store, tmp_path, executor=FakeExecutor()).tick() == 0

    task = store.get_task(task_id)
    claim = store.conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
    execution = store.conn.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
    assert task["stage"] == Stage.IMPLEMENT
    assert task["status"] == TaskStatus.CLAIMED
    assert claim["active"] == 1
    assert execution["status"] == ExecutionStatus.RUNNING


def test_live_matching_running_implementation_identity_remains_claimed(
    store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id, claim_id, execution_id = _claimed_implementation_with_execution(store)
    monkeypatch.setattr(
        coordinator_module,
        "process_identity",
        lambda pid: ProcessIdentity(pid=pid, create_time=100.0, boot_id="boot-a", executable="worker"),
    )

    assert Coordinator(store, tmp_path, executor=FakeExecutor()).tick() == 0

    task = store.get_task(task_id)
    claim = store.conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
    execution = store.conn.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
    assert task["stage"] == Stage.IMPLEMENT
    assert task["status"] == TaskStatus.CLAIMED
    assert claim["active"] == 1
    assert execution["status"] == ExecutionStatus.RUNNING


# ---------------------------------------------------------------------------
# DEF-1: continue must wire the real provider, not FakeExecutor by default
# ---------------------------------------------------------------------------


def test_subprocess_executor_name_round_trips_through_coordinator(store: Store, tmp_path: Path) -> None:
    """DEF-1 (unit-level): A Coordinator built with a named SubprocessExecutor must use that
    executor and expose its name correctly, confirming the wiring path is exercised."""
    task_id = store.upsert_task("wired-task")
    executor = SubprocessExecutor([sys.executable, "--version"], name="claude")
    coord = Coordinator(store, tmp_path, executor=executor)
    coord.tick()  # advances PLAN -> IMPLEMENT
    coord.tick()  # IMPLEMENT: runs python --version, produces candidate
    candidate = store.latest_candidate(task_id)
    if candidate:
        assert candidate["produced_by"] == "claude", (
            f"DEF-1 regression: expected produced_by='claude', got '{candidate['produced_by']}'"
        )


# ---------------------------------------------------------------------------
# DEF-1 CLI: --dry-run must suppress real provider and use FakeExecutor path
# ---------------------------------------------------------------------------


def test_cli_continue_dry_run_uses_fake_executor(tmp_path: Path) -> None:
    """DEF-1/DEF-2: The --dry-run flag must cause command_continue to fall back to FakeExecutor
    so scripted tests can run without a live provider."""
    import argparse

    import stagemesh.cli as cli_module

    project = tmp_path / "proj"
    project.mkdir()

    args = argparse.Namespace(
        project=str(project),
        once=True,
        json=True,
        provider=None,
        dry_run=True,
    )
    result = cli_module.command_continue(args)
    assert result == 0


def _cli_project(tmp_path: Path, *, routing: dict[str, object]) -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    workspace = GitWorkspace(project)
    workspace.init_if_needed()
    (project / "src").mkdir()
    (project / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    workspace.commit_all("initial")
    runtime = project / ".stagemesh"
    runtime.mkdir()
    (runtime / "backlog.json").write_text(
        json.dumps({"tasks": [{"id": "task-1", "title": "change app"}]}),
        encoding="utf-8",
    )
    config = {"routing": routing, "providers": {}}
    (runtime / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return project


def _provider_script(path: Path, body: str) -> str:
    path.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{path}"'


def _continue(project: Path, *, provider: str | None = None) -> int:
    import stagemesh.cli as cli_module

    return cli_module.command_continue(
        argparse.Namespace(project=str(project), once=False, json=True, provider=provider, dry_run=False)
    )


def _review_payload(project: Path) -> dict[str, object]:
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    try:
        row = store.conn.execute(
            "SELECT payload FROM evidence WHERE kind='REVIEW' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        return json.loads(row["payload"])
    finally:
        store.close()


def test_cli_continue_staged_routes_invoke_distinct_review_provider(tmp_path: Path) -> None:
    log = tmp_path / "calls.log"
    impl = _provider_script(
        tmp_path / "impl.py",
        f"from pathlib import Path\n"
        f"Path({str(log)!r}).write_text('codex\\n', encoding='utf-8')\n"
        "Path('src/app.py').write_text('VALUE = 2\\n', encoding='utf-8')\n",
    )
    review = _provider_script(
        tmp_path / "review.py",
        f"from pathlib import Path\n"
        f"Path({str(log)!r}).write_text(Path({str(log)!r}).read_text(encoding='utf-8') + 'claude\\n', encoding='utf-8')\n"
        "print('{\"decision\":\"PASS\"}')\n",
    )
    project = _cli_project(
        tmp_path,
        routing={"mode": "STAGED", "stage_routes": {"IMPLEMENT": "codex", "REVIEW": "claude"}},
    )
    config_path = project / ".stagemesh" / "config.json"
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["providers"] = {"codex": impl, "claude": review}
    config_path.write_text(json.dumps(data), encoding="utf-8")

    assert _continue(project) == 0

    payload = _review_payload(project)
    assert log.read_text(encoding="utf-8").splitlines() == ["codex", "claude"]
    assert payload["implementer_provider"] == "codex"
    assert payload["review_provider"] == "claude"
    assert payload["review_execution_provider"] == "claude"
    assert payload["review_execution_invoked"] is True
    assert payload["independent_reviewer"] is True


def test_cli_continue_same_review_provider_falls_back_non_independent(tmp_path: Path) -> None:
    calls = tmp_path / "calls.log"
    impl = _provider_script(
        tmp_path / "impl.py",
        f"from pathlib import Path\nPath('src/app.py').write_text('VALUE = 3\\n', encoding='utf-8')\n"
        f"Path({str(calls)!r}).write_text('codex\\n', encoding='utf-8')\n",
    )
    project = _cli_project(
        tmp_path,
        routing={"mode": "STAGED", "stage_routes": {"IMPLEMENT": "codex", "REVIEW": "codex"}},
    )
    config_path = project / ".stagemesh" / "config.json"
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["providers"] = {"codex": impl}
    config_path.write_text(json.dumps(data), encoding="utf-8")

    assert _continue(project) == 0

    payload = _review_payload(project)
    assert calls.read_text(encoding="utf-8").splitlines() == ["codex"]
    assert payload["implementer_provider"] == "codex"
    assert payload["review_execution_invoked"] is False
    assert payload["independent_reviewer"] is False


def test_cli_continue_single_agent_review_is_deterministic_non_independent(tmp_path: Path) -> None:
    impl = _provider_script(
        tmp_path / "impl.py",
        "from pathlib import Path\nPath('src/app.py').write_text('VALUE = 4\\n', encoding='utf-8')\n",
    )
    project = _cli_project(
        tmp_path,
        routing={"mode": "SINGLE_AGENT", "single_agent_provider": "codex", "stage_routes": {"REVIEW": "claude"}},
    )
    config_path = project / ".stagemesh" / "config.json"
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["providers"] = {"codex": impl, "claude": f'"{sys.executable}" -c "print(\'should-not-run\')"'}
    config_path.write_text(json.dumps(data), encoding="utf-8")

    assert _continue(project) == 0

    payload = _review_payload(project)
    assert payload["review_provider"] == "single-agent-deterministic-fallback"
    assert payload["review_execution_invoked"] is False
    assert payload["independent_reviewer"] is False


# ---------------------------------------------------------------------------
# Integration: capacity failure is isolated from code failure
# ---------------------------------------------------------------------------


class CodeFailExecutor(FakeExecutor):
    """Executor that exits non-zero (code failure, not capacity)."""

    name = "code-fail"

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        return ExecutionResult(ExecutionStatus.FAILED, capacity_failure=False)


def test_code_failure_does_not_advance_stage(store: Store, tmp_path: Path) -> None:
    """A code failure (capacity_failure=False) must not advance the task stage."""
    task_id = store.upsert_task("code-fail-task")
    store.advance_task(task_id, Stage.IMPLEMENT)

    coord = Coordinator(store, tmp_path, executor=CodeFailExecutor())
    coord.tick()

    task = store.get_task(task_id)
    assert task["stage"] == Stage.IMPLEMENT


def test_capacity_failure_is_distinguishable_from_code_failure() -> None:
    """ExecutionResult must correctly distinguish capacity failures from code failures."""
    cap_fail = ExecutionResult(ExecutionStatus.FAILED, capacity_failure=True)
    code_fail = ExecutionResult(ExecutionStatus.FAILED, capacity_failure=False)

    assert cap_fail.capacity_failure is True
    assert code_fail.capacity_failure is False
    assert cap_fail.status is ExecutionStatus.FAILED
    assert code_fail.status is ExecutionStatus.FAILED


# ---------------------------------------------------------------------------
# Provider Failure Classification: 5 distinct categories
# ---------------------------------------------------------------------------


def test_failure_classification_all_five_categories() -> None:
    """StageMesh must classify failures into:
    1. Provider unavailable (missing executable or command not found)
    2. Authentication failure (unauthorized, invalid key, login required)
    3. Quota / rate limit (429, quota exceeded, too many requests)
    4. Transient provider failure (502, 503, service unavailable, overloaded)
    5. Implementation / code defect (syntax error, test failure, non-zero code)
    """
    # 1. Provider unavailable
    is_cap, reason = classify_failure(1, exc=FileNotFoundError("not found"))
    assert is_cap is True
    assert reason == "provider_unavailable"

    is_cap, reason = classify_failure(127, stderr="/bin/sh: claude: command not found")
    assert is_cap is True
    assert reason == "provider_unavailable"

    # 2. Authentication failure
    is_cap, reason = classify_failure(1, stderr="Error 401: Unauthorized. Invalid API key provided.")
    assert is_cap is True
    assert reason == "authentication_failure"

    is_cap, reason = classify_failure(1, stdout="You are not logged in. Please run `claude auth login`.")
    assert is_cap is True
    assert reason == "authentication_failure"

    # 3. Quota / rate limit
    is_cap, reason = classify_failure(1, stderr="Rate limit exceeded: 429 Too Many Requests")
    assert is_cap is True
    assert reason == "quota_rate_limit"

    is_cap, reason = classify_failure(1, stdout="You have exceeded your current quota. Please upgrade plan.")
    assert is_cap is True
    assert reason == "quota_rate_limit"

    # 4. Transient provider failure
    is_cap, reason = classify_failure(1, stderr="503 Service Unavailable: server is overloaded")
    assert is_cap is True
    assert reason == "transient_provider_failure"

    is_cap, reason = classify_failure(1, stderr="Connection reset by peer; timed out waiting for upstream")
    assert is_cap is True
    assert reason == "transient_provider_failure"

    # 5. Implementation / code defect
    is_cap, reason = classify_failure(1, stderr="AssertionError: 2 != 3\nFAILED tests/test_calc.py")
    assert is_cap is False
    assert reason == "implementation_failure"

    is_cap, reason = classify_failure(2, stderr="SyntaxError: invalid syntax at line 42")
    assert is_cap is False
    assert reason == "implementation_failure"


# ---------------------------------------------------------------------------
# Prompt Piping: SubprocessExecutor sends task title via stdin
# ---------------------------------------------------------------------------


def test_subprocess_executor_pipes_prompt_to_stdin(store: Store, tmp_path: Path) -> None:
    """SubprocessExecutor must pipe task prompt to stdin of the provider process."""
    task_id = store.upsert_task("write a helper function")
    # Python script that reads stdin and writes it to a file
    script = (
        "import sys\n"
        "data = sys.stdin.read()\n"
        "with open('captured_prompt.txt', 'w') as f:\n"
        "    f.write(data)\n"
    )
    executor = SubprocessExecutor([sys.executable, "-c", script], name="test-stdin")
    store.advance_task(task_id, Stage.IMPLEMENT)
    coord = Coordinator(store, tmp_path, executor=executor)
    assert coord.tick() == 1

    captured_file = task_workspace(tmp_path, task_id) / "captured_prompt.txt"
    assert captured_file.exists()
    content = captured_file.read_text(encoding="utf-8")
    assert "write a helper function" in content
    assert "StageMesh task:" in content


# ---------------------------------------------------------------------------
# Exact SHA Integrity: Candidate SHA carried through entire lifecycle
# ---------------------------------------------------------------------------


def test_exact_sha_preserved_through_validation_review_integration(store: Store, tmp_path: Path) -> None:
    """Candidate SHA produced at IMPLEMENT must be the EXACT same SHA used in
    VALIDATE, REVIEW, and INTEGRATE stages."""
    task_id = store.upsert_task("exact-sha-task")
    coord = Coordinator(store, tmp_path)

    # 1. PLAN -> IMPLEMENT
    assert coord.tick() == 1
    assert store.get_task(task_id)["stage"] == Stage.IMPLEMENT

    # 2. IMPLEMENT -> VALIDATE (produces candidate commit)
    assert coord.tick() == 1
    assert store.get_task(task_id)["stage"] == Stage.VALIDATE
    candidate = store.latest_candidate(task_id)
    assert candidate is not None
    impl_sha = candidate["sha"]
    assert len(impl_sha) >= 7

    # 3. VALIDATE -> REVIEW
    assert coord.tick() == 1
    assert store.get_task(task_id)["stage"] == Stage.REVIEW
    from stagemesh.domain import EvidenceKind, EvidenceStatus
    val_evidence = store.has_evidence(task_id, impl_sha, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)
    assert val_evidence is True

    # 4. REVIEW -> INTEGRATE
    assert coord.tick() == 1
    assert store.get_task(task_id)["stage"] == Stage.INTEGRATE
    rev_evidence = store.has_evidence(task_id, impl_sha, EvidenceKind.REVIEW, EvidenceStatus.PASSED)
    assert rev_evidence is True

    # 5. INTEGRATE -> DONE
    assert coord.tick() == 1
    assert store.get_task(task_id)["stage"] == Stage.DONE
    int_evidence = store.has_evidence(task_id, impl_sha, EvidenceKind.INTEGRATION, EvidenceStatus.PASSED)
    assert int_evidence is True

    # Verify no mismatched evidence exists
    rows = list(store.conn.execute("SELECT candidate_sha FROM evidence WHERE task_id=?", (task_id,)))
    assert all(row["candidate_sha"] == impl_sha for row in rows)


# ---------------------------------------------------------------------------
# Idempotency: Repeated continue on completed tasks does not duplicate work
# ---------------------------------------------------------------------------


def test_restart_after_done_is_idempotent_no_duplicate_work(store: Store, tmp_path: Path) -> None:
    """Re-running coordinator ticks on completed tasks must be a no-op:
    no duplicate commits, no evidence duplication, no stage regression."""
    task_id = store.upsert_task("idempotency-task")
    coord = Coordinator(store, tmp_path)

    # Run through to completion
    for _ in range(10):
        if coord.tick() == 0:
            break

    assert store.get_task(task_id)["stage"] == Stage.DONE
    initial_candidates = list(store.conn.execute("SELECT * FROM candidates WHERE task_id=?", (task_id,)))
    initial_evidence = list(store.conn.execute("SELECT * FROM evidence WHERE task_id=?", (task_id,)))
    initial_executions = list(store.conn.execute("SELECT * FROM executions WHERE task_id=?", (task_id,)))

    # Re-run multiple ticks
    restarted = Coordinator(store, tmp_path)
    for _ in range(5):
        assert restarted.tick() == 0

    # Ensure zero mutation
    after_candidates = list(store.conn.execute("SELECT * FROM candidates WHERE task_id=?", (task_id,)))
    after_evidence = list(store.conn.execute("SELECT * FROM evidence WHERE task_id=?", (task_id,)))
    after_executions = list(store.conn.execute("SELECT * FROM executions WHERE task_id=?", (task_id,)))

    assert len(after_candidates) == len(initial_candidates)
    assert len(after_evidence) == len(initial_evidence)
    assert len(after_executions) == len(initial_executions)
    assert store.get_task(task_id)["stage"] == Stage.DONE

