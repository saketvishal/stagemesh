from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from stagemesh import continuous_driver, driver_scheduler
from stagemesh.audit import record_audit
from stagemesh.cli import build_parser, main
from stagemesh.continuous_driver import (
    TICK_FINISHED,
    TICK_STARTED,
    DriverConfig,
    DriverConfigError,
    child_owner_path,
    config_path,
    consecutive_failures,
    load_config,
    run_tick,
    save_config,
    status,
)
from stagemesh.driver_scheduler import DriverSchedulerError, WindowsDriverScheduler, driver_command
from stagemesh.execution import kill_process_tree, popen_session_kwargs
from stagemesh.persistence import Store
from stagemesh.process_identity import popen_identity, process_identity


@pytest.fixture(autouse=True)
def no_real_provider_auth_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install tests must not shell out to whatever coding CLIs the developer machine has logged in."""
    from stagemesh import provider_auth

    monkeypatch.setattr(provider_auth, "uses_real_providers", lambda _commands: False)


@pytest.fixture(autouse=True)
def windows_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USERNAME", "owner")
    monkeypatch.setenv("USERDOMAIN", "HOST")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / ".stagemesh").mkdir()
    return tmp_path


@pytest.fixture
def store(project: Path) -> Store:
    result = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    result.migrate()
    yield result
    result.close()


def installed(project: Path, **overrides) -> DriverConfig:
    config = DriverConfig(project=str(project), interval_seconds=60, window_seconds=60, **overrides)
    save_config(project, config)
    return config


def report_path(argv: list[str]) -> Path:
    return Path(argv[argv.index("--report") + 1])


def test_configuration_is_project_bound_and_builds_only_the_existing_run_window_command(project: Path) -> None:
    config = installed(project, auto_merge_ff=True, strict_live=True)

    argv = config.window_argv(project, project / "report.json")

    assert argv[1:6] == ["-m", "stagemesh.cli", "--project", str(project), "run-window"]
    assert "--auto-merge-ff" in argv and "--strict-live" in argv
    assert "--follow-up" not in argv
    assert load_config(project) == config
    with pytest.raises(DriverConfigError, match="another project"):
        config.validate(project / "other")


def test_two_ticks_resume_dependent_work_after_an_intentional_failure_without_reimplementation(
    project: Path, store: Store
) -> None:
    installed(project)
    calls: list[list[str]] = []

    def runner(argv: list[str], cwd: Path, timeout: float) -> int:
        calls.append(argv)
        assert cwd == project and timeout == 1860
        if len(calls) == 1:
            payload = {
                "tasks": [
                    {"task_id": "A", "outcome": "done", "pr_url": "https://example.test/pr/1"},
                    {"task_id": "B", "outcome": "blocked", "stop_reason": "INTENTIONAL_FIXTURE_FAILURE"},
                ],
                "merged_prs": [{"pr": 1}],
                "stop_reason": None,
            }
        else:
            payload = {
                "tasks": [{"task_id": "B", "outcome": "done", "pr_url": "https://example.test/pr/2"}],
                "merged_prs": [{"pr": 2}],
                "stop_reason": None,
            }
        report_path(argv).write_text(json.dumps(payload), encoding="utf-8")
        return 0

    first = run_tick(store, project, runner=runner)
    second = run_tick(store, project, runner=runner)

    assert first.state == second.state == "progress"
    assert len(calls) == 2
    assert all(command.count("run-window") == 1 for command in calls)
    events = [event for event in status(store, project)["latest_tick"].items()]
    assert events


def test_crash_checkpoint_is_recovered_by_the_next_tick_and_recorded(project: Path, store: Store) -> None:
    installed(project)
    record_audit(store, TICK_STARTED, {"tick_id": "crashed-tick", "report": "missing.json"})

    def runner(argv: list[str], cwd: Path, timeout: float) -> int:
        report_path(argv).write_text(json.dumps({"tasks": [], "merged_prs": []}), encoding="utf-8")
        return 0

    result = run_tick(store, project, runner=runner)

    assert result.state == "idle"
    assert result.recovered_interrupted_tick == "crashed-tick"
    current = status(store, project)
    assert current["interrupted_tick"] is None
    assert current["latest_tick"]["recovered_interrupted_tick"] == "crashed-tick"


def test_no_work_is_idle_evidence_not_green_task_success(project: Path, store: Store) -> None:
    installed(project)

    def runner(argv: list[str], cwd: Path, timeout: float) -> int:
        report_path(argv).write_text(
            json.dumps({"tasks": [], "merged_prs": [], "next_command": "nothing to do"}), encoding="utf-8"
        )
        return 0

    result = run_tick(store, project, runner=runner)

    assert result.state == "idle"
    assert "not a completed autonomy task" in result.detail
    assert consecutive_failures(store) == 0


def test_failed_ticks_exhaust_a_bounded_budget_until_explicit_resume(project: Path, store: Store) -> None:
    installed(project, max_consecutive_failures=2)
    for index in range(2):
        record_audit(
            store,
            TICK_FINISHED,
            {"tick_id": f"failed-{index}", "state": "blocked", "exit_code": 2, "detail": "preflight refused"},
        )
    called = False

    def runner(argv: list[str], cwd: Path, timeout: float) -> int:
        nonlocal called
        called = True
        return 0

    result = run_tick(store, project, runner=runner)

    assert result.state == "blocked"
    assert "retry budget exhausted" in result.detail
    assert not called


def test_paused_and_stopped_ticks_never_start_a_window(project: Path, store: Store) -> None:
    for state in ("paused", "stopped"):
        save_config(project, DriverConfig(project=str(project), state=state))
        result = run_tick(store, project, runner=lambda *_: pytest.fail("runner must not be called"))
        assert result.state == state


def test_surviving_run_window_identity_blocks_an_overlapping_tick(project: Path, store: Store) -> None:
    installed(project)
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **popen_session_kwargs(),
    )
    try:
        continuous_driver.save_child_owner(project, popen_identity(process))

        result = run_tick(store, project, runner=lambda *_: pytest.fail("overlapping window started"))

        assert result.state == "busy"  # a running window is neither a failure nor a reason to start another
        assert "still live" in result.detail
        assert child_owner_path(project).is_file()
        assert continuous_driver.consecutive_failures(store) == 0
    finally:
        kill_process_tree(process)
        process.wait(timeout=10)


def test_launch_sentinel_exists_before_process_creation_and_is_cleaned_on_launch_error(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_after_observing_sentinel(*args, **kwargs):
        state, identity = continuous_driver.child_owner_state(project)
        assert state == "LIVE"
        assert identity is not None and identity.pid == os.getpid()
        raise OSError("fixture launch failure")

    monkeypatch.setattr(continuous_driver.subprocess, "Popen", fail_after_observing_sentinel)

    with pytest.raises(OSError, match="fixture launch failure"):
        continuous_driver._default_runner(["python", "run-window"], project, 1)
    assert not child_owner_path(project).exists()


def test_parent_never_overwrites_the_wrappers_registered_run_window_child(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The wrapper can register its real interpreter and child before the parent's Popen returns (Windows stub included)."""
    from dataclasses import asdict

    from stagemesh.domain import ProcessIdentity

    wrapper = ProcessIdentity(pid=111, create_time=1.0, boot_id="boot", executable="python.exe")
    child = ProcessIdentity(pid=222, create_time=2.0, boot_id="boot", executable="python.exe")
    launcher = ProcessIdentity(pid=333, create_time=3.0, boot_id="boot", executable="launcher-stub.exe")

    class ParentInterrupted(Exception):
        pass

    class Launched:
        def communicate(self, *, timeout):
            raw = json.loads(child_owner_path(project).read_text(encoding="utf-8"))
            assert raw == {**asdict(wrapper), "child": asdict(child)}
            raise ParentInterrupted

    def start(*args, **kwargs):
        assert continuous_driver.child_owner_state(project)[0] == "LIVE"  # sentinel precedes any child
        continuous_driver.save_child_owner(project, wrapper, child)
        return Launched()

    monkeypatch.setattr(continuous_driver.subprocess, "Popen", start)
    monkeypatch.setattr(continuous_driver, "popen_identity", lambda _process: launcher)

    with pytest.raises(ParentInterrupted):
        continuous_driver._default_runner(["unused"], project, 1)
    assert json.loads(child_owner_path(project).read_text(encoding="utf-8"))["child"] == asdict(child)


