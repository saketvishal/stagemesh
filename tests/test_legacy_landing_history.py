"""Read-only recovery of completed legacy landings; no live providers or GitHub."""
from dataclasses import replace

import pytest
from autonomy_support import TASK, git

from stagemesh.autonomy.dependencies import PRState
from stagemesh import fast_forward as ff
from test_fast_forward import rig


@pytest.mark.parametrize("problem", [None, "diverged", "wrong_base", "wrong_merge_base", "missing", "http_error"])
def test_completed_legacy_history_with_unfetched_remote_tip(tmp_path, monkeypatch, problem):
    repo, remote, base, sha, store, _ci, adapter, pr = rig(tmp_path, monkeypatch)
    store.add_audit_event("window.fast_forward_push_intent", {"pr": 7, "candidate_sha": sha, "base_sha": base})
    store.add_audit_event("window.fast_forward_merged", {"pr": 7, "candidate_sha": sha, "task_id": TASK})
    tip = "f" * 40
    monkeypatch.setattr(adapter, "get", lambda number: replace(pr, state=PRState.MERGED, merge_commit_sha=sha))
    monkeypatch.setattr(adapter, "remote_tip", lambda *args, **kwargs: tip)
    calls = []
    def request(method, path):
        calls.append((method, path))
        data = {"status": "diverged" if problem == "diverged" else "ahead",
                "base_commit": {"sha": base if problem == "wrong_base" else sha},
                "merge_base_commit": {"sha": base if problem == "wrong_merge_base" else sha}}
        return (503 if problem == "http_error" else 200), ({} if problem == "missing" else data)
    monkeypatch.setattr(adapter, "_request", request)
    before = store.conn.execute("SELECT count(*) FROM audit_events").fetchone()[0]
    merger = ff.make_merger(store, repo, "refs/heads/main", ("web",))
    if problem is None:
        assert merger.pending_records() == []
    else:
        with pytest.raises(RuntimeError, match="legacy fast-forward intent"):
            merger.pending_records()
    assert calls == [("GET", f"/compare/{sha}...{tip}?per_page=1")]
    assert store.conn.execute("SELECT count(*) FROM audit_events").fetchone()[0] == before
    assert git(repo, "rev-parse", "main") == git(remote, "rev-parse", "main") == base


def test_local_completed_history_needs_no_comparison_request(tmp_path, monkeypatch):
    repo, _remote, base, sha, _store, _ci, adapter, _pr = rig(tmp_path, monkeypatch)
    monkeypatch.setattr(adapter, "_request", lambda *args: pytest.fail("local history should not need GitHub"))
    assert adapter.historical_ancestor(base, sha)
    assert not adapter.historical_ancestor(sha, base)
    assert not adapter.historical_ancestor(sha, "not-a-sha")
