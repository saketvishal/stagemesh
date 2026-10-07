from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    merged = os.environ.copy()
    merged.update(env or {})
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, env=merged, check=False)
    if result.returncode != 0:
        raise AssertionError(f"{command} failed\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
    return result.stdout


def run_failure(command: list[str], cwd: Path, expected: str) -> None:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)
    output = result.stdout + result.stderr
    if result.returncode == 0 or expected not in output:
        raise AssertionError(output)


def assert_project_local_workflows() -> None:
    ci_workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    autonomy_workflow = (ROOT / ".github" / "workflows" / "autonomy.yml").read_text(encoding="utf-8")
    if "PYTHONPATH: src" in ci_workflow or "PYTHONPATH: src" in autonomy_workflow:
        raise AssertionError("hosted workflows must not rely on PYTHONPATH=src")
    if "python scripts/bootstrap_stagemesh.py" not in ci_workflow:
        raise AssertionError("ci workflow must bootstrap the project-owned StageMesh runtime")
    if "./.stagemesh/bin/stagemesh --project . ci --future-feature-gate" not in ci_workflow:
        raise AssertionError("linux ci workflow must use the project-local StageMesh shim")
    if ".stagemesh\\bin\\stagemesh.cmd --project . ci --future-feature-gate" not in ci_workflow:
        raise AssertionError("windows ci workflow must use the project-local StageMesh shim")
    if "python scripts/bootstrap_stagemesh.py" not in autonomy_workflow:
        raise AssertionError("autonomy workflow must bootstrap the project-owned StageMesh runtime")


def assert_installed_runtime_identity(project: Path) -> None:
    doctor = run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "doctor"], ROOT)
    required = ["version:", "executable path:", "python interpreter:", "imported package path:", "editable/development status: installed"]
    missing = [item for item in required if item not in doctor]
    if missing:
        raise AssertionError(f"doctor missing {missing}\n{doctor}")
    if str(ROOT / "src") in doctor:
        raise AssertionError(f"doctor imported from repository source checkout\n{doctor}")


def assert_plain_continue(project: Path) -> None:
    run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "init"], ROOT)
    objective = project / "objective.json"
    objective.write_text(
        json.dumps(
            {
                "id": "project-local-acceptance",
                "title": "project-local acceptance",
                "tasks": [
                    {"id": "one", "title": "first bounded task"},
                    {"id": "two", "title": "dependent bounded task", "dependencies": ["one"]},
                ],
            }
        ),
        encoding="utf-8",
    )
    run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "plan", str(objective)], ROOT)
    for _ in range(12):
        run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "continue", "--once", "--dry-run"], ROOT)
    status_json = run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "status", "--json"], ROOT)
    status = json.loads(status_json)
    if status.get("done_count") != 2 or status.get("blocked_task_count") != 0 or status.get("failed_execution_count") != 0:
        raise AssertionError(status_json)


def assert_objective_root_refusal(project: Path) -> None:
    run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "init"], ROOT)
    store_db = project / ".stagemesh" / "stagemesh.sqlite3"
    setup = (
        "from stagemesh.persistence import Store; "
        f"s=Store(r'{store_db}'); s.migrate(); "
        "task=s.upsert_task('objective root', source='github', source_id='71'); "
        "s.cache_source('github','71',{'eligible':True,'state':'OPEN','labels':['stagemesh:objective'],'objective_root':True},'OPEN'); "
        "s.save_objective('71','objective root',{'source':'github','source_id':'71'}); "
        "s.close(); print(task)"
    )
    task_id = run([sys.executable, "-c", setup], ROOT).strip()
    run_failure(
        [sys.executable, "-m", "stagemesh.cli", "--project", str(project), "run-ready", "--task", task_id, "--dry-run"],
        ROOT,
        "objective_root_not_runnable",
    )


def main() -> int:
    assert_project_local_workflows()
    with tempfile.TemporaryDirectory(prefix="stagemesh-project-local-acceptance-") as raw:
        project = Path(raw) / "project"
        project.mkdir()
        assert_installed_runtime_identity(project)
        assert_plain_continue(project)
    with tempfile.TemporaryDirectory(prefix="stagemesh-objective-root-acceptance-") as raw:
        project = Path(raw) / "project"
        project.mkdir()
        assert_objective_root_refusal(project)
    print("project-local acceptance: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
