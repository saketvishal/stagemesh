from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest
from test_parallel import Rig, ScriptedExecutor

from stagemesh.audit import record_audit
from stagemesh.concurrency import IntegrationLock
from stagemesh.coordinator import Coordinator
from stagemesh.delivery_state import PR_MERGED_OBSERVED, record_publication, task_delivery
from stagemesh.domain import Stage
from stagemesh.github import GitHubClient
from stagemesh.outbound_lifecycle import sync_outbound_outcomes
from stagemesh.parallel import worker_id_for
from stagemesh.persistence import Store
from stagemesh.serialized_integration import SerializedIntegrator
from stagemesh.task_sources import BLOCKED_OUTBOUND_LABEL, GitHubOutboundSync, _blocked_marker


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


class MergedPRTransport(FakeTransport):
    def __init__(
        self,
        candidate: str,
        *,
        changed_head: bool = False,
        merged: bool = True,
        merged_by: str = "owner",
        checks: str = "success",
        contain_candidate: bool = True,
        merge_on_base: bool = True,
        state: str = "closed",
        base_ref: str = "main",
        head_repo: str = "o/r",
        label_status: int = 200,
    ):
        super().__init__()
        self.candidate = candidate
        self.head = "a" * 40 if changed_head else candidate
        self.merge = "b" * 40
        self.tip = "c" * 40
        self.merged = merged
        self.merged_by = merged_by
        self.checks = checks
        self.contain_candidate = contain_candidate
        self.merge_on_base = merge_on_base
        self.state = state
        self.base_ref = base_ref
        self.head_repo = head_repo
        self.label_status = label_status

    def request(self, method, path, body=None):
        self.requests.append((method, path, body))
        root = "/repos/o/r"
        if method == "GET" and path == root:
            return 200, {}, {"full_name": "o/r", "default_branch": "main"}
        if method == "GET" and path == f"{root}/pulls/236":
            return 200, {}, {
                "number": 236,
                "state": self.state if self.merged else "open",
                "merged": self.merged,
                "merged_at": "2026-11-23T23:34:12Z" if self.merged else None,
                "merge_commit_sha": self.merge if self.merged else None,
                "merged_by": {"login": self.merged_by},
                "head": {"sha": self.head, "ref": "wave/task-A", "repo": {"full_name": self.head_repo}},
                "base": {"ref": self.base_ref, "repo": {"full_name": "o/r"}},
            }
        if method == "GET" and path == f"{root}/branches/main":
            return 200, {}, {"commit": {"sha": self.tip}}
        if method == "GET" and path.startswith(f"{root}/compare/"):
            pair = path.removeprefix(f"{root}/compare/")
            ancestor, descendant = pair.split("...", 1)
            allowed = {
                (self.candidate, self.head): self.contain_candidate,
                (self.head, self.merge): True,
                (self.merge, self.tip): self.merge_on_base,
            }
            if allowed.get((ancestor, descendant), False):
                return 200, {}, {"status": "ahead", "merge_base_commit": {"sha": ancestor}}
            return 200, {}, {"status": "diverged", "merge_base_commit": {"sha": "d" * 40}}
        if method == "GET" and path == f"{root}/commits/{self.head}/check-runs?per_page=100":
            runs = [] if self.checks == "missing" else [{
                "name": "check",
                "head_sha": self.head,
                "status": "completed",
                "conclusion": self.checks,
            }]
            return 200, {}, {"total_count": len(runs), "check_runs": runs}
        if method == "GET" and path.startswith(f"{root}/issues/101/comments?"):
            return 200, {}, []
        if method == "DELETE":
            return self.label_status, {}, {}
        if method == "POST":
            return 201, {}, {"id": 1}
        if method == "PATCH":
            return 200, {}, {}
        return 404, {}, {"message": "not found"}


def _published_done_task(tmp_path):
    store = Store(tmp_path / "ledger.sqlite3")
    store.migrate()
    task_id = store.upsert_task("task 101", source="github", source_id="101")
    candidate = "1" * 40
    store.add_candidate(task_id, candidate, "codex", durable_handoff=True)
    store.advance_task(task_id, Stage.DONE)
    record_publication(
        store,
        task_id,
        "https://github.com/o/r/pull/236",
        candidate,
        "main",
        repository="o/r",
    )
    return store, task_id, candidate


