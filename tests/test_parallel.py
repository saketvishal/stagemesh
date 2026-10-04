from __future__ import annotations

import _thread
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import stagemesh.cli as cli_module
from stagemesh.concurrency import IntegrationLock, ProviderLimiter, contract_conflict, patterns_overlap
from stagemesh.config import ConfigValidationError, load_config
from stagemesh.contracts import parse_contract
from stagemesh.coordinator import Coordinator
from stagemesh.domain import EvidenceKind, ExecutionKind, ExecutionStatus
from stagemesh.execution import ExecutionResult, Executor
from stagemesh.git import GitWorkspace
from stagemesh.parallel import ParallelRunner, recover_orphaned_claims, worker_id_for
from stagemesh.persistence import Store
from stagemesh.serialized_integration import SerializedIntegrator
from stagemesh.workspaces import prepare_task_workspace, sweep_task_worktrees, task_workspace, worktree_root

from test_run_ready import _project


def contract_for(path: str, **extra: object) -> dict:
    return {
        "objective": f"touch {path}",
        "allowed_files": [path],
        "required_tests": [{"name": "smoke", "command": [sys.executable, "-c", "pass"]}],
        **extra,
    }


class ScriptedExecutor(Executor):
    """Writes one file per task into the task's own worktree and records when each task ran."""

    name = "scripted"

    def __init__(self, files: dict[str, tuple[str, str]], barrier: threading.Barrier | None = None, fail: set[str] | None = None):
        self.files = files
        self.barrier = barrier
        self.fail = fail or set()
        self.lock = threading.Lock()
        self.log: list[tuple[str, str, float]] = []
        self.worktrees: dict[str, Path] = {}

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        with self.lock:
            self.log.append(("start", task_id, time.monotonic()))
        if task_id in self.fail:
            raise RuntimeError(f"provider crashed on {task_id}")
        execution_id = store.start_execution(task_id=task_id, claim_id=claim_id, kind=ExecutionKind.IMPLEMENTATION)
        run_path = prepare_task_workspace(project, task_id)
        self.worktrees[task_id] = run_path
        from stagemesh.workspaces import record_task_baseline

        record_task_baseline(store, task_id, run_path)
        if self.barrier is not None:
            self.barrier.wait(timeout=20)  # only passes when the tasks really run at the same time
        rel, content = self.files[task_id]
        (run_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (run_path / rel).write_text(content, encoding="utf-8")
        sha = GitWorkspace(run_path).commit_all(f"implement {task_id}")
        store.add_candidate(task_id, sha, self.name, durable_handoff=True)
        store.finish_execution(execution_id, ExecutionStatus.SUCCEEDED, sha)
        with self.lock:
            self.log.append(("end", task_id, time.monotonic()))
        return ExecutionResult(ExecutionStatus.SUCCEEDED, sha, durable_handoff=True)


class Rig:
    def __init__(self, tmp_path: Path, task_ids: list[str], files: dict[str, tuple[str, str]] | None = None, **project_kwargs):
        self.files = files or {t: (f"out/{t}.txt", f"{t}\n") for t in task_ids}
        self.project = _project(tmp_path, task_ids, contracts=[], **project_kwargs)
        for task_id in task_ids:
            (self.project / ".stagemesh" / "contracts" / f"{task_id}.json").write_text(
                json.dumps(contract_for(self.files[task_id][0])), encoding="utf-8"
            )
        self.store = Store(self.project / ".stagemesh" / "stagemesh.sqlite3")
        self.store.migrate()
        cli_module._sync_all_sources(self.store, self.project, load_config(self.project), None)
        ref = GitWorkspace(self.project).run("symbolic-ref", "-q", "HEAD").stdout.strip()
        self.ref = ref
        self.lock = IntegrationLock(self.project / ".stagemesh" / "integration.lock", timeout_seconds=30)

    def runner(self, executor: Executor, concurrency: int = 2, runner_class=ParallelRunner, **kwargs) -> ParallelRunner:
        integrator = SerializedIntegrator(self.ref, False, self.lock, max_rebases=kwargs.pop("max_rebases", 2))

        def make(target, store, task_id):
            return Coordinator(store, self.project, executor=executor, integrator=integrator, target=target, worker_id=worker_id_for(task_id))

        runner = runner_class(self.store, self.project, make, concurrency=concurrency, poll_seconds=0.05, **kwargs)
        integrator.on_event = lambda task_id, event, detail: runner.note(task_id, event, **detail)
        return runner

    def tree(self) -> set[str]:
        return set(GitWorkspace(self.project).run("ls-tree", "-r", "--name-only", self.ref).stdout.split())

    def outcomes(self, summary) -> dict[str, str]:
        return {t.task_id: t.summary.stop_reason for t in summary.tasks}


def test_two_independent_tasks_run_concurrently_in_separate_worktrees(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ["A", "B"])
    executor = ScriptedExecutor(rig.files, barrier=threading.Barrier(2))
    summary = rig.runner(executor).run()
    assert rig.outcomes(summary) == {"A": "DONE", "B": "DONE"}, summary.to_dict()
    assert summary.succeeded
    assert executor.worktrees["A"] != executor.worktrees["B"]
    assert {p.parent for p in executor.worktrees.values()} == {worktree_root(rig.project)}
    assert {"out/A.txt", "out/B.txt"} <= rig.tree()
    for task in summary.tasks:  # each task reports its own lifecycle
        events = [e["event"] for e in task.to_dict()["lifecycle"]]
        assert events[0] == "selected" and "started" in events and events[-1] == "finished"
        assert [e["seq"] for e in task.events] == list(range(1, len(task.events) + 1))
    assert not rig.store.conn.execute("SELECT 1 FROM claims WHERE active=1").fetchall()
    assert not (worktree_root(rig.project) / "x").exists() and not any(worktree_root(rig.project).iterdir())  # DONE removes worktrees


def test_worktree_root_defaults_to_project_runtime_directory(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1"])
    assert worktree_root(project) == project / ".stagemesh" / "worktrees"


def test_configured_worktree_root_is_used(tmp_path: Path) -> None:
    configured = tmp_path / "stagemesh-task-worktrees"
    project = _project(tmp_path, ["T-1"], config={"runtime": {"worktree_root": str(configured)}})
    assert load_config(project).runtime.worktree_root == configured
    assert worktree_root(project) == configured
    assert task_workspace(project, "T-1").parent == configured


def test_unsafe_worktree_roots_are_refused_unless_explicitly_allowed(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1"], config={"runtime": {"worktree_root": str(Path.home())}})
    with pytest.raises(ConfigValidationError, match="home directory"):
        load_config(project)

    (project / ".stagemesh" / "config.json").write_text(
        json.dumps({"runtime": {"worktree_root": str(project / "src" / "worktrees")}}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigValidationError, match="product source"):
        load_config(project)

    (project / ".stagemesh" / "config.json").write_text(
        json.dumps({"runtime": {"worktree_root": str(Path.home()), "allow_unsafe_worktree_root": True}}),
        encoding="utf-8",
    )
    assert load_config(project).runtime.allow_unsafe_worktree_root is True


@pytest.mark.skipif(
    os.name != "nt",
    reason="Windows-style absolute paths are only absolute on Windows",
)
def test_configured_worktree_root_accepts_windows_style_paths(tmp_path: Path) -> None:
    configured = tmp_path / "windows-worktrees"
    project = _project(tmp_path, ["T-1"], config={"runtime": {"worktree_root": str(configured)}})
    assert load_config(project).runtime.worktree_root == configured


def test_sweep_does_not_delete_legacy_global_worktrees(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ["A"])
    old_root = (
        rig.project.parent
        / ".sm-wt"
        / hashlib.sha1(str(rig.project.resolve()).encode("utf-8")).hexdigest()[:10]
    )
    old_worktree = old_root / "deadbeef0000"
    old_worktree.mkdir(parents=True)
    (old_worktree / "stray.txt").write_text("legacy\n", encoding="utf-8")

    assert sweep_task_worktrees(rig.project, rig.store) == []
    assert old_worktree.exists()


def test_concurrency_limit_is_respected(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ["A", "B", "C"])
    executor = ScriptedExecutor(rig.files)
    peak = {"now": 0, "max": 0}
    original = executor.run

    def counted(store, task_id, claim_id, project):
        with executor.lock:
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
        time.sleep(0.3)
        try:
            return original(store, task_id, claim_id, project)
        finally:
            with executor.lock:
                peak["now"] -= 1

    executor.run = counted  # type: ignore[method-assign]
    summary = rig.runner(executor, concurrency=2).run()
    assert set(rig.outcomes(summary).values()) == {"DONE"} and len(summary.tasks) == 3
    assert peak["max"] == 2


def test_dependent_task_waits_for_its_dependency(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ["A", "B"], dependencies={"B": ["A"]})
    executor = ScriptedExecutor(rig.files)
    summary = rig.runner(executor).run()
    assert rig.outcomes(summary) == {"A": "DONE", "B": "DONE"}
    marks = {(kind, task): at for kind, task, at in executor.log}
    assert marks[("end", "A")] <= marks[("start", "B")]  # B only started once A was fully done
    assert [t.task_id for t in summary.tasks] == ["A", "B"]
    first = rig.store.conn.execute("SELECT 1 FROM audit_events WHERE event_type='task.advance'").fetchall()
    assert first


def test_failed_dependency_leaves_dependent_unrun(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ["A", "B"], dependencies={"B": ["A"]})
    summary = rig.runner(ScriptedExecutor(rig.files, fail={"A"})).run()
    assert [t.task_id for t in summary.tasks] == ["A"]
    assert summary.stop_reason == "PARTIAL"
    assert any(s["task_id"] == "B" and "waiting for" in s["reason"] for s in summary.skipped)


def test_blocked_stale_and_unplannable_tasks_are_never_started(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ["A", "B", "C", "D"], labels={"D": ["stagemesh:blocked"]})
    rig.store.block_task(rig.store.get_task("B")["id"])
    (rig.project / ".stagemesh" / "contracts" / "C.json").unlink()  # no contract; auto-planning is off below
    summary = rig.runner(ScriptedExecutor(rig.files), concurrency=4, auto_plan=False).run()
    assert [t.task_id for t in summary.tasks] == ["A"]
    reasons = {s["task_id"]: s["reason"] for s in summary.skipped}
    assert "blocked" in reasons["B"] and "excluded label" in reasons["D"] and "auto-planning is disabled" in reasons["C"]


def test_overlapping_protected_files_and_exclusive_resources_do_not_run_together(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ["A", "B", "C"])
    for task_id, extra in (("A", {"exclusive_resources": ["test-db"]}), ("B", {"exclusive_resources": ["test-db"]}), ("C", {})):
        (rig.project / ".stagemesh" / "contracts" / f"{task_id}.json").write_text(
            json.dumps(contract_for(rig.files[task_id][0], **extra)), encoding="utf-8"
        )
    executor = ScriptedExecutor(rig.files)
    summary = rig.runner(executor, concurrency=3).run()
    assert set(rig.outcomes(summary).values()) == {"DONE"}
    marks = {(kind, task): at for kind, task, at in executor.log}
    a_then_b = marks[("end", "A")] <= marks[("start", "B")]
    b_then_a = marks[("end", "B")] <= marks[("start", "A")]
    assert a_then_b or b_then_a  # never overlapping
    assert any(d["reason"] == "exclusive_resource: test-db" for d in summary.deferred)


def test_contract_conflict_rules() -> None:
    def c(**kw):
        return parse_contract({"objective": "x", **kw})

    assert contract_conflict(c(protected_files=["src/core/**"]), c(allowed_files=["src/core/a.py"])).kind == "protected_files"
    assert contract_conflict(c(protected_files=["src/core/**"]), c(allowed_files=["docs/**"])) is None
    assert contract_conflict(c(exclusive_resources=["Port-80"]), c(exclusive_resources=["port-80"])).kind == "exclusive_resource"
    assert contract_conflict(c(allowed_files=["a/**"]), c(allowed_files=["a/**"])) is None  # shared write scope alone is not a conflict
    assert patterns_overlap("src/a/*.py", "src/b/*.py") is False
    assert patterns_overlap("**", "src/b/*.py") is True
    assert patterns_overlap("docs/*.md", "docs/*.py") is False


def test_exclusive_resources_do_not_change_existing_contract_digests() -> None:
    from stagemesh.contracts import canonical_contract_json

    assert "exclusive_resources" not in canonical_contract_json(parse_contract({"objective": "x"}))
    assert "exclusive_resources" in canonical_contract_json(parse_contract({"objective": "x", "exclusive_resources": ["db"]}))


def test_integration_is_serialized_and_main_advancing_is_rebased(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ["A", "B"])
    state = {"inside": 0, "max": 0, "entries": []}
    guard = threading.Lock()
    real_hold = rig.lock.hold

    @contextlib.contextmanager
    def watched(owner):
        with real_hold(owner):
            with guard:
                state["inside"] += 1
                state["max"] = max(state["max"], state["inside"])
                state["entries"].append(owner)
            time.sleep(0.15)
            try:
                yield
            finally:
                with guard:
                    state["inside"] -= 1

    rig.lock.hold = watched  # type: ignore[method-assign]
    executor = ScriptedExecutor(rig.files, barrier=threading.Barrier(2))
    summary = rig.runner(executor).run()
    assert rig.outcomes(summary) == {"A": "DONE", "B": "DONE"}, summary.to_dict()
    assert state["max"] == 1  # never two integrations at once
    assert {"out/A.txt", "out/B.txt"} <= rig.tree()  # both landed, none lost
    rebased = rig.store.conn.execute("SELECT payload FROM audit_events WHERE event_type='integration.rebased'").fetchall()
    assert len(rebased) == 1  # the task that lost the race was rebased once, then re-validated and re-reviewed
    loser = json.loads(rebased[0]["payload"])["task_id"]
    stages = [e.get("stage") for e in next(t for t in summary.tasks if t.task_id == loser).events if e["event"] == "stage"]
    assert stages.count("VALIDATE") == 2 and stages.count("REVIEW") == 2 and stages[-1] == "INTEGRATE"
    assert any(e["event"] == "integration_rebased" for t in summary.tasks for e in t.events)


def test_integration_lock_excludes_threads_and_survives_holder_failure(tmp_path: Path) -> None:
    lock = IntegrationLock(tmp_path / "x.lock", timeout_seconds=10)
    order: list[str] = []

    def hold(name: str, delay: float, fail: bool = False) -> None:
        try:
            with lock.hold(name):
                order.append(f"{name}:in")
                time.sleep(delay)
                order.append(f"{name}:out")
                if fail:
                    raise RuntimeError("boom")
        except RuntimeError:
            pass

    first = threading.Thread(target=hold, args=("one", 0.3, True))
    first.start()
    time.sleep(0.1)
    second = threading.Thread(target=hold, args=("two", 0.0))
    second.start()
    first.join(); second.join()
    assert order == ["one:in", "one:out", "two:in", "two:out"]  # a crashed holder still releases the lock


def test_integration_lock_excludes_other_processes(tmp_path: Path) -> None:
    path = tmp_path / "p.lock"
    code = (
        "import sys, time; from pathlib import Path\n"
        "from stagemesh.concurrency import IntegrationLock\n"
        "lock = IntegrationLock(Path(sys.argv[1]), timeout_seconds=30)\n"
        "with lock.hold('child'):\n"
        "    print('held', flush=True); time.sleep(1.0)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", code, str(path)], stdout=subprocess.PIPE, text=True, env={**__import__("os").environ, "PYTHONPATH": str(Path(__file__).parent.parent / "src")})
    assert child.stdout.readline().strip() == "held"
    started = time.monotonic()
    with IntegrationLock(path, timeout_seconds=30).hold("parent"):
        waited = time.monotonic() - started
    child.wait(timeout=10)
    assert waited >= 0.5  # blocked until the other process let go


def test_unresolvable_main_advance_leaves_a_typed_state_and_keeps_other_tasks(tmp_path: Path) -> None:
    files = {"A": ("shared.txt", "from A\n"), "B": ("shared.txt", "from B\n"), "C": ("out/C.txt", "C\n")}
    rig = Rig(tmp_path, ["A", "B", "C"], files=files)
    summary = rig.runner(ScriptedExecutor(files, barrier=threading.Barrier(2)), concurrency=2, max_steps=40).run()
    outcomes = rig.outcomes(summary)
    winner, loser = ("A", "B") if outcomes["A"] == "DONE" else ("B", "A")
    assert outcomes[winner] == "DONE" and outcomes[loser] != "DONE", outcomes
    codes = set()
    for row in rig.store.conn.execute("SELECT payload FROM evidence WHERE task_id=? AND kind=?", (loser, EvidenceKind.INTEGRATION)):
        codes |= {f["code"] for f in json.loads(row["payload"]).get("findings", [])}
    assert "integration_rebase_conflict" in codes
    assert GitWorkspace(rig.project).run("show", f"{rig.ref}:shared.txt").stdout == files[winner][1]  # ref never took the loser
    assert outcomes.get("C") == "DONE"  # the third task was not affected


def test_one_task_crashing_does_not_stop_the_others(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ["A", "B"])
    summary = rig.runner(ScriptedExecutor(rig.files, fail={"A"})).run()
    outcomes = rig.outcomes(summary)
    assert outcomes["B"] == "DONE" and outcomes["A"] != "DONE"
    assert summary.stop_reason == "PARTIAL" and not summary.succeeded
    assert "out/B.txt" in rig.tree() and "out/A.txt" not in rig.tree()
    assert not rig.store.conn.execute("SELECT 1 FROM claims WHERE active=1").fetchall()


def test_provider_limiter_caps_concurrent_runs_and_waits_for_a_slot() -> None:
    limiter = ProviderLimiter(default_limit=1)
    inside = {"now": 0, "max": 0}
    guard = threading.Lock()

    def use() -> None:
        with limiter.slot(["codex"]) as name:
            assert name == "codex"
            with guard:
                inside["now"] += 1
                inside["max"] = max(inside["max"], inside["now"])
            time.sleep(0.1)
            with guard:
                inside["now"] -= 1

    threads = [threading.Thread(target=use) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert inside["max"] == 1 and limiter.active("codex") == 0


def test_provider_limiter_spills_to_next_provider_and_honours_cooldown() -> None:
    limiter = ProviderLimiter(limits={"codex": 1, "claude": 1})
    assert limiter.acquire(["codex", "claude"]) == "codex"
    assert limiter.acquire(["codex", "claude"]) == "claude"  # codex is full, so the work goes to the next provider
    assert limiter.acquire(["codex", "claude"], timeout=0.2) is None  # everything busy: waits, then gives up
    limiter.release("codex")
    limiter.cool_down("codex", 60, "quota_rate_limit")
    assert "provider_cooldown" in (limiter.cooling("codex") or "")
    assert limiter.acquire(["codex"], timeout=0.2) is None  # cooling providers are never handed work
    limiter.release("claude")
    assert limiter.acquire(["codex", "claude"]) == "claude"


def test_pooled_executor_never_overloads_a_provider_across_tasks(tmp_path: Path) -> None:
    from stagemesh.provider_pool import PooledExecutor, ProviderLog, ProviderPool
    from stagemesh.providers import RuntimeCommandAdapter

    rig = Rig(tmp_path, ["A", "B", "C"])
    active = {"now": 0, "max": 0}
    guard = threading.Lock()

    class CountingAdapter(RuntimeCommandAdapter):
        def check_capacity(self) -> str:
            return "AVAILABLE"

        def execute(self, store, task_id, claim_id, project):
            with guard:
                active["now"] += 1
                active["max"] = max(active["max"], active["now"])
            try:
                time.sleep(0.2)
                return ScriptedExecutor(rig.files).run(store, task_id, claim_id, project)
            finally:
                with guard:
                    active["now"] -= 1

    limiter = ProviderLimiter(default_limit=1)
    adapter = CountingAdapter("codex", (sys.executable,))
    integrator = SerializedIntegrator(rig.ref, False, rig.lock)

    def make(target, store, task_id):
        pool = ProviderPool([adapter], {"IMPLEMENT": ("codex",), "REVIEW": ("codex",)}, require_independent=False, log=ProviderLog(echo=False), limiter=limiter)
        return Coordinator(store, rig.project, executor=PooledExecutor(pool), integrator=integrator, target=target, worker_id=worker_id_for(task_id))

    runner = ParallelRunner(rig.store, rig.project, make, concurrency=3, poll_seconds=0.05, limiter=limiter)
    summary = runner.run()
    assert set(rig.outcomes(summary).values()) == {"DONE"}, summary.to_dict()
    assert active["max"] == 1  # three tasks in flight, one provider run at a time
    assert summary.providers["limits"]["codex"]["limit"] == 1


def test_provider_cooldown_is_shared_between_tasks(tmp_path: Path) -> None:
    from stagemesh.provider_pool import ProviderLog, ProviderPool
    from stagemesh.providers import RuntimeCommandAdapter

    class Always(RuntimeCommandAdapter):
        def check_capacity(self) -> str:
            return "AVAILABLE"

    limiter = ProviderLimiter()
    pool = ProviderPool([Always("codex", (sys.executable,))], {"IMPLEMENT": ("codex",)}, log=ProviderLog(echo=False), limiter=limiter)
    store = Store(tmp_path / "s.sqlite3")
    store.migrate()
    assert pool.evaluate(store, "IMPLEMENT", "T-1")[0].eligible
    limiter.cool_down("codex", 30, "quota_rate_limit")  # a failure seen by task T-1 ...
    verdict = pool.evaluate(store, "IMPLEMENT", "T-2")[0]  # ... also keeps T-2 off that provider
    assert not verdict.eligible and verdict.reason.startswith("provider_cooldown")


def test_restart_recovers_dead_runs_claims_and_orphaned_worktrees(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ["A", "B"])
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    dead_worker = f"parallel-{gone.pid}-A"
    # State a hard-killed run leaves behind: A claimed with a half-finished edit, plus a worktree no task owns.
    claim_id = rig.store.acquire_claim("A", dead_worker)
    assert claim_id and rig.store.get_task("A")["status"] == "CLAIMED"
    execution_id = rig.store.start_execution(task_id="A", claim_id=claim_id, kind=ExecutionKind.IMPLEMENTATION)
    partial = prepare_task_workspace(rig.project, "A") / "half-done.txt"
    partial.write_text("partial\n", encoding="utf-8")
    orphan = worktree_root(rig.project) / "deadbeef0000"
    orphan.mkdir(parents=True)
    (orphan / "stray.txt").write_text("x", encoding="utf-8")
    live_claim = rig.store.acquire_claim("B", f"parallel-{__import__('os').getpid()}-B")  # a LIVE owner must not be touched
    assert live_claim

    released = recover_orphaned_claims(rig.store, rig.project)
    assert [r["task_id"] for r in released] == ["A"]
    assert rig.store.get_task("A")["status"] == "OPEN" and rig.store.get_task("B")["status"] == "CLAIMED"
    assert not any(e["id"] == execution_id for e in rig.store.running_executions())
    assert not partial.exists()  # partial edits discarded, worktree itself kept for resume
    assert task_workspace(rig.project, "A").exists()
    swept = sweep_task_worktrees(rig.project, rig.store)
    assert [Path(s["worktree"]) for s in swept] == [orphan] and not orphan.exists()
    assert task_workspace(rig.project, "A").exists()
    rig.store.release_claim(live_claim)

    summary = rig.runner(ScriptedExecutor(rig.files)).run()  # a fresh run picks both up from where they were
    assert rig.outcomes(summary) == {"A": "DONE", "B": "DONE"}


def test_interrupt_releases_claims_keeps_worktrees_and_a_restart_finishes(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ["A", "B"])
    holder: dict[str, ParallelRunner] = {}

    class Hanging(ScriptedExecutor):
        def run(self, store, task_id, claim_id, project):
            execution_id = store.start_execution(task_id=task_id, claim_id=claim_id, kind=ExecutionKind.IMPLEMENTATION)
            run_path = prepare_task_workspace(project, task_id)
            (run_path / "half.txt").write_text("partial\n", encoding="utf-8")
            if self.barrier is not None:
                self.barrier.wait(timeout=20)
            if task_id == "A":
                _thread.interrupt_main()  # Ctrl+C while both providers are mid-run
            deadline = time.monotonic() + 20
            while not holder["runner"].stop.is_set() and time.monotonic() < deadline:
                time.sleep(0.02)
            store.finish_execution(execution_id, ExecutionStatus.FAILED)
            return ExecutionResult(ExecutionStatus.FAILED, failure_reason="killed")

    runner = rig.runner(Hanging(rig.files, barrier=threading.Barrier(2)), interrupt_grace_seconds=5)
    holder["runner"] = runner
    summary = runner.run()
    assert summary.interrupted and summary.stop_reason == "INTERRUPTED"
    assert set(rig.outcomes(summary).values()) == {"INTERRUPTED"}
    assert not rig.store.conn.execute("SELECT 1 FROM claims WHERE active=1").fetchall()  # nothing orphaned
    assert not list(rig.store.running_executions())
    for task_id in ("A", "B"):
        assert rig.store.get_task(task_id)["status"] == "OPEN"
        assert task_workspace(rig.project, task_id).exists() and not (task_workspace(rig.project, task_id) / "half.txt").exists()
        assert any(e["event"] == "interrupted" for e in next(t for t in summary.tasks if t.task_id == task_id).events)
    resumed = rig.runner(ScriptedExecutor(rig.files)).run()
    assert rig.outcomes(resumed) == {"A": "DONE", "B": "DONE"}


def test_cli_parallel_json_reports_each_task_separately(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1", "T-2"])
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_module.main(["--project", str(project), "continue", "--dry-run", "--json", "--parallel", "2"])
    data = json.loads(out.getvalue())
    assert code == 0 and data["mode"] == "parallel" and data["concurrency"] == 2 and data["succeeded"] is True
    assert data["task_outcomes"] == {"T-1": "DONE", "T-2": "DONE"}
    for task in data["tasks"]:
        assert task["stop_reason"] == "DONE" and task["lifecycle"][0]["event"] == "selected"
        assert task["steps"][-1]["new"]["stage"] == "DONE"
        assert all(step["task_id"] == task["task_id"] for step in task["steps"])


def test_cli_parallel_output_is_grouped_by_task(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1", "T-2"])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli_module.main(["--project", str(project), "continue", "--dry-run", "--parallel", "2"])
    assert code == 0
    lines = [line for line in out.getvalue().splitlines() if line.strip()]
    assert all(line.startswith(("[T-1] ", "[T-2] ", "[run]", "Parallel run")) for line in lines), lines
    assert any(line.startswith("[T-1]") and "Validation" in line for line in lines)
    assert any(line.startswith("[T-2]") and "Integration" in line for line in lines)
    assert "Parallel run stopped: done" in out.getvalue()


def test_cli_parallel_rejects_incompatible_flags(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1"])
    for extra in (["--task", "T-1"], ["--choose"], ["--once"]):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            assert cli_module.main(["--project", str(project), "continue", "--dry-run", "--parallel", "2", *extra]) == 2
        assert "cannot be combined" in err.getvalue()
    with contextlib.redirect_stderr(io.StringIO()):
        assert cli_module.main(["--project", str(project), "continue", "--dry-run", "--parallel", "0"]) == 2


def test_cli_parallel_with_nothing_runnable_is_a_refusal(tmp_path: Path) -> None:
    project = _project(tmp_path, [])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli_module.main(["--project", str(project), "continue", "--dry-run", "--json", "--parallel", "3"])
    data = json.loads(out.getvalue())
    assert code == 2 and data["stop_reason"] == "REFUSED:no_eligible_task" and data["tasks"] == []


def test_parallel_config_validation(tmp_path: Path) -> None:
    project = tmp_path / "p"
    (project / ".stagemesh").mkdir(parents=True)
    (project / ".stagemesh" / "config.json").write_text(
        json.dumps({"parallel": {"provider_max_concurrency": 3}, "providers": {"codex": {"command": "codex", "max_concurrency": 4}}}),
        encoding="utf-8",
    )
    config = load_config(project)
    assert config.parallel.provider_max_concurrency == 3 and config.provider_specs["codex"].max_concurrency == 4
    (project / ".stagemesh" / "config.json").write_text(json.dumps({"parallel": {"provider_max_concurrency": 0}}), encoding="utf-8")
    with pytest.raises(ConfigValidationError):
        load_config(project)


def test_review_providers_honour_shared_cooldown_and_cool_down_on_failure(tmp_path: Path) -> None:
    from stagemesh.provider_pool import FallbackReviewAdapter, ProviderLog, ProviderPool
    from stagemesh.review import INFRASTRUCTURE_FAILURE

    calls: list[str] = []

    class Reviewer:
        def __init__(self, name: str, answer: str):
            self.name, self.answer = name, answer

        def review_candidate(self, prompt, project, sha):
            calls.append(self.name)
            return self.answer

    down = json.dumps({"decision": INFRASTRUCTURE_FAILURE, "reason": "quota_rate_limit"})
    ok = json.dumps({"decision": "PASS"})
    limiter = ProviderLimiter()
    pool = ProviderPool([], {}, log=ProviderLog(echo=False), limiter=limiter, cooldown_seconds=60)
    store = Store(tmp_path / "s.sqlite3")
    store.migrate()
    first = FallbackReviewAdapter(pool, store, "T-1", [Reviewer("codex", down), Reviewer("grok", ok)])
    assert first.review_candidate("p", tmp_path, "sha") == ok and calls == ["codex", "grok"]
    assert "provider_cooldown" in (limiter.cooling("codex") or "")  # a failure seen by T-1 cools codex for everyone
    second = FallbackReviewAdapter(pool, store, "T-2", [Reviewer("codex", ok), Reviewer("grok", ok)])
    assert second.review_candidate("p", tmp_path, "sha") == ok
    assert calls == ["codex", "grok", "grok"]  # T-2 never sent codex a request while it was cooling
    third = FallbackReviewAdapter(pool, store, "T-3", [Reviewer("codex", ok)])
    assert json.loads(third.review_candidate("p", tmp_path, "sha"))["decision"] == INFRASTRUCTURE_FAILURE
    assert calls == ["codex", "grok", "grok"] and limiter.active("codex") == 0


def test_interrupt_kills_review_style_provider_processes_without_a_recorded_pid() -> None:
    from stagemesh.execution import communicate_bounded, kill_active_provider_processes

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    result: list[tuple] = []
    worker = threading.Thread(target=lambda: result.append(communicate_bounded(proc, "prompt", 120)))
    worker.start()
    time.sleep(0.5)
    started = time.monotonic()
    assert kill_active_provider_processes() == 1
    worker.join(timeout=15)
    assert not worker.is_alive() and time.monotonic() - started < 15 and proc.poll() is not None
    assert kill_active_provider_processes() == 0  # nothing left registered
