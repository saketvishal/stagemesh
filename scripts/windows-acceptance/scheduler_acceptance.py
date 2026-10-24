"""Real Windows Task Scheduler acceptance for the local continuous driver's schedule.

Creates uniquely named TEMPORARY tasks, exercises registration, XML round trip, on-demand start, overlap prevention,
end-of-task process-tree termination, restart and the execution time limit, and ALWAYS deletes them. Intended for a
disposable CI runner or a machine whose owner authorized a temporary task. Evidence is written as JSON and printed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

from stagemesh.driver_scheduler import TASK_NAMESPACE, WindowsDriverScheduler

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


def task_state(name: str) -> str:
    out = schtasks("/Query", "/TN", name, "/FO", "LIST", "/V")
    for line in out.stdout.splitlines():
        if line.strip().startswith("Status:"):
            return line.split(":", 1)[1].strip()
    return f"unknown(rc={out.returncode})"


def run() -> int:
    root = Path(tempfile.mkdtemp(prefix="sm-accept-"))
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
            worker_pid, grandchild_pid = first[0]
            check(task_state(name).lower() == "running", "status_running", status=task_state(name))
            run_again = schtasks("/Run", "/TN", name)
            time.sleep(15)
            check(len(started_pids(log)) == 1, "overlap_prevented_ignore_new", rc=run_again.returncode, starts=len(started_pids(log)))
            ended = schtasks("/End", "/TN", name)
            gone = wait_for(lambda: not alive(worker_pid) and not alive(grandchild_pid), 30)
            check(bool(gone), "end_terminates_process_tree", end_rc=ended.returncode, worker=worker_pid, grandchild=grandchild_pid,
                  worker_alive=alive(worker_pid), grandchild_alive=alive(grandchild_pid))
            record("restart", rc=schtasks("/Run", "/TN", name).returncode)
            second = wait_for(lambda: len(started_pids(log)) >= 2 and started_pids(log), 120)
            check(bool(second) and second[1][0] != worker_pid, "restart_starts_a_new_process", pids=second)
            schtasks("/End", "/TN", name)
            if second:
                wait_for(lambda: not alive(second[1][0]) and not alive(second[1][1]), 30)

        # Execution time limit: Task Scheduler itself must terminate a task that outlives its limit.
        limited, limited_log = install("limited", seconds=600, limit=60)
        schtasks("/Run", "/TN", limited.handle)
        started = wait_for(lambda: started_pids(limited_log), 120)
        check(bool(started), "limited_task_started", pids=started)
        if started:
            pids = started[0]
            terminated = wait_for(lambda: not alive(pids[0]) and not alive(pids[1]), 240, interval=2)
            check(bool(terminated), "execution_time_limit_terminates_tree", worker=pids[0], grandchild=pids[1],
                  worker_alive=alive(pids[0]), grandchild_alive=alive(pids[1]))
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