@pytest.mark.parametrize("changed_head", [False, True])
def test_externally_merged_stage_owned_pr_closes_issue_once(tmp_path, changed_head):
    store, task_id, candidate = _published_done_task(tmp_path)
    transport = MergedPRTransport(candidate, changed_head=changed_head)
    sync = GitHubOutboundSync(
        store,
        GitHubClient("o", "r", transport),
        approver_id="github:owner",
    )

    for _ in range(2):
        sync_outbound_outcomes(
            store, sync, frozenset({"github"}), [store.get_task(task_id)], {}
        )

    assert task_delivery(store, task_id, "o/r").state == "MERGED"
    observed = store.conn.execute(
        "SELECT payload FROM audit_events WHERE event_type=?", (PR_MERGED_OBSERVED,)
    ).fetchall()
    assert len(observed) == 1
    payload = json.loads(observed[0]["payload"])
    assert payload["candidate_sha"] == candidate
    assert payload["merged_head_sha"] == transport.head
    assert payload["head_changed_after_publication"] is changed_head
    check_paths = [path for method, path, _ in transport.requests if method == "GET" and "check-runs" in path]
    assert bool(check_paths) is changed_head
    assert transport.calls("DELETE") == [
        ("DELETE", "/repos/o/r/issues/101/labels/stagemesh%3Atask-blocked", None)
    ]
    assert len(transport.calls("PATCH")) == 1
    assert len([call for call in transport.calls("POST") if call[1].endswith("/comments")]) == 1


@pytest.mark.parametrize(
    "options",
    [
        {"merged": False},
        {"changed_head": True, "merged_by": "someone-else"},
        {"changed_head": True, "checks": "failure"},
        {"changed_head": True, "checks": "missing"},
        {"changed_head": True, "contain_candidate": False},
        {"merge_on_base": False},
        {"state": "open"},
        {"base_ref": "release"},
        {"head_repo": "somebody/fork"},
        {"merged_by": ""},
    ],
)
def test_external_merge_reconciliation_fails_closed(tmp_path, options):
    store, task_id, candidate = _published_done_task(tmp_path)
    transport = MergedPRTransport(candidate, **options)
    sync = GitHubOutboundSync(
        store,
        GitHubClient("o", "r", transport),
        approver_id="github:owner",
    )

    sync_outbound_outcomes(
        store, sync, frozenset({"github"}), [store.get_task(task_id)], {}
    )

    assert task_delivery(store, task_id, "o/r").state != "MERGED"
    assert transport.calls("DELETE") == []
    assert transport.calls("PATCH") == []
    assert store.conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE event_type=?", (PR_MERGED_OBSERVED,)
    ).fetchone()[0] == 0


def test_changed_head_requires_configured_github_approver(tmp_path):
    store, task_id, candidate = _published_done_task(tmp_path)
    transport = MergedPRTransport(candidate, changed_head=True)
    sync = GitHubOutboundSync(store, GitHubClient("o", "r", transport))

    sync_outbound_outcomes(
        store, sync, frozenset({"github"}), [store.get_task(task_id)], {}
    )

    assert task_delivery(store, task_id, "o/r").state == "PUBLISHED"
    assert transport.calls("DELETE") == []
    assert transport.calls("PATCH") == []


def test_merged_issue_is_not_closed_when_blocked_label_cannot_be_removed(tmp_path):
    store, task_id, candidate = _published_done_task(tmp_path)
    transport = MergedPRTransport(candidate, label_status=403)
    sync = GitHubOutboundSync(
        store,
        GitHubClient("o", "r", transport),
        approver_id="github:owner",
    )

    sync_outbound_outcomes(
        store, sync, frozenset({"github"}), [store.get_task(task_id)], {}
    )

    assert task_delivery(store, task_id, "o/r").state == "MERGED"
    assert transport.calls("PATCH") == []
    assert [call for call in transport.calls("POST") if call[1].endswith("/comments")] == []


