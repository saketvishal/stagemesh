from __future__ import annotations

import json
import os
import sys

import pytest

from stagemesh.candidate_environments import _is_link, prepare_candidate_environments
from stagemesh.domain import ExecutionKind, ExecutionStatus
from stagemesh.git import GitWorkspace
from stagemesh.persistence import Store
from stagemesh.providers import RuntimeCommandAdapter
from stagemesh.workspace_guard import owned_workspace
from stagemesh.workspaces import prepare_task_workspace


def node_repo(tmp_path):
    project = tmp_path / "operator"
    project.mkdir()
    git = GitWorkspace(project)
    git.init_if_needed()
    git.run("config", "user.name", "Fixture Owner")
    git.run("config", "user.email", "owner@example.org")
    (project / ".gitignore").write_text("node_modules/\n.stagemesh/\n")
    web = project / "apps/web"
    web.mkdir(parents=True)
    (web / "package.json").write_text('{"name":"fixture","version":"1.0.0"}')
    (web / "package-lock.json").write_text('{"lockfileVersion":3}')
    git.commit_all("fixture")
    modules = web / "node_modules"
    (modules / ".bin").mkdir(parents=True)
    (modules / "dependency.txt").write_text("operator original")
    (modules / ".bin/tsc.cmd").write_text("@echo off\necho private typecheck\n")
    config = project / ".stagemesh/validation-environments.json"
    config.parent.mkdir(exist_ok=True)
    config.write_text(json.dumps({"schema_version": 1, "node": {"apps/web/node_modules": "apps/web/node_modules"}}))
    return project, git, modules, config


def test_reused_workspace_is_provisioned_under_lease_and_repeatable(tmp_path):
    project, git, modules, config = node_repo(tmp_path)
    text = config.read_text()
    config.unlink()
    target = prepare_task_workspace(project, "existing")
    config.write_text(text)
    store = Store(project / ".stagemesh/stagemesh.sqlite3")
    store.migrate()
    store.upsert_task("existing", source_id="existing")
    try:
        for _ in range(2):
            with owned_workspace(store, project, "existing", ExecutionKind.IMPLEMENTATION) as lease:
                assert lease.path == target
                assert (target / "apps/web/node_modules/.bin/tsc.cmd").exists()
        private = target / "apps/web/node_modules/dependency.txt"
        assert not os.path.samefile(private, modules / "dependency.txt")
        private.write_text("candidate mutation")
        assert (modules / "dependency.txt").read_text() == "operator original"
        assert git.run("status", "--porcelain").stdout == ""
    finally:
        store.close()


def test_setup_failure_stops_before_provider_and_can_be_recovered(tmp_path):
    project, _git, _modules, config = node_repo(tmp_path)
    config.write_text('{"schema_version":1,"node":{"apps/web/node_modules":"missing/node_modules"}}')
    store = Store(project / ".stagemesh/stagemesh.sqlite3")
    store.migrate()
    store.upsert_task("blocked", source_id="blocked")
    sentinel = tmp_path / "provider-called"
    adapter = RuntimeCommandAdapter("fixture", (sys.executable, "-c", f"from pathlib import Path; Path({str(sentinel)!r}).touch()"))
    try:
        result = adapter.execute(store, "blocked", None, project)
        assert result.status == ExecutionStatus.FAILED
        assert result.failure_reason.startswith("candidate_environment_unavailable:")
        assert not sentinel.exists()
        assert list(store.running_executions()) == []
        assert not store.has_active_claim("blocked")
        config.write_text(json.dumps({"schema_version":1,"node":{"apps/web/node_modules":"apps/web/node_modules"}}))
        with owned_workspace(store, project, "blocked", ExecutionKind.IMPLEMENTATION) as lease:
            assert (lease.path / "apps/web/node_modules/.bin/tsc.cmd").exists()
    finally:
        store.close()


def test_python_environments_can_be_prepared_twice(tmp_path):
    from test_candidate_environments import _fixture

    project, checkout, _packages = _fixture(tmp_path)
    prepare_candidate_environments(project, checkout)
    prepare_candidate_environments(project, checkout)


def test_declared_legacy_python_aliases_become_private_without_source_changes(tmp_path):
    from test_candidate_environments import _fixture

    project, checkout, packages = _fixture(tmp_path)
    source = project / "backend/.venv"
    before = (packages / "dependency.py").read_bytes()
    for relative in (".venv", "backend/.venv"):
        target = checkout / relative
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(source), str(target))
        else:
            target.symlink_to(source, target_is_directory=True)
    prepare_candidate_environments(project, checkout)
    prepare_candidate_environments(project, checkout)
    for relative in (".venv", "backend/.venv"):
        target = checkout / relative
        assert not _is_link(target)
        assert target.resolve().is_relative_to(checkout)
        assert (target / ".stagemesh-environment.json").is_file()
    assert (packages / "dependency.py").read_bytes() == before
    assert not (source / ".stagemesh-environment.json").exists()
    assert not (project / "pth-ran").exists()


