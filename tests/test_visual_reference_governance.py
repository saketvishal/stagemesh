"""Hermetic acceptance for opt-in visual-reference governance."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from stagemesh.domain import EvidenceStatus
from stagemesh.e2e_evidence import E2EEvidenceValidationError, review_e2e_projection
from stagemesh.git import GitWorkspace
from stagemesh.persistence import Store
from stagemesh.recovery import format_doctor, task_doctor
from stagemesh.review import Reviewer
from stagemesh.validation import Validator
from stagemesh.validation_services import ValidationServiceError, discover_validation_services

REQUIREMENTS = (
    {
        "id": "workspace-ready-desktop",
        "kind": "browser",
        "target": "/synthetic/workspace",
        "state": "ready",
        "viewport": "1440x900",
    },
    {
        "id": "workspace-ready-mobile",
        "kind": "browser",
        "target": "/synthetic/workspace",
        "state": "ready",
        "viewport": "390x844",
    },
    {
        "id": "workspace-empty-mobile",
        "kind": "browser",
        "target": "/synthetic/workspace",
        "state": "empty",
        "viewport": "390x844",
    },
    {
        "id": "workspace-error-desktop",
        "kind": "browser",
        "target": "/synthetic/workspace",
        "state": "error",
        "viewport": "1440x900",
    },
    {
        "id": "workspace-recovered-desktop",
        "kind": "browser",
        "target": "/synthetic/workspace",
        "state": "recovery",
        "viewport": "1440x900",
    },
    {
        "id": "activity-shared-desktop",
        "kind": "browser",
        "target": "/synthetic/activity",
        "state": "ready",
        "viewport": "1440x900",
    },
)


def _visual_policy() -> dict:
    artifacts = []
    for requirement in REQUIREMENTS:
        requirement_id = requirement["id"]
        artifacts.extend(
            [
                {
                    "requirement_id": requirement_id,
                    "role": "actual",
                    "path": f"results/{requirement_id}.actual.png",
                    "content_type": "image/png",
                },
                {
                    "requirement_id": requirement_id,
                    "role": "reference",
                    "path": f"references/{requirement_id}.png",
                    "content_type": "image/png",
                },
                {
                    "requirement_id": requirement_id,
                    "role": "diff",
                    "path": f"results/{requirement_id}.diff.png",
                    "content_type": "image/png",
                },
            ]
        )
    return {
        "revision": "synthetic-design-v1",
        "inputs": [
            *(
                {"path": f"references/{requirement['id']}.png", "role": "reference"}
                for requirement in REQUIREMENTS
            ),
            {"path": "fixtures/visual-state.json", "role": "fixture"},
            {"path": "visual-thresholds.json", "role": "tolerance"},
            {"path": "scripts/compare-visuals.py", "role": "gate"},
            {"path": "package.json", "role": "package"},
        ],
        "artifacts": artifacts,
        "comparison": {
            "algorithm": "synthetic-pixel-ratio-v1",
            "max_difference_ratio": 0.01,
            "stability_runs": 2,
        },
        "functional_gates": ["unit"],
    }


def _evaluator_code(
    *,
    mismatch_ids: tuple[str, ...] = (),
    stable: bool = True,
    omit_check: str | None = None,
    omit_artifact: str | None = None,
    mutate_input: str | None = None,
) -> str:
    checks = [
        {
            "id": requirement["id"],
            "status": "PASS",
            "difference_ratio": 0.25 if requirement["id"] in mismatch_ids else 0.0,
            "stable": stable,
            "render_count": 2,
        }
        for requirement in REQUIREMENTS
    ]
    if omit_check is not None:
        checks = [check for check in checks if check["id"] != omit_check]
    output_paths = [
        f"results/{requirement['id']}.{role}.png"
        for requirement in REQUIREMENTS
        for role in ("actual", "diff")
    ]
    if omit_artifact is not None:
        output_paths.remove(omit_artifact)
    statements = [
        "import json,os",
        "from pathlib import Path",
        "Path('results').mkdir(exist_ok=True)",
        *(f"Path({path!r}).write_bytes(b'synthetic-image')" for path in output_paths),
    ]
    if mutate_input is not None:
        statements.append(f"Path({mutate_input!r}).write_text('mutated')")
    payload = {"checks": checks}
    statements.extend(
        [
            f"payload={payload!r}",
            "payload['policy_fingerprint']=os.environ['STAGEMESH_E2E_POLICY_FINGERPRINT']",
            "Path('results/stagemesh-results.json').write_text(json.dumps(payload))",
        ]
    )
    return ";".join(statements)


def _write_inputs(project: Path) -> None:
    (project / "references").mkdir()
    (project / "fixtures").mkdir()
    (project / "scripts").mkdir()
    for requirement in REQUIREMENTS:
        (project / "references" / f"{requirement['id']}.png").write_bytes(b"approved-reference")
    (project / "fixtures" / "visual-state.json").write_text('{"state":"synthetic"}')
    (project / "visual-thresholds.json").write_text('{"max_difference_ratio":0.01}')
    (project / "scripts" / "compare-visuals.py").write_text("# project-owned comparator\n")
    (project / "package.json").write_text('{"name":"synthetic-visual-fixture","private":true}')


def _configure(project: Path, code: str, *, policy: dict | None = None) -> None:
    service = {
        "name": "synthetic-ui-fidelity",
        "kind": "browser",
        "command": [sys.executable, "-c", code],
        "timeout_seconds": 30,
        "evaluator": "synthetic-independent-evaluator",
        "require_independent": True,
        "result_manifest": "results/stagemesh-results.json",
        "requirements": list(REQUIREMENTS),
        "visual_reference": policy or _visual_policy(),
    }
    runtime = project / ".stagemesh"
    runtime.mkdir(exist_ok=True)
    (runtime / "config.json").write_text(json.dumps({"validation_services": [service]}))


@pytest.fixture
def visual_evaluation(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    _write_inputs(project)
    (project / "app.py").write_text("VALUE = 1\n")
    git = GitWorkspace(project)
    git.init_if_needed()
    git.run("config", "user.name", "Test")
    git.run("config", "user.email", "test@example.invalid")
    baseline = git.commit_all("visual baseline")
    (project / "app.py").write_text("VALUE = 2\n")
    candidate = git.commit_all("visual candidate")

    runtime = project / ".stagemesh"
    (runtime / "contracts").mkdir(parents=True)
    (runtime / "contracts" / "VISUAL.json").write_text(
        json.dumps(
            {
                "objective": "Validate synthetic visual governance",
                "allowed_files": ["app.py"],
                "protected_files": [
                    "references/**",
                    "fixtures/**",
                    "visual-thresholds.json",
                    "scripts/compare-visuals.py",
                    "package.json",
                ],
                "required_tests": [
                    {"name": "unit", "command": [sys.executable, "-c", "pass"]}
                ],
            }
        )
    )
    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    task_id = store.upsert_task("Validate synthetic visual governance", source_id="VISUAL")
    store.add_candidate(task_id, candidate, "synthetic-implementer", True)

    def validate(code: str, *, policy: dict | None = None):
        _configure(project, code, policy=policy)
        status = Validator().validate(store, task_id, candidate, project)
        row = store.conn.execute(
            "SELECT payload FROM evidence WHERE kind='VALIDATION' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        return status, json.loads(row["payload"])

    yield project, store, task_id, baseline, candidate, validate
    store.close()


def test_benign_visual_match_is_bound_auditable_and_reviewable(visual_evaluation):
    project, store, task_id, baseline, candidate, validate = visual_evaluation

    status, payload = validate(_evaluator_code())

    assert status is EvidenceStatus.PASSED
    evidence = payload["e2e_evaluations"][0]
    visual = evidence["metadata"]["visual_reference"]
    assert evidence["candidate_sha"] == candidate
    assert evidence["baseline_sha"] == baseline
    assert evidence["result"] == "PASS"
    assert visual["revision"] == "synthetic-design-v1"
    assert len(visual["protected_inputs"]) == 10
    assert len(visual["artifacts"]) == 18
    assert {item["role"] for item in visual["artifacts"]} == {"actual", "reference", "diff"}
    assert {item["id"] for item in evidence["metadata"]["requirements"]} == {
        "workspace-ready-desktop",
        "workspace-ready-mobile",
        "workspace-empty-mobile",
        "workspace-error-desktop",
        "workspace-recovered-desktop",
        "activity-shared-desktop",
    }

    projection = review_e2e_projection(payload, project)
    assert projection[0]["artifact_root"].startswith(".stagemesh/artifacts/e2e/")
    assert len(projection[0]["artifacts"]) == 18
    assert projection[0]["visual_reference"]["policy_fingerprint"] == visual["policy_fingerprint"]

    report = task_doctor(store, project, task_id)
    assert report["e2e"]["status"] == "passed"
    assert report["e2e"]["configured_services"][0]["visual_reference"] is True
    assert report["e2e"]["evaluations"][0]["visual_reference"]["typed_artifact_count"] == 18
    assert "; visual=1" in format_doctor(report)

    class CapturingReviewer:
        name = "synthetic-independent-code-reviewer"

        def __init__(self):
            self.prompt = ""

        def review_candidate(self, prompt, _project, _candidate):
            self.prompt = prompt
            return '{"decision":"PASS"}'

    adapter = CapturingReviewer()
    assert Reviewer(adapter=adapter, require_independent=True).review(
        store, task_id, candidate, project
    ) is EvidenceStatus.PASSED
    assert "synthetic-design-v1" in adapter.prompt
    assert '"role": "actual"' in adapter.prompt
    assert ".stagemesh/artifacts/e2e/" in adapter.prompt
    assert str(project) not in adapter.prompt
    assert "approved-reference" not in adapter.prompt


@pytest.mark.parametrize(
    ("code", "failure_kind", "message"),
    [
        (
            _evaluator_code(mismatch_ids=("workspace-ready-desktop", "workspace-ready-mobile")),
            "assertion",
            "approved threshold",
        ),
        (
            _evaluator_code(mismatch_ids=("activity-shared-desktop",)),
            "assertion",
            "approved threshold",
        ),
        (_evaluator_code(stable=False), "environment", "unstable"),
        (
            _evaluator_code(omit_check="workspace-error-desktop"),
            "environment",
            "omits required coverage",
        ),
        (
            _evaluator_code(omit_artifact="results/workspace-ready-desktop.diff.png"),
            "environment",
            "artifact",
        ),
        (
            _evaluator_code(mutate_input="references/workspace-ready-desktop.png"),
            "environment",
            "protected input changed",
        ),
    ],
)
def test_visual_mismatch_instability_missing_evidence_and_input_mutation_fail_closed(
    visual_evaluation,
    code,
    failure_kind,
    message,
):
    _project, _store, _task_id, _baseline, _candidate, validate = visual_evaluation

    status, payload = validate(code)

    assert status is EvidenceStatus.FAILED
    finding = next(item for item in payload["findings"] if item.get("code") == "gate_failed")
    assert finding["failure_kind"] == failure_kind
    assert message in (finding.get("stderr") or "")
    assert payload["e2e_evaluations"][0]["result"] == "FAIL"


def test_visual_policy_requires_declared_functional_gate(visual_evaluation):
    _project, _store, _task_id, _baseline, _candidate, validate = visual_evaluation
    policy = _visual_policy()
    policy["functional_gates"] = ["cross-screen-functional"]

    status, payload = validate(_evaluator_code(), policy=policy)

    assert status is EvidenceStatus.FAILED
    assert any("not declared in the bound contract" in item.get("stderr", "") for item in payload["findings"])


def test_visual_pass_manifest_cannot_override_nonzero_command(visual_evaluation):
    _project, _store, _task_id, _baseline, _candidate, validate = visual_evaluation

    status, payload = validate(_evaluator_code() + ";raise SystemExit(3)")

    assert status is EvidenceStatus.FAILED
    finding = next(item for item in payload["findings"] if item.get("code") == "gate_failed")
    assert finding["failure_kind"] == "assertion"
    assert finding["returncode"] == 3
    assert payload["e2e_evaluations"][0]["result"] == "FAIL"


def test_visual_input_content_changes_service_identity(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    _write_inputs(project)
    _configure(project, _evaluator_code())

    before = discover_validation_services(project)
    (project / "fixtures" / "visual-state.json").write_text('{"state":"changed"}')
    after = discover_validation_services(project)

    assert before.fingerprint != after.fingerprint
    assert "changed" not in json.dumps(after.to_dict())


def test_visual_input_symlink_cannot_escape_candidate(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    _write_inputs(project)
    _configure(project, _evaluator_code())
    external = tmp_path / "outside.json"
    external.write_text('{"private":"outside"}')
    fixture = project / "fixtures" / "visual-state.json"
    fixture.unlink()
    try:
        fixture.symlink_to(external)
    except OSError:
        pytest.skip("host cannot create symlinks")

    with pytest.raises(ValidationServiceError, match="escapes"):
        discover_validation_services(project)


@pytest.mark.parametrize(
    "command",
    [
        [sys.executable, "compare.py", "--update-snapshots"],
        [sys.executable, "update-goldens.py"],
        [sys.executable, "compare.py", "--refresh-baseline"],
    ],
)
def test_visual_update_modes_are_rejected_before_execution(tmp_path: Path, command: list[str]):
    project = tmp_path / "project"
    project.mkdir()
    _write_inputs(project)
    _configure(project, _evaluator_code())
    config = json.loads((project / ".stagemesh" / "config.json").read_text())
    config["validation_services"][0]["command"] = command
    (project / ".stagemesh" / "config.json").write_text(json.dumps(config))

    with pytest.raises(ValidationServiceError, match="update modes"):
        discover_validation_services(project)


def test_visual_policy_rejects_missing_state_artifact(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    _write_inputs(project)
    policy = _visual_policy()
    policy["artifacts"] = [
        item
        for item in policy["artifacts"]
        if not (
            item["requirement_id"] == "activity-shared-desktop" and item["role"] == "diff"
        )
    ]
    _configure(project, _evaluator_code(), policy=policy)

    with pytest.raises(ValidationServiceError, match="actual, reference, and diff"):
        discover_validation_services(project)


def test_review_projection_rejects_tampered_visual_artifact(visual_evaluation):
    project, _store, _task_id, _baseline, _candidate, validate = visual_evaluation
    status, payload = validate(_evaluator_code())
    assert status is EvidenceStatus.PASSED
    artifact = payload["e2e_evaluations"][0]["artifacts"][0]
    (Path(payload["e2e_artifact_root"]) / artifact["path"]).write_bytes(b"tampered")

    with pytest.raises(E2EEvidenceValidationError, match="mismatch"):
        review_e2e_projection(payload, project)
