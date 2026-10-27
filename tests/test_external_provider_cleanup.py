from threading import Event, Thread
from types import SimpleNamespace

import pytest

from stagemesh import execution
from stagemesh.domain import ProcessIdentity
from stagemesh.queue_control import OperatorStopRequested, provider_stop_scope


@pytest.mark.parametrize("outcome", ["false", "raises", "confirmed"])
@pytest.mark.parametrize("stop_requested", [False, True])
def test_external_tree_cleanup_is_published_before_worker_completion(monkeypatch, outcome, stop_requested):
    reading, release_io, killing, release_proof, finished = (Event() for _ in range(5))
    identity = ProcessIdentity(123, 1.0, "boot", "python.exe")
    results, errors, cleanup_counts, controller_errors = [], [], [], []

    class Process:
        pid = identity.pid
        exited = False

        def communicate(self, *, input):
            reading.set()
            assert release_io.wait(5)
            return "partial output", ""

        def poll(self):
            return 0 if self.exited else None

    class Control:
        def poll(self):
            return {"terminate_running": True, "reason": "external stop", "rowid": 1} if killing.is_set() else None

        def note(self, *args, **kwargs):
            pass

    proc = Process()

    def kill(process):
        process.exited = True
        killing.set()
        release_io.set()
        assert release_proof.wait(5)
        if outcome == "raises":
            raise OSError("fixture lost cleanup proof")
        return outcome == "confirmed"

    def communicate():
        try:
            results.append(execution.communicate_bounded(proc, "", 60))
        except BaseException as exc:  # noqa: BLE001 -- marshal worker failures to assertions in the test thread
            errors.append(exc)
        finally:
            finished.set()

    def stop():
        try:
            cleanup_counts.append(execution.kill_active_provider_processes())
        except BaseException as exc:  # noqa: BLE001 -- marshal controller failures to the test thread
            controller_errors.append(exc)

    monkeypatch.setattr(execution, "popen_identity", lambda _: identity)
    monkeypatch.setattr(execution, "process_identity", lambda _: None if proc.exited else identity)
    monkeypatch.setattr(execution, "kill_process_tree", kill)
    monkeypatch.setattr(execution, "current_provider_stop", lambda: Control() if stop_requested else None)
    owner, controller = Thread(target=communicate), Thread(target=stop)
    owner.start()
    try:
        assert reading.wait(5)
        controller.start()
        assert killing.wait(5)
        assert not finished.wait(0.2), "worker accepted root exit before external tree proof was published"
        release_proof.set()
        owner.join(5)
        controller.join(5)
        assert not owner.is_alive() and not controller.is_alive()
        assert not controller_errors
        assert cleanup_counts == [int(outcome == "confirmed")]
        if outcome == "confirmed" and not stop_requested:
            assert results == [("partial output", "", False)] and not errors
        else:
            assert not results and len(errors) == 1
            assert isinstance(errors[0], OperatorStopRequested)
            assert errors[0].cleanup_complete is (outcome == "confirmed")
    finally:
        release_io.set()
        release_proof.set()
        owner.join(5)
        if controller.ident is not None:
            controller.join(5)


