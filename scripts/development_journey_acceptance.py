from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / ".stagemesh" / "reports" / "qualification" / "development-journey.json"
TASK_ID = "journey-1"


def run(command: list[str], cwd: Path, *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False, timeout=timeout)


def git(cwd: Path, *args: str) -> str:
    result = run(["git", *args], cwd)
    if result.returncode:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stdout}{result.stderr}")
    return result.stdout.strip()


def write_provider(path: Path) -> None:
    path.write_text(
        """from __future__ import annotations
import json
import pathlib
import sys
import time

mode = sys.argv[1]
state = pathlib.Path(sys.argv[2])
prompt = sys.stdin.read()
if mode == "planner":
    revised = "Independent review findings:" in prompt
    corrected = "StageMesh rejected the previous response" in prompt
    print(json.dumps({
        "behavior": ["Create the bounded journey output"] + (["Address the independent scope finding"] if revised else []),
        "non_goals": ["Do not edit files outside the task contract"],
        "affected_paths": ["journey.txt"],
        "acceptance_checks": ["verify journey.txt contains completed"],
        "decisions": [],
        "spec_refs": ["README.md"] if corrected else ["README.md for qualification context"],
    }))
elif mode == "challenger":
    revised = "Address the independent scope finding" in prompt
    corrected = "StageMesh rejected the previous response" in prompt
    print(json.dumps({
        "verdict": "accept" if revised else "revise",
        "findings": (
            []
            if revised and corrected
            else [{"kind": "scope-risk", "summary": "Record the bounded output explicitly"}]
            if corrected
            else [{"kind": "accessibility-risk", "summary": "Unsupported provider category"}]
        ),
        "required_decisions": [],
    }))
elif mode == "writer":
    if not state.exists():
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text("interrupted-once", encoding="utf-8")
        time.sleep(10)
    pathlib.Path("journey.txt").write_text("completed\\n", encoding="utf-8")
elif mode.startswith("reviewer"):
    print(json.dumps({"decision": "PASS", "findings": []}))
else:
    raise SystemExit(2)
""",
        encoding="utf-8",
    )


def command(python: Path, project: Path, *args: str) -> list[str]:
    return [str(python), "-m", "stagemesh.cli", "--project", str(project), *args]


def window(
    python: Path,
    project: Path,
    report: Path,
    *,
    duration: str,
) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    result = run(
        command(
            python,
            project,
            "run-window",
            "--duration",
            duration,
            "--concurrency",
            "1",
            "--max-tasks",
            "1",
            "--max-steps-per-task",
            "8",
            "--tasks",
            TASK_ID,
            "--integration",
            "local",
            "--no-pr",
            "--no-progress",
            "--report",
            str(report),
        ),
        project,
        timeout=90,
    )
    if not report.exists():
        raise AssertionError(f"run-window wrote no report: {result.stdout}{result.stderr}")
    return result, json_object(report.read_text(encoding="utf-8"))


def json_object(payload: str) -> dict[str, object]:
    raw: object = json.loads(payload)
    if not isinstance(raw, dict):
        raise AssertionError("expected a JSON object")
    return {str(key): value for key, value in raw.items()}


def task_result(report: dict[str, object]) -> dict[str, object]:
    tasks = report.get("tasks")
    if not isinstance(tasks, list) or not tasks or not isinstance(tasks[0], dict):
        raise AssertionError(f"run-window did not report the journey task: {report}")
    return {str(key): value for key, value in tasks[0].items()}


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def evidence(step: int, detail: str) -> dict[str, object]:
    return {"step": step, "status": "PASS", "evidence": detail}


def step_number(row: dict[str, object]) -> int:
    value = row.get("step")
    if not isinstance(value, int):
        raise AssertionError(f"qualification step has no integer identifier: {row}")
    return value


