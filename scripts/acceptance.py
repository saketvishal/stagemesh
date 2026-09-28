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
        run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "plan", str(objective)], ROOT, env)
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
        backlog = project / ".stagemesh" / "backlog.json"
        for _ in range(16):
            run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "continue", "--once"], ROOT, env)
        status = run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "status"], ROOT, env)
        if "DONE DONE first task" not in status or "DONE DONE dependent task" not in status:
            raise AssertionError(status)
        if "future work" in status:
            raise AssertionError("deferred task was dispatched")
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
                "abc1234",
            ],
            ROOT,
            env,
        )
        if "evidence:" not in evidence_add:
            raise AssertionError(evidence_add)
        evidence_list = run(
            [sys.executable, "-m", "stagemesh.cli", "--project", str(project), "evidence", "list"],
            ROOT,
            env,
        )
        if "hosted-ci PASS abc1234" not in evidence_list:
            raise AssertionError(evidence_list)
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
        config_output = run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "config"], ROOT, env)
        if (
            "github.configured: False" not in config_output
            or "database_url: sqlite://default" not in config_output
            or "routing.mode: STAGED" not in config_output
        ):
            raise AssertionError(config_output)
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
        backend_output = run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "backend"], ROOT, env)
        if (
            "name: sqlite" not in backend_output
            or "available: True" not in backend_output
            or "postgres schema contract: 18 tables" not in backend_output
        ):
            raise AssertionError(backend_output)
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
        health = run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "health"], ROOT, env)
        if "ok: True" not in health or "done: 2" not in health:
            raise AssertionError(health)
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
        operator = run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "operator"], ROOT, env)
        if "workers=1" not in operator or "worker worker-1 provider=codex" not in operator:
            raise AssertionError(operator)
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
        if "<h2>Tasks</h2>" not in dashboard_text or "<h2>Workers</h2>" not in dashboard_text or "worker-1" not in dashboard_text:
            raise AssertionError(dashboard_text)
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
        repo_report = ROOT / ".stagemesh" / "final-report.md"
        repo_report_output = run(
            [sys.executable, "-m", "stagemesh.cli", "--project", str(ROOT), "report", "--output", str(repo_report)],
            ROOT,
            env,
        )
        repo_report_text = repo_report.read_text(encoding="utf-8") if repo_report.exists() else ""
        if (
            not repo_report.exists()
            or "acceptance report status: PASS" not in repo_report_text
            or "acceptance matrix status: INCOMPLETE" not in repo_report_text
        ):
            raise AssertionError(repo_report_output)
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
        root_config = ROOT / ".stagemesh" / "config.json"
        original_root_config = root_config.read_text(encoding="utf-8") if root_config.exists() else None
        root_config.parent.mkdir(parents=True, exist_ok=True)
        root_config.write_text('{"providers":{"custom":"python --version"}}', encoding="utf-8")
        try:
            live = run([sys.executable, "scripts/live_acceptance.py"], ROOT, env)
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
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                env={**os.environ.copy(), **env},
                check=False,
            )
            if failed.returncode == 0 or "future-feature: FAIL" not in failed.stdout:
                raise AssertionError(failed.stdout + failed.stderr)
        finally:
            marker.unlink(missing_ok=True)
        shutil.rmtree(project / ".git", ignore_errors=True)
    print("acceptance: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
