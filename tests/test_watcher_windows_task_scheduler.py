"""Task Scheduler adapter tests (SDD-001 section 9.3)."""

from __future__ import annotations

from build_coordinator.watcher.windows_task_scheduler import (
    FakeTaskSchedulerAdapter,
    SchtasksError,
    SchtasksAdapter,
    TaskDefinition,
    stable_task_name,
)


def test_stable_task_name_is_deterministic_and_scoped():
    name_a = stable_task_name("C:/stagemesh-orchestrator", "saketvishal/stagemesh-orchestrator")
    name_b = stable_task_name("C:/stagemesh-orchestrator", "saketvishal/stagemesh-orchestrator")
    name_other = stable_task_name("C:/stagemesh", "saketvishal/stagemesh")
    assert name_a == name_b
    assert name_a != name_other
    assert name_a.startswith("BuildCoordinator-")


def test_stable_task_name_never_embeds_secrets_or_branch_names():
    name = stable_task_name("C:/stagemesh-orchestrator", "saketvishal/stagemesh-orchestrator")
    assert "feature/" not in name
    assert "token" not in name.lower()


def test_task_definition_command_has_no_secrets_in_arguments():
    definition = TaskDefinition(
        task_name="BuildCoordinator-abc123",
        command="C:/venv/python.exe",
        arguments="-m build_coordinator.cli watcher run --foreground --repo saketvishal/stagemesh-orchestrator",
        working_directory="C:/stagemesh-orchestrator",
    )
    for forbidden in ("token", "secret", "password", "authorization", "ghp_"):
        assert forbidden not in definition.arguments.lower()
        assert forbidden not in definition.command.lower()


def test_fake_adapter_install_is_idempotent():
    adapter = FakeTaskSchedulerAdapter()
    definition = TaskDefinition("BuildCoordinator-abc", "python.exe", "watcher run --foreground", "C:/repo")
    adapter.create_or_update(definition)
    adapter.create_or_update(definition)
    assert adapter.task_exists("BuildCoordinator-abc")
    assert adapter.query_definition("BuildCoordinator-abc") == definition


def test_fake_adapter_uninstall_removes_only_named_task():
    adapter = FakeTaskSchedulerAdapter()
    d1 = TaskDefinition("BuildCoordinator-abc", "python.exe", "a", "C:/repo-a")
    d2 = TaskDefinition("BuildCoordinator-def", "python.exe", "b", "C:/repo-b")
    adapter.create_or_update(d1)
    adapter.create_or_update(d2)
    adapter.delete("BuildCoordinator-abc")
    assert not adapter.task_exists("BuildCoordinator-abc")
    assert adapter.task_exists("BuildCoordinator-def")


def test_fake_adapter_uninstall_is_idempotent_when_absent():
    adapter = FakeTaskSchedulerAdapter()
    adapter.delete("BuildCoordinator-never-installed")  # must not raise


def test_fake_adapter_run_requires_install():
    adapter = FakeTaskSchedulerAdapter()
    try:
        adapter.run("BuildCoordinator-not-installed")
        assert False, "expected SchtasksError"
    except SchtasksError:
        pass


def test_fake_adapter_run_marks_running():
    adapter = FakeTaskSchedulerAdapter()
    adapter.create_or_update(TaskDefinition("BuildCoordinator-abc", "python.exe", "x", "C:/repo"))
    assert not adapter.is_running("BuildCoordinator-abc")
    adapter.run("BuildCoordinator-abc")
    assert adapter.is_running("BuildCoordinator-abc")


def test_schtasks_access_denied_is_unknown_not_not_installed(monkeypatch):
    adapter = SchtasksAdapter()

    def fake_run(args):
        class Result:
            returncode = 1
            stdout = ""
            stderr = "ERROR: Access is denied."

        return Result()

    monkeypatch.setattr(adapter, "_run", fake_run)

    status = adapter.query_status("BuildCoordinator-abc")
    assert status.installed is None
    assert status.running is None
    assert status.state == "ACCESS_DENIED"


def test_schtasks_path_query_failure_is_unknown_not_not_installed(monkeypatch):
    adapter = SchtasksAdapter()

    def fake_run(args):
        class Result:
            returncode = 1
            stdout = "ERROR: The system cannot find the path specified."
            stderr = ""

        return Result()

    monkeypatch.setattr(adapter, "_run", fake_run)

    status = adapter.query_status("BuildCoordinator-abc")
    assert status.installed is None
    assert status.running is None
    assert status.state == "QUERY_UNAVAILABLE"


def test_schtasks_not_found_is_not_installed(monkeypatch):
    adapter = SchtasksAdapter()

    def fake_run(args):
        class Result:
            returncode = 1
            stdout = "ERROR: The system cannot find the file specified."
            stderr = ""

        return Result()

    monkeypatch.setattr(adapter, "_run", fake_run)

    status = adapter.query_status("BuildCoordinator-abc")
    assert status.installed is False
    assert status.running is False
    assert status.state == "NOT_FOUND"