def test_unrelated_python_alias_is_preserved_and_refused(tmp_path):
    from test_candidate_environments import _fixture

    project, checkout, _packages = _fixture(tmp_path)
    outside = tmp_path / "unrelated"
    outside.mkdir()
    (outside / "keep").write_text("untouched")
    target = checkout / ".venv"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(outside), str(target))
    else:
        target.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        prepare_candidate_environments(project, checkout)
    assert _is_link(target)
    assert (outside / "keep").read_text() == "untouched"


@pytest.mark.skipif(os.name != "nt", reason="Real Windows directory sharing semantics")
def test_windows_setup_succeeds_with_directory_handle_that_denies_rename(tmp_path, monkeypatch):
    import ctypes
    from ctypes import wintypes

    import stagemesh.candidate_environments as environments

    project, _git, modules, _config = node_repo(tmp_path)
    checkout = prepare_task_workspace(project, "windows-open-directory")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handles = []
    copytree = environments.shutil.copytree

    def copy_with_reader(src, dst, *args, **kwargs):
        copied = copytree(src, dst, *args, **kwargs)
        if os.path.samefile(src, modules):
            handle = kernel.CreateFileW(str(dst), 0x80000000, 3, None, 3, 0x02000000, None)
            assert handle != ctypes.c_void_p(-1).value, ctypes.get_last_error()
            handles.append(handle)
            # Negative control: this actual Windows handle forbids the old operation.
            with pytest.raises(PermissionError):
                os.rename(dst, str(dst) + "-renamed")
        return copied

    monkeypatch.setattr(environments.shutil, "copytree", copy_with_reader)
    try:
        prepare_candidate_environments(project, checkout)
        prepare_candidate_environments(project, checkout)
        target = checkout / "apps/web/node_modules"
        assert (target / ".bin/tsc.cmd").is_file()
        assert (target / ".stagemesh-environment.json").is_file()
        assert not os.path.samefile(target / "dependency.txt", modules / "dependency.txt")
    finally:
        for handle in handles:
            kernel.CloseHandle(handle)


def test_failed_node_copy_leaves_no_ready_environment_and_can_retry(tmp_path, monkeypatch):
    import stagemesh.candidate_environments as environments

    project, _git, modules, _config = node_repo(tmp_path)
    checkout = prepare_task_workspace(project, "copy-failed")
    copytree = environments.shutil.copytree
    def fail(src, dst, *args, **kwargs):
        from pathlib import Path
        (Path(dst) / "partial").write_text("incomplete")
        raise PermissionError("fixture copy denied")
    monkeypatch.setattr(environments.shutil, "copytree", fail)
    with pytest.raises(PermissionError, match="copy denied"):
        prepare_candidate_environments(project, checkout)
    assert not (checkout / "apps/web/node_modules").exists()
    assert (modules / "dependency.txt").read_text() == "operator original"
    monkeypatch.setattr(environments.shutil, "copytree", copytree)
    prepare_candidate_environments(project, checkout)
    assert (checkout / "apps/web/node_modules/.stagemesh-environment.json").is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows publishes directly under its workspace lease")
def test_windows_interruption_cannot_inherit_source_readiness_marker(tmp_path):
    import subprocess

    from stagemesh.candidate_environments import _marker

    project, _git, modules, _config = node_repo(tmp_path)
    checkout = prepare_task_workspace(project, "interrupted-copy")
    marker = _marker(modules, checkout / "apps/web", ("package.json", "package-lock.json", "npm-shrinkwrap.json"))
    (modules / ".stagemesh-environment.json").write_text(json.dumps(marker))
    code = (
        "import os; from pathlib import Path; import stagemesh.candidate_environments as e\n"
        "original=e.shutil.copytree\n"
        "def interrupted(*args,**kwargs):\n"
        "    original(*args,**kwargs)\n"
        f"    if Path(args[0]) == Path({str(modules)!r}):\n"
        "        os._exit(23)\n"
        "e.shutil.copytree=interrupted\n"
        f"e.prepare_candidate_environments(Path({str(project)!r}),Path({str(checkout)!r}))\n"
    )
    result = subprocess.run([sys.executable, "-c", code], timeout=30, capture_output=True)
    assert result.returncode == 23, result.stderr
    target = checkout / "apps/web/node_modules"
    assert target.is_dir()
    assert not (target / ".stagemesh-environment.json").exists()
    with pytest.raises(ValueError, match="existing"):
        prepare_candidate_environments(project, checkout)
    assert json.loads((modules / ".stagemesh-environment.json").read_text()) == marker