def test_scoped_interrupt_never_signals_foreign_project_or_task(tmp_path, monkeypatch):
    processes, owners, errors, killed = [], [], [], []
    database = tmp_path / "project.sqlite3"

    class Process:
        def __init__(self, pid):
            self.pid, self.exited = pid, False
            self.reading, self.release = Event(), Event()

        def communicate(self, *, input):
            self.reading.set()
            assert self.release.wait(5)
            return "", ""

        def poll(self):
            return 0 if self.exited else None

    def identity(proc):
        return ProcessIdentity(proc.pid, float(proc.pid), "boot", "python.exe")

    def run(proc, path, task):
        control = SimpleNamespace(store=SimpleNamespace(db_path=path), task_id=task, poll=lambda: None)
        try:
            with provider_stop_scope(control):
                execution.communicate_bounded(proc, "", 60)
        except BaseException as exc:  # noqa: BLE001 -- every worker failure is asserted after joining
            errors.append(exc)

    def kill(proc):
        killed.append(proc.pid)
        proc.exited = True
        proc.release.set()
        return True

    monkeypatch.setattr(execution, "popen_identity", identity)
    monkeypatch.setattr(execution, "kill_process_tree", kill)
    scopes = [(database, "own"), (database, "foreign"), (tmp_path / "other.sqlite3", "own")]
    try:
        for pid, (path, task) in enumerate(scopes, 100):
            proc = Process(pid)
            processes.append(proc)
            owner = Thread(target=run, args=(proc, path, task))
            owners.append(owner)
            owner.start()
            assert proc.reading.wait(5)
        assert execution.kill_active_provider_processes(database=database, task_ids={"own"}) == 1
        owners[0].join(5)
        assert not owners[0].is_alive()
        assert killed == [processes[0].pid]
        assert all(proc.poll() is None for proc in processes[1:])
        assert execution.kill_active_provider_processes(database=database, task_ids={"own"}) == 0
    finally:
        for proc in processes:
            proc.exited = True
            proc.release.set()
        for owner in owners:
            owner.join(5)
    assert not errors and all(not owner.is_alive() for owner in owners)


@pytest.mark.parametrize("claimless_research", [False, True])
def test_halt_fences_late_execution_before_a_delayed_worker_can_release_it(tmp_path, monkeypatch, claimless_research):
    from test_bounded_execution import TASK, _setup

    from stagemesh import parallel
    from stagemesh.domain import ExecutionKind
    from stagemesh.parallel import ParallelRunner, worker_id_for
    from stagemesh.run_ready import RunSummary

    project, store = _setup(tmp_path)
    claim = store.acquire_claim(TASK, worker_id_for(TASK))
    assert claim
    identity = ProcessIdentity(123, 1.0, "boot", "python.exe")
    reading, release_io, release_worker = Event(), Event(), Event()
    errors, created, discarded = [], [], []
    monkeypatch.setattr(parallel, "discard_partial_edits", lambda *args: discarded.append(args))

    class Process:
        pid = identity.pid
        exited = False

        def communicate(self, *, input):
            reading.set()
            assert release_io.wait(5)
            return "partial", ""

        def poll(self):
            return 0 if self.exited else None

    proc = Process()

    def kill(process):
        process.exited = True
        release_io.set()
        return False

    def run():
        control = SimpleNamespace(store=SimpleNamespace(db_path=store.db_path), task_id=TASK, poll=lambda: None)
        try:
            with provider_stop_scope(control):
                execution.communicate_bounded(proc, "", 60)
        except BaseException as exc:  # noqa: BLE001 -- capture the deferred owner outcome for assertions
            errors.append(exc)
        finally:
            assert release_worker.wait(5)  # model a descheduled owner before its durable exception handler

    monkeypatch.setattr(execution, "popen_identity", lambda _: identity)
    monkeypatch.setattr(execution, "kill_process_tree", kill)
    monkeypatch.setattr(parallel, "process_identity", lambda _: None if proc.exited else identity)
    runner = ParallelRunner(store, project, lambda *_: None, concurrency=1, interrupt_grace_seconds=0)
    owner = Thread(target=run)
    runner._threads[TASK] = owner
    runner._lifecycles[TASK] = SimpleNamespace(summary=RunSummary(True, "UNSET", task_id=TASK), events=[])
    snapshot = runner._merged_stop_snapshot

    def late_execution(connection):
        result = snapshot(connection)
        created.append(connection.start_execution(
            task_id=TASK, claim_id=None if claimless_research else claim,
            kind=ExecutionKind.REVIEW if claimless_research else ExecutionKind.IMPLEMENTATION, actor="stand-in",
            pid=identity.pid, process_create_time=identity.create_time, boot_id=identity.boot_id,
            executable=identity.executable,
        ))
        return result

    monkeypatch.setattr(runner, "_merged_stop_snapshot", late_execution)
    owner.start()
    try:
        assert reading.wait(5)
        runner._halt(runner.summary, interrupted=True, reason="fixture interrupt", stop_reason="INTERRUPTED")
        retained = list(store.running_executions())
        assert len(retained) == 1 and retained[0]["id"] == created[0]
        assert retained[0]["result"] == "cleanup_incomplete" and retained[0]["process_create_time"] is None
        assert store.has_active_claim(TASK)
        assert not discarded
        runner._finalize(runner.summary)
        assert store.has_active_claim(TASK)
        assert not discarded
        assert store.latest_candidate(TASK) is None
    finally:
        proc.exited = True
        release_io.set()
        release_worker.set()
        owner.join(5)
        store.close()
    assert not owner.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], OperatorStopRequested)
    assert not errors[0].cleanup_complete


