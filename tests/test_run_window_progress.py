"""Live run-window progress. Fake hooks write real ledger rows from a worker thread, like a child `continue` process would."""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path

from test_survivor_wave import Rig, make_store

from stagemesh.domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus
from stagemesh.progress import ProgressReporter, with_progress
from stagemesh.survivor_wave import RunResult, TaskRecord


class WatchedStream(io.StringIO):
    """Records every flush and signals when a wanted line has been printed."""

    def __init__(self) -> None:
        super().__init__()
        self.flushes = 0
        self.seen: dict[str, threading.Event] = {}

    def want(self, needle: str) -> threading.Event:
        return self.seen.setdefault(needle, threading.Event())

    def write(self, text: str) -> int:
        written = super().write(text)
        for needle, event in self.seen.items():
            if needle in self.getvalue():
                event.set()
        return written

    def flush(self) -> None:
        self.flushes += 1
        super().flush()


def _long_task(stream: WatchedStream, observed: dict[str, bool]):
    def behave(rig: Rig, task_id: str) -> RunResult:
        store = rig.worker_store()
        try:
            store.add_audit_event("provider.selection", {"task_id": task_id, "stage": "IMPLEMENT", "order": ["grok", "claude"], "verdicts": []})
            run = store.start_execution(task_id=task_id, claim_id=None, kind=ExecutionKind.IMPLEMENTATION)
            # still "implementing": progress must already be on screen, and a heartbeat must follow the silence
            observed["started_shown"] = stream.want("implementation started").wait(10)
            observed["provider_shown"] = stream.want("implementation provider grok").wait(10)
            observed["heartbeat_shown"] = stream.want("implementation still running (provider grok)").wait(10)
            sha = "a" * 40
            store.add_candidate(task_id, sha, "grok", True)
            store.finish_execution(run, ExecutionStatus.SUCCEEDED, sha)
            store.start_execution(task_id=task_id, claim_id=None, kind=ExecutionKind.REVIEW)
            observed["review_start_shown"] = stream.want("review started").wait(10)
            store.add_evidence(task_id, sha, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)
            store.add_evidence(task_id, sha, EvidenceKind.REVIEW, EvidenceStatus.FAILED)
            store.add_evidence(task_id, sha, EvidenceKind.INTEGRATION, EvidenceStatus.PASSED)
            observed["integration_shown"] = stream.want("integration passed").wait(10)
        finally:
            store.close()
        return RunResult("DONE")

    return behave


def test_progress_is_shown_while_a_long_implementation_and_review_are_still_running(tmp_path: Path) -> None:
    store, project = make_store(tmp_path, ["T-1"])
    stream, observed = WatchedStream(), {}
    rig = Rig(project, {"T-1": _long_task(stream, observed)})
    hooks = with_progress(rig.hooks(), project / ".stagemesh" / "stagemesh.sqlite3", ProgressReporter(stream), heartbeat_seconds=0.2, poll_seconds=0.05)

    result = hooks.run_task("T-1", 8)

    assert result.stop_reason == "DONE"
    assert observed == {
        "started_shown": True,
        "provider_shown": True,
        "heartbeat_shown": True,
        "review_start_shown": True,
        "integration_shown": True,
    }
    out = stream.getvalue()
    for expected in ("task T-1: started", "candidate aaaaaaaaaa created by grok", "validation passed", "review failed", "task T-1: finished: DONE"):
        assert expected in out
    assert stream.flushes >= out.count("\n")  # every line was flushed as it was written