def test_pool_reports_setup_failure_without_provider_use_or_fallback(tmp_path):
    from test_provider_pool import TASK, Rig

    from stagemesh.progress import TaskCursor, poll_task
    from stagemesh.provider_pool import PooledExecutor

    rig = Rig(tmp_path, {"first": "ok", "second": "ok"})
    mapping = rig.project / ".stagemesh/validation-environments.json"
    mapping.write_text('{"schema_version":1,"node":{"node_modules":"missing/node_modules"}}')
    try:
        result = PooledExecutor(rig.pool).run(rig.store, TASK, None, rig.project)
        assert result.status == ExecutionStatus.FAILED
        assert result.failure_reason.startswith("candidate_environment_unavailable:")
        assert not result.capacity_failure
        rows = rig.store.conn.execute('select event_type,payload from audit_events').fetchall()
        types = [row["event_type"] for row in rows]
        assert "provider.used" not in types
        assert "provider.failure" not in types
        assert "provider.no_progress" not in types
        events = [json.loads(row["payload"]) for row in rows if row["event_type"] == "workspace.setup_failed"]
        assert len(events) == 1
        assert events[0]["provider_invoked"] is False
        assert list(rig.store.running_executions()) == []
        assert not rig.store.has_active_claim(TASK)
        cursor = TaskCursor(since=0)
        lines = poll_task(rig.store.conn, TASK, cursor)
        assert any("environment setup failed; provider not invoked:" in line for line in lines)
        assert not any("implementation by" in line for line in lines)
        assert poll_task(rig.store.conn, TASK, cursor) == []
        from stagemesh.survivor_wave import RunResult, infrastructure_signature
        assert infrastructure_signature(RunResult("NO_PROGRESS", result.failure_reason)) == "message:candidate_environment_unavailable:"
    finally:
        rig.store.close()


def test_python_reuse_checks_current_interpreter(tmp_path, monkeypatch):
    from test_candidate_environments import _fixture

    import stagemesh.candidate_environments as environments

    project, checkout, _packages = _fixture(tmp_path)
    prepare_candidate_environments(project, checkout)
    original = environments._probe_python
    def probe(python):
        result = original(python)
        if python.is_relative_to(checkout):
            result["version"] = [3, 0, 0]
        return result
    monkeypatch.setattr(environments, "_probe_python", probe)
    with pytest.raises(ValueError, match="interpreter differs"):
        prepare_candidate_environments(project, checkout)


def test_python_marker_update_does_not_write_through_hardlink(tmp_path):
    from test_candidate_environments import _fixture

    project, checkout, _packages = _fixture(tmp_path)
    prepare_candidate_environments(project, checkout)
    outside = tmp_path / "operator-record.json"
    outside.write_text("operator original")
    marker = checkout / ".venv/.stagemesh-environment.json"
    marker.unlink()
    os.link(outside, marker)
    prepare_candidate_environments(project, checkout)
    assert outside.read_text() == "operator original"
    assert not os.path.samefile(outside, marker)


def test_python_reuse_refuses_linked_marker(tmp_path, monkeypatch):
    from test_candidate_environments import _fixture

    import stagemesh.candidate_environments as environments

    project, checkout, _packages = _fixture(tmp_path)
    prepare_candidate_environments(project, checkout)
    marker = checkout / ".venv/.stagemesh-environment.json"
    original = environments._is_link
    monkeypatch.setattr(environments, "_is_link", lambda path: path == marker or original(path))
    with pytest.raises(ValueError, match="marker must not be a link"):
        prepare_candidate_environments(project, checkout)


def test_reused_node_tree_rejects_new_escape_link(tmp_path):
    project, _git, modules, _config = node_repo(tmp_path)
    checkout = prepare_task_workspace(project, "reuse-escape")
    prepare_candidate_environments(project, checkout)
    target = checkout / "apps/web/node_modules"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(modules), str(target / "injected"))
    else:
        (target / "injected").symlink_to(modules, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        prepare_candidate_environments(project, checkout)
    assert (modules / "dependency.txt").read_text() == "operator original"


def test_legacy_shared_node_link_is_replaced_without_touching_source(tmp_path):
    project, _git, modules, _config = node_repo(tmp_path)
    checkout = prepare_task_workspace(project, "legacy")
    target = checkout / "apps/web/node_modules"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(modules), str(target))
    else:
        target.symlink_to(modules, target_is_directory=True)
    prepare_candidate_environments(project, checkout)
    assert not _is_link(target)
    (target / "dependency.txt").write_text("private")
    assert (modules / "dependency.txt").read_text() == "operator original"


