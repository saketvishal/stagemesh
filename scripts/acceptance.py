from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    merged = os.environ.copy()
    merged.update(env or {})
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, env=merged)
    if result.returncode != 0:
        raise AssertionError(f"{command} failed\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
    return result.stdout


def run_failure(command: list[str], cwd: Path, expected: str, env: dict[str, str] | None = None) -> None:
    merged = os.environ.copy()
    merged.update(env or {})
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, env=merged, check=False)
    output = result.stdout + result.stderr
    if result.returncode != 2 or expected not in output:
        raise AssertionError(output)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="stagemesh-acceptance-") as raw:
        project = Path(raw) / "project"
        project.mkdir()
        env = {"PYTHONPATH": str(ROOT / "src")}
        run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "init"], ROOT, env)
        init_json = run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "init", "--json"], ROOT, env)
        init_data = json.loads(init_json)
        if (
            init_data["project"] != str(project.resolve())
            or init_data["runtime"] != str((project / ".stagemesh").resolve())
            or init_data["db"] != str((project / ".stagemesh" / "stagemesh.sqlite3").resolve())
            or init_data["schema_version"] != 2
            or init_data["registered"] is not False
        ):
            raise AssertionError(init_json)
        registry = project / "registry.json"
        run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "init",
                "--register",
                "--registry",
                str(registry),
            ],
            ROOT,
            env,
        )
        run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "init",
                "--register",
                "--registry",
                str(registry),
            ],
            ROOT,
            env,
        )
        registry_output = run(
            [sys.executable, "-m", "stagemesh.cli", "--project", str(project), "registry", "--registry", str(registry)],
            ROOT,
            env,
        )
        if "project" not in registry_output:
            raise AssertionError(registry_output)
        registry_json_output = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "registry",
                "--registry",
                str(registry),
                "--json",
            ],
            ROOT,
            env,
        )
        registry_json_data = json.loads(registry_json_output)
        if registry_json_data != {
            "projects": [
                {
                    "name": "project",
                    "path": str(project.resolve()),
                    "db_path": str(project.resolve() / ".stagemesh" / "stagemesh.sqlite3"),
                }
            ]
        }:
            raise AssertionError(registry_json_data)
        registry_data = json.loads(registry.read_text(encoding="utf-8"))
        if len(registry_data["projects"]) != 1 or Path(registry_data["projects"][0]["path"]) != project.resolve():
            raise AssertionError(registry_data)
        objective = project / "objective.json"
        objective.write_text(
            json.dumps(
                {
                    "id": "obj-1",
                    "title": "synthetic objective",
                    "tasks": [
                        {"id": "one", "title": "first task"},
                        {"id": "two", "title": "dependent task", "dependencies": ["one"]},
                        {"id": "deferred", "title": "future work", "eligible": False},
                    ],
                }
            ),
            encoding="utf-8",
        )
        plan_json = run(
            [sys.executable, "-m", "stagemesh.cli", "--project", str(project), "plan", str(objective), "--json"],
            ROOT,
            env,
        )
        plan_data = json.loads(plan_json)
        if (
            plan_data["objective_id"] != "obj-1"
            or plan_data["title"] != "synthetic objective"
            or plan_data["task_count"] != 3
            or plan_data["backlog"] != str((project / ".stagemesh" / "backlog.json").resolve())
        ):
            raise AssertionError(plan_json)
        invalid_objective = project / "invalid-objective.json"
        invalid_objective.write_text(
            json.dumps(
                {
                    "id": "obj-invalid",
                    "title": "invalid objective",
                    "tasks": [{"id": "bad", "title": "bad task", "dependencies": ["missing"]}],
                }
            ),
            encoding="utf-8",
        )
        invalid_plan = subprocess.run(
            [sys.executable, "-m", "stagemesh.cli", "--project", str(project), "plan", str(invalid_objective)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            env={**os.environ.copy(), **env},
            check=False,
        )
        if invalid_plan.returncode == 0 or "unknown dependency" not in (invalid_plan.stdout + invalid_plan.stderr):
            raise AssertionError(invalid_plan.stdout + invalid_plan.stderr)
        invalid_json_objective = project / "invalid-json-objective.json"
        invalid_json_objective.write_text("{not-json", encoding="utf-8")
        run_failure(
            [sys.executable, "-m", "stagemesh.cli", "--project", str(project), "plan", str(invalid_json_objective)],
            ROOT,
            "objective error:",
            env,
        )
        outside_objective = project.parent / "outside-objective.json"
        outside_objective.write_text('{"id":"outside","title":"outside","tasks":[]}', encoding="utf-8")
        outside_plan = subprocess.run(
            [sys.executable, "-m", "stagemesh.cli", "--project", str(project), "plan", str(outside_objective)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            env={**os.environ.copy(), **env},
            check=False,
        )
        if outside_plan.returncode != 2 or "security boundary error:" not in (
            outside_plan.stdout + outside_plan.stderr
        ):
            raise AssertionError(outside_plan.stdout + outside_plan.stderr)
        backlog = project / ".stagemesh" / "backlog.json"
        first_continue_json = run(
            [sys.executable, "-m", "stagemesh.cli", "--project", str(project), "continue", "--once", "--json"],
            ROOT,
            env,
        )
        if json.loads(first_continue_json)["progressed"] <= 0:
            raise AssertionError(first_continue_json)
        for _ in range(16):
            run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "continue", "--once"], ROOT, env)
        status = run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "status"], ROOT, env)
        if "DONE DONE first task" not in status or "DONE DONE dependent task" not in status:
            raise AssertionError(status)
        if "future work" in status:
            raise AssertionError("deferred task was dispatched")
        status_json = run(
            [sys.executable, "-m", "stagemesh.cli", "--project", str(project), "status", "--json"],
            ROOT,
            env,
        )
        status_data = json.loads(status_json)
        if (
            status_data["done_count"] != 2
            or status_data["backlog_state"] != "ACTIVE"
            or status_data["blocked_task_count"] != 0
            or status_data["failed_execution_count"] != 0
            or status_data["unknown_execution_count"] != 0
        ):
            raise AssertionError(status_json)
        if {task["title"] for task in status_data["tasks"]} != {"first task", "dependent task"}:
            raise AssertionError(status_json)
        adapter_tasks = project / "adapter-tasks.json"
        adapter_tasks.write_text(
            '{"tasks":[{"id":"adapter-1","title":"configured adapter task"}]}',
            encoding="utf-8",
        )
        config_file = project / ".stagemesh" / "config.json"
        google_ax_tasks = project / "google-ax-tasks.json"
        google_ax_tasks.write_text(
            '{"tasks":[{"id":"google-ax-1","title":"google ax exported task"}]}',
            encoding="utf-8",
        )
        config_file.write_text(
            '{"task_sources":[{"name":"linear","type":"json","path":"adapter-tasks.json"},{"name":"google-ax","type":"google-ax","path":"google-ax-tasks.json"}]}',
            encoding="utf-8",
        )
        config_with_source = run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "config"], ROOT, env)
        if "task_source.linear: json" not in config_with_source:
            raise AssertionError(config_with_source)
        config_source_json = run(
            [sys.executable, "-m", "stagemesh.cli", "--project", str(project), "config", "--json"],
            ROOT,
            env,
        )
        config_source_data = json.loads(config_source_json)
        if (
            len(config_source_data["task_sources"]) != 2
            or config_source_data["task_sources"][0]["name"] != "linear"
            or config_source_data["task_sources"][0]["type"] != "json"
            or not config_source_data["task_sources"][0]["path"].endswith("adapter-tasks.json")
            or config_source_data["task_sources"][1]["name"] != "google-ax"
            or config_source_data["task_sources"][1]["type"] != "google-ax"
            or not config_source_data["task_sources"][1]["path"].endswith("google-ax-tasks.json")
        ):
            raise AssertionError(config_source_json)
        for _ in range(8):
            run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "continue", "--once"], ROOT, env)
        status = run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "status"], ROOT, env)
        if "DONE DONE configured adapter task" not in status or "DONE DONE google ax exported task" not in status:
            raise AssertionError(status)
        original_backlog = backlog.read_text(encoding="utf-8")
        backlog.write_text('{"tasks":[{"id":"bad","title":"bad","dependencies":["missing"]}]}', encoding="utf-8")
        invalid_backlog = subprocess.run(
            [sys.executable, "-m", "stagemesh.cli", "--project", str(project), "continue", "--once"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            env={**os.environ.copy(), **env},
            check=False,
        )
        backlog.write_text(original_backlog, encoding="utf-8")
        if invalid_backlog.returncode != 2 or "task source error:" not in (invalid_backlog.stdout + invalid_backlog.stderr):
            raise AssertionError(invalid_backlog.stdout + invalid_backlog.stderr)
        audit_log = project / ".stagemesh" / "audit.jsonl"
        audit_output = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "audit",
                "--output",
                str(audit_log),
            ],
            ROOT,
            env,
        )
        if not audit_log.exists() or "audit:" not in audit_output:
            raise AssertionError(audit_output)
        audit_json = run(
            [sys.executable, "-m", "stagemesh.cli", "--project", str(project), "audit", "--json", "--limit", "5"],
            ROOT,
            env,
        )
        audit_data = json.loads(audit_json)
        audit_event_types = {event["event_type"] for event in audit_data["events"]}
        if "task.advance" not in audit_event_types or any(not isinstance(event["payload"], dict) for event in audit_data["events"]):
            raise AssertionError(audit_json)
        invalid_audit = subprocess.run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "audit",
                "--output",
                str(audit_log),
                "--limit",
                "0",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            env={**os.environ.copy(), **env},
            check=False,
        )
        if invalid_audit.returncode != 2 or "audit error:" not in (invalid_audit.stdout + invalid_audit.stderr):
            raise AssertionError(invalid_audit.stdout + invalid_audit.stderr)
        retry_output = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "retries",
                "fail",
                "github:acceptance",
                "--reason",
                "rate-limit",
            ],
            ROOT,
            env,
        )
        if "attempts=1" not in retry_output:
            raise AssertionError(retry_output)
        retry_list = run(
            [sys.executable, "-m", "stagemesh.cli", "--project", str(project), "retries", "list"],
            ROOT,
            env,
        )
        if "github:acceptance" not in retry_list:
            raise AssertionError(retry_list)
        retry_json = run(
            [sys.executable, "-m", "stagemesh.cli", "--project", str(project), "retries", "list", "--json"],
            ROOT,
            env,
        )
        retry_data = json.loads(retry_json)
        if retry_data["retries"][0]["key"] != "github:acceptance" or retry_data["retries"][0]["attempts"] != 1:
            raise AssertionError(retry_json)
        retry_fail_json = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "retries",
                "fail",
                "github:json",
                "--reason",
                "secondary-limit",
                "--json",
            ],
            ROOT,
            env,
        )
        retry_fail_data = json.loads(retry_fail_json)
        if (
            retry_fail_data["key"] != "github:json"
            or retry_fail_data["allowed"] is not False
            or retry_fail_data["attempts"] != 1
            or retry_fail_data["reason"] != "secondary-limit"
        ):
            raise AssertionError(retry_fail_json)
        invalid_retry = subprocess.run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "retries",
                "fail",
                "github:invalid",
                "--reason",
                "",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            env={**os.environ.copy(), **env},
            check=False,
        )
        if invalid_retry.returncode != 2 or "retry error:" not in (invalid_retry.stdout + invalid_retry.stderr):
            raise AssertionError(invalid_retry.stdout + invalid_retry.stderr)
        run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "retries",
                "success",
                "github:acceptance",
            ],
            ROOT,
            env,
        )
        retry_success_json = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "retries",
                "success",
                "github:json",
                "--json",
            ],
            ROOT,
            env,
        )
        if json.loads(retry_success_json) != {"cleared": True, "key": "github:json"}:
            raise AssertionError(retry_success_json)
        empty_retry_json = run(
            [sys.executable, "-m", "stagemesh.cli", "--project", str(project), "retries", "list", "--json"],
            ROOT,
            env,
        )
        if json.loads(empty_retry_json)["retries"] != []:
            raise AssertionError(empty_retry_json)
        project_candidate = run(["git", "rev-parse", "HEAD"], project).strip()
        evidence_add = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "evidence",
                "add",
                "hosted-ci",
                "PASS",
                "https://example.invalid/run/acceptance",
                "--candidate-sha",
                project_candidate,
            ],
            ROOT,
            env,
        )
        if "evidence:" not in evidence_add:
            raise AssertionError(evidence_add)
        evidence_id = evidence_add.strip().split()[-1]
        duplicate_evidence_add = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "evidence",
                "add",
                "hosted-ci",
                "PASS",
                "https://example.invalid/run/acceptance",
                "--candidate-sha",
                project_candidate,
            ],
            ROOT,
            env,
        )
        if duplicate_evidence_add != evidence_add:
            raise AssertionError(duplicate_evidence_add)
        evidence_add_json = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "evidence",
                "add",
                "hosted-ci",
                "PASS",
                "https://example.invalid/run/acceptance",
                "--candidate-sha",
                project_candidate,
                "--json",
            ],
            ROOT,
            env,
        )
        evidence_add_data = json.loads(evidence_add_json)
        if (
            evidence_add_data["id"] != evidence_id
            or evidence_add_data["kind"] != "hosted-ci"
            or evidence_add_data["status"] != "PASS"
            or evidence_add_data["candidate_sha"] != project_candidate
            or evidence_add_data["candidate_match"] is not True
        ):
            raise AssertionError(evidence_add_json)
        mismatch_evidence = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "evidence",
                "add",
                "live-provider",
                "PASS",
                "https://example.invalid/provider",
                "--candidate-sha",
                "def5678",
            ],
            ROOT,
            env,
        )
        if "evidence:" not in mismatch_evidence:
            raise AssertionError(mismatch_evidence)
        mismatch_evidence_json = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "evidence",
                "add",
                "live-provider",
                "PASS",
                "https://example.invalid/provider",
                "--candidate-sha",
                "def5678",
                "--json",
            ],
            ROOT,
            env,
        )
        if json.loads(mismatch_evidence_json)["candidate_match"] is not False:
            raise AssertionError(mismatch_evidence_json)
        evidence_list = run(
            [sys.executable, "-m", "stagemesh.cli", "--project", str(project), "evidence", "list"],
            ROOT,
            env,
        )
        if f"hosted-ci PASS {project_candidate}" not in evidence_list:
            raise AssertionError(evidence_list)
        evidence_json = run(
            [sys.executable, "-m", "stagemesh.cli", "--project", str(project), "evidence", "list", "--json"],
            ROOT,
            env,
        )
        evidence_data = json.loads(evidence_json)
        if evidence_data["candidate_sha"] != project_candidate:
            raise AssertionError(evidence_json)
        evidence_matches = {record["kind"]: record["candidate_match"] for record in evidence_data["records"]}
        if evidence_matches != {"hosted-ci": True, "live-provider": False}:
            raise AssertionError(evidence_json)
        if len(evidence_data["records"]) != 2:
            raise AssertionError(evidence_json)
        invalid_evidence = subprocess.run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "evidence",
                "add",
                "hosted-ci",
                "PASS",
                "not-a-url",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            env={**os.environ.copy(), **env},
            check=False,
        )
        if invalid_evidence.returncode != 2 or "external evidence error:" not in (
            invalid_evidence.stdout + invalid_evidence.stderr
        ):
            raise AssertionError(invalid_evidence.stdout + invalid_evidence.stderr)
        doctor = run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "doctor"], ROOT, env)
        required = [
            "version:",
            "executable path:",
            "python interpreter:",
            "imported package path:",
            "db:",
            "schema version: 2",
            "backend: sqlite",
        ]
        missing = [item for item in required if item not in doctor]
        if missing:
            raise AssertionError(f"doctor missing {missing}")
        doctor_json = run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "doctor", "--json"], ROOT, env)
        doctor_data = json.loads(doctor_json)
        if (
            doctor_data["schema_version"] != 2
            or doctor_data["backend"] != "sqlite"
            or doctor_data["backend_available"] is not True
            or doctor_data["github_configured"] is not False
            or doctor_data["project"] != str(project.resolve())
            or doctor_data["db"] != str((project / ".stagemesh" / "stagemesh.sqlite3").resolve())
        ):
            raise AssertionError(doctor_json)
        config_output = run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "config"], ROOT, env)
        if (
            "github.configured: False" not in config_output
            or "database_url: sqlite://default" not in config_output
            or "routing.mode: STAGED" not in config_output
        ):
            raise AssertionError(config_output)
        config_json = run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "config", "--json"], ROOT, env)
        config_data = json.loads(config_json)
        if (
            config_data["github"]["configured"] is not False
            or config_data["database_url"] != "sqlite://default"
            or config_data["routing"]["mode"] != "STAGED"
            or "token" in config_data["github"]
        ):
            raise AssertionError(config_json)
        invalid_config = project / "invalid-config.json"
        invalid_config.write_text('{"routing":{"mode":"ROUND_ROBIN"}}', encoding="utf-8")
        invalid_config_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "config",
                "--config",
                str(invalid_config),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            env={**os.environ.copy(), **env},
            check=False,
        )
        if invalid_config_result.returncode != 2 or "config error: unsupported routing mode" not in (
            invalid_config_result.stdout + invalid_config_result.stderr
        ):
            raise AssertionError(invalid_config_result.stdout + invalid_config_result.stderr)
        invalid_config.write_text("{not-json", encoding="utf-8")
        run_failure(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "config",
                "--config",
                str(invalid_config),
            ],
            ROOT,
            "config error:",
            env,
        )
        broken_registry = project / "broken-registry.json"
        broken_registry.write_text("{not-json", encoding="utf-8")
        run_failure(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "registry",
                "--registry",
                str(broken_registry),
            ],
            ROOT,
            "registry error:",
            env,
        )
        broken_registry.write_text(
            json.dumps(
                {
                    "projects": [
                        {"name": "dup", "path": str(project), "db_path": str(project / ".stagemesh" / "one.sqlite3")},
                        {"name": "dup", "path": str(project / "other"), "db_path": str(project / ".stagemesh" / "two.sqlite3")},
                    ]
                }
            ),
            encoding="utf-8",
        )
        run_failure(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "registry",
                "--registry",
                str(broken_registry),
            ],
            ROOT,
            "registry error:",
            env,
        )
        backend_output = run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "backend"], ROOT, env)
        if (
            "name: sqlite" not in backend_output
            or "available: True" not in backend_output
            or "postgres schema contract: 18 tables" not in backend_output
        ):
            raise AssertionError(backend_output)
        backend_json = run(
            [sys.executable, "-m", "stagemesh.cli", "--project", str(project), "backend", "--json"],
            ROOT,
            env,
        )
        backend_data = json.loads(backend_json)
        if (
            backend_data["name"] != "sqlite"
            or backend_data["available"] is not True
            or backend_data["database_url"] != "sqlite://default"
            or backend_data["migration_applied"] is not None
            or backend_data["postgres_schema_contract"]["table_count"] != 18
            or "tasks" not in backend_data["postgres_schema_contract"]["tables"]
        ):
            raise AssertionError(backend_json)
        postgres_config = project / "postgres-config.json"
        postgres_config.write_text('{"database_url":"postgresql://example/db"}', encoding="utf-8")
        postgres_backend = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "backend",
                "--config",
                str(postgres_config),
            ],
            ROOT,
            env,
        )
        if "name: postgres" not in postgres_backend or "sqlite default available" in postgres_backend:
            raise AssertionError(postgres_backend)
        postgres_backend_json = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "backend",
                "--config",
                str(postgres_config),
                "--json",
            ],
            ROOT,
            env,
        )
        postgres_backend_data = json.loads(postgres_backend_json)
        if (
            postgres_backend_data["name"] != "postgres"
            or postgres_backend_data["database_url"] != "postgresql://example/db"
            or postgres_backend_data["migration_applied"] is not None
            or postgres_backend_data["postgres_schema_contract"]["table_count"] != 18
        ):
            raise AssertionError(postgres_backend_json)
        provider_acceptance = run(
            [sys.executable, "-m", "stagemesh.cli", "--project", str(project), "provider-acceptance"],
            ROOT,
            env,
        )
        if (
            "status: PASS" not in provider_acceptance
            or "chosen_provider: secondary" not in provider_acceptance
            or "single_agent_provider: solo" not in provider_acceptance
            or "review_provider: reviewer" not in provider_acceptance
        ):
            raise AssertionError(provider_acceptance)
        provider_acceptance_json = run(
            [sys.executable, "-m", "stagemesh.cli", "--project", str(project), "provider-acceptance", "--json"],
            ROOT,
            env,
        )
        provider_acceptance_data = json.loads(provider_acceptance_json)
        if (
            provider_acceptance_data["status"] != "PASS"
            or provider_acceptance_data["chosen_provider"] != "secondary"
            or provider_acceptance_data["execution_status"] != "SUCCEEDED"
            or provider_acceptance_data["capacity_failure_isolated"] is not True
            or provider_acceptance_data["single_agent_provider"] != "solo"
            or provider_acceptance_data["review_provider"] != "reviewer"
        ):
            raise AssertionError(provider_acceptance_json)
        github_acceptance = run(
            [sys.executable, "-m", "stagemesh.cli", "--project", str(project), "github-acceptance"],
            ROOT,
            env,
        )
        if (
            "status: PASS" not in github_acceptance
            or "rate_limit_status: UNKNOWN" not in github_acceptance
            or "detected_repo: stage/mesh" not in github_acceptance
        ):
            raise AssertionError(github_acceptance)
        github_acceptance_json = run(
            [sys.executable, "-m", "stagemesh.cli", "--project", str(project), "github-acceptance", "--json"],
            ROOT,
            env,
        )
        github_acceptance_data = json.loads(github_acceptance_json)
        if (
            github_acceptance_data["status"] != "PASS"
            or github_acceptance_data["rate_limit_status"] != "UNKNOWN"
            or github_acceptance_data["detected_repo"] != {"owner": "stage", "repo": "mesh"}
            or github_acceptance_data["deferred_skipped"] is not True
        ):
            raise AssertionError(github_acceptance_json)
        health = run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "health"], ROOT, env)
        if (
            "ok: True" not in health
            or "done: 4" not in health
            or "blocked_tasks: 0" not in health
            or "failed_executions: 0" not in health
            or "unknown_executions: 0" not in health
        ):
            raise AssertionError(health)
        health_json = run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "health", "--json"], ROOT, env)
        health_data = json.loads(health_json)
        if (
            health_data["ok"] is not True
            or health_data["done_count"] != 4
            or health_data["blocked_task_count"] != 0
            or health_data["failed_execution_count"] != 0
            or health_data["unknown_execution_count"] != 0
            or health_data["backlog_state"] != "ACTIVE"
        ):
            raise AssertionError(health_json)
        run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "worker",
                "worker-1",
                "--provider",
                "codex",
                "--capability",
                "code",
            ],
            ROOT,
            env,
        )
        worker_json = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "worker",
                "worker-json",
                "--provider",
                "claude",
                "--capability",
                "review",
                "--json",
            ],
            ROOT,
            env,
        )
        worker_data = json.loads(worker_json)
        if (
            worker_data["worker_id"] != "worker-json"
            or worker_data["provider"] != "claude"
            or worker_data["capabilities"] != ["review"]
            or worker_data["lease_seconds"] != 300
        ):
            raise AssertionError(worker_json)
        operator = run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "operator"], ROOT, env)
        if "workers=2" not in operator or "worker worker-1 provider=codex" not in operator:
            raise AssertionError(operator)
        operator_json = run(
            [sys.executable, "-m", "stagemesh.cli", "--project", str(project), "operator", "--json"],
            ROOT,
            env,
        )
        operator_data = json.loads(operator_json)
        if operator_data["summary"] != "ok":
            raise AssertionError(operator_json)
        section_rows = {section["name"]: section["rows"] for section in operator_data["sections"]}
        worker_providers = {row["id"]: row["provider"] for row in section_rows.get("Workers", [])}
        if worker_providers.get("worker-1") != "codex" or worker_providers.get("worker-json") != "claude":
            raise AssertionError(operator_json)
        invalid_worker = subprocess.run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "worker",
                "worker-bad",
                "--lease-seconds",
                "0",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            env={**os.environ.copy(), **env},
            check=False,
        )
        if invalid_worker.returncode != 2 or "worker error:" not in (invalid_worker.stdout + invalid_worker.stderr):
            raise AssertionError(invalid_worker.stdout + invalid_worker.stderr)
        dashboard = project / ".stagemesh" / "dashboard.html"
        dashboard_output = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "dashboard",
                "--output",
                str(dashboard),
            ],
            ROOT,
            env,
        )
        if not dashboard.exists() or "dashboard:" not in dashboard_output:
            raise AssertionError(dashboard_output)
        dashboard_text = dashboard.read_text(encoding="utf-8")
        if (
            "<h2>Status Summary</h2>" not in dashboard_text
            or "<strong>tasks</strong>" not in dashboard_text
            or "<h2>Tasks</h2>" not in dashboard_text
            or "<h2>Workers</h2>" not in dashboard_text
            or "worker-1" not in dashboard_text
        ):
            raise AssertionError(dashboard_text)
        dashboard_json = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "dashboard",
                "--output",
                str(dashboard),
                "--json",
            ],
            ROOT,
            env,
        )
        dashboard_json_data = json.loads(dashboard_json)
        if (
            dashboard_json_data["output"] != str(dashboard.resolve())
            or dashboard_json_data["bytes"] <= 0
            or dashboard_json_data["summary"]["workers"] != "2"
            or "Status Summary" not in dashboard_json_data["sections"]
            or "Tasks" not in dashboard_json_data["sections"]
            or "External Evidence" not in dashboard_json_data["sections"]
        ):
            raise AssertionError(dashboard_json)
        demo_project = project / ".stagemesh" / "demo-project"
        demo_output = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "demo",
                "--output",
                str(demo_project),
            ],
            ROOT,
            env,
        )
        demo_objective = demo_project / "objective.json"
        if "demo:" not in demo_output or not demo_objective.exists():
            raise AssertionError(demo_output)
        demo_project_json = project / ".stagemesh" / "demo-project-json"
        demo_json = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "demo",
                "--output",
                str(demo_project_json),
                "--json",
            ],
            ROOT,
            env,
        )
        demo_json_data = json.loads(demo_json)
        if (
            demo_json_data["root"] != str(demo_project_json.resolve())
            or not Path(demo_json_data["objective"]).exists()
            or not Path(demo_json_data["readme"]).exists()
        ):
            raise AssertionError(demo_json)
        run([sys.executable, "-m", "stagemesh.cli", "--project", str(demo_project), "init"], ROOT, env)
        run([sys.executable, "-m", "stagemesh.cli", "--project", str(demo_project), "plan", str(demo_objective)], ROOT, env)
        for _ in range(10):
            run([sys.executable, "-m", "stagemesh.cli", "--project", str(demo_project), "continue", "--once"], ROOT, env)
        demo_status = run(
            [sys.executable, "-m", "stagemesh.cli", "--project", str(demo_project), "status", "--json"],
            ROOT,
            env,
        )
        demo_status_data = json.loads(demo_status)
        if demo_status_data["done_count"] != 2:
            raise AssertionError(demo_status)
        duplicate_demo = subprocess.run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "demo",
                "--output",
                str(demo_project),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            env={**os.environ.copy(), **env},
            check=False,
        )
        if duplicate_demo.returncode != 2 or "demo error:" not in (duplicate_demo.stdout + duplicate_demo.stderr):
            raise AssertionError(duplicate_demo.stdout + duplicate_demo.stderr)
        release_dir = ROOT / ".stagemesh" / "acceptance-release"
        release_output = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(ROOT),
                "release",
                "--candidate-sha",
                "abc1234",
                "--output",
                str(release_dir),
            ],
            ROOT,
            env,
        )
        if "archive:" not in release_output or not (release_dir / "stagemesh-release-manifest.json").exists():
            raise AssertionError(release_output)
        release_json = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(ROOT),
                "release",
                "--candidate-sha",
                "abc1234",
                "--output",
                str(release_dir),
                "--json",
            ],
            ROOT,
            env,
        )
        release_json_data = json.loads(release_json)
        if (
            release_json_data["candidate_sha"] != "abc1234"
            or release_json_data["version"] != "0.1.0"
            or release_json_data["file_count"] <= 0
            or release_json_data["archive_size"] <= 0
            or not Path(release_json_data["archive"]).exists()
            or not Path(release_json_data["manifest"]).exists()
        ):
            raise AssertionError(release_json)
        invalid_release = subprocess.run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(ROOT),
                "release",
                "--candidate-sha",
                "../bad",
                "--output",
                str(release_dir),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            env={**os.environ.copy(), **env},
            check=False,
        )
        if invalid_release.returncode != 2 or "release error:" not in (invalid_release.stdout + invalid_release.stderr):
            raise AssertionError(invalid_release.stdout + invalid_release.stderr)
        release_manifest = json.loads((release_dir / "stagemesh-release-manifest.json").read_text(encoding="utf-8"))
        release_paths = {entry["path"] for entry in release_manifest["files"]}
        if "pyproject.toml" not in release_paths or "scripts/clean_acceptance.py" not in release_paths:
            raise AssertionError(release_manifest)
        if any(".stagemesh" in Path(path).parts or ".tmp-install" in Path(path).parts for path in release_paths):
            raise AssertionError(release_manifest)
        archives = sorted(release_dir.glob("stagemesh-*.zip"))
        if not archives:
            raise AssertionError(release_output)
        with zipfile.ZipFile(archives[-1]) as archive:
            archive_names = set(archive.namelist())
        if "stagemesh-release-manifest.json" not in archive_names or "pyproject.toml" not in archive_names:
            raise AssertionError(archive_names)
        packet_output = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "work",
                "enqueue",
                "one",
                "--stage",
                "VALIDATE",
            ],
            ROOT,
            env,
        )
        packet_id = packet_output.strip().split()[-1]
        json_packet_output = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "work",
                "enqueue",
                "one",
                "--stage",
                "REVIEW",
                "--json",
            ],
            ROOT,
            env,
        )
        json_packet_id = json.loads(json_packet_output)["packet_id"]
        poll_output = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "work",
                "poll",
                "worker-2",
                "--lease-seconds",
                "60",
            ],
            ROOT,
            env,
        )
        if packet_id not in poll_output:
            raise AssertionError(poll_output)
        poll_json = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "work",
                "poll",
                "worker-json",
                "--lease-seconds",
                "60",
                "--json",
            ],
            ROOT,
            env,
        )
        poll_data = json.loads(poll_json)
        if poll_data["packets"][0]["id"] != json_packet_id or poll_data["packets"][0]["stage"] != "REVIEW":
            raise AssertionError(poll_json)
        work_list = run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "work", "list"], ROOT, env)
        if packet_id not in work_list or json_packet_id not in work_list or "CLAIMED" not in work_list:
            raise AssertionError(work_list)
        work_list_json = run(
            [sys.executable, "-m", "stagemesh.cli", "--project", str(project), "work", "list", "--json"],
            ROOT,
            env,
        )
        work_list_data = json.loads(work_list_json)
        packet_states = {packet["id"]: packet for packet in work_list_data["packets"]}
        if (
            packet_states[packet_id]["status"] != "CLAIMED"
            or packet_states[packet_id]["worker_id"] != "worker-2"
            or packet_states[json_packet_id]["stage"] != "REVIEW"
            or packet_states[json_packet_id]["worker_id"] != "worker-json"
        ):
            raise AssertionError(work_list_json)
        invalid_poll = subprocess.run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "work",
                "poll",
                "worker-2",
                "--limit",
                "0",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            env={**os.environ.copy(), **env},
            check=False,
        )
        if invalid_poll.returncode != 2 or "work queue error:" not in (invalid_poll.stdout + invalid_poll.stderr):
            raise AssertionError(invalid_poll.stdout + invalid_poll.stderr)
        renew_output = run(
            [sys.executable, "-m", "stagemesh.cli", "--project", str(project), "work", "renew", packet_id, "worker-2"],
            ROOT,
            env,
        )
        if "renewed: True" not in renew_output:
            raise AssertionError(renew_output)
        renew_json = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "work",
                "renew",
                json_packet_id,
                "worker-json",
                "--json",
            ],
            ROOT,
            env,
        )
        if json.loads(renew_json) != {"packet_id": json_packet_id, "renewed": True}:
            raise AssertionError(renew_json)
        invalid_ack = subprocess.run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "work",
                "ack",
                packet_id,
                "--status",
                "BOGUS",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            env={**os.environ.copy(), **env},
            check=False,
        )
        if invalid_ack.returncode != 2 or "work queue error:" not in (invalid_ack.stdout + invalid_ack.stderr):
            raise AssertionError(invalid_ack.stdout + invalid_ack.stderr)
        run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "work",
                "ack",
                packet_id,
            ],
            ROOT,
            env,
        )
        ack_json = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "work",
                "ack",
                json_packet_id,
                "--json",
            ],
            ROOT,
            env,
        )
        if json.loads(ack_json) != {"packet_id": json_packet_id, "status": "SUCCEEDED"}:
            raise AssertionError(ack_json)
        report = project / ".stagemesh" / "final-report.md"
        report_output = run(
            [sys.executable, "-m", "stagemesh.cli", "--project", str(project), "report", "--output", str(report)],
            ROOT,
            env,
        )
        if not report.exists() or "report:" not in report_output:
            raise AssertionError(report_output)
        acceptance_report = ROOT / ".stagemesh" / "acceptance-report.json"
        report_output = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(ROOT),
                "acceptance-report",
                "--output",
                str(acceptance_report),
                "--skip-acceptance",
            ],
            ROOT,
            env,
        )
        if not acceptance_report.exists() or "acceptance-report:" not in report_output:
            raise AssertionError(report_output)
        acceptance_report_data = json.loads(acceptance_report.read_text(encoding="utf-8"))
        proof_gap_text = json.dumps(acceptance_report_data.get("proof_gaps", []))
        if acceptance_report_data.get("proof_status") != "BLOCKED_ON_EXTERNAL_EVIDENCE":
            raise AssertionError(acceptance_report_data)
        if "provider:codex:execution: NOT_PROVEN" not in proof_gap_text or "github:sync:" not in proof_gap_text:
            raise AssertionError(acceptance_report_data)
        acceptance_report_json = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(ROOT),
                "acceptance-report",
                "--output",
                str(acceptance_report),
                "--skip-acceptance",
                "--json",
            ],
            ROOT,
            env,
        )
        acceptance_report_json_data = json.loads(acceptance_report_json)
        if (
            acceptance_report_json_data["status"] != "PASS"
            or acceptance_report_json_data["proof_status"] != "BLOCKED_ON_EXTERNAL_EVIDENCE"
            or not isinstance(acceptance_report_json_data["checks"], list)
        ):
            raise AssertionError(acceptance_report_json)
        run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(ROOT),
                "evidence",
                "add",
                "hosted-ci",
                "PASS",
                "https://example.invalid/run/acceptance",
                "--candidate-sha",
                "abc1234",
            ],
            ROOT,
            env,
        )
        acceptance_matrix = ROOT / ".stagemesh" / "acceptance-matrix.json"
        matrix_output = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(ROOT),
                "acceptance-matrix",
                "--output",
                str(acceptance_matrix),
            ],
            ROOT,
            env,
        )
        matrix_text = acceptance_matrix.read_text(encoding="utf-8") if acceptance_matrix.exists() else ""
        if "acceptance-matrix:" not in matrix_output or '"status": "INCOMPLETE"' not in matrix_text:
            raise AssertionError(matrix_output + matrix_text)
        if (
            "Linux acceptance" not in matrix_text
            or "MISSING_EXTERNAL_EVIDENCE" not in matrix_text
            or "https://example.invalid/run/acceptance" in matrix_text
        ):
            raise AssertionError(matrix_text)
        matrix_json = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(ROOT),
                "acceptance-matrix",
                "--output",
                str(acceptance_matrix),
                "--json",
            ],
            ROOT,
            env,
        )
        matrix_json_data = json.loads(matrix_json)
        if matrix_json_data["status"] != "INCOMPLETE" or matrix_json_data["proven"] >= matrix_json_data["total"]:
            raise AssertionError(matrix_json)
        e2e_acceptance = ROOT / ".stagemesh" / "end-to-end-acceptance.json"
        e2e_output = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(ROOT),
                "end-to-end-acceptance",
                "--output",
                str(e2e_acceptance),
            ],
            ROOT,
            env,
        )
        e2e_text = e2e_acceptance.read_text(encoding="utf-8") if e2e_acceptance.exists() else ""
        if (
            "end-to-end-acceptance:" not in e2e_output
            or '"status": "COMPLETE"' not in e2e_text
            or '"total": 18' not in e2e_text
            or "interrupt validation" not in e2e_text
        ):
            raise AssertionError(e2e_output + e2e_text)
        e2e_json = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(ROOT),
                "end-to-end-acceptance",
                "--output",
                str(e2e_acceptance),
                "--json",
            ],
            ROOT,
            env,
        )
        e2e_json_data = json.loads(e2e_json)
        if e2e_json_data["status"] != "COMPLETE" or e2e_json_data["proven"] != 18 or e2e_json_data["total"] != 18:
            raise AssertionError(e2e_json)
        completion_audit = ROOT / ".stagemesh" / "completion-audit.json"
        audit_output = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(ROOT),
                "completion-audit",
                "--output",
                str(completion_audit),
            ],
            ROOT,
            env,
        )
        if not completion_audit.exists() or "completion-audit:" not in audit_output:
            raise AssertionError(audit_output)
        completion_json = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(ROOT),
                "completion-audit",
                "--output",
                str(completion_audit),
                "--json",
            ],
            ROOT,
            env,
        )
        completion_json_data = json.loads(completion_json)
        completion_statuses = {item["requirement"]: item["status"] for item in completion_json_data["items"]}
        if (
            completion_json_data["complete"] is not False
            or completion_statuses.get("Linux acceptance") != "MISSING_EXTERNAL_EVIDENCE"
            or completion_statuses.get("live GitHub sync") != "REQUIRES_CREDENTIALS"
            or completion_statuses.get("PostgreSQL storage") != "INTERFACE_READY"
        ):
            raise AssertionError(completion_json)
        repo_report = ROOT / ".stagemesh" / "final-report.md"
        repo_report_output = run(
            [sys.executable, "-m", "stagemesh.cli", "--project", str(ROOT), "report", "--output", str(repo_report)],
            ROOT,
            env,
        )
        repo_report_text = repo_report.read_text(encoding="utf-8") if repo_report.exists() else ""
        if (
            not repo_report.exists()
            or "acceptance report status: PASS proof=BLOCKED_ON_EXTERNAL_EVIDENCE" not in repo_report_text
            or "acceptance matrix status: INCOMPLETE" not in repo_report_text
            or "end-to-end acceptance status: COMPLETE (18/18 steps proven)" not in repo_report_text
        ):
            raise AssertionError(repo_report_output)
        repo_report_json = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(ROOT),
                "report",
                "--output",
                str(repo_report),
                "--json",
            ],
            ROOT,
            env,
        )
        repo_report_json_data = json.loads(repo_report_json)
        if (
            repo_report_json_data["candidate_sha"]
            != run(["git", "-c", "safe.directory=C:/stagemesh-vnext", "rev-parse", "HEAD"], ROOT).strip()
            or repo_report_json_data["output"] != str(repo_report.resolve())
            or repo_report_json_data["bytes"] <= 0
            or repo_report_json_data["external_evidence_records_for_candidate"] != 0
        ):
            raise AssertionError(repo_report_json)
        status_doc = (ROOT / "docs" / "status.md").read_text(encoding="utf-8")
        if "acceptance report: `PASS proof=BLOCKED_ON_EXTERNAL_EVIDENCE`" not in status_doc:
            raise AssertionError(status_doc)
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        if workflow.count("python -m stagemesh.cli --project . ci --future-feature-gate") != 2:
            raise AssertionError(workflow)
        if workflow.count("PYTHONPATH: src") != 2:
            raise AssertionError(workflow)
        readiness = ROOT / ".stagemesh" / "release-readiness.json"
        readiness_output = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(ROOT),
                "release-readiness",
                "--output",
                str(readiness),
                "--skip-acceptance",
                "--skip-checks",
            ],
            ROOT,
            env,
        )
        readiness_text = readiness.read_text(encoding="utf-8") if readiness.exists() else ""
        if "release-readiness:" not in readiness_output or "BLOCKED_ON_EXTERNAL_EVIDENCE" not in readiness_text:
            raise AssertionError(readiness_output + readiness_text)
        readiness_data = json.loads(readiness_text)
        if "external_evidence" not in readiness_data or not isinstance(readiness_data["external_evidence"], list):
            raise AssertionError(readiness_text)
        readiness_json = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(ROOT),
                "release-readiness",
                "--output",
                str(readiness),
                "--skip-acceptance",
                "--skip-checks",
                "--json",
            ],
            ROOT,
            env,
        )
        readiness_json_data = json.loads(readiness_json)
        if (
            readiness_json_data["overall_status"] != "BLOCKED_ON_EXTERNAL_EVIDENCE"
            or readiness_json_data["local_status"] != "PASS"
            or not isinstance(readiness_json_data["local_proof_gaps"], list)
            or not isinstance(readiness_json_data["external_gaps"], list)
            or not isinstance(readiness_json_data["external_evidence"], list)
        ):
            raise AssertionError(readiness_json)
        capacity = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "capacity",
                "--primary-down",
            ],
            ROOT,
            env,
        )
        if "chosen: claude" not in capacity:
            raise AssertionError(capacity)
        capacity_json = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "capacity",
                "--primary-down",
                "--json",
            ],
            ROOT,
            env,
        )
        capacity_data = json.loads(capacity_json)
        if capacity_data["chosen"] != "claude":
            raise AssertionError(capacity_json)
        capacity_states = {item["provider"]: item for item in capacity_data["providers"]}
        if capacity_states["codex"]["kind"] != "CAPACITY" or capacity_states["claude"]["usable"] is not True:
            raise AssertionError(capacity_json)
        invalid_capacity = subprocess.run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "capacity",
                "--primary",
                "",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            env={**os.environ.copy(), **env},
            check=False,
        )
        if invalid_capacity.returncode != 2 or "capacity error:" not in (invalid_capacity.stdout + invalid_capacity.stderr):
            raise AssertionError(invalid_capacity.stdout + invalid_capacity.stderr)
        marker = ROOT / ".stagemesh-broken-feature"
        marker.unlink(missing_ok=True)
        ci = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(ROOT),
                "ci",
                "--future-feature-gate",
                "--skip-acceptance",
            ],
            ROOT,
            env,
        )
        if (
            "provider_acceptance: PASS" not in ci
            or "github_acceptance: PASS" not in ci
            or "live_acceptance: PASS" not in ci
            or "future-feature: PASS" not in ci
        ):
            raise AssertionError(ci)
        ci_json = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(ROOT),
                "ci",
                "--future-feature-gate",
                "--skip-acceptance",
                "--json",
            ],
            ROOT,
            env,
        )
        ci_data = json.loads(ci_json)
        if ci_data["status"] != "PASS":
            raise AssertionError(ci_json)
        gate_statuses = {gate["name"]: gate["passed"] for gate in ci_data["gates"]}
        if gate_statuses.get("future-feature") is not True or gate_statuses.get("live_acceptance") is not True:
            raise AssertionError(ci_json)
        ci_wait = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(ROOT),
                "ci-wait",
                "pending",
                "--elapsed-seconds",
                "30",
            ],
            ROOT,
            env,
        )
        if "should_wait: True" not in ci_wait or "release_worker: True" not in ci_wait:
            raise AssertionError(ci_wait)
        ci_wait_json = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(ROOT),
                "ci-wait",
                "pending",
                "--elapsed-seconds",
                "30",
                "--json",
            ],
            ROOT,
            env,
        )
        ci_wait_data = json.loads(ci_wait_json)
        if (
            ci_wait_data["should_wait"] is not True
            or ci_wait_data["release_worker"] is not True
            or ci_wait_data["reason"] != "ci pending"
        ):
            raise AssertionError(ci_wait_json)
        root_config = ROOT / ".stagemesh" / "config.json"
        original_root_config = root_config.read_text(encoding="utf-8") if root_config.exists() else None
        root_config.parent.mkdir(parents=True, exist_ok=True)
        root_config.write_text('{"providers":{"custom":"python --version"}}', encoding="utf-8")
        try:
            live = run([sys.executable, "scripts/live_acceptance.py"], ROOT, env)
            live_json = run([sys.executable, "scripts/live_acceptance.py", "--json"], ROOT, env)
        finally:
            if original_root_config is None:
                root_config.unlink(missing_ok=True)
            else:
                root_config.write_text(original_root_config, encoding="utf-8")
        if (
            "github:" not in live
            or "github:sync:" not in live
            or "provider:codex:" not in live
            or "provider:custom: AVAILABLE" not in live
            or "provider:custom:execution: NOT_PROVEN" not in live
            or "provider:codex:execution: NOT_PROVEN" not in live
        ):
            raise AssertionError(live)
        live_data = json.loads(live_json)
        live_checks = {check["name"]: check["status"] for check in live_data["checks"]}
        if (
            live_data["status"] != "PASS"
            or live_checks.get("github") not in {"NOT_CONFIGURED", "OK", "UNKNOWN", "STALE"}
            or live_checks.get("github:sync") not in {"NOT_CONFIGURED", "NOT_PROVEN"}
            or live_checks.get("provider:custom") != "AVAILABLE"
            or live_checks.get("provider:custom:execution") != "NOT_PROVEN"
            or live_checks.get("provider:codex:execution") != "NOT_PROVEN"
        ):
            raise AssertionError(live_json)
        marker.write_text("broken\n", encoding="utf-8")
        try:
            failed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "stagemesh.cli",
                    "--project",
                    str(ROOT),
                    "ci",
                    "--future-feature-gate",
                    "--skip-acceptance",
                    "--json",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                env={**os.environ.copy(), **env},
                check=False,
            )
            failed_ci = json.loads(failed.stdout)
            failed_gates = {gate["name"]: gate["passed"] for gate in failed_ci["gates"]}
            if failed.returncode == 0 or failed_ci["status"] != "FAIL" or failed_gates.get("future-feature") is not False:
                raise AssertionError(failed.stdout + failed.stderr)
        finally:
            marker.unlink(missing_ok=True)
        shutil.rmtree(project / ".git", ignore_errors=True)
    print("acceptance: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
