# ruff: noqa: PLW1510, BLE001, B023, I001, RUF100
"""Real Windows Task Scheduler acceptance for the local continuous driver's schedule.

Creates uniquely named TEMPORARY tasks, exercises registration, XML round trip, on-demand start, overlap prevention,
end-of-task process-tree termination, restart and the execution time limit, and ALWAYS deletes them. Intended for a
disposable CI runner or a machine whose owner authorized a temporary task. Evidence is written as JSON and printed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

from stagemesh.driver_scheduler import TASK_NAMESPACE, WindowsDriverScheduler, build_task_xml, current_user_id

WORKER = r'''
import os, subprocess, sys, time
from datetime import datetime
log, seconds = sys.argv[1], float(sys.argv[2])
def note(text):
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(f"{datetime.now().astimezone().isoformat()} {text}\n")
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
note(f"start pid={os.getpid()} grandchild={child.pid}")
time.sleep(seconds)
note(f"end pid={os.getpid()}")
child.kill()
'''

evidence: list[dict] = []


def record(step: str, **details) -> None:
    entry = {"t": datetime.now().astimezone().isoformat(), "step": step, **details}
    evidence.append(entry)
    print(json.dumps(entry), flush=True)


def schtasks(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, timeout=60)


def alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in out


def started_pids(log: Path) -> list[tuple[int, int]]:
    pairs = []
    if log.is_file():
        for line in log.read_text(encoding="utf-8").splitlines():
            if " start pid=" in line:
                parts = dict(item.split("=") for item in line.split()[1:] if "=" in item)
                pairs.append((int(parts["pid"]), int(parts["grandchild"])))
    return pairs


def wait_for(predicate, seconds: float, interval: float = 1.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def python_and_cmd_table() -> list[list]:
    script = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -in 'python.exe','cmd.exe' } | "
              "ForEach-Object { '{0}|{1}|{2}|{3}' -f $_.ProcessId,$_.ParentProcessId,$_.Name,$_.CreationDate.ToString('s') }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=60).stdout
    return [line.split("|") for line in out.splitlines() if line.count("|") == 3]


def kill_leftovers(root: Path) -> None:
    """Kill only processes whose command line points into this run's own temp directory (never by a remembered PID)."""
    script = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -ne 'powershell.exe' -and $_.CommandLine -and $_.CommandLine.Contains('"
              + str(root).replace("'", "''") + "') -and $_.ProcessId -ne $PID } | ForEach-Object { $_.ProcessId }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=60).stdout
    for line in out.split():
        if line.isdigit() and int(line) != os.getpid():
            subprocess.run(["taskkill", "/F", "/T", "/PID", line], check=False, capture_output=True)


def sweep_stale_temp_tasks() -> list[str]:
    """Remove temporary tasks left by an earlier killed run. Only names this script creates (StageMesh\\accept-*) match."""
    listed = schtasks("/Query", "/FO", "CSV", "/NH")
    swept = []
    for line in listed.stdout.splitlines():
        name = line.split(",")[0].strip('"')
        if name.startswith("\\StageMesh\\accept-"):
            schtasks("/Delete", "/TN", name.lstrip("\\"), "/F")
            swept.append(name)
    return swept


def task_state(name: str) -> str:
    out = schtasks("/Query", "/TN", name, "/FO", "LIST", "/V")
    for line in out.stdout.splitlines():
        if line.strip().startswith("Status:"):
            return line.split(":", 1)[1].strip()
    return f"unknown(rc={out.returncode})"


def run() -> int:
    if sys.platform != "win32":
        print("this acceptance script needs Windows Task Scheduler", file=sys.stderr)
        return 2
    if os.environ.get("STAGEMESH_ACCEPT_TEMP_TASKS") != "1":
        print("refusing to create temporary scheduled tasks: set STAGEMESH_ACCEPT_TEMP_TASKS=1 to authorize this run", file=sys.stderr)
        return 2
    root = Path(tempfile.mkdtemp(prefix="sm-accept-")).resolve()
    record("swept_stale_temp_tasks", names=sweep_stale_temp_tasks())
    project = root / "project"
    (project / ".stagemesh").mkdir(parents=True)
    worker = root / "worker.py"
    worker.write_text(WORKER, encoding="utf-8")
    created: list[WindowsDriverScheduler] = []
    failures: list[str] = []

    def check(condition: bool, label: str, **details) -> None:
        record(label, ok=bool(condition), **details)
        if not condition:
            failures.append(label)

    def install(suffix: str, seconds: int, limit: int) -> tuple[WindowsDriverScheduler, Path]:
        log = root / f"{suffix}.log"
        scheduler = WindowsDriverScheduler(
            project / suffix, task_name=f"StageMesh\\accept-{uuid.uuid4().hex[:10]}", execution_limit_seconds=limit
        )
        (project / suffix / ".stagemesh").mkdir(parents=True, exist_ok=True)
        created.append(scheduler)
        handle = scheduler.ensure([sys.executable, str(worker), str(log), str(seconds)], 600)
        record("created", task=handle, python=sys.version.split()[0], limit_seconds=limit)
        return scheduler, log

    try:
        record("environment", platform=sys.platform, python=sys.version, executable=sys.executable)
        scheduler, log = install("main", seconds=300, limit=7200)
        name = scheduler.handle
        check(scheduler.exists(name), "registered_task_exists", task=name)

        xml_out = schtasks("/Query", "/TN", name, "/XML")
        check(xml_out.returncode == 0, "xml_round_trip_query", rc=xml_out.returncode)
        if xml_out.returncode == 0:
            root_xml = ElementTree.fromstring(xml_out.stdout.split("?>", 1)[-1].strip())
            ns = {"t": TASK_NAMESPACE}
            settings = {c.tag.split("}")[1]: c.text for c in root_xml.find("t:Settings", ns)}
            check(root_xml.find("t:Triggers/t:LogonTrigger", ns) is not None, "logon_trigger_registered")
            check(root_xml.find("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", ns).text == "PT10M", "repetition_registered")
            check(settings.get("StartWhenAvailable") == "true", "start_when_available_registered")
            check(settings.get("DisallowStartIfOnBatteries") == "false", "battery_start_allowed_registered")
            check(settings.get("StopIfGoingOnBatteries") == "false", "battery_stop_disabled_registered")
            check(settings.get("MultipleInstancesPolicy") == "IgnoreNew", "ignore_new_registered")
            check(settings.get("ExecutionTimeLimit") in {"PT2H", "PT120M", "PT7200S"}, "time_limit_registered", value=settings.get("ExecutionTimeLimit"))

        record("run_on_demand", rc=schtasks("/Run", "/TN", name).returncode)
        first = wait_for(lambda: started_pids(log), 120)
        check(bool(first), "scheduler_started_the_task", pids=first, status=task_state(name))
        if first:
            check(task_state(name).lower() == "running", "status_running", status=task_state(name))
            run_again = schtasks("/Run", "/TN", name)
            time.sleep(15)
            check(len(started_pids(log)) == 1, "overlap_prevented_ignore_new", rc=run_again.returncode, starts=len(started_pids(log)))
            record("process_table_while_running", table=python_and_cmd_table())
            ended = schtasks("/End", "/TN", name)
            time.sleep(10)
            record("process_table_after_end", end_rc=ended.returncode, status=task_state(name), table=python_and_cmd_table(),
                   worker_alive=alive(first[0][0]), grandchild_alive=alive(first[0][1]))
            schtasks("/End", "/TN", name)
            kill_leftovers(root)

        # Controlled variants: how the action is launched decides whether Task Scheduler's end/limit reaches the tree.
        for variant in ("direct", "cmd"):
            for kind, limit_seconds in (("end", 7200), ("limit", 60)):
                label = f"{variant}-{kind}"
                task_log = root / f"{label}.log"
                scheduler = WindowsDriverScheduler(
                    project / label, task_name=f"StageMesh\\accept-{uuid.uuid4().hex[:10]}", execution_limit_seconds=limit_seconds
                )
                (project / label / ".stagemesh").mkdir(parents=True, exist_ok=True)
                created.append(scheduler)
                worker_argv = [sys.executable, str(worker), str(task_log), "900"]
                if variant == "direct":  # the production path: the tick (here a stand-in worker) is the task's own process
                    scheduler.ensure(worker_argv, 600, limit_seconds)
                    created_rc = 0
                else:  # comparison only: the pre-XML shape, where cmd.exe is the task's process
                    launcher = root / f"{label}.cmd"
                    launcher.write_text(f"@echo off\r\n{subprocess.list2cmdline(worker_argv)}\r\n", encoding="utf-8")
                    xml = build_task_xml(launcher, 600, user_id=current_user_id(), start=datetime.now().replace(microsecond=0),
                                         execution_limit_seconds=limit_seconds)
                    definition = root / f"{label}.xml"
                    definition.write_text(xml, encoding="utf-16")
                    created_rc = schtasks("/Create", "/TN", scheduler.handle, "/XML", str(definition), "/F").returncode
                record("variant_created", variant=label, task=scheduler.handle, rc=created_rc)
                schtasks("/Run", "/TN", scheduler.handle)
                pids = wait_for(lambda: started_pids(task_log), 120)
                if not pids:
                    check(False, f"{label}_started")
                    continue
                worker_pid, grandchild_pid = pids[0]
                before = python_and_cmd_table()
                cmd_pid = next((int(r[1]) for r in before if int(r[0]) == worker_pid), None) if variant == "cmd" else None
                primary = (lambda: not alive(cmd_pid)) if variant == "cmd" and cmd_pid else (lambda: not alive(worker_pid))
                began = time.monotonic()
                if kind == "end":
                    schtasks("/End", "/TN", scheduler.handle)
                gone = wait_for(primary, 40 if kind == "end" else 330, interval=2)
                # Demonstrated on real Windows: Task Scheduler's End and ExecutionTimeLimit terminate ONLY the task's own
                # process (the action), never its descendants. Descendant cleanup is therefore the driver's job.
                check(bool(gone), f"{label}_terminates_the_task_primary_process", primary=cmd_pid or worker_pid,
                      seconds=round(time.monotonic() - began, 1), status=task_state(scheduler.handle))
                record(f"{label}_descendants_after_scheduler_termination", worker_alive=alive(worker_pid),
                       grandchild_alive=alive(grandchild_pid), table_before=before, table_after=python_and_cmd_table())
                schtasks("/End", "/TN", scheduler.handle)
                kill_leftovers(root)
    except Exception as exc:  # recorded, never raised past cleanup
        record("unexpected_error", error=repr(exc))
        failures.append("unexpected_error")
    finally:
        for scheduler in created:
            try:
                scheduler.remove(scheduler.handle)
                record("removed", task=scheduler.handle, exists_after=scheduler.exists(scheduler.handle))
            except Exception as exc:
                record("removal_failed", task=scheduler.handle, error=repr(exc))
                failures.append("removal_failed")
                schtasks("/Delete", "/TN", scheduler.handle, "/F")
        remaining = [s.handle for s in created if schtasks("/Query", "/TN", s.handle).returncode == 0]
        record("cleanup_verified", remaining=remaining)
        if remaining:
            failures.append("tasks_remain")
        for log in root.glob("*.log"):
            record("worker_log", file=log.name, lines=log.read_text(encoding="utf-8").splitlines())
        out = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "evidence.json"
        out.write_text(json.dumps({"failures": failures, "evidence": evidence}, indent=2), encoding="utf-8")
    print("FAILURES:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
