from __future__ import annotations

import json
import threading

import pytest
from test_parallel import Rig, ScriptedExecutor

from stagemesh.audit import record_audit
from stagemesh.concurrency import IntegrationLock
from stagemesh.coordinator import Coordinator
from stagemesh.github import GitHubClient
from stagemesh.parallel import worker_id_for
from stagemesh.serialized_integration import SerializedIntegrator
from stagemesh.task_sources import GitHubOutboundSync, _blocked_marker


class FakeTransport:
    def __init__(self, post_status: int = 201, raises: bool = False, comments: list[dict] | None = None):
        self.requests: list[tuple[str, str, object]] = []
        self.post_status = post_status
        self.raises = raises
        self.comments = comments or []
        self.lock = threading.Lock()

    def request(self, method, path, body=None):
        with self.lock:
            self.requests.append((method, path, body))
        if self.raises:
            raise OSError("network down")
        if method == "GET":
            return 200, {}, list(self.comments) if path.endswith("&page=1") else []
        if method == "POST":
            return self.post_status, {}, {"id": 1}
        return 200, {}, {}

    def calls(self, method: str) -> list[tuple[str, str, object]]:
        return [r for r in self.requests if r[0] == method]


def _github_rig(tmp_path, task_ids, github_ids):
    rig = Rig(tmp_path, task_ids)
    for task_id, number in github_ids.items():
        rig.store.conn.execute("UPDATE tasks SET source='github', source_id=? WHERE id=?", (number, task_id))
    rig.store.conn.commit()
    return rig


def _coordinator(rig, transport, executor=None, cls=Coordinator, target=None):
    client = GitHubClient("o", "r", transport)
    integrator = SerializedIntegrator(rig.ref, False, IntegrationLock(rig.project / ".stagemesh" / "integration.lock"))
    return cls(
        rig.store, rig.project, executor=executor, integrator=integrator, target=target,
        outbound_sync=GitHubOutboundSync(rig.store, client), outbound_sources=frozenset({"github"}),
    )


def _outbound(rig, issue):
    return [
        (row["status"], json.loads(row["payload"]))
        for row in rig.store.conn.execute(
            "SELECT status, payload FROM source_events WHERE source_id=? AND direction='outbound' ORDER BY rowid", (issue,)
        )
    ]


def test_queue_run_path_comments_and_closes_github_task_once_and_skips_local_task(tmp_path):
    rig = _github_rig(tmp_path, ["A", "B"], {"A": "101"})  # B stays a local-only task
    transport = FakeTransport()
    executor = ScriptedExecutor(rig.files)
    integrator = SerializedIntegrator(rig.ref, False, rig.lock, max_rebases=2)

    def make(target, store, task_id):
        client = GitHubClient("o", "r", transport)
        return Coordinator(
            store, rig.project, executor=executor, integrator=integrator, target=target, worker_id=worker_id_for(task_id),
            outbound_sync=GitHubOutboundSync(store, client), outbound_sources=frozenset({"github"}),
        )

    from stagemesh.parallel import ParallelRunner

    summary = ParallelRunner(rig.store, rig.project, make, concurrency=2, poll_seconds=0.05).run()

    assert {t.task_id: t.summary.stop_reason for t in summary.tasks} == {"A": "DONE", "B": "DONE"}
    sha = rig.store.latest_candidate("A")["sha"]
    posts, patches = transport.calls("POST"), transport.calls("PATCH")
    assert len(posts) == 1 and sha in posts[0][2]["body"] and "/issues/101/" in posts[0][1]
    assert len(patches) == 1 and patches[0][1].endswith("/issues/101") and patches[0][2] == {"state": "closed"}
    assert all("/issues/101" in r[1] for r in transport.requests)  # nothing for local-only B
    assert _outbound(rig, "101")[-1][0] == "OK"


def test_done_sync_is_idempotent_across_ticks(tmp_path):
    rig = _github_rig(tmp_path, ["A"], {"A": "101"})
    transport = FakeTransport()
    coord = _coordinator(rig, transport, ScriptedExecutor(rig.files))
    for _ in range(12):
        coord.tick()
    assert rig.store.get_task("A")["status"] == "DONE"
    assert len(transport.calls("POST")) == 1 and len(transport.calls("PATCH")) == 1


def test_enabling_sync_does_not_touch_tasks_that_were_already_done(tmp_path):
    rig = _github_rig(tmp_path, ["A"], {"A": "101"})
    plain = Coordinator(rig.store, rig.project, executor=ScriptedExecutor(rig.files),
                        integrator=SerializedIntegrator(rig.ref, False, rig.lock, max_rebases=2))
    for _ in range(12):
        plain.tick()
    assert rig.store.get_task("A")["status"] == "DONE"
    transport = FakeTransport()
    _coordinator(rig, transport).tick()
    assert transport.requests == [] and _outbound(rig, "101") == []


class _BlockingCoordinator(Coordinator):
    def _advance_task(self, task_id):
        record_audit(self.store, "task.blocked", {"task_id": task_id, "reason": "validation_gate"})
        self.store.block_task(task_id)
        return 1


def test_coordinator_tick_posts_blocked_reason_once_and_skips_local_task(tmp_path):
    rig = _github_rig(tmp_path, ["A", "B"], {"A": "101"})
    transport = FakeTransport()
    coord = _coordinator(rig, transport, cls=_BlockingCoordinator)
    coord.tick()
    coord.tick()
    posts = transport.calls("POST")
    assert len(posts) == 1 and "validation_gate" in posts[0][2]["body"] and "/issues/101/" in posts[0][1]
    assert transport.calls("PATCH") == []  # blocked tasks are commented, never closed or relabelled
    assert rig.store.get_task("A")["status"] == "BLOCKED"
    assert all("/issues/101/" in r[1] for r in transport.requests)


@pytest.mark.parametrize("transport", [FakeTransport(post_status=500), FakeTransport(raises=True)])
def test_sync_failure_is_recorded_and_never_changes_local_done_state(tmp_path, transport):
    rig = _github_rig(tmp_path, ["A"], {"A": "101"})
    coord = _coordinator(rig, transport, ScriptedExecutor(rig.files))
    for _ in range(12):
        coord.tick()
    assert rig.store.get_task("A")["status"] == "DONE" and rig.store.get_task("A")["stage"] == "DONE"
    statuses = [status for status, _ in _outbound(rig, "101")]
    assert statuses and "OK" not in statuses and statuses[0] in {"UNKNOWN", "ERROR"}
    assert len(transport.calls("POST")) <= 1  # failure backoff prevents a retry storm on later ticks


def test_blocked_dedupe_reads_every_comment_page(tmp_path):
    rig = _github_rig(tmp_path, ["A"], {"A": "101"})
    marker = _blocked_marker("validation_gate")
    comments = [{"body": "noise"}] * 100

    class Paged(FakeTransport):
        def request(self, method, path, body=None):
            if method == "GET":
                self.requests.append((method, path, body))
                return 200, {}, comments if path.endswith("&page=1") else [{"body": f"StageMesh blocked\n{marker}"}]
            return super().request(method, path, body)

    transport = Paged()
    GitHubOutboundSync(rig.store, GitHubClient("o", "r", transport)).publish_blocked("101", "validation_gate")
    assert transport.calls("POST") == []
    assert [status for status, _ in _outbound(rig, "101")] == ["OK"]
