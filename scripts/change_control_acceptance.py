from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from stagemesh.change_control import ChangeContract, write_contract
from stagemesh.coordinator import Coordinator
from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage, TaskStatus
from stagemesh.execution import FakeExecutor
from stagemesh.integration import Integrator
from stagemesh.persistence import Store
from stagemesh.remediation import RemediationPolicy
from stagemesh.validation import Validator


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout)
    return proc.stdout.strip()


def init_repo(root: Path) -> None:
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "acceptance@example.invalid")
    git(root, "config", "user.name", "StageMesh Acceptance")
    (root / ".gitignore").write_text(".stagemesh/\n", encoding="utf-8")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-m", "base")


def test_out_of_contract_candidate_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        init_repo(root)
        store = Store(root / ".stagemesh" / "state.sqlite3")
        store.migrate()
        task_id = store.upsert_task("bounded", source_id="CC-1")
        write_contract(
            root,
            task_id,
            ChangeContract.from_mapping(
                {
                    "objective": "add only the allowed implementation file",
                    "allowed_paths": ["src/allowed.py"],
                    "validation_commands": [
                        "python -m compileall -q src"
                    ],
                }
            ),
        )
        (root / "src").mkdir()
        (root / "src" / "allowed.py").write_text("VALUE = 1\n", encoding="utf-8")
        (root / "forbidden.txt").write_text("oops\n", encoding="utf-8")
        git(root, "add", "-A")
        git(root, "commit", "-m", "candidate")
        sha = git(root, "rev-parse", "HEAD")
        store.add_candidate(task_id, sha, "acceptance", True)

        status = Validator(require_contract=True).validate(
            store,
            task_id,
            sha,
            root,
        )
        assert status is EvidenceStatus.FAILED
        findings = store.open_findings_for_candidate(task_id, sha)
        assert any(
            "out-of-scope path changed: forbidden.txt" in row["message"]
            for row in findings
        )
        store.close()


def test_missing_contract_blocks_before_executor() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        init_repo(root)
        store = Store(root / ".stagemesh" / "state.sqlite3")
        store.migrate()
        task_id = store.upsert_task("must not execute", source_id="CC-2")
        coord = Coordinator(
            store,
            root,
            executor=FakeExecutor(),
            validator=Validator(require_contract=True),
            remediation_policy=RemediationPolicy(3),
        )
        assert coord.tick() == 0
        task = store.get_task(task_id)
        assert task["status"] == TaskStatus.BLOCKED
        assert store.latest_candidate(task_id) is None
        store.close()


def test_failed_validation_schedules_bounded_repair() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        init_repo(root)
        store = Store(root / ".stagemesh" / "state.sqlite3")
        store.migrate()
        task_id = store.upsert_task("repair", source_id="CC-3")
        write_contract(
            root,
            task_id,
            ChangeContract.from_mapping(
                {
                    "objective": "touch only src/allowed.py",
                    "allowed_paths": ["src/allowed.py"],
                }
            ),
        )
        (root / "bad.txt").write_text("bad\n", encoding="utf-8")
        git(root, "add", "-A")
        git(root, "commit", "-m", "bad candidate")
        sha = git(root, "rev-parse", "HEAD")
        store.add_candidate(task_id, sha, "acceptance", True)
        store.advance_task(task_id, Stage.VALIDATE)

        coord = Coordinator(
            store,
            root,
            validator=Validator(require_contract=True),
            remediation_policy=RemediationPolicy(3),
        )
        assert coord.tick() == 1
        assert store.get_task(task_id)["stage"] == Stage.IMPLEMENT
        assert store.remediation_attempt_count_for_task(task_id) == 1
        store.close()


def test_git_integrator_incorporates_exact_candidate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        init_repo(root)
        store = Store(root / ".stagemesh" / "state.sqlite3")
        store.migrate()
        task_id = store.upsert_task("integrate", source_id="CC-4")

        git(root, "checkout", "-b", "candidate")
        (root / "feature.txt").write_text("candidate\n", encoding="utf-8")
        git(root, "add", "feature.txt")
        git(root, "commit", "-m", "feature")
        candidate_sha = git(root, "rev-parse", "HEAD")
        git(root, "checkout", "main")

        status = Integrator(apply=True).integrate(
            store,
            task_id,
            candidate_sha,
            root,
        )
        assert status is EvidenceStatus.PASSED
        assert (root / "feature.txt").read_text(encoding="utf-8") == "candidate\n"
        assert store.has_evidence(
            task_id,
            candidate_sha,
            EvidenceKind.INTEGRATION,
            EvidenceStatus.PASSED,
        )
        store.close()


def main() -> int:
    test_out_of_contract_candidate_is_rejected()
    test_missing_contract_blocks_before_executor()
    test_failed_validation_schedules_bounded_repair()
    test_git_integrator_incorporates_exact_candidate()
    print("change-control acceptance: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