def test_cli_wires_protected_approval_authority_into_merge_reconciliation(tmp_path):
    from stagemesh.cli import _github_outbound

    store = Store(tmp_path / "ledger.sqlite3")
    store.migrate()
    config = SimpleNamespace(
        task_sources=(SimpleNamespace(name="issues", kind="github"),),
        github=SimpleNamespace(owner="o", repo="r", token="token"),
        approval_authority=SimpleNamespace(approver_id="github:owner"),
    )

    sync, sources = _github_outbound(config, store, dry_run=False)

    assert sources == frozenset({"issues"})
    assert sync.approver_id == "github:owner"


def test_run_window_start_reconciles_merged_pr_without_eligible_coding_work(tmp_path):
    from stagemesh.cli import _reconcile_github_outbound

    store, task_id, candidate = _published_done_task(tmp_path)
    transport = MergedPRTransport(candidate, changed_head=True)
    sync = GitHubOutboundSync(
        store,
        GitHubClient("o", "r", transport),
        approver_id="github:owner",
    )

    _reconcile_github_outbound(store, sync, frozenset({"github"}))

    assert store.get_task(task_id)["status"] == "DONE"
    assert task_delivery(store, task_id, "o/r").state == "MERGED"
    assert len(transport.calls("PATCH")) == 1


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
    comments = [post for post in posts if post[1].endswith("/comments")]
    labels = [post for post in posts if post[1].endswith("/labels")]
    assert len(comments) == 1
    assert "validation_gate" in comments[0][2]["body"] and "/issues/101/" in comments[0][1]
    assert labels == [
        ("POST", "/repos/o/r/issues/101/labels", {"labels": [BLOCKED_OUTBOUND_LABEL]})
    ]
    assert transport.calls("PATCH") == []  # blocked tasks are commented, never closed or relabelled
    assert rig.store.get_task("A")["status"] == "BLOCKED"
    assert all("/issues/101/" in r[1] for r in transport.requests)


def test_open_task_clears_stagemesh_blocked_label_once_and_records_audit(tmp_path):
    rig = _github_rig(tmp_path, ["A"], {"A": "101"})
    transport = FakeTransport()
    sync = GitHubOutboundSync(rig.store, GitHubClient("o", "r", transport))
    sync.publish_blocked("101", "provider unavailable")
    transport.requests.clear()

    sync_outbound_outcomes(
        rig.store, sync, frozenset({"github"}), [rig.store.get_task("A")], {}
    )
    sync_outbound_outcomes(
        rig.store, sync, frozenset({"github"}), [rig.store.get_task("A")], {}
    )

    assert transport.calls("DELETE") == [
        ("DELETE", "/repos/o/r/issues/101/labels/stagemesh%3Atask-blocked", None)
    ]
    assert _outbound(rig, "101")[-1] == (
        "OK", {"label": "OK", "outcome": "UNBLOCKED"}
    )
    audit = [json.loads(row[0]) for row in rig.store.conn.execute(
        "SELECT payload FROM audit_events WHERE event_type='source.outbound' ORDER BY rowid"
    )]
    assert audit[-1]["outcome"] == "UNBLOCKED"


def test_removing_an_already_absent_stagemesh_label_is_success(tmp_path):
    rig = _github_rig(tmp_path, ["A"], {"A": "101"})

    class MissingLabel(FakeTransport):
        def request(self, method, path, body=None):
            self.requests.append((method, path, body))
            if method == "DELETE":
                return 404, {}, {"message": "Label does not exist"}
            return super().request(method, path, body)

    sync = GitHubOutboundSync(rig.store, GitHubClient("o", "r", MissingLabel()))
    event_id = sync.publish_unblocked("101")
    event = rig.store.conn.execute("SELECT status FROM source_events WHERE id=?", (event_id,)).fetchone()
    assert event["status"] == "OK"


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
    assert [post for post in transport.calls("POST") if post[1].endswith("/comments")] == []
    assert [status for status, _ in _outbound(rig, "101")] == ["OK"]
