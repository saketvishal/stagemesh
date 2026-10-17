"""Live progress for `run-window`: one flushed line per ledger event of each task, plus a heartbeat while a step runs.

Each task runs in a child `continue --json` process whose output is captured, so progress is read from the ledger the child writes
(executions, candidates, evidence, provider events) by a read-only poller per running task. It never calls a provider or writes to
the ledger. Lines go to stderr so the report (human or `--json`) on stdout stays exactly as it was.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import IO, Any

DEFAULT_HEARTBEAT_SECONDS = 60.0
POLL_SECONDS = 2.0

_STAGE_LABEL = {"IMPLEMENT": "implementation", "IMPLEMENTATION": "implementation", "REVIEW": "review", "VALIDATE": "validation", "VALIDATION": "validation"}


class ProgressReporter:
    """Thread-safe line printer that flushes after every line."""

    def __init__(self, stream: IO[str] | None = None, clock: Callable[[], float] = time.time):
        self._stream = stream
        self._clock = clock
        self._lock = threading.Lock()
        self._started = clock()

    def line(self, text: str) -> None:
        stamp = _duration(self._clock() - self._started)
        with self._lock:
            stream = self._stream or sys.stderr
            try:
                stream.write(f"[{stamp}] {text}\n")
                stream.flush()
            except (OSError, ValueError):  # a closed terminal must not kill the window
                pass


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}h{minutes:02d}m{secs:02d}s" if hours else f"{minutes}m{secs:02d}s"


def _loads(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


@dataclass
class TaskCursor:
    """What a poller has already reported, so each ledger row prints once."""

    since: float
    seen: set[str] = field(default_factory=set)
    running: dict[str, tuple[str, float, str]] = field(default_factory=dict)  # execution id -> (kind, started_at, actor)
    provider: dict[str, str] = field(default_factory=dict)  # stage label -> latest known provider


def poll_task(conn: sqlite3.Connection, task_id: str, cursor: TaskCursor) -> list[str]:
    """New progress lines for the task since the last poll, oldest first. Read-only."""
    events: list[tuple[float, str]] = []

    def fresh(key: str) -> bool:
        if key in cursor.seen:
            return False
        cursor.seen.add(key)
        return True

    for row in conn.execute(
        "SELECT id, event_type, payload, created_at FROM audit_events WHERE event_type IN ('provider.selection', 'provider.used', 'workspace.setup_failed') "
        "AND created_at>=? ORDER BY created_at, rowid",
        (cursor.since,),
    ):
        payload = _loads(row["payload"])
        if payload.get("task_id") != task_id or not fresh(row["id"]):
            continue
        label = _STAGE_LABEL.get(str(payload.get("stage")), str(payload.get("stage") or "").lower())
        if row["event_type"] == "workspace.setup_failed":
            events.append((row["created_at"], f"task {task_id}: environment setup failed; provider not invoked: {payload.get('reason')}"))
        elif row["event_type"] == "provider.selection":
            order = payload.get("order") or []
            if order:
                cursor.provider[label] = str(order[0])
                events.append((row["created_at"], f"task {task_id}: {label} provider {order[0]} (order: {' > '.join(map(str, order))})"))
        else:
            provider = str(payload.get("provider") or "")
            cursor.provider[label] = provider
            events.append((row["created_at"], f"task {task_id}: {label} by {provider}: {payload.get('outcome')}"))
    for row in conn.execute(
        "SELECT id, kind, status, actor, result, started_at, updated_at FROM executions WHERE task_id=? AND started_at>=? ORDER BY started_at, rowid",
        (task_id, cursor.since),
    ):
        label = _STAGE_LABEL.get(row["kind"], row["kind"].lower())
        actor = str(row["actor"] or cursor.provider.get(label) or "")
        by = f" (provider {actor})" if actor else ""
        if fresh(f"start:{row['id']}"):
            events.append((row["started_at"], f"task {task_id}: {label} started{by}"))
            cursor.running[row["id"]] = (row["kind"], row["started_at"], actor)
        if row["status"] != "RUNNING" and row["id"] in cursor.running:
            del cursor.running[row["id"]]
            if row["kind"] == "IMPLEMENTATION" and fresh(f"end:{row['id']}"):
                took = _duration(row["updated_at"] - row["started_at"])
                reason = f": {row['result']}" if row["result"] else ""
                events.append((row["updated_at"], f"task {task_id}: {label} {str(row['status']).lower()}{by} after {took}{reason}"))
    for row in conn.execute("SELECT id, sha, produced_by, created_at FROM candidates WHERE task_id=? AND created_at>=? ORDER BY created_at", (task_id, cursor.since)):
        if fresh(row["id"]):
            events.append((row["created_at"], f"task {task_id}: candidate {str(row['sha'])[:10]} created by {row['produced_by']}"))
    for row in conn.execute(
        "SELECT id, candidate_sha, kind, status, created_at FROM evidence WHERE task_id=? AND created_at>=? AND kind!='HANDOFF' ORDER BY created_at, rowid",
        (task_id, cursor.since),
    ):
        if fresh(row["id"]):
            label = {"VALIDATION": "validation", "REVIEW": "review", "INTEGRATION": "integration"}.get(row["kind"], row["kind"].lower())
            events.append((row["created_at"], f"task {task_id}: {label} {str(row['status']).lower()} on {str(row['candidate_sha'])[:10]}"))
    events.sort(key=lambda item: item[0])
    return [text for _, text in events]


def heartbeat_lines(task_id: str, cursor: TaskCursor, now: float) -> list[str]:
    lines = []
    for kind, started, actor in cursor.running.values():
        label = _STAGE_LABEL.get(kind, kind.lower())
        provider = actor
        by = f" (provider {provider})" if provider else ""
        lines.append(f"task {task_id}: {label} still running{by}, {_duration(now - started)} so far")
    return lines


class TaskMonitor:
    """Prints a task's ledger events as they appear, and a heartbeat while an implementation/review/validation step runs."""

    def __init__(
        self,
        db_path: Path,
        task_id: str,
        reporter: ProgressReporter,
        *,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
        poll_seconds: float = POLL_SECONDS,
        clock: Callable[[], float] = time.time,
    ):
        self.db_path, self.task_id, self.reporter = Path(db_path), task_id, reporter
        self.heartbeat_seconds, self.poll_seconds, self.clock = heartbeat_seconds, poll_seconds, clock
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"progress-{task_id}", daemon=True)
        self._cursor = TaskCursor(since=clock())

    def __enter__(self) -> TaskMonitor:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=self.poll_seconds + 5)

    def _run(self) -> None:
        try:
            conn = sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error:
            return
        last_output = self.clock()
        try:
            while True:
                stopping = self._stop.is_set()
                try:
                    lines = poll_task(conn, self.task_id, self._cursor)
                except sqlite3.Error:  # a locked or mid-write ledger: try again next poll
                    lines = []
                for text in lines:
                    self.reporter.line(text)
                now = self.clock()
                if lines:
                    last_output = now
                elif self.heartbeat_seconds > 0 and now - last_output >= self.heartbeat_seconds:
                    for text in heartbeat_lines(self.task_id, self._cursor, now):
                        self.reporter.line(text)
                    last_output = now
                if stopping:
                    return
                self._stop.wait(self.poll_seconds)
        finally:
            conn.close()


