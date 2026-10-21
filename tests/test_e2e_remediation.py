"""Configured E2E evaluation is separate, candidate-bound and actionable."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from stagemesh.domain import EvidenceStatus
from stagemesh.git import GitWorkspace
from stagemesh.persistence import Store
from stagemesh.remediation import remediation_context
from stagemesh.validation import Validator
from stagemesh.validation_services import discover_validation_services


@pytest.fixture
def evaluation(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    git = GitWorkspace(project)
    git.init_if_needed()
    git.run("config", "user.name", "Test")
    git.run("config", "user.email", "test@example.invalid")
    (project / "app.py").write_text("VALUE = 1\n")
    git.commit_all("base")
    (project / "app.py").write_text("VALUE = 2\n")
    sha = git.commit_all("candidate")
    runtime = project / ".stagemesh"
    (runtime / "contracts").mkdir(parents=True)
    (runtime / "contracts" / "E2E.json").write_text(json.dumps({
        "objective": "Evaluate configured flows", "allowed_files": ["app.py"],
        "required_tests": [{"name": "unit", "command": [sys.executable, "-c", "pass"]}],
    }))
    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    task = store.upsert_task("Evaluate configured flows", source_id="E2E")
    store.add_candidate(task, sha, "test", True)
    def configure(code="pass", **extra):
        service = {"name": "flow", "kind": "api", "command": [sys.executable, "-c", code], **extra}
        (runtime / "config.json").write_text(json.dumps({"validation_services": [service]}))
    def validate():
        status = Validator().validate(store, task, sha, project)
        payload = json.loads(store.conn.execute("SELECT payload FROM evidence ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        assert not list(store.running_executions())
        return status, payload
    yield project, store, task, sha, configure, validate
    store.close()


@pytest.mark.parametrize("kind", ["api", "browser"])
def test_configured_evaluation_runs_candidate_and_retains_artifacts(evaluation, kind):
    project, store, task, sha, configure, validate = evaluation
    configure("from pathlib import Path; assert '2' in Path('app.py').read_text(); Path('trace.log').write_text('request complete')", kind=kind, artifacts=["trace.log"])
    (project / "app.py").write_text("DIRTY IMPLEMENTATION")
    status, payload = validate()
    assert status is EvidenceStatus.PASSED
    manifest = payload["e2e_evaluations"][0]
    assert manifest["candidate_sha"] == sha
    assert manifest["kind"] == kind
    assert manifest["result"] == "PASS"
    artifact = manifest["artifacts"][0]
    assert (Path(payload["e2e_artifact_root"]) / artifact["path"]).read_text() == "request complete"
    assert not (project / "trace.log").exists()
    assert store.latest_candidate(task)["sha"] == sha


@pytest.mark.parametrize("code, expected", [
    ("raise AssertionError('expected checkout button')", "assertion"),
    ("raise RuntimeError('browser executable doesn\'t exist; playwright install')", "environment"),
    ("raise RuntimeError('ECONNREFUSED: connection refused')", "environment"),
])
def test_failure_kind_and_actionable_output_reach_remediation(evaluation, code, expected):
    project, store, task, sha, configure, validate = evaluation
    configure(code)
    status, payload = validate()
    assert status is EvidenceStatus.FAILED
    finding = next(f for f in payload["findings"] if f.get("code") == "gate_failed")
    assert finding["failure_kind"] == expected
    assert finding["stderr"]
    store.add_task_remediation(task, "VALIDATE", sha)
    context = remediation_context(store, task, project)
    assert context["failed_gates"][0]["failure_kind"] == expected
    assert context["findings"][0]["stderr"]


def test_missing_tool_is_not_an_assertion(evaluation):
    _, _, _, _, configure, validate = evaluation
    configure(command=["stagemesh-nonexistent-e2e-tool"])
    status, payload = validate()
    assert status is EvidenceStatus.FAILED
    assert payload["findings"][0]["failure_kind"] == "environment"


def test_redaction_precedes_bounding_and_reaches_remediation(evaluation, monkeypatch):
    project, store, task, sha, configure, validate = evaluation
    monkeypatch.setenv("E2E_PASSWORD", "opaque-sensitive-value")
    configure("import os; print('x'*5000); print(os.environ['E2E_PASSWORD']); raise AssertionError('token=do-not-relay')", env_keys=["E2E_PASSWORD"])
    status, payload = validate()
    assert status is EvidenceStatus.FAILED
    store.add_task_remediation(task, "VALIDATE", sha)
    context = remediation_context(store, task, project)
    text = json.dumps(context)
    assert "opaque-sensitive-value" not in text
    assert "do-not-relay" not in text
    assert len(context["findings"][0]["stdout"]) <= 2500
    assert "AssertionError" in text


@pytest.mark.parametrize("extra", [
    {"cwd": "../outside"}, {"artifacts": ["../outside.log"]},
    {"artifacts": [".env"]}, {"timeout_seconds": 0}, {"command": []},
    {"env_keys": ["REQUIRED_E2E_MISSING"]},
])
def test_invalid_prerequisites_fail_closed(evaluation, extra):
    _, _, _, _, configure, validate = evaluation
    configure(**extra)
    status, payload = validate()
    assert status is EvidenceStatus.FAILED
    assert payload["findings"]


def test_missing_configured_artifact_cannot_pass(evaluation):
    _, _, _, _, configure, validate = evaluation
    configure(artifacts=["missing-trace.zip"])
    status, payload = validate()
    assert status is EvidenceStatus.FAILED
    assert any("artifact" in f["message"] for f in payload["findings"])


def test_environment_values_invalidate_service_identity(evaluation, monkeypatch):
    project, _, _, _, configure, _ = evaluation
    configure(env_keys=["E2E_TARGET"])
    monkeypatch.setenv("E2E_TARGET", "first")
    first = discover_validation_services(project)
    monkeypatch.setenv("E2E_TARGET", "second")
    second = discover_validation_services(project)
    assert first.fingerprint != second.fingerprint
    assert "second" not in json.dumps(second.to_dict())


def test_service_change_during_evaluation_cannot_pass(evaluation):
    project, _, _, _, configure, validate = evaluation
    config = project / ".stagemesh" / "config.json"
    configure("from pathlib import Path; Path(" + repr(str(config)) + ").write_text('{}')")
    status, payload = validate()
    assert status is EvidenceStatus.FAILED
    assert any("changed" in f["message"] for f in payload["findings"])