def test_existing_unmanaged_node_directory_is_preserved(tmp_path):
    project, _git, _modules, _config = node_repo(tmp_path)
    checkout = prepare_task_workspace(project, "unmanaged")
    target = checkout / "apps/web/node_modules"
    target.mkdir()
    sentinel = target / "keep"
    sentinel.write_text("resumable work")
    with pytest.raises(ValueError, match="existing"):
        prepare_candidate_environments(project, checkout)
    assert sentinel.read_text() == "resumable work"


def test_internal_dependency_links_stay_private(tmp_path):
    project, _git, modules, _config = node_repo(tmp_path)
    actual = modules / "actual"
    actual.mkdir()
    (actual / "data.txt").write_text("original")
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(actual), str(modules / "alias"))
    else:
        (modules / "alias").symlink_to(actual, target_is_directory=True)
    checkout = prepare_task_workspace(project, "linked")
    prepare_candidate_environments(project, checkout)
    target = checkout / "apps/web/node_modules"
    assert (target / "alias").resolve().is_relative_to(target)
    (target / "alias/data.txt").write_text("private")
    assert (actual / "data.txt").read_text() == "original"


def test_dependency_link_escape_is_refused_without_partial_copy(tmp_path):
    project, _git, modules, _config = node_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("original")
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(outside), str(modules / "escape"))
    else:
        (modules / "escape").symlink_to(outside, target_is_directory=True)
    checkout = prepare_task_workspace(project, "escape")
    with pytest.raises(ValueError, match="escapes"):
        prepare_candidate_environments(project, checkout)
    assert not (checkout / "apps/web/node_modules").exists()
    assert not list((checkout / "apps/web").glob(".stagemesh-node-*"))
    assert (outside / "keep").read_text() == "original"


@pytest.mark.parametrize("bad", ["missing", "malformed", "corrupt-frozen"])
def test_wave_refuses_unknown_constraints_before_baseline_or_provider(tmp_path, bad):
    from test_survivor_wave import OLD, Rig, make_store

    from stagemesh.survivor_wave import run_wave

    store, project = make_store(tmp_path, ["unknown"])
    path = project / ".stagemesh/contracts/unknown.json"
    if bad == "missing":
        path.unlink()
    elif bad == "malformed":
        path.write_text("{")
    else:
        store.bind_task_contract("unknown", OLD, 1, "invalid-digest", path.read_text())
    rig = Rig(project)
    try:
        report = run_wave(store, ["unknown"], rig.hooks(), project=project)
        assert rig.runs == rig.advanced == rig.prs == []
        assert report.tasks[0].outcome == "skipped"
        assert report.tasks[0].reason.startswith("invalid_contract:")
    finally:
        store.close()


@pytest.mark.parametrize("enforce_resources", [False, True])
def test_shared_browser_port_with_real_server(tmp_path, monkeypatch, enforce_resources):
    import http.server
    import socket
    import threading
    import time
    import urllib.request

    from test_survivor_wave import Rig, make_store

    import stagemesh.survivor_wave as wave

    store, project = make_store(tmp_path, ["browser-a", "browser-b"])
    for task_id in ("browser-a", "browser-b"):
        path = project / ".stagemesh/contracts" / f"{task_id}.json"
        payload = json.loads(path.read_text())
        payload["exclusive_resources"] = ["shared-browser-port"]
        path.write_text(json.dumps(payload))
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ready")
        def log_message(self, *args):
            pass
    class Server(http.server.ThreadingHTTPServer):
        allow_reuse_address = os.name != "nt"
        def server_bind(self):
            if os.name == "nt":
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()
    overlap = threading.Barrier(2)
    def browser(rig, task_id):
        if not enforce_resources:
            overlap.wait(timeout=5)
        with Server(("127.0.0.1", port), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for _ in range(6):
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as response:
                        assert response.read() == b"ready"
                    time.sleep(0.025)
            finally:
                server.shutdown()
                thread.join(timeout=3)
        return rig.finish_done(task_id)
    if not enforce_resources:
        monkeypatch.setattr(wave, "contract_conflict", lambda *args: None)
    rig = Rig(project, {"browser-a": browser, "browser-b": browser})
    try:
        report = wave.run_wave(store, ["browser-a", "browser-b"], rig.hooks(), concurrency=2, project=project)
        outcomes = [task.outcome for task in report.tasks]
        if enforce_resources:
            assert outcomes == ["done", "done"]
        else:
            assert "infrastructure_failure" in outcomes  # negative control: old admission races the same port
    finally:
        store.close()
