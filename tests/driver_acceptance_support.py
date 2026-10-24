from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage, TaskStatus
from stagemesh.persistence import Store


class SimulatedWindowCrash(RuntimeError):
    pass


def report_path(argv: list[str]) -> Path:
    return Path(argv[argv.index("--report") + 1])


def state_path(project: Path) -> Path:
    return project / ".stagemesh" / "driver-acceptance" / "fake-window.json"


def seed_window_state(
    project: Path,
    *,
    ineligible: tuple[str, ...] = (),
    crash_after_merge: str | None = None,
) -> None:
    path = state_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "calls": {name: [] for name in ("provider", "candidate", "evidence", "pr", "merge")},
                "events": [],
                "attempts": {},
                "ineligible": list(ineligible),
                "crash_after_merge": crash_after_merge,
                "crash_injected": False,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def load_window_state(project: Path) -> dict[str, Any]:
    return json.loads(state_path(project).read_text(encoding="utf-8"))


def calls_by_task(state: dict[str, Any], category: str) -> Counter[str]:
    return Counter(str(token).split(":", 1)[0] for token in state["calls"][category])


def snapshot_tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class FakeWindowProcess:
    def __init__(self, project: Path):
        self.project = project.resolve()
        self.path = state_path(self.project)
        self.state = load_window_state(self.project)

    def __call__(self, argv: list[str], cwd: Path, timeout_seconds: float) -> int:
        assert cwd == self.project
        assert timeout_seconds > 0
        store = Store(self.project / ".stagemesh" / "stagemesh.sqlite3")
        store.migrate()
        try:
            runnable = self._runnable_task(store)
            if runnable is None:
                blocked = bool(store.tasks()) and all(
                    str(task["status"]) == TaskStatus.BLOCKED for task in store.tasks()
                )
                self._write_report(
                    argv,
                    {
                        "tasks": [],
                        "merged_prs": [],
                        "stop_reason": "all tasks are blocked" if blocked else None,
                        "next_command": "waiting for eligible work",
                    },
                )
                return 2 if blocked else 0
            return self._run_task(store, runnable, argv)
        finally:
            store.close()

    def _runnable_task(self, store: Store) -> str | None:
        ineligible = set(self.state["ineligible"])
        for task in store.tasks():
            task_id = str(task["id"])
            if task_id in ineligible:
                continue
            if str(task["status"]) != TaskStatus.OPEN:
                continue
            if store.incomplete_dependencies(task_id):
                continue
            return task_id
        return None

    def _run_task(self, store: Store, task_id: str, argv: list[str]) -> int:
        attempt = int(self.state["attempts"].get(task_id, 0)) + 1
        self.state["attempts"][task_id] = attempt
        self._record("provider", f"{task_id}:{attempt}", f"provider:{task_id}:{attempt}")
        candidate_sha = hashlib.sha1(f"driver-acceptance:{task_id}:{attempt}".encode()).hexdigest()
        self._candidate(store, task_id, candidate_sha, attempt)

        if task_id == "B" and attempt == 1:
            self._evidence(store, task_id, candidate_sha, EvidenceKind.VALIDATION, EvidenceStatus.FAILED, attempt)
            self._write_report(
                argv,
                {
                    "tasks": [{"task_id": task_id, "outcome": "open", "candidate_sha": candidate_sha}],
                    "merged_prs": [],
                    "stop_reason": "INTENTIONAL_FIXTURE_FAILURE: B validation failed once",
                },
            )
            return 1

        for kind in (EvidenceKind.VALIDATION, EvidenceKind.REVIEW, EvidenceKind.INTEGRATION):
            self._evidence(store, task_id, candidate_sha, kind, EvidenceStatus.PASSED, attempt)
        self._record("pr", task_id, f"pr:{task_id}")
        self._record("merge", task_id, f"merge:{task_id}")
        workspace = self.project / ".stagemesh" / "worktrees" / f"task-{task_id}"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "result.txt").write_text(f"{task_id}:{attempt}:done\n", encoding="utf-8")
        store.advance_task(task_id, Stage.DONE)

        if self.state["crash_after_merge"] == task_id and not self.state["crash_injected"]:
            self.state["crash_injected"] = True
            self._save()
            raise SimulatedWindowCrash(f"crash after {task_id} merge and before report checkpoint")

        self._write_report(
            argv,
            {
                "tasks": [
                    {
                        "task_id": task_id,
                        "outcome": "done",
                        "candidate_sha": candidate_sha,
                        "pr_url": f"https://fake.invalid/pull/{task_id}",
                    }
                ],
                "merged_prs": [{"task_id": task_id, "candidate_sha": candidate_sha}],
                "stop_reason": None,
            },
        )
        return 0

    def _candidate(self, store: Store, task_id: str, sha: str, attempt: int) -> None:
        token = f"{task_id}:{sha}"
        if token in self.state["calls"]["candidate"]:
            return
        store.add_candidate(task_id, sha, "fake-provider", True)
        self._record("candidate", token, f"candidate:{task_id}:{attempt}")

    def _evidence(
        self,
        store: Store,
        task_id: str,
        sha: str,
        kind: EvidenceKind,
        status: EvidenceStatus,
        attempt: int,
    ) -> None:
        token = f"{task_id}:{sha}:{kind}:{status}"
        if token in self.state["calls"]["evidence"]:
            return
        store.add_evidence(task_id, sha, kind, status, {"fixture": "driver-acceptance", "attempt": attempt})
        self._record("evidence", token, f"evidence:{task_id}:{attempt}:{kind}:{status}")

    def _record(self, category: str, token: str, event: str) -> None:
        if token in self.state["calls"][category]:
            return
        self.state["calls"][category].append(token)
        self.state["events"].append(event)
        self._save()

    def _write_report(self, argv: list[str], payload: dict[str, Any]) -> None:
        path = report_path(argv)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _save(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.path)
