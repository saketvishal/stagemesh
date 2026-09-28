from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    merged = os.environ.copy()
    merged.update(env or {})
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, env=merged)
    if result.returncode != 0:
        raise AssertionError(f"{command} failed\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
    return result.stdout


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
        registry_output = run(
            [sys.executable, "-m", "stagemesh.cli", "--project", str(project), "registry", "--registry", str(registry)],
            ROOT,
            env,
        )
        if "project" not in registry_output:
            raise AssertionError(registry_output)
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
        backlog = project / ".stagemesh" / "backlog.json"
        for _ in range(16):
            run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "continue", "--once"], ROOT, env)
        status = run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "status"], ROOT, env)
        if "DONE DONE first task" not in status or "DONE DONE dependent task" not in status:
            raise AssertionError(status)
        if "future work" in status:
            raise AssertionError("deferred task was dispatched")
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
        if "github.configured: False" not in config_output or "database_url: sqlite://default" not in config_output:
            raise AssertionError(config_output)
        backend_output = run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "backend"], ROOT, env)
        if "name: sqlite" not in backend_output or "available: True" not in backend_output:
            raise AssertionError(backend_output)
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
                "acceptance-sha",
                "--output",
                str(release_dir),
            ],
            ROOT,
            env,
        )
        if "archive:" not in release_output or not (release_dir / "stagemesh-release-manifest.json").exists():
            raise AssertionError(release_output)
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
            [sys.executable, "-m", "stagemesh.cli", "--project", str(project), "work", "poll", "worker-2"],
            ROOT,
            env,
        )
        if packet_id not in poll_output:
            raise AssertionError(poll_output)
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
        if not repo_report.exists() or "acceptance report status: PASS" not in repo_report.read_text(encoding="utf-8"):
            raise AssertionError(repo_report_output)
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
        if "future-feature: PASS" not in ci:
            raise AssertionError(ci)
        live = run([sys.executable, "scripts/live_acceptance.py"], ROOT, env)
        if "github:" not in live or "provider:codex:" not in live:
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