def test_pr_opening_is_reported_and_other_hooks_are_untouched(tmp_path: Path) -> None:
    store, project = make_store(tmp_path, ["T-1"])
    stream = WatchedStream()
    rig = Rig(project)
    plain = rig.hooks()
    hooks = with_progress(plain, project / ".stagemesh" / "stagemesh.sqlite3", ProgressReporter(stream))

    url = hooks.open_pr(TaskRecord(task_id="T-1", title="t"))

    assert url == "https://example.invalid/pr/T-1"
    assert "task T-1: opening PR" in stream.getvalue() and f"PR opened {url}" in stream.getvalue()
    assert hooks.advance_baseline is plain.advance_baseline and hooks.target_sha is plain.target_sha
    assert with_progress(rig_without_pr(rig), project / "x.sqlite3", ProgressReporter(stream)).open_pr is None


def rig_without_pr(rig: Rig):
    hooks = rig.hooks()
    hooks.open_pr = None
    return hooks


def test_fallback_heartbeat_uses_execution_actor_not_preferred_provider(tmp_path: Path) -> None:
    from stagemesh.progress import TaskCursor, heartbeat_lines, poll_task

    store, _project = make_store(tmp_path, ["T-1"])
    store.add_audit_event("provider.selection", {"task_id": "T-1", "stage": "IMPLEMENT", "order": ["claude", "grok", "agy"]})
    cursor = TaskCursor(since=0)
    first = store.start_execution(task_id="T-1", claim_id=None, kind=ExecutionKind.IMPLEMENTATION, actor="claude")
    poll_task(store.conn, "T-1", cursor)
    store.finish_execution(first, ExecutionStatus.FAILED, result="no_implementation_change")
    second = store.start_execution(task_id="T-1", claim_id=None, kind=ExecutionKind.IMPLEMENTATION, actor="grok")
    lines = poll_task(store.conn, "T-1", cursor)
    assert any("implementation failed (provider claude)" in line and "no_implementation_change" in line for line in lines)
    assert any("implementation started (provider grok)" in line for line in lines)
    assert all("provider grok" in line for line in heartbeat_lines("T-1", cursor, 1e12))
    store.finish_execution(second, ExecutionStatus.FAILED, result="no_implementation_change")
    store.start_execution(task_id="T-1", claim_id=None, kind=ExecutionKind.IMPLEMENTATION, actor="agy")
    lines = poll_task(store.conn, "T-1", cursor)
    assert any("implementation started (provider agy)" in line for line in lines)
    assert all("provider agy" in line for line in heartbeat_lines("T-1", cursor, 1e12))
    assert poll_task(store.conn, "T-1", cursor) == []
    store.close()


def test_cli_json_stdout_stays_valid_and_progress_is_on_stderr(tmp_path: Path, capsys, monkeypatch) -> None:
    import stagemesh.cli as cli_module
    import stagemesh.run_window as rw
    from test_run_window import FRESH, IN_SYNC, OK_PROVIDERS

    store, project = make_store(tmp_path, ["T-1", "T-2"])
    store.close()
    rig = Rig(project)
    hooks = rig.hooks()
    hooks.repo_state = lambda: IN_SYNC
    monkeypatch.setattr(cli_module, "_wave_hooks", lambda *args, **kwargs: hooks)
    monkeypatch.setattr(cli_module, "_current_branch_ref", lambda project: "refs/heads/main")
    monkeypatch.setattr(rw, "check_runtime", lambda project: FRESH)
    monkeypatch.setattr(rw, "provider_pool_check", OK_PROVIDERS)

    cli_module.main(["--project", str(project), "run-window", "--integration", "local", "--tasks", "T-1", "--json", "--report", str(tmp_path / "r.json")])

    captured = capsys.readouterr()
    report = json.loads(captured.out)  # stdout is only the report
    assert "T-1" in json.dumps(report)
    assert "task T-1: started" in captured.err and "task T-1: finished: DONE" in captured.err

    cli_module.main(["--project", str(project), "run-window", "--integration", "local", "--tasks", "T-2", "--json", "--no-progress", "--report", str(tmp_path / "r2.json")])
    quiet = capsys.readouterr()
    assert json.loads(quiet.out) and quiet.err == "" and rig.runs == ["T-1", "T-2"]