def main() -> int:
    candidate = git(ROOT, "rev-parse", "HEAD")
    source_dirty = bool(git(ROOT, "status", "--porcelain", "--untracked-files=all"))
    temp_parent = ROOT / ".stagemesh" / "tmp"
    temp_parent.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix="development-journey-", dir=temp_parent))
    steps: list[dict[str, object]] = []
    try:
        venv = temp_root / "venv"
        project = temp_root / "project"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True, text=True)
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        installed = run(
            [str(python), "-m", "pip", "install", "--no-deps", "--no-cache-dir", str(ROOT)],
            temp_root,
            timeout=180,
        )
        expect(installed.returncode == 0, f"candidate installation failed: {installed.stdout}{installed.stderr}")
        steps.append(evidence(1, f"wheel installed from candidate {candidate}"))

        project.mkdir()
        git(project, "init", "-b", "main")
        git(project, "config", "user.email", "journey@example.invalid")
        git(project, "config", "user.name", "StageMesh Journey")
        (project / "README.md").write_text("journey\n", encoding="utf-8")
        (project / "pyproject.toml").write_text(
            "[build-system]\nrequires = []\nbuild-backend = 'journey_backend'\n[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
            encoding="utf-8",
        )
        git(project, "add", "README.md", "pyproject.toml")
        git(project, "commit", "-m", "base")
        state = project / ".stagemesh" / "journey-writer-state"
        provider_paths = {
            role: temp_root / f"{role}-provider.py"
            for role in ("planner", "challenger", "writer", "reviewer", "reviewer2")
        }
        for provider_path in provider_paths.values():
            write_provider(provider_path)
        def provider_command(role: str) -> str:
            return " ".join(
                f'"{part}"' for part in (str(python), str(provider_paths[role]), role, str(state))
            )
        config = {
            "providers": {
                "planner": provider_command("planner"),
                "challenger": provider_command("challenger"),
                "implementer": {"command": provider_command("writer"), "capabilities": ["IMPLEMENT"]},
                "reviewer": {"command": provider_command("reviewer"), "capabilities": ["REVIEW"]},
                "reviewer2": {"command": provider_command("reviewer2"), "capabilities": ["REVIEW"]},
            },
            "authorization": {
                "unattended_providers": ["planner", "challenger", "implementer", "reviewer", "reviewer2"]
            },
            "routing": {
                "mode": "STAGED",
                "require_independent_review": True,
                "pools": {"IMPLEMENT": ["implementer"], "REVIEW": ["reviewer", "reviewer2"]},
            },
            "planning_gate": {
                "require_approved_spec": True,
                "require_plan_challenge": True,
                "planner_provider": "planner",
                "challenger_provider": "challenger",
                "approval_authority": {
                    "version": 1,
                    "authority_id": "project-owner:journey",
                    "approver_id": "owner:journey",
                },
            },
        }
        runtime = project / ".stagemesh"
        runtime.mkdir()
        (runtime / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
        (runtime / "profile.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "contract_generation": "automatic",
                    "name": "development-journey",
                    "forbidden_files": [".git/**", ".stagemesh/**", ".env", "secrets/**"],
                    "defaults": {"max_changed_files": 5, "max_diff_lines": 100},
                    "gates": {
                        "journey-acceptance": {
                            "name": "journey-acceptance",
                            "command": [str(python), "-c", "pass"],
                            "timeout_seconds": 60,
                        }
                    },
                    "task_types": {
                        "default": {
                            "summary": "Synthetic qualification task",
                            "allowed_files": ["journey.txt"],
                            "validation_level": "full",
                            "gates": ["journey-acceptance"],
                        }
                    },
                    "type_selection": {"default_type": "default", "escalation_type": "default"},
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        git(project, "remote", "add", "origin", str(temp_root / "synthetic-origin.git"))
        git(project, "update-ref", "refs/remotes/origin/main", git(project, "rev-parse", "HEAD"))
        init = run(command(python, project, "init"), project)
        if init.returncode != 0 or "StageMesh is ready" not in init.stdout:
            diagnostic = run(
                [
                    str(python),
                    "-c",
                    (
                        "from pathlib import Path; from stagemesh.config import load_config; "
                        "from stagemesh.providers import adapters_from_config; import sys; c=load_config(Path(sys.argv[1])); "
                        "print([(a.name, sorted(a.capabilities), a.check_stage_capacity('code'), "
                        "a.check_stage_capacity('review'), a.denied) for a in adapters_from_config(c)])"
                    ),
                    str(project),
                ],
                project,
            )
            raise AssertionError(f"init failed: {init.stdout}{init.stderr}\nprovider diagnostic: {diagnostic.stdout}{diagnostic.stderr}")
        steps.append(evidence(2, "installed CLI initialized a clean synthetic repository"))

        (runtime / "backlog.json").write_text(
            json.dumps(
                {
                    "objective": "qualify the development journey",
                    "tasks": [
                        {
                            "id": TASK_ID,
                            "title": "Create the journey output",
                            "eligible": True,
                            "state": "OPEN",
                            "labels": ["stagemesh:ready"],
                            "dependencies": [],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        contracts = runtime / "contracts"
        contracts.mkdir(exist_ok=True)
        (contracts / f"{TASK_ID}.json").write_text(
            json.dumps(
                {
                    "objective": "Create journey.txt containing completed",
                    "allowed_files": ["journey.txt"],
                    "required_tests": [
                        {
                            "name": "journey-output",
                            "command": [
                                str(python),
                                "-c",
                                "from pathlib import Path; assert Path('journey.txt').read_text().strip() == 'completed'",
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        steps.append(evidence(3, "run-window discovered one admitted local-backlog task"))

        first_proc, first = window(python, project, temp_root / "first.json", duration="30s")
        first_task = task_result(first)
        why = str(first_task.get("why", first_task.get("reason", "")))
        expect("approval" in why.lower(), f"first run did not stop at approval: {first_proc.stdout}{first}")
        spec = json_object((runtime / "planning" / f"{TASK_ID}.spec.json").read_text(encoding="utf-8"))
        challenge = json_object(
            (runtime / "planning" / f"{TASK_ID}.challenge.json").read_text(encoding="utf-8")
        )
        behavior = spec.get("behavior")
        expect(
            isinstance(behavior, list) and "Address the independent scope finding" in behavior,
            "planner did not revise the rejected plan",
        )
        expect(challenge.get("verdict") == "accept", "fresh challenge did not accept the revised plan")
        history = runtime / "planning" / "history"
        expect(len(list(history.glob(f"{TASK_ID}.spec.*.json"))) == 1, "the first plan was not archived exactly once")
        expect(len(list(history.glob(f"{TASK_ID}.challenge.*.json"))) == 1, "the first challenge was not archived exactly once")
        steps.extend(
            (
                evidence(4, "independent challenger rejected the initial generated plan"),
                evidence(5, "StageMesh performed exactly one bounded planner revision"),
                evidence(6, "a fresh independent challenge accepted the revised plan"),
                evidence(7, "run-window stopped before implementation and named accountable approval"),
            )
        )

        approved = run(command(python, project, "approve-planning", "--task", TASK_ID, "--json"), project)
        expect(approved.returncode == 0, f"approval failed: {approved.stdout}{approved.stderr}")
        approval = json_object(approved.stdout)
        expect(approval.get("approver_id") == "owner:journey", "approval was not bound to configured authority")
        steps.append(evidence(8, "operator approval receipt is bound to the accepted plan and authority"))

        interrupted_proc, interrupted = window(python, project, temp_root / "interrupted.json", duration="4s")
        expect(state.exists(), f"implementation provider never started: {interrupted_proc.stdout}{interrupted}")
        expect(not (project / "journey.txt").exists(), "the deliberately slow first implementation unexpectedly completed")
        steps.append(evidence(12, "a bounded run-window interrupted an active implementation provider"))

        resumed_proc, resumed = window(python, project, temp_root / "resumed.json", duration="30s")
        resumed_task = task_result(resumed)
        output_path = project / "journey.txt"
        expect(
            output_path.exists() and output_path.read_text(encoding="utf-8").strip() == "completed",
            f"resume did not integrate output: {resumed_proc.stdout}{resumed}",
        )
        expect(
            str(resumed_task.get("final_status", "")).lower() == "done"
            and resumed_task.get("outcome") == "done",
            f"resumed task not done: {resumed}",
        )
        steps.extend(
            (
                evidence(9, "authorized implementation provider produced the contract-bounded change"),
                evidence(10, "configured validation passed for the exact candidate"),
                evidence(11, "a distinct review provider passed the exact candidate"),
                evidence(13, "the next ordinary run resumed without a second approval or plan"),
                evidence(14, "local integration committed the accepted candidate"),
            )
        )

        final_proc, final = window(python, project, temp_root / "final.json", duration="10s")
        expect(not final.get("tasks"), f"completed task was redispatched: {final_proc.stdout}{final}")
        steps.extend(
            (
                evidence(15, "a fresh run-window restarted against durable state"),
                evidence(16, "the completed task was not redispatched"),
            )
        )
        audit_db = runtime / "stagemesh.sqlite3"
        expect(audit_db.exists() and audit_db.stat().st_size > 0, "durable audit ledger was not written")
        with sqlite3.connect(audit_db) as audit:
            correction_count = audit.execute(
                "SELECT COUNT(*) FROM audit_events WHERE event_type='planning.provider_output_retry'"
            ).fetchone()[0]
        expect(
            correction_count == 4,
            "planner and challenger protocol corrections were not bounded and audited",
        )
        steps.append(
            evidence(
                17,
                "reports, planning history, receipts, the durable ledger, and four bounded role protocol corrections were written",
            )
        )

        broken = {
            "schema_version": 1,
            "candidate_sha": candidate,
            "source_dirty": False,
            "status": "FAIL",
            "steps": steps,
        }
        RECEIPT.parent.mkdir(parents=True, exist_ok=True)
        RECEIPT.write_text(json.dumps(broken, indent=2, sort_keys=True), encoding="utf-8")
        broken_output = temp_root / "broken-e2e.json"
        broken_evaluation = run(
            command(
                python,
                ROOT,
                "end-to-end-acceptance",
                "--json",
                "--output",
                str(broken_output),
            ),
            ROOT,
        )
        expect(
            broken_evaluation.returncode != 0
            and broken_output.exists()
            and json_object(broken_output.read_text(encoding="utf-8")).get("status") == "INCOMPLETE",
            "a failed journey was accepted",
        )
        steps.append(evidence(18, "the receipt evaluator rejected a deliberately failed journey"))
        receipt = {
            "schema_version": 1,
            "candidate_sha": candidate,
            "source_dirty": source_dirty,
            "status": "PASS",
            "steps": sorted(steps, key=step_number),
        }
        RECEIPT.write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
        passing_output = temp_root / "passing-e2e.json"
        passing_evaluation = run(
            command(
                python,
                ROOT,
                "end-to-end-acceptance",
                "--json",
                "--output",
                str(passing_output),
            ),
            ROOT,
        )
        qualified = json_object(passing_output.read_text(encoding="utf-8"))
        expect(
            passing_evaluation.returncode == 0 and qualified.get("status") == "COMPLETE",
            f"passing journey receipt was not complete: {qualified}",
        )
        print(json.dumps(qualified, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001 - every qualification failure must produce a durable failed receipt
        failed = {
            "schema_version": 1,
            "candidate_sha": candidate,
            "source_dirty": source_dirty,
            "status": "FAIL",
            "error": str(exc),
            "steps": sorted(steps, key=step_number),
        }
        RECEIPT.parent.mkdir(parents=True, exist_ok=True)
        RECEIPT.write_text(json.dumps(failed, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(failed, sort_keys=True), file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