def test_wrapper_startup_failure_releases_only_the_prelaunch_sentinel(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from stagemesh.domain import ProcessIdentity

    class Launched:
        returncode = 1

        def communicate(self, *, timeout):
            return "", "wrapper import failed"

    monkeypatch.setattr(continuous_driver.subprocess, "Popen", lambda *a, **kw: Launched())
    monkeypatch.setattr(continuous_driver, "popen_identity", lambda _p: ProcessIdentity(111, 1.0, "boot", "python.exe"))

    assert continuous_driver._default_runner(["unused"], project, 1) == 1
    assert not child_owner_path(project).exists()


def test_parent_preserves_unconfirmed_child_after_wrapper_exit(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import asdict

    from stagemesh.domain import ProcessIdentity

    wrapper = ProcessIdentity(111, 1.0, "boot", "python.exe")

    class Launched:
        returncode = 1

        def communicate(self, *, timeout):
            return "", "wrapper exited before confirming child cleanup"

    def start(*args, **kwargs):
        continuous_driver.save_child_owner(project, wrapper)
        return Launched()

    monkeypatch.setattr(continuous_driver.subprocess, "Popen", start)
    monkeypatch.setattr(continuous_driver, "popen_identity", lambda _p: wrapper)
    monkeypatch.setattr(continuous_driver, "process_identity", lambda _pid: None)

    assert continuous_driver._default_runner(["unused"], project, 1) == 1
    assert json.loads(child_owner_path(project).read_text(encoding="utf-8")) == asdict(wrapper)
    assert continuous_driver.child_owner_state(project)[0] == "ORPHANED"


def test_concurrent_owner_publications_use_separate_temporary_files(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from stagemesh.domain import ProcessIdentity

    barrier = Barrier(2)
    retry = continuous_driver._retry_file_operation

    def publish_together(operation):
        barrier.wait(timeout=10)
        return retry(operation)

    monkeypatch.setattr(continuous_driver, "_retry_file_operation", publish_together)
    identities = [ProcessIdentity(pid, float(pid), "boot", "python.exe") for pid in (111, 222)]
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = [workers.submit(continuous_driver.save_child_owner, project, identity) for identity in identities]
        for result in results:
            assert result.result(timeout=10) == child_owner_path(project)
    assert json.loads(child_owner_path(project).read_text(encoding="utf-8"))["pid"] in {111, 222}
    assert list(child_owner_path(project).parent.glob("*.tmp")) == []


def test_child_finishes_after_driver_parent_crash_and_next_tick_recovers(project: Path, store: Store) -> None:
    installed(project)
    started = project / "child-started"
    finished = project / "child-finished"
    child = project / "bounded-child.py"
    child.write_text(
        "import pathlib, sys, time\n"
        "pathlib.Path(sys.argv[1]).write_text('started', encoding='utf-8')\n"
        "time.sleep(1.5)\n"
        "pathlib.Path(sys.argv[2]).write_text('finished', encoding='utf-8')\n",
        encoding="utf-8",
    )
    parent = project / "driver-parent.py"
    parent.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "from stagemesh.audit import record_audit\n"
        "from stagemesh.continuous_driver import TICK_STARTED, _default_runner\n"
        "from stagemesh.persistence import Store\n"
        "project = Path(sys.argv[1])\n"
        "store = Store(project / '.stagemesh' / 'stagemesh.sqlite3')\n"
        "store.migrate()\n"
        "record_audit(store, TICK_STARTED, {'tick_id': 'parent-crash'})\n"
        "store.close()\n"
        "raise SystemExit(_default_runner(sys.argv[2:], project, 30))\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    source_root = str(Path(continuous_driver.__file__).resolve().parents[1])
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (source_root, environment.get("PYTHONPATH"))))
    parent_process = subprocess.Popen(
        [sys.executable, str(parent), str(project), sys.executable, str(child), str(started), str(finished)],
        cwd=project,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **popen_session_kwargs(),
    )
    try:
        deadline = time.time() + 10
        while (not started.is_file() or not child_owner_path(project).is_file()) and time.time() < deadline:
            time.sleep(0.05)
        assert started.is_file() and child_owner_path(project).is_file()

        parent_process.kill()
        parent_process.wait(timeout=10)
        deadline = time.time() + 10
        while (not finished.is_file() or child_owner_path(project).exists()) and time.time() < deadline:
            time.sleep(0.05)
        assert finished.is_file()
        assert not child_owner_path(project).exists()

        def idle_runner(argv: list[str], cwd: Path, timeout: float) -> int:
            report_path(argv).write_text(json.dumps({"tasks": [], "merged_prs": []}), encoding="utf-8")
            return 0

        recovered = run_tick(store, project, runner=idle_runner)
        assert recovered.state == "idle"
        assert recovered.recovered_interrupted_tick == "parent-crash"
    finally:
        if parent_process.poll() is None:
            kill_process_tree(parent_process)
            parent_process.wait(timeout=10)


def test_hard_timeout_terminates_a_real_subprocess_tree(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    child_pid = project / "child.pid"
    real_popen = subprocess.Popen

    class ReadinessGatedPopen(real_popen):  # type: ignore[misc, valid-type]
        """Start the hard-timeout clock only after the grandchild is provably alive.

        A fixed short timeout raced interpreter start-up on loaded hosts. The tree-ready marker removes the race
        without lengthening the test and without overriding the caller's timeout: the deadline still fires, but only
        once the tree exists. Only the driver's own `subprocess` binding is replaced, never the process-wide module.
        """

        def communicate(self, input=None, timeout=None):
            ready_deadline = time.monotonic() + 60
            while not child_pid.is_file() or not child_pid.read_text(encoding="utf-8").strip():
                if self.poll() is not None or time.monotonic() > ready_deadline:
                    kill_process_tree(self)
                    raise AssertionError("the test process tree never became ready")
                time.sleep(0.02)
            return super().communicate(input, timeout=timeout)

    class GatedSubprocess:
        Popen = ReadinessGatedPopen

        def __getattr__(self, name: str):
            return getattr(subprocess, name)

    monkeypatch.setattr(continuous_driver, "subprocess", GatedSubprocess())
    script = project / "tree.py"
    script.write_text(
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='utf-8')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    assert continuous_driver._default_runner([sys.executable, str(script), str(child_pid)], project, 1) == 124
    assert child_pid.is_file()
    pid = int(child_pid.read_text(encoding="utf-8"))
    deadline = time.time() + 10
    while process_identity(pid) is not None and time.time() < deadline:
        time.sleep(0.05)

    assert process_identity(pid) is None
    assert not child_owner_path(project).exists()


def test_driver_scheduler_is_project_owned_idempotent_and_reversible(project: Path) -> None:
    calls: list[list[str]] = []
    scheduled = False

    def runner(argv) -> subprocess.CompletedProcess[str]:
        nonlocal scheduled
        command = list(argv)
        calls.append(command)
        if "/Create" in command:
            scheduled = True
        elif "/Delete" in command:
            scheduled = False
        return subprocess.CompletedProcess(command, 0 if scheduled or "/Delete" in command else 1, "", "")

    scheduler = WindowsDriverScheduler(project, runner=runner)
    handle = scheduler.ensure(driver_command(project), 600)
    same = scheduler.ensure(driver_command(project), 600)

    assert handle == same == scheduler.handle
    assert scheduler.definition.is_file()
    definition = scheduler.definition.read_text(encoding="utf-16")
    assert f"<Command>{sys.executable}</Command>" in definition and "driver tick" in definition  # direct exec, no cmd.exe
    assert "--log-file" in definition
    assert calls[0][0:2] == ["schtasks", "/Create"]
    assert calls[0][calls[0].index("/XML") + 1] == str(scheduler.definition)
    assert "/SC" not in calls[0] and "/TR" not in calls[0]
    scheduler.remove(handle)
    assert not scheduler.definition.exists()
    with pytest.raises(DriverSchedulerError, match="another project"):
        scheduler.remove("StageMesh\\driver-not-ours")


def test_scheduler_query_errors_fail_closed_and_cadence_is_exact(project: Path) -> None:
    denied = lambda argv: subprocess.CompletedProcess(argv, 1, "", "ERROR: Access is denied.")
    scheduler = WindowsDriverScheduler(project, runner=denied)

    with pytest.raises(DriverSchedulerError, match="Access is denied"):
        scheduler.exists(scheduler.handle)
    with pytest.raises(DriverSchedulerError, match="whole number of minutes"):
        scheduler.ensure(driver_command(project), 90)
    assert not scheduler.definition.exists()


def test_config_is_written_atomically_inside_the_project(project: Path) -> None:
    save_config(project, DriverConfig(project=str(project)))

    assert config_path(project).is_file()
    assert not config_path(project).with_suffix(".json.tmp").exists()


def test_cli_requires_explicit_provider_acknowledgement_before_persistent_install(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = build_parser().parse_args(["driver", "install"])
    assert args.driver_command == "install"

    code = main(["--project", str(project), "driver", "install"])

    assert code == 2
    assert "--acknowledge-provider-use" in capsys.readouterr().err
    assert not config_path(project).exists()


def test_failed_scheduler_install_leaves_an_inert_recoverable_configuration(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenScheduler:
        handle = "StageMesh\\driver-test"

        def ensure(self, argv, interval_seconds, execution_limit_seconds=None):
            raise DriverSchedulerError("fixture scheduler failure")

    monkeypatch.setattr(driver_scheduler, "default_driver_scheduler", lambda project: BrokenScheduler())

    code = main(
        [
            "--project",
            str(project),
            "driver",
            "install",
            "--acknowledge-provider-use",
            "--interval",
            "1m",
        ]
    )

    assert code == 2
    assert "fixture scheduler failure" in capsys.readouterr().err
    assert load_config(project).state == "paused"


def test_uninstall_removes_only_driver_startup_and_preserves_the_ledger(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed(project)
    removed: list[str] = []

    class RecordingScheduler:
        handle = "StageMesh\\driver-test"

        def exists(self, handle):
            return handle == self.handle

        def remove(self, handle):
            removed.append(handle)

    monkeypatch.setattr(driver_scheduler, "default_driver_scheduler", lambda project: RecordingScheduler())

    code = main(["--project", str(project), "driver", "uninstall", "--reason", "fixture cleanup", "--json"])

    assert code == 0
    assert removed == ["StageMesh\\driver-test"]
    assert not config_path(project).exists()
    assert (project / ".stagemesh" / "stagemesh.sqlite3").is_file()


def test_task_definition_has_startup_recovery_power_and_bounding_behavior(project: Path) -> None:
    from datetime import datetime
    from xml.etree import ElementTree

    from stagemesh.driver_scheduler import TASK_NAMESPACE, build_task_xml

    xml = build_task_xml(
        project / "run.cmd", 600, user_id="HOST\\owner & co", start=datetime(2026, 10, 23, 14, 0, 0), execution_limit_seconds=7200
    )
    root = ElementTree.fromstring(xml.split("\n", 1)[1])
    ns = {"t": TASK_NAMESPACE}

    assert root.find("t:Triggers/t:LogonTrigger/t:UserId", ns).text == "HOST\\owner & co"
    assert root.find("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", ns).text == "PT10M"
    assert root.find("t:Triggers/t:TimeTrigger/t:Repetition/t:StopAtDurationEnd", ns).text == "false"
    assert root.find("t:Triggers/t:TimeTrigger/t:StartBoundary", ns).text == "2026-10-23T14:00:00"
    settings = {child.tag.split("}")[1]: child.text for child in root.find("t:Settings", ns)}
    assert settings["StartWhenAvailable"] == "true"
    assert settings["DisallowStartIfOnBatteries"] == "false" and settings["StopIfGoingOnBatteries"] == "false"
    assert settings["MultipleInstancesPolicy"] == "IgnoreNew"
    assert settings["ExecutionTimeLimit"] == "PT120M"
    principal = root.find("t:Principals/t:Principal", ns)
    assert principal.find("t:RunLevel", ns).text == "LeastPrivilege"
    assert principal.find("t:LogonType", ns).text == "InteractiveToken"
    assert root.find("t:Actions/t:Exec/t:Command", ns).text == str(project / "run.cmd")
    with pytest.raises(DriverSchedulerError):
        build_task_xml(project / "run.cmd", 90, user_id="u", start=datetime(2026, 1, 1))


def test_task_definition_can_exec_a_program_directly_with_escaped_arguments(project: Path) -> None:
    from datetime import datetime
    from xml.etree import ElementTree

    from stagemesh.driver_scheduler import TASK_NAMESPACE, build_task_xml

    xml = build_task_xml(
        Path("C:/Python/python.exe"), 600, user_id="u", start=datetime(2026, 1, 1), arguments='-m stagemesh.cli --project "C:/a & b" driver tick'
    )
    root = ElementTree.fromstring(xml.split("\n", 1)[1])
    exec_node = root.find("t:Actions/t:Exec", {"t": TASK_NAMESPACE})

    assert exec_node.find("t:Arguments", {"t": TASK_NAMESPACE}).text == '-m stagemesh.cli --project "C:/a & b" driver tick'


def test_failed_registration_leaves_no_definition_and_limit_rounds_up(project: Path) -> None:
    from datetime import datetime

    from stagemesh.driver_scheduler import build_task_xml

    failing = lambda argv: subprocess.CompletedProcess(argv, 1, "", "ERROR: Access is denied.")
    scheduler = WindowsDriverScheduler(project, runner=failing)

    with pytest.raises(DriverSchedulerError, match="Access is denied"):
        scheduler.ensure(driver_command(project), 600)
    assert not scheduler.definition.exists() and not scheduler.definition.with_suffix(".xml.tmp").exists()
    assert "<ExecutionTimeLimit>PT2M</ExecutionTimeLimit>" in build_task_xml(
        Path("python.exe"), 600, user_id="u", start=datetime(2026, 1, 1), execution_limit_seconds=119
    )


def test_scheduler_watchdog_outlasts_the_tick_hard_timeout() -> None:
    config = DriverConfig(project="p", window_seconds=5400)

    assert continuous_driver.scheduler_limit_seconds(config) > config.window_seconds + 1800


def test_tick_log_file_receives_the_tick_output(project: Path) -> None:
    from stagemesh.cli import main as cli_main

    save_config(project, DriverConfig(project=str(project), state="paused"))
    log = project / ".stagemesh" / "driver" / "driver.log"

    assert cli_main(["--project", str(project), "driver", "tick", "--log-file", str(log)]) == 0
    assert "driver tick: paused" in log.read_text(encoding="utf-8")


def test_a_dead_recorded_owner_is_cleared_even_when_it_differs_from_the_launched_process(project: Path) -> None:
    """Windows venv launcher stub: the process Popen started is not the process that wrote the owner record."""
    from stagemesh.continuous_driver import clear_dead_child_owner, save_child_owner
    from stagemesh.process_identity import ProcessIdentity

    launched = ProcessIdentity(pid=os.getpid(), create_time=1.0, boot_id="b", executable="launcher-stub.exe")
    dead_real = ProcessIdentity(pid=2**22 + 12345, create_time=2.0, boot_id="b", executable="python.exe")
    save_child_owner(project, dead_real)

    assert not continuous_driver.clear_child_owner(project, launched)  # exact-identity clearing cannot succeed
    assert clear_dead_child_owner(project)
    assert not child_owner_path(project).exists()


def test_a_live_recorded_owner_is_never_cleared_as_dead(project: Path) -> None:
    from stagemesh.continuous_driver import clear_dead_child_owner, save_child_owner

    live = process_identity(os.getpid())
    assert live is not None
    save_child_owner(project, live)

    assert not clear_dead_child_owner(project)
    assert child_owner_path(project).exists()


def test_busy_ticks_do_not_consume_the_failure_budget(project: Path, store: Store) -> None:
    installed(project, max_consecutive_failures=2)
    for index, state in enumerate(["crashed", "busy", "busy", "busy"]):
        record_audit(store, continuous_driver.TICK_FINISHED, {"tick_id": f"t{index}", "state": state, "exit_code": 1, "detail": "x"})

    assert continuous_driver.consecutive_failures(store) == 1


def test_dead_wrapper_with_live_run_window_child_is_not_orphaned(project: Path) -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **popen_session_kwargs(),
    )
    try:
        dead_wrapper = subprocess.Popen([sys.executable, "-c", "pass"])
        dead_identity = popen_identity(dead_wrapper)
        dead_wrapper.wait(timeout=30)
        continuous_driver.save_child_owner(project, dead_identity, popen_identity(child))

        state, _identity = continuous_driver.child_owner_state(project)

        assert state == "LIVE"
        assert continuous_driver.clear_dead_child_owner(project) is False
        assert continuous_driver.release_orphaned_child is not None
        kill_process_tree(child)
        child.wait(timeout=30)
        assert continuous_driver.child_owner_state(project)[0] == "ORPHANED"
        assert continuous_driver.clear_dead_child_owner(project) is True
    finally:
        kill_process_tree(child)


def test_scheduler_existence_does_not_depend_on_message_language(tmp_path: Path) -> None:
    scheduler = WindowsDriverScheduler(tmp_path)
    handle = scheduler.handle
    registered = "\"\\" + handle + "\",\"N/A\",\"Ready\"\n"

    def run(listed_stdout: str, listed_code: int = 0):
        def runner(argv):
            if "/TN" in argv:
                return subprocess.CompletedProcess(argv, 1, "", "FEHLER: Das System kann die Datei nicht finden.")
            return subprocess.CompletedProcess(argv, listed_code, listed_stdout, "" if listed_code == 0 else "boom")

        return WindowsDriverScheduler(tmp_path, runner=runner)

    assert run("").exists(handle) is False
    assert run(registered).exists(handle) is True
    with pytest.raises(DriverSchedulerError):
        run("", listed_code=1).exists(handle)


def test_registration_runner_errors_become_scheduler_errors_and_clean_up(tmp_path: Path) -> None:
    def runner(argv):
        raise subprocess.TimeoutExpired(argv, 60)

    scheduler = WindowsDriverScheduler(tmp_path, runner=runner)
    with pytest.raises(DriverSchedulerError):
        scheduler.ensure([sys.executable, "-m", "stagemesh.cli"], 60)
    assert not scheduler.definition.exists()
    assert not scheduler.definition.with_suffix(".xml.tmp").exists()


def test_local_integration_is_opt_in_and_never_adds_pr_or_merge_flags(project: Path) -> None:
    default = installed(project).window_argv(project, project / "r.json")
    assert "--integration" not in default and "--pr-only" in default
    local = installed(project, integration="local").window_argv(project, project / "r.json")
    assert local[local.index("--integration") + 1] == "local"
    assert "--pr-only" not in local and "--auto-merge-ff" not in local
    with pytest.raises(continuous_driver.DriverConfigError):
        DriverConfig(project=str(project), integration="merge").validate(project)


class MemoryScheduler:
    name = "memory"
    handle = "StageMesh\\driver-memory"

    def __init__(self, *, fail_ensure: bool = False) -> None:
        self.registered = False
        self.fail_ensure = fail_ensure

    def ensure(self, argv, interval_seconds, execution_limit_seconds=None):
        if self.fail_ensure:
            raise DriverSchedulerError("simulated registration failure")
        self.registered = True
        return self.handle

    def remove(self, handle):
        self.registered = False

    def exists(self, handle):
        return self.registered


def drive(project: Path, *args: str) -> int:
    return main(["--project", str(project), "driver", *args])


def test_resume_never_pauses_a_running_driver_whose_window_is_live(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scheduler = MemoryScheduler()
    monkeypatch.setattr(driver_scheduler, "default_driver_scheduler", lambda _project: scheduler)
    installed(project, state="running")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **popen_session_kwargs())
    try:
        continuous_driver.save_child_owner(project, popen_identity(child))

        assert drive(project, "resume") == 2
        assert continuous_driver.load_config(project).state == "running"
    finally:
        kill_process_tree(child)


def test_stop_and_uninstall_remove_the_schedule_even_when_the_config_is_unreadable(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler = MemoryScheduler()
    scheduler.registered = True
    monkeypatch.setattr(driver_scheduler, "default_driver_scheduler", lambda _project: scheduler)
    installed(project)
    config_path(project).write_text("{not json", encoding="utf-8")

    assert drive(project, "stop") == 0
    assert scheduler.registered is False

    scheduler.registered = True
    assert drive(project, "uninstall") == 0
    assert scheduler.registered is False


def test_failed_reinstall_restores_the_previous_running_configuration(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scheduler = MemoryScheduler(fail_ensure=True)
    monkeypatch.setattr(driver_scheduler, "default_driver_scheduler", lambda _project: scheduler)
    before = installed(project, state="running", max_tasks=3)

    assert drive(project, "install", "--acknowledge-provider-use", "--max-tasks", "5") == 2
    assert continuous_driver.load_config(project) == before


def test_tick_recovers_automatically_when_wrapper_and_child_are_both_proven_dead(project: Path, store: Store) -> None:
    installed(project)
    dead = [subprocess.Popen([sys.executable, "-c", "pass"]) for _ in range(2)]
    identities = [popen_identity(proc) for proc in dead]
    for proc in dead:
        proc.wait(timeout=30)
    continuous_driver.save_child_owner(project, identities[0], identities[1])
    started: list[list[str]] = []

    def runner(argv, _project, _timeout):
        started.append(argv)
        report_path(argv).write_text(json.dumps({"selected": [], "tasks": []}), encoding="utf-8")
        return 0

    result = run_tick(store, project, runner=runner)

    assert started and result.state != "blocked"
    assert not child_owner_path(project).exists()
    assert any(event["event_type"] == continuous_driver.OWNER_RECOVERED for event in continuous_driver.latest_events(store))


def test_tick_stays_blocked_for_a_dead_wrapper_with_no_recorded_child(project: Path, store: Store) -> None:
    installed(project)
    wrapper = subprocess.Popen([sys.executable, "-c", "pass"])
    identity = popen_identity(wrapper)
    wrapper.wait(timeout=30)
    continuous_driver.save_child_owner(project, identity)

    result = run_tick(store, project, runner=lambda *_: pytest.fail("started without proof the child is gone"))

    assert result.state == "blocked" and child_owner_path(project).is_file()


def test_driver_child_interrupt_kills_the_window_and_clears_its_record(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from stagemesh import driver_child

    started: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    class InterruptedPopen(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            started.append(self)
            self._interrupt = True

        def wait(self, timeout=None):
            if self._interrupt:
                self._interrupt = False
                raise KeyboardInterrupt
            return super().wait(timeout)

    monkeypatch.setattr(driver_child.subprocess, "Popen", InterruptedPopen)
    with pytest.raises(KeyboardInterrupt):
        driver_child.main([str(project), sys.executable, "-c", "import time; time.sleep(60)"])

    assert started and started[0].poll() is not None  # the window did not survive the interrupt
    assert not child_owner_path(project).exists()


def test_malformed_child_record_is_unknown_not_a_crash(project: Path) -> None:
    child_owner_path(project).parent.mkdir(parents=True, exist_ok=True)
    wrapper = subprocess.Popen([sys.executable, "-c", "pass"])
    identity = popen_identity(wrapper)
    wrapper.wait(timeout=30)
    from dataclasses import asdict

    child_owner_path(project).write_text(json.dumps({**asdict(identity), "child": {"pid": "oops", "create_time": 1}}), encoding="utf-8")

    assert continuous_driver.child_owner_state(project)[0] == "UNKNOWN"
    assert continuous_driver.clear_dead_child_owner(project) is False


def test_scheduler_exists_matches_the_exact_task_name_only(tmp_path: Path) -> None:
    scheduler = WindowsDriverScheduler(tmp_path)
    handle = scheduler.handle

    def runner(argv):
        if "/TN" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "")
        listing = f'"\\\\{handle}-old","N/A","Ready"\n"\\\\Other\\\\{handle}","N/A","Ready"\n'
        return subprocess.CompletedProcess(argv, 0, listing, "")

    assert WindowsDriverScheduler(tmp_path, runner=runner).exists(handle) is False
