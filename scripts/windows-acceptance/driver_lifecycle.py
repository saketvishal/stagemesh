"""Disposable-project acceptance of the local continuous driver with MOCK providers only.

Windows mode (default) installs the real driver through its own `driver install` into a throw-away project, so Task Scheduler
itself launches the ticks. `--direct` runs ticks in-process-style (`driver tick`) for a Linux dry run of the same phases.
No real provider is configured: every provider name resolves to scripts/windows-acceptance/mock_provider.py, and the script
refuses to start if anything else would answer. Nothing outside the temp project and its one uniquely named task is touched.

Phases: install -> repeated scheduled ticks complete tasks -> pause (no provider calls) -> resume -> overlap prevention ->
whole-tree kill and automatic recovery -> stop -> uninstall. Evidence is written as JSON.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MOCK = HERE / "mock_provider.py"
CAPS = {"codex": "IMPLEMENT", "grok": "IMPLEMENT", "claude": "REVIEW", "agy": "REVIEW"}
EVIDENCE: list[dict] = []


def note(text: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


def record(step: str, **details) -> None:
    EVIDENCE.append({"step": step, "at": time.time(), **details})
    note(f"{step} {json.dumps(details, default=str)[:300]}")


def cli(project: Path, *args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "stagemesh.cli", "--project", str(project), *args],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def git(project: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=project, capture_output=True, text=True, check=True).stdout.strip()


def calls(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_providers(project: Path, calls_file: Path, *, sleep: float = 0.0) -> None:
    base = f'"{sys.executable}" "{MOCK}" --calls "{calls_file}"' + (f" --sleep {sleep}" if sleep else "")
    config = {"providers": {
        name: {"command": base + (" --role review" if stage == "REVIEW" else " --role implement"), "capabilities": [stage]}
        for name, stage in CAPS.items()
    }}
    (project / ".stagemesh" / "config.json").write_text(json.dumps(config), encoding="utf-8")


def assert_mock_only(project: Path) -> None:
    from stagemesh.config import load_config
    from stagemesh.providers import adapters_from_config

    leaked = sorted(key for key in os.environ if key.startswith("STAGEMESH_") and key.endswith("_CMD"))
    if leaked:
        raise SystemExit(f"refusing to run: provider command overrides are set in the environment: {leaked}")
    adapters = adapters_from_config(load_config(project))
    offenders = [a.name for a in adapters for command in a.all_commands() if str(MOCK) not in " ".join(command)]
    if offenders or not adapters:
        raise SystemExit(f"refusing to run: non-mock provider commands resolved: {offenders}")


def make_project(root: Path, task_count: int) -> tuple[Path, Path]:
    project = root / "project"
    project.mkdir()
    git(project, "init", "-q", "-b", "main")
    # Identity for THIS disposable repository only (StageMesh itself never writes git identity).
    git(project, "config", "user.name", "Acceptance Owner")
    git(project, "config", "user.email", "acceptance-owner@example.test")
    (project / "README.md").write_text("disposable acceptance project\n", encoding="utf-8")
    git(project, "add", "README.md")
    git(project, "commit", "-q", "-m", "init")
    if cli(project, "init").returncode != 0:
        raise SystemExit("stagemesh init failed")
    calls_file = root / "mock-calls.jsonl"
    write_providers(project, calls_file)
    from stagemesh.persistence import Store

    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    (project / ".stagemesh" / "contracts").mkdir(exist_ok=True)
    for number in range(1, task_count + 1):
        task = store.upsert_task(f"mock task {number}", source_id=f"MOCK-{number}", project=str(project))
        (project / ".stagemesh" / "contracts" / f"{task}.json").write_text(json.dumps({
            "objective": "write mock-output.txt",
            "allowed_files": ["mock-output.txt"],
            "required_tests": [{"name": "smoke", "command": [sys.executable, "-c", "pass"]}],
        }), encoding="utf-8")
    store.close()
    return project, calls_file


def tasks_done(project: Path) -> list[str]:
    from stagemesh.persistence import Store

    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    try:
        return sorted(r["id"] for r in store.conn.execute("SELECT id FROM tasks WHERE status='DONE'"))
    finally:
        store.close()


def ticks(project: Path) -> list[dict]:
    from stagemesh import continuous_driver
    from stagemesh.persistence import Store

    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    try:
        events = continuous_driver.latest_events(store, 1000)
    finally:
        store.close()
    return list(reversed(events))


def finished(project: Path) -> list[dict]:
    return [e["payload"] for e in ticks(project) if e["event_type"] == "driver.tick_finished"]


def wait_for(predicate, seconds: float, label: str, interval: float = 5.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    value = predicate()
    if not value:
        record(f"TIMEOUT {label}")
    return value


def project_processes(project: Path) -> list[tuple[int, str]]:
    """(pid, commandline) of processes whose command line names this project; Windows via CIM, else /proc."""
    needle = str(project)
    found: list[tuple[int, str]] = []
    if sys.platform == "win32":
        script = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.Contains('"
                  + needle.replace("'", "''") + "') } | ForEach-Object { '{0}|{1}' -f $_.ProcessId,$_.CommandLine }")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=60).stdout
        for line in out.splitlines():
            pid, _, command = line.partition("|")
            if pid.strip().isdigit() and int(pid) != os.getpid():
                found.append((int(pid), command))
    else:
        for entry in Path("/proc").iterdir():
            if entry.name.isdigit() and int(entry.name) != os.getpid():
                try:
                    command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
                except OSError:
                    continue
                if needle in command:
                    found.append((int(entry.name), command))
    return found


def kill_tree(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    else:
        import signal
        try:
            os.kill(pid, signal.SIGKILL)  # never killpg: the dry-run victims share this script's process group
        except OSError:
            pass


def schedule_exists(project: Path) -> bool:
    from stagemesh.driver_scheduler import WindowsDriverScheduler

    scheduler = WindowsDriverScheduler(project)
    return scheduler.exists(scheduler.handle)


def run(direct: bool, tasks: int) -> int:
    from stagemesh import continuous_driver

    if os.environ.get("STAGEMESH_ACCEPT_DRIVER") != "1":
        print("refusing to install a driver: set STAGEMESH_ACCEPT_DRIVER=1 to authorize this disposable run", file=sys.stderr)
        return 2
    if not direct and sys.platform != "win32":
        print("scheduler mode needs Windows; use --direct for a dry run", file=sys.stderr)
        return 2
    root = Path(tempfile.mkdtemp(prefix="sm-driver-accept-"))
    failures: list[str] = []

    def check(ok: bool, label: str, **details) -> None:
        record(("PASS " if ok else "FAIL ") + label, **details)
        if not ok:
            failures.append(label)

    project, calls_file = make_project(root, tasks)
    interval = 60
    try:
        assert_mock_only(project)
        record("project_ready", project=str(project), mock_only=True, tasks=tasks, mode="direct" if direct else "task-scheduler")
        if direct:
            from stagemesh import continuous_driver

            continuous_driver.save_config(project, continuous_driver.DriverConfig(
                project=str(project), interval_seconds=interval, window_seconds=90, concurrency=1, max_tasks=1,
                max_steps_per_task=8, integration="local"))
            installed_ok = True
        else:
            result = cli(project, "driver", "install", "--acknowledge-provider-use", "--interval", "1m", "--window-duration", "90s",
                         "--concurrency", "1", "--max-tasks", "1", "--max-steps-per-task", "8", "--integration", "local", "--json")
            installed_ok = result.returncode == 0
            record("install", rc=result.returncode, out=result.stdout[-400:], err=result.stderr[-400:])
        check(installed_ok, "driver_installed")
        if not installed_ok:
            return 1
        if not direct:
            check(schedule_exists(project), "schedule_registered")

        def tick_now() -> None:
            if direct:
                cli(project, "driver", "tick", "--json", timeout=300)

        # 1. repeated execution completes tasks without anyone running anything
        def done_at_least(n: int):
            tick_now()
            return len(tasks_done(project)) >= n
        target = min(2, tasks)
        reached = wait_for(lambda: done_at_least(target), 420 if not direct else 60, "repeated_ticks", 20 if not direct else 1)
        check(bool(reached), "repeated_execution_completes_tasks", done=tasks_done(project), ticks=len(finished(project)))
        stages = [c["stage"] for c in calls(calls_file)]
        check("implement" in stages and "review" in stages, "mock_provider_ran_implement_and_independent_review", stages=stages)

        # 2. pause: no provider is reached while paused
        before = len(calls(calls_file))
        result = cli(project, "driver", "pause", "--reason", "acceptance pause", "--json")
        check(result.returncode == 0, "pause_command", out=result.stdout[-200:], err=result.stderr[-200:])
        time.sleep(1 if direct else 3 * interval + 20)
        if direct:
            tick_now()
        check(len(calls(calls_file)) == before, "paused_driver_makes_no_provider_calls", before=before, after=len(calls(calls_file)))

        # 3. resume: work continues
        done_before = len(tasks_done(project))
        if direct:  # `driver resume` re-registers the OS schedule, which a dry run does not have
            from stagemesh.persistence import Store

            resume_store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
            from stagemesh.parallel import request_queue_control

            continuous_driver.set_state(project, "running")
            request_queue_control(resume_store, "resumed", "acceptance resume")
            continuous_driver.record_state(resume_store, "running", "acceptance resume")
            resume_store.close()
            result = subprocess.CompletedProcess([], 0, "", "")
        else:
            result = cli(project, "driver", "resume", "--reason", "acceptance resume", "--json")
        check(result.returncode == 0, "resume_command", out=result.stdout[-200:], err=result.stderr[-200:])
        if tasks > done_before:
            reached = wait_for(lambda: (tick_now() or len(tasks_done(project)) > done_before), 420 if not direct else 60, "resume_progress", 20 if not direct else 1)
            check(bool(reached), "resume_continues_work", done=tasks_done(project))

        # 4. overlap prevention with a slow provider: exactly one run-window at a time
        write_providers(project, calls_file, sleep=70)
        remaining = tasks - len(tasks_done(project))
        if remaining > 0:
            overlap_calls = len(calls(calls_file))
            if direct:
                import threading
                threading.Thread(target=tick_now, daemon=True).start()
                time.sleep(15)
                second = cli(project, "driver", "tick", "--json", timeout=60)
                record("direct_second_tick", out=second.stdout[-300:])
            peak = 0
            deadline = time.time() + (200 if not direct else 40)
            seen_window = False
            while time.time() < deadline:
                windows = [p for p in project_processes(project) if "run-window" in p[1] and "driver_child" not in p[1]]
                if windows:
                    seen_window = True
                peak = max(peak, len(windows))
                if seen_window and not windows:
                    break
                time.sleep(3)
            check(seen_window and peak <= 1, "no_overlapping_run_windows", peak_concurrent_windows=peak)
            check(len([c for c in calls(calls_file)[overlap_calls:] if c["stage"] == "implement"]) <= 2, "bounded_provider_calls_during_overlap")

        # 5. crash recovery: kill the entire driver process tree mid-window, the next tick recovers by itself
        remaining = tasks - len(tasks_done(project))
        if remaining > 0:
            write_providers(project, calls_file, sleep=40)
            started = len(calls(calls_file))
            if direct:
                import threading
                threading.Thread(target=tick_now, daemon=True).start()
            wait_for(lambda: len(calls(calls_file)) > started, 240, "window_started_for_crash", 2)
            victims = project_processes(project)
            record("killing_process_tree", processes=[(p, c[:120]) for p, c in victims])
            for pid, _command in victims:
                kill_tree(pid)
            time.sleep(3)
            check(not [p for p in project_processes(project) if "run-window" in p[1]], "tree_fully_killed")
            write_providers(project, calls_file)
            done_before = len(tasks_done(project))
            reached = wait_for(lambda: (tick_now() or len(tasks_done(project)) > done_before), 420 if not direct else 90, "recovery_progress", 20 if not direct else 1)
            check(bool(reached), "recovered_after_crash_and_continued", done=tasks_done(project))
            owner = project / ".stagemesh" / "driver" / "active-window.json"
            check(not owner.exists() or reached, "no_stale_owner_blocks_progress", owner_exists=owner.exists())
        states = [f.get("state") for f in finished(project)]
        record("tick_history", states=states, done=tasks_done(project), calls=len(calls(calls_file)))
        done = tasks_done(project)
        check(len(done) == len(set(done)), "no_task_completed_twice", done=done)

        # 6. stop then uninstall
        stop = cli(project, "driver", "stop", "--terminate-running", "--reason", "acceptance stop", "--json")
        check(stop.returncode == 0, "stop_command", out=stop.stdout[-200:], err=stop.stderr[-200:])
        if not direct:
            check(not schedule_exists(project), "schedule_removed_by_stop")
        time.sleep(2)
        after_stop = len(calls(calls_file))
        time.sleep(1 if direct else interval + 15)
        check(len(calls(calls_file)) == after_stop, "stopped_driver_makes_no_provider_calls")
        un = cli(project, "driver", "uninstall", "--reason", "acceptance uninstall", "--json")
        check(un.returncode == 0, "uninstall_command", out=un.stdout[-300:], err=un.stderr[-200:])
        check((project / ".stagemesh" / "stagemesh.sqlite3").is_file(), "ledger_preserved_after_uninstall")
        check(not [p for p in project_processes(project)], "no_project_processes_remain", left=project_processes(project)[:3])
    finally:
        if not direct:
            try:
                from stagemesh.driver_scheduler import WindowsDriverScheduler

                scheduler = WindowsDriverScheduler(project)
                if scheduler.exists(scheduler.handle):
                    scheduler.remove(scheduler.handle)
                    record("cleanup_removed_schedule")
                record("cleanup_verified", schedule_exists=scheduler.exists(scheduler.handle))
            except Exception as exc:  # noqa: BLE001 -- cleanup must report, never mask the result
                record("cleanup_error", error=str(exc))
        for pid, _command in project_processes(project):
            kill_tree(pid)
        out = os.environ.get("STAGEMESH_ACCEPT_EVIDENCE")
        payload = {"failures": failures, "evidence": EVIDENCE}
        if out:
            Path(out).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        log = project / ".stagemesh" / "driver" / "driver.log"
        if log.is_file() and out:
            shutil.copyfile(log, Path(out).with_suffix(".driver.log"))
    note(f"failures: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--direct", action="store_true")
    parser.add_argument("--tasks", type=int, default=5)
    arguments = parser.parse_args()
    raise SystemExit(run(arguments.direct, arguments.tasks))
