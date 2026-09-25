"""Global invocation, registry, init/agent/doctor and the agent wrapper's deterministic parts."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys

import pytest
from test_project_backlog import REPO_ROOT, git, make_project_repo, stagemesh

from build_coordinator import __version__
from build_coordinator.agents import machine
from build_coordinator.agents.profiles import HEADLESS_FAILED, NOT_HEADLESS, NOT_INSTALLED, PROFILES, RuntimeProfile, probe_runtime
from build_coordinator.agents.wrapper import classify_failure, parse_verdict, render_prompt
from build_coordinator.project.definition import machine_environment, register_project, registered_roots


def payload(proc):
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_global_continue_from_an_empty_directory_coordinates_every_registered_project(tmp_path, registry):
    alpha, _ = make_project_repo(
        tmp_path, {f"A-{i}": {"review": "NONE"} for i in range(1, 4)}, concurrency=2, subdir="alpha", project_id="alpha", name="Alpha"
    )
    beta, _ = make_project_repo(
        tmp_path, {f"B-{i}": {"review": "NONE"} for i in range(1, 3)}, concurrency=1, subdir="beta", project_id="beta", name="Beta"
    )
    for root in (alpha, beta):
        register_project(root)
    empty = tmp_path / "stagemesh-test"
    empty.mkdir()

    out = payload(stagemesh(["continue"], cwd=empty, registry=registry))
    assert out["mode"] == "global"
    runs = out["projects"]
    assert runs["alpha"]["tasks_by_state"] == {"DONE": 3} and runs["alpha"]["peak_parallel_builders"] == 2
    assert runs["beta"]["tasks_by_state"] == {"DONE": 2} and runs["beta"]["peak_parallel_builders"] == 1
    for root in (alpha, beta):  # state and workspaces never cross projects
        assert (root / ".build-coordinator" / "coordinator.sqlite3").is_file()
        assert list((root / ".build-coordinator" / "worktrees").iterdir())
    assert not (empty / ".build-coordinator").exists()


def test_a_broken_project_does_not_stop_unrelated_projects(tmp_path, registry):
    good, _ = make_project_repo(tmp_path, {"G-1": {"review": "NONE"}}, concurrency=1, subdir="good", project_id="good", name="Good")
    bad, _ = make_project_repo(tmp_path, {"X-1": {}}, concurrency=1, subdir="bad", project_id="bad", name="Bad")
    (bad / ".stagemesh" / "tasks" / "backlog.yaml").write_text("tasks: [{id: X-1}]\n", encoding="utf-8")
    register_project(good)
    register_project(bad)
    proc = stagemesh(["continue"], cwd=tmp_path, registry=registry)
    assert proc.returncode == 1
    runs = json.loads(proc.stdout)["projects"]
    assert runs["good"]["tasks_by_state"] == {"DONE": 1} and runs["bad"]["returncode"] != 0


def test_project_mode_wins_inside_a_project_and_global_needs_registered_projects(tmp_path, registry):
    root, _ = make_project_repo(tmp_path, {"P-1": {"review": "NONE"}}, concurrency=1)
    inside = payload(stagemesh(["continue", "--json"], cwd=root, registry=registry))
    assert "mode" not in inside and inside["final"]["tasks_by_state"] == {"DONE": 1}
    empty = tmp_path / "empty"
    empty.mkdir()
    nothing = stagemesh(["continue"], cwd=empty, registry=tmp_path / "no-projects.json")
    assert nothing.returncode != 0 and "stagemesh project add" in (nothing.stderr + nothing.stdout)


def test_registry_add_list_remove_and_machine_local_environment(tmp_path, registry):
    root, _ = make_project_repo(tmp_path, {"E-1": {}}, concurrency=1)
    venv = tmp_path / "venv" / "bin"
    added = payload(stagemesh(["project", "add", str(root), "--path-prepend", str(venv), "--env", "FOO=bar"], cwd=tmp_path, registry=registry))
    assert added["machine_environment"]["env"] == {"FOO": "bar"}
    listing = payload(stagemesh(["project", "list"], cwd=tmp_path, registry=registry))
    assert [p["project_id"] for p in listing["projects"]] == ["fixture"]
    assert machine_environment(root)["path_prepend"] == [str(venv.resolve())]
    assert "FOO" not in (root / ".stagemesh" / "project.yaml").read_text(encoding="utf-8")  # never in the repo
    register_project(root)  # re-adding keeps machine-local settings
    assert machine_environment(root)["env"] == {"FOO": "bar"}
    assert payload(stagemesh(["project", "remove", "fixture"], cwd=tmp_path, registry=registry))["removed"]
    assert registered_roots() == []
    assert root.exists()


def test_registry_reads_the_legacy_bare_path_format(tmp_path, registry):
    root, _ = make_project_repo(tmp_path, {"L-1": {}}, concurrency=1)
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps({"projects": [str(root)]}), encoding="utf-8")
    assert [r.resolve() for r in registered_roots()] == [root.resolve()]


def test_version_and_entry_points(tmp_path):
    out = subprocess.run(
        [sys.executable, "-P", "-m", "build_coordinator", "--version"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )
    assert out.stdout.strip() == f"stagemesh {__version__}"
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{__version__}"' in pyproject and 'name = "stagemesh"' in pyproject
    assert 'stagemesh = "build_coordinator.cli:main"' in pyproject


def test_init_creates_a_registered_project_without_activating_any_task(tmp_path, registry):
    repo = tmp_path / "My App"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "init")
    out = payload(stagemesh(["init", str(repo)], cwd=tmp_path, registry=registry))
    assert out["project"] == "my-app" and out["registered_in"] == str(registry)
    assert (repo / ".stagemesh" / "project.yaml").is_file()
    assert not list((repo / ".stagemesh" / "tasks").glob("*.yaml")), "the sample task must not be live"
    assert ".build-coordinator/" in (repo / ".gitignore").read_text(encoding="utf-8")
    again = payload(stagemesh(["init", str(repo)], cwd=tmp_path, registry=registry))
    assert again["created"] == []  # idempotent
    discovered = payload(stagemesh(["project", "discover", "my-app"], cwd=tmp_path, registry=registry))
    assert discovered["task_definitions"] == []


def test_init_refuses_a_directory_that_is_not_a_committed_repository(tmp_path, registry):
    plain = tmp_path / "plain"
    plain.mkdir()
    proc = stagemesh(["init", str(plain)], cwd=tmp_path, registry=registry)
    assert proc.returncode != 0 and "not a git repository" in proc.stderr + proc.stdout


def test_doctor_explains_problems_and_tells_the_user_what_to_do(tmp_path, registry):
    root, _ = make_project_repo(tmp_path, {"D-1": {"review": "TWO_REVIEWERS"}}, concurrency=1)
    register_project(root)
    home = tmp_path / "home"
    proc = stagemesh(["doctor", "--json"], cwd=tmp_path, registry=registry, extra_env={"STAGEMESH_HOME": str(home)})
    report = json.loads(proc.stdout)
    checks = {(c["area"], c["check"]): c for c in report["checks"]}
    assert proc.returncode == 1 and not report["healthy"]
    assert checks[("runtimes", "agent setup")]["hint"].startswith("run `stagemesh agent setup`")
    assert checks[("project:fixture", "review policy")]["status"] == "FAIL"
    assert "execution.reviewers: 2" in checks[("project:fixture", "review policy")]["hint"]
    assert checks[("project:fixture", "discovery")]["status"] == "OK"
    assert checks[("project:fixture", "upstream")]["status"] == "OK"


def test_runtime_is_never_ready_because_an_executable_exists(monkeypatch, tmp_path):
    missing = probe_runtime(RuntimeProfile("ghost", "acme", "Ghost", ("definitely-not-installed-xyz",), ()), live=False)
    assert missing.state == NOT_INSTALLED
    profile = RuntimeProfile("fake", "acme", "Fake", ("fake-agent",), ("CODING",), auth_args=None)
    monkeypatch.setattr(RuntimeProfile, "executable", lambda self: sys.executable)
    monkeypatch.setattr(RuntimeProfile, "command", lambda self, role, cwd, **kw: [sys.executable, "-c", "print('nope')"])
    status = probe_runtime(profile, live=True, timeout=60)
    assert status.state == HEADLESS_FAILED and not status.ready



def test_gui_only_runtime_is_reported_not_headless_with_evidence(monkeypatch):
    profile = RuntimeProfile("gui", "acme", "GUI", ("gui",), ("CODING",), headless=False, help_args=("chat", "--help"))
    monkeypatch.setattr(RuntimeProfile, "executable", lambda self: sys.executable)
    monkeypatch.setattr(
        "build_coordinator.agents.profiles._run", lambda argv, **kw: (0, "Usage: gui chat [options] [prompt]\n --mode\n --maximize")
    )
    status = probe_runtime(profile)
    assert status.state == NOT_HEADLESS and status.evidence["headless_markers_found"] == []
    assert PROFILES["antigravity"].headless is False


def test_agent_setup_state_lives_in_the_user_home_and_holds_no_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("STAGEMESH_HOME", str(tmp_path / "home"))
    ghost = probe_runtime(RuntimeProfile("ghost", "acme", "Ghost", ("definitely-not-installed-xyz",), ()), live=False)
    monkeypatch.setattr("build_coordinator.agents.machine.discover_runtimes", lambda **kw: [ghost])
    machine.setup_agents(live=False)
    text = machine.agents_path().read_text(encoding="utf-8")
    assert "ghost" in text and machine.ready_runtime_ids() == []
    assert not any(word in text.lower() for word in ("token", "apikey", "api_key", "secret", "password"))


def test_wrapper_prompt_carries_the_task_and_never_asks_agents_to_self_report_lifecycle():
    raw = json.dumps(
        {
            "task_definition": {
                "task_id": "T-1",
                "title": "Do it",
                "description": "Make X.",
                "acceptance_criteria": ["X works"],
                "required_validation": ["pytest -q"],
                "permitted_scope": ["src/"],
            },
            "task_envelope": {"known_failures": ["pytest -q -> exit 1: boom"]},
        }
    )
    builder = render_prompt("BUILDER", raw, base_ref="main", reviewed_sha=None)
    assert "Make X." in builder and "X works" in builder and "pytest -q" in builder and "boom" in builder
    assert "Do NOT run git commit" in builder and "result file" not in builder.lower()
    reviewer = render_prompt("REVIEWER", raw, base_ref="main", reviewed_sha="abc123")
    assert "abc123" in reviewer and "git diff main...HEAD" in reviewer and "must not modify" in reviewer


def test_reviewer_prompt_surfaces_open_findings_and_requires_dispositions():
    raw = json.dumps(
        {
            "task_definition": {"task_id": "T-1", "title": "Do it", "description": "Make X.", "acceptance_criteria": ["X works"]},
            "task_envelope": {},
            "resume_context": {
                "open_findings_from_prior_review": [
                    {"id": "abc123", "description": "missing null check", "attempts": 1, "first_seen_cycle": "review-cycle:e1"}
                ]
            },
        }
    )
    reviewer = render_prompt("REVIEWER", raw, base_ref="main", reviewed_sha="abc123")
    assert "id=abc123" in reviewer and "missing null check" in reviewer
    assert "finding_dispositions" in reviewer
    assert "RESOLVED" in reviewer and "STILL_OPEN" in reviewer


def test_remediation_prompt_surfaces_open_findings_from_registry():
    raw = json.dumps(
        {
            "task_definition": {"task_id": "T-1", "title": "Do it", "description": "Make X.", "acceptance_criteria": ["X works"]},
            "task_envelope": {},
            "resume_context": {
                "open_findings": [
                    {"id": "abc123", "description": "missing null check", "attempts": 2, "first_seen_cycle": "review-cycle:e1"}
                ]
            },
        }
    )
    remediation = render_prompt("REMEDIATION", raw, base_ref="main", reviewed_sha=None)
    assert "id=abc123" in remediation and "missing null check" in remediation


def test_reviewer_verdict_parsing_is_strict():
    good = 'analysis\n```json\n{"verdict": "GREEN", "findings": [], "required_remediation": [], "ready_for_integration": true}\n```'
    assert parse_verdict(good)["verdict"] == "GREEN"
    assert parse_verdict('```json\n{"verdict": "APPROVE"}\n```') is None
    assert parse_verdict("looks good to me") is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Error: not logged in. Please run /login", "AUTH_FAILURE"),
        ("You have hit your usage limit", "QUOTA_EXHAUSTED"),
        ("429 Too Many Requests", "RATE_LIMITED"),
        ("getaddrinfo ENOTFOUND api.example.com", "NETWORK_FAILURE"),
        ("segmentation fault", "EXECUTION_FAILURE"),
    ],
)
def test_provider_failures_are_classified_into_the_routing_taxonomy(text, expected):
    assert classify_failure(text) == expected


def test_delivered_outside_stagemesh_is_reconciled_with_an_audit_trail(tmp_path, registry):
    root, _ = make_project_repo(tmp_path, {"O-1": {}, "O-2": {"delivered_by": "commit abc: landed by hand"}}, concurrency=1)
    register_project(root)
    first = json.loads(stagemesh(["project", "sync", "fixture"], cwd=tmp_path, registry=registry).stdout)
    assert {t["task_id"]: t["action"] for t in first["tasks"]} == {"O-1": "CREATED", "O-2": "RECONCILED"}
    again = json.loads(stagemesh(["project", "sync", "fixture"], cwd=tmp_path, registry=registry).stdout)
    assert again["counts"] == {"SKIPPED": 2}
    status = json.loads(stagemesh(["project", "status", "fixture"], cwd=tmp_path, registry=registry).stdout)
    assert {t["task_id"]: t["state"] for t in status["tasks"]} == {"O-1": "READY", "O-2": "DONE"}
    assert status["executions"] == []  # no execution or review evidence was invented
    with sqlite3.connect(root / ".build-coordinator" / "coordinator.sqlite3") as db:
        rows = db.execute("select event_data from build_task_events where task_id='O-2' and to_state is not null").fetchall()
    reasons = [json.loads(d).get("reason") for (d,) in rows]
    assert reasons and all(r and r.startswith("DELIVERED_OUTSIDE_STAGEMESH") for r in reasons)


def test_wrapper_safe_output_handles_non_ascii_unicode_under_narrow_encoding(monkeypatch):
    from build_coordinator.agents.wrapper import _safe_write_stdout_tail
    import io

    sample = "Ran test -> \u2192 with emoji \U0001f680 and unicode \u4e2d\u6587"

    # 1. When sys.stdout has a binary buffer
    raw_buffer = io.BytesIO()
    text_wrapper = io.TextIOWrapper(raw_buffer, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", text_wrapper)
    _safe_write_stdout_tail(sample)
    raw_buffer.seek(0)
    written = raw_buffer.read().decode("utf-8", errors="replace")
    assert "Ran test -> \u2192" in written

    # 2. When sys.stdout has NO binary buffer and strict cp1252 encoding
    class StrictNarrowWriter:
        def __init__(self, encoding="cp1252"):
            self.encoding = encoding
            self.data = ""

        def write(self, s):
            s.encode(self.encoding, errors="strict")
            self.data += s

        def flush(self):
            pass

    narrow = StrictNarrowWriter(encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", narrow)
    _safe_write_stdout_tail(sample)
    assert "Ran test" in narrow.data
    narrow.data.encode("cp1252")


def test_wrapper_safe_output_survives_narrow_console_failures(monkeypatch):
    """Buffer, encoding, and write failures must not escape the stdout tail."""
    from build_coordinator.agents.wrapper import _safe_write_stdout_tail

    sample = "caf\u00e9 \u2192 emoji \U0001f680 CJK \u4e2d lone \ud800"

    class StrictCp1252:
        encoding = "cp1252"

        def __init__(self):
            self.data = ""

        def write(self, text):
            text.encode("cp1252", errors="strict")
            self.data += text

        def flush(self):
            pass

    class BufferRejects(StrictCp1252):
        def __init__(self):
            super().__init__()
            self.buffer = self._Buf()

        class _Buf:
            def write(self, data):
                raise OSError(22, "Invalid argument")

            def flush(self):
                raise OSError(22, "Invalid argument")

    class DetachedBuffer(StrictCp1252):
        @property
        def buffer(self):
            raise ValueError("underlying buffer has been detached")

    class EncodingMismatch(StrictCp1252):
        """Attribute says utf-8; the encoder is strict cp1252."""

        encoding = "utf-8"

    class NoneEncoding(StrictCp1252):
        encoding = None

    class RejectAll:
        encoding = "cp1252"

        def write(self, text):
            raise UnicodeEncodeError("cp1252", text or "", 0, 1, "character maps to <undefined>")

        def flush(self):
            raise OSError(22, "Invalid argument")

    for writer in (BufferRejects(), DetachedBuffer(), EncodingMismatch(), NoneEncoding()):
        monkeypatch.setattr(sys, "stdout", writer)
        _safe_write_stdout_tail(sample)
        assert "caf" in writer.data
        writer.data.encode("cp1252")

    monkeypatch.setattr(sys, "stdout", RejectAll())
    _safe_write_stdout_tail(sample)

    preserved = StrictCp1252()
    monkeypatch.setattr(sys, "stdout", preserved)
    _safe_write_stdout_tail(sample)
    assert "caf\u00e9" in preserved.data
    assert "\u2192" not in preserved.data
    assert "\U0001f680" not in preserved.data


def test_wrapper_main_survives_non_ascii_output_without_crashing_or_losing_result(monkeypatch, tmp_path):
    import io
    from build_coordinator.agents import wrapper

    result_path = tmp_path / "result.json"
    monkeypatch.setenv("BUILD_COORDINATOR_ROLE", "REVIEWER")
    monkeypatch.setenv("BUILD_COORDINATOR_RESULT_PATH", str(result_path))
    monkeypatch.setenv("BUILD_COORDINATOR_EXECUTION_ID", "exec-test-123")
    monkeypatch.setenv("BUILD_COORDINATOR_TASK_ID", "SM-TEST")
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))

    unicode_output = '''Review passed: -> \u2192
```json
{"verdict": "GREEN", "findings": [], "required_remediation": [], "ready_for_integration": true}
```'''
    monkeypatch.setattr(wrapper, "run_agent", lambda *a, **kw: (0, unicode_output))

    raw_buffer = io.BytesIO()
    text_wrapper = io.TextIOWrapper(raw_buffer, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", text_wrapper)

    code = wrapper.main(["--runtime", "codex"])
    assert code == 0
    assert result_path.is_file()
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["status"] == "SUCCEEDED"
    assert payload["verdict"] == "GREEN"
    assert payload["execution_id"] == "exec-test-123"


def test_wrapper_main_preserves_result_and_exit_when_console_rejects_unicode(monkeypatch, tmp_path):
    """A cp1252 console that rejects the stdout tail must not drop the result contract."""
    import io
    from build_coordinator.agents import wrapper

    class NarrowConsole:
        """Binary writes fail the way a legacy Windows console does; text is strict cp1252."""

        encoding = "cp1252"

        def __init__(self):
            self.data = ""
            self.buffer = self._Buf()

        class _Buf:
            def write(self, data):
                raise OSError(22, "Invalid argument")

            def flush(self):
                raise OSError(22, "Invalid argument")

        def write(self, text):
            text.encode("cp1252", errors="strict")
            self.data += text

        def flush(self):
            pass

    class RejectAll:
        encoding = "cp1252"

        def write(self, text):
            raise UnicodeEncodeError("cp1252", text or "", 0, 1, "character maps to <undefined>")

        def flush(self):
            raise OSError(22, "Invalid argument")

    verdict = (
        "Review passed: \u2192 \U0001f680\n"
        "```json\n"
        '{"verdict": "GREEN", "findings": [], "required_remediation": [], "ready_for_integration": true}\n'
        "```"
    )

    def run(writer, agent_result, name):
        result_path = tmp_path / f"{name}.json"
        monkeypatch.setenv("BUILD_COORDINATOR_ROLE", "REVIEWER")
        monkeypatch.setenv("BUILD_COORDINATOR_RESULT_PATH", str(result_path))
        monkeypatch.setenv("BUILD_COORDINATOR_EXECUTION_ID", "exec-narrow")
        monkeypatch.setenv("BUILD_COORDINATOR_TASK_ID", "SM-019")
        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        monkeypatch.setattr(wrapper, "run_agent", lambda *a, **kw: agent_result)
        monkeypatch.setattr(sys, "stdout", writer)
        code = wrapper.main(["--runtime", "codex"])
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        return code, payload, writer

    code, payload, console = run(NarrowConsole(), (0, verdict), "success")
    assert code == 0
    assert payload["status"] == "SUCCEEDED"
    assert payload["verdict"] == "GREEN"
    assert payload["execution_id"] == "exec-narrow"
    assert "Review passed" in console.data
    console.data.encode("cp1252")

    code, payload, _console = run(NarrowConsole(), (1, "usage limit \u2192 \U0001f680"), "failed")
    assert code == 1
    assert payload["status"] == "FAILED"
    assert payload["provider_failure"] == "QUOTA_EXHAUSTED"
    assert payload["execution_id"] == "exec-narrow"

    code, payload, _console = run(RejectAll(), (0, verdict), "rejected")
    assert code == 0
    assert payload["status"] == "SUCCEEDED"
    assert payload["verdict"] == "GREEN"