@pytest.mark.parametrize("kind", ["implementation", "review"])
def test_incomplete_owner_fences_execution_before_unregistering(tmp_path, monkeypatch, kind):
    import os

    from test_bounded_execution import TASK, _setup

    from stagemesh.domain import ExecutionKind
    from stagemesh.queue_control import QueueStopControl

    _, store = _setup(tmp_path)
    class Process:
        pid = 123

    proc = Process()
    identity = execution.process_identity(os.getpid()) if kind == "review" else ProcessIdentity(123, 1.0, "boot")
    execution_id = store.start_execution(
        task_id=TASK, claim_id=None, kind=ExecutionKind(kind.upper()), actor="stand-in",
        pid=identity.pid, process_create_time=identity.create_time, boot_id=identity.boot_id,
        executable=identity.executable,
    )
    original_retain = execution.retain_provider_cleanup
    fenced = []

    def retain(*args):
        assert proc in execution._ACTIVE_PROVIDER_PROCESSES
        fenced.append(original_retain(*args))

    def incomplete(*args):
        raise OperatorStopRequested("fixture incomplete", cleanup_complete=False)

    monkeypatch.setattr(execution, "retain_provider_cleanup", retain)
    monkeypatch.setattr(execution, "_communicate_bounded", incomplete)
    try:
        with provider_stop_scope(QueueStopControl(store, TASK)), pytest.raises(OperatorStopRequested):
            execution.communicate_bounded(proc, "", 60)
        assert fenced == [{execution_id}]
        assert proc not in execution._ACTIVE_PROVIDER_PROCESSES
        retained = list(store.running_executions())
        assert len(retained) == 1 and retained[0]["result"] == "cleanup_incomplete"
        assert retained[0]["process_create_time"] is None
    finally:
        store.close()


def test_external_stop_attempts_all_targets_and_publishes_false_if_fencing_fails(monkeypatch):
    class Process:
        def __init__(self, pid):
            self.pid = pid

        def poll(self):
            return None

    processes = [Process(123), Process(456)]
    attempted, fenced = [], []

    def kill(proc):
        attempted.append(proc.pid)
        raise OSError("fixture tree proof lost")

    def retain(identity):
        fenced.append(identity.pid)
        raise OSError("fixture database unavailable")

    monkeypatch.setattr(execution, "popen_identity", lambda proc: ProcessIdentity(proc.pid, 1.0, "boot"))
    monkeypatch.setattr(execution, "kill_process_tree", kill)
    with execution._ACTIVE_LOCK:
        execution._ACTIVE_PROVIDER_PROCESSES.update(processes)
    try:
        with pytest.raises(OperatorStopRequested) as caught:
            execution.kill_active_provider_processes(on_uncertain=retain)
        assert not caught.value.cleanup_complete
        assert sorted(attempted) == sorted(fenced) == [123, 456]
        assert all(execution._PROVIDER_TERMINATIONS[proc].result(timeout=0) is False for proc in processes)
        # A later halt still gets a durable-fencing opportunity after the original failed kill.
        replayed = []
        assert execution.kill_active_provider_processes(on_uncertain=lambda identity: replayed.append(identity.pid)) == 0
        assert sorted(replayed) == [123, 456]
        assert len(attempted) == 2
    finally:
        with execution._ACTIVE_LOCK:
            for proc in processes:
                execution._ACTIVE_PROVIDER_PROCESSES.discard(proc)
                execution._PROVIDER_TERMINATIONS.pop(proc, None)