def with_progress(hooks: Any, db_path: Path, reporter: ProgressReporter, *, heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS, poll_seconds: float = POLL_SECONDS) -> Any:
    """Copy of the wave hooks whose task runner and PR opener report progress; everything else is untouched."""
    run_task, open_pr = hooks.run_task, hooks.open_pr

    def run(task_id: str, max_steps: int) -> Any:
        reporter.line(f"task {task_id}: started (up to {max_steps} steps)")
        with TaskMonitor(db_path, task_id, reporter, heartbeat_seconds=heartbeat_seconds, poll_seconds=poll_seconds):
            result = run_task(task_id, max_steps)
        reporter.line(f"task {task_id}: finished: {result.stop_reason}")
        return result

    def pr(record: Any) -> str:
        reporter.line(f"task {record.task_id}: opening PR")
        try:
            url = open_pr(record)
        except Exception as exc:
            reporter.line(f"task {record.task_id}: PR open failed: {exc}")
            raise
        reporter.line(f"task {record.task_id}: PR opened {url}")
        return url

    setter = getattr(open_pr, "set_deadline", None)
    if callable(setter):
        pr.set_deadline = setter
    retarget = getattr(hooks, "retarget", None)
    return replace(
        hooks, run_task=run, open_pr=None if open_pr is None else pr,
        retarget=(lambda ref: with_progress(
            retarget(ref), db_path, reporter,
            heartbeat_seconds=heartbeat_seconds, poll_seconds=poll_seconds,
        )) if callable(retarget) else None,
    )
