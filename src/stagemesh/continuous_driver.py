"""Crash-resumable local supervision around the existing bounded ``run-window`` driver.

This module never selects, implements, validates, reviews, publishes or merges work itself. A tick runs the exact configured
``run-window`` command, journals the result in the existing ledger and relies on run-window's coordinator and recovery policies.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .audit import record_audit
from .concurrency import IntegrationLock, IntegrationLockTimeout
from .domain import ProcessIdentity
from .execution import kill_process_tree, popen_session_kwargs
from .persistence import Store
from .process_identity import classify_process, owner_identity, popen_identity, process_identity

SCHEMA_VERSION = 1
CONFIG_NAME = "continuous-driver.json"
CHILD_OWNER_NAME = "active-window.json"
INSTALLED = "driver.installed"
STATE_CHANGED = "driver.state_changed"
TICK_STARTED = "driver.tick_started"
TICK_FINISHED = "driver.tick_finished"
SCHEDULED = "driver.scheduled"
DESCHEDULED = "driver.descheduled"
_STATES = {"running", "paused", "stopped"}


class DriverConfigError(ValueError):
    pass


@dataclass(frozen=True)
class DriverConfig:
    project: str
    state: str = "running"
    interval_seconds: int = 300
    window_seconds: int = 2700
    concurrency: int = 2
    max_tasks: int = 10
    max_steps_per_task: int = 8
    max_consecutive_failures: int = 3
    auto_merge_ff: bool = False
    strict_live: bool = False

    def validate(self, project: Path) -> DriverConfig:
        resolved = str(Path(project).resolve())
        if str(Path(self.project).resolve()) != resolved:
            raise DriverConfigError("driver configuration belongs to another project")
        if self.state not in _STATES:
            raise DriverConfigError(f"unsupported driver state: {self.state}")
        for name in (
            "window_seconds",
            "concurrency",
            "max_tasks",
            "max_steps_per_task",
            "max_consecutive_failures",
        ):
            if int(getattr(self, name)) < 1:
                raise DriverConfigError(f"{name} must be at least 1")
        if self.interval_seconds < 60 or self.interval_seconds % 60:
            raise DriverConfigError("interval_seconds must be an exact whole number of minutes")
        return self

    def window_argv(self, project: Path, report: Path) -> list[str]:
        argv = [
            sys.executable,
            "-m",
            "stagemesh.cli",
            "--project",
            str(Path(project).resolve()),
            "run-window",
            "--duration",
            f"{self.window_seconds}s",
            "--concurrency",
            str(self.concurrency),
            "--max-tasks",
            str(self.max_tasks),
            "--max-steps-per-task",
            str(self.max_steps_per_task),
            "--report",
            str(report),
            "--json",
            "--no-progress",
        ]
        if self.auto_merge_ff:
            argv.append("--auto-merge-ff")
        else:
            argv.append("--pr-only")
        if self.strict_live:
            argv.append("--strict-live")
        return argv


@dataclass(frozen=True)
class DriverTick:
    tick_id: str
    state: str
    exit_code: int | None
    report: str | None
    detail: str
    recovered_interrupted_tick: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


WindowRunner = Callable[[list[str], Path, float], int]


def config_path(project: Path) -> Path:
    return Path(project).resolve() / ".stagemesh" / CONFIG_NAME


def child_owner_path(project: Path) -> Path:
    return Path(project).resolve() / ".stagemesh" / "driver" / CHILD_OWNER_NAME


def save_child_owner(project: Path, identity: ProcessIdentity) -> Path:
    path = child_owner_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(asdict(identity), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def child_owner_state(project: Path) -> tuple[str | None, ProcessIdentity | None]:
    path = child_owner_path(project)
    if not path.is_file():
        return None, None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        identity = ProcessIdentity(
            pid=raw.get("pid"),
            create_time=raw.get("create_time"),
            boot_id=raw.get("boot_id"),
            executable=raw.get("executable"),
        )
    except (AttributeError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return "UNKNOWN", None
    state = classify_process(identity, process_identity(identity.pid))
    return ("ORPHANED" if state == "DEAD" else state), identity


def clear_child_owner(project: Path, identity: ProcessIdentity, *, allow_live: bool = False) -> bool:
    state, saved = child_owner_state(project)
    if saved != identity or (not allow_live and state not in {"ORPHANED", None}):
        return False
    child_owner_path(project).unlink(missing_ok=True)
    return True


def release_orphaned_child(project: Path) -> bool:
    state, identity = child_owner_state(project)
    if state is None:
        return False
    if state != "ORPHANED" or identity is None:
        raise DriverConfigError(
            f"cannot release prior run-window ownership while process identity is {state or 'UNKNOWN'}"
        )
    return clear_child_owner(project, identity)


def save_config(project: Path, config: DriverConfig) -> Path:
    project = Path(project).resolve()
    config.validate(project)
    path = config_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, **asdict(config)}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def load_config(project: Path) -> DriverConfig:
    project = Path(project).resolve()
    path = config_path(project)
    if not path.is_file():
        raise DriverConfigError("local continuous driver is not installed for this project")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DriverConfigError(f"invalid driver configuration: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        raise DriverConfigError("unsupported driver configuration schema")
    fields = {name: raw[name] for name in DriverConfig.__dataclass_fields__ if name in raw}
    try:
        return DriverConfig(**fields).validate(project)
    except (KeyError, TypeError, ValueError) as exc:
        raise DriverConfigError(f"invalid driver configuration: {exc}") from exc


def set_state(project: Path, state: str) -> DriverConfig:
    if state not in _STATES:
        raise DriverConfigError(f"unsupported driver state: {state}")
    current = load_config(project)
    updated = DriverConfig(**{**asdict(current), "state": state})
    save_config(project, updated)
    return updated


def remove_config(project: Path) -> None:
    path = config_path(project)
    if not path.is_file():
        raise DriverConfigError("local continuous driver is not installed for this project")
    path.unlink()


def install(store: Store, project: Path, config: DriverConfig, handle: str) -> None:
    save_config(project, config)
    record_audit(store, INSTALLED, {"project": str(Path(project).resolve()), "config": asdict(config)})
    record_audit(store, SCHEDULED, {"handle": handle, "scheduler": "windows-task-scheduler"})


def record_state(store: Store, state: str, reason: str) -> None:
    record_audit(store, STATE_CHANGED, {"state": state, "reason": reason})


def record_descheduled(store: Store, handle: str) -> None:
    record_audit(store, DESCHEDULED, {"handle": handle})


def latest_events(store: Store, limit: int = 500) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in store.audit_events(limit):
        if not str(row["event_type"]).startswith("driver."):
            continue
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            payload = {}
        events.append({"event_type": row["event_type"], "payload": payload, "created_at": row["created_at"]})
    return events


def interrupted_tick(store: Store) -> str | None:
    finished = {
        str(event["payload"].get("tick_id"))
        for event in latest_events(store)
        if event["event_type"] == TICK_FINISHED
    }
    for event in latest_events(store):
        if event["event_type"] == TICK_STARTED:
            tick_id = str(event["payload"].get("tick_id"))
            if tick_id not in finished:
                return tick_id
    return None


def consecutive_failures(store: Store) -> int:
    count = 0
    for event in latest_events(store):
        if event["event_type"] == STATE_CHANGED and event["payload"].get("state") == "running":
            break
        if event["event_type"] != TICK_FINISHED:
            continue
        state = str(event["payload"].get("state"))
        if state in {"blocked", "crashed", "timeout"}:
            count += 1
            continue
        break
    return count


def status(store: Store, project: Path, *, schedule_exists: bool | None = None) -> dict[str, Any]:
    config = load_config(project)
    events = latest_events(store)
    latest = next((event for event in events if event["event_type"] == TICK_FINISHED), None)
    return {
        "installed": True,
        "state": config.state,
        "schedule_exists": schedule_exists,
        "interrupted_tick": interrupted_tick(store),
        "consecutive_failures": consecutive_failures(store),
        "latest_tick": None if latest is None else latest["payload"],
        "active_window_child": child_owner_state(project)[0],
        "config": asdict(config),
    }


def _default_runner(argv: list[str], project: Path, timeout_seconds: float) -> int:
    wrapped = [sys.executable, "-m", "stagemesh.driver_child", str(project), *argv]
    environment = os.environ.copy()
    source_root = str(Path(__file__).resolve().parents[1])
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (source_root, environment.get("PYTHONPATH"))))
    launch_identity = owner_identity()
    save_child_owner(project, launch_identity)
    try:
        process = subprocess.Popen(
            wrapped,
            cwd=project,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **popen_session_kwargs(),
        )
    except BaseException:
        clear_child_owner(project, launch_identity, allow_live=True)
        raise
    identity = popen_identity(process)
    save_child_owner(project, identity)
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        tree_killed = kill_process_tree(process)
        wrapper_exited = False
        try:
            process.communicate(timeout=5)
            wrapper_exited = classify_process(identity, process_identity(identity.pid)) == "DEAD"
        except subprocess.TimeoutExpired:
            pass
        if tree_killed and wrapper_exited:
            clear_child_owner(project, identity)
        return 124
    clear_child_owner(project, identity)
    if process.returncode and (stderr or stdout):
        print((stderr or stdout)[-1000:], file=sys.stderr)
    return int(process.returncode or 0)


def _read_report(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _classify(exit_code: int, report: dict[str, Any] | None) -> tuple[str, str]:
    if exit_code == 124:
        return "timeout", "run-window exceeded the driver hard timeout; durable task state was preserved"
    if report is None:
        return "crashed", f"run-window exited {exit_code} without a readable report"
    if exit_code != 0:
        detail = str(report.get("stop_reason") or report.get("next_command") or f"run-window exited {exit_code}")
        return "blocked", detail[:500]
    tasks = list(report.get("tasks") or [])
    merged = list(report.get("merged_prs") or [])
    if not tasks and not merged:
        return "idle", "no eligible work; this is an idle observation, not a completed autonomy task"
    return "progress", f"run-window handled {len(tasks)} task(s) and landed {len(merged)} pull request(s)"


def run_tick(
    store: Store,
    project: Path,
    *,
    runner: WindowRunner = _default_runner,
    clock: Callable[[], float] = time.time,
) -> DriverTick:
    project = Path(project).resolve()
    config = load_config(project)
    tick_id = uuid.uuid4().hex
    if config.state != "running":
        return DriverTick(tick_id, config.state, None, None, f"driver is {config.state}")
    failures = consecutive_failures(store)
    if failures >= config.max_consecutive_failures:
        detail = f"bounded retry budget exhausted after {failures} consecutive failed ticks; resume explicitly after fixing the blocker"
        record_audit(store, TICK_FINISHED, {"tick_id": tick_id, "state": "blocked", "exit_code": None, "detail": detail})
        return DriverTick(tick_id, "blocked", None, None, detail)
    lock = IntegrationLock(project / ".stagemesh" / "driver" / "driver.lock", timeout_seconds=0.05)
    try:
        with lock.hold("continuous-driver"):
            child_state, child_identity = child_owner_state(project)
            if child_state in {"LIVE", "UNKNOWN", "ORPHANED"}:
                detail = (
                    "a prior run-window child is still live"
                    if child_state == "LIVE"
                    else (
                        "a prior run-window owner exited without confirming its process tree stopped"
                        if child_state == "ORPHANED"
                        else "a prior run-window child identity cannot be verified"
                    )
                )
                detail += "; refusing overlapping provider work until its identity is proven dead"
                record_audit(
                    store,
                    TICK_FINISHED,
                    {
                        "tick_id": tick_id,
                        "state": "blocked",
                        "exit_code": None,
                        "detail": detail,
                        "child_state": child_state,
                        "child_identity": None if child_identity is None else asdict(child_identity),
                    },
                )
                return DriverTick(tick_id, "blocked", None, None, detail)
            recovered = interrupted_tick(store)
            reports = project / ".stagemesh" / "driver" / "reports"
            reports.mkdir(parents=True, exist_ok=True)
            report_path = reports / f"{tick_id}.json"
            if recovered is not None:
                record_audit(
                    store,
                    TICK_FINISHED,
                    {
                        "tick_id": recovered,
                        "state": "recovered_after_crash",
                        "exit_code": None,
                        "detail": "a later tick acquired the project lock; run-window recovery remains authoritative",
                        "recovered_by_tick": tick_id,
                    },
                )
            record_audit(
                store,
                TICK_STARTED,
                {
                    "tick_id": tick_id,
                    "started_at": clock(),
                    "recovered_interrupted_tick": recovered,
                    "report": str(report_path),
                },
            )
            exit_code = runner(config.window_argv(project, report_path), project, config.window_seconds + 1800)
            report = _read_report(report_path)
            state, detail = _classify(exit_code, report)
            record_audit(
                store,
                TICK_FINISHED,
                {
                    "tick_id": tick_id,
                    "state": state,
                    "exit_code": exit_code,
                    "detail": detail,
                    "report": str(report_path),
                    "finished_at": clock(),
                    "recovered_interrupted_tick": recovered,
                },
            )
            return DriverTick(tick_id, state, exit_code, str(report_path), detail, recovered)
    except IntegrationLockTimeout:
        return DriverTick(tick_id, "busy", None, None, "another local driver tick owns this project")
