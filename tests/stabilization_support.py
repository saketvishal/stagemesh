"""Synthetic harness for the runtime stabilization matrix.

A tiny Git project, a fake provider CLI whose behavior per (provider, task, stage) comes from a plan file, and a helper that runs the
real `stagemesh continue` code path (config, pools, cooldowns, workspaces, validation, review, integration) in-process. No network,
no real provider tokens, no external project.
"""

from __future__ import annotations

import contextlib
import io
import json
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import stagemesh.cli as cli_module
from stagemesh.git import GitWorkspace
from stagemesh.persistence import Store

PROVIDERS = ("codex", "claude", "grok")  # the built-in names are always in the pool, so the fake CLI must own all of them

# Implementation modes: ok, bad (fails validation), noop, quota, timeout, mutate (rewrites the sealed HEAD in the task workspace).
# Review modes: pass, fail, quota. A list is consumed one entry per call (the last entry repeats).
FAKE_PROVIDER = r'''
import json, pathlib, re, subprocess, sys, time

plan = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
me = sys.argv[2]
prompt = sys.stdin.read()
log = pathlib.Path(sys.argv[1]).with_name("calls.log")
review = "Review candidate" in prompt
if review:
    task = re.search(r"for task (\S+) under", prompt).group(1)
else:
    task = re.search(r"StageMesh task: task (\S+)", prompt).group(1)
stage = "review" if review else "implement"
rules = plan.get(me, {})
mode = rules.get(f"{stage}:{task}", rules.get(stage, "pass" if review else "ok"))
previous = sum(1 for line in log.read_text(encoding="utf-8").splitlines() if line.split()[:3] == [me, stage, task]) if log.exists() else 0
if isinstance(mode, list):
    mode = mode[min(previous, len(mode) - 1)]
with log.open("a", encoding="utf-8") as handle:
    handle.write(f"{me} {stage} {task} {mode}\n")
if mode == "quota":
    sys.stderr.write("You've hit your weekly limit · resets 1am (America/Chicago)")
    sys.exit(1)
if review:
    print(json.dumps({"decision": "FAIL", "findings": [{"severity": "error", "message": "synthetic review defect"}]}) if mode == "fail" else '{"decision":"PASS"}')
    sys.exit(0)
if mode == "noop":
    sys.exit(0)
if mode == "timeout":
    time.sleep(60)
    sys.exit(0)
if mode == "mutate":
    subprocess.run(["git", "commit", "--amend", "--allow-empty", "-q", "-m", "provider rewrote HEAD"], check=True)
    sys.exit(0)
out = pathlib.Path("out")
out.mkdir(exist_ok=True)
(out / f"{task}.txt").write_text((mode + "\n") if str(mode).startswith("bad") else "done\n", encoding="utf-8")
'''

# Operator detours that normal recovery must never ask for.
FORBIDDEN_OPERATOR_TEXT = (
    "task-doctor",
    "retry-task",
    "--provider",
    "choose another provider",
    "choose a provider",
    "sqlite",
    "git reset",
    "git checkout",
    "repair git",
)


def gate(task_id: str) -> list[str]:
    code = f"from pathlib import Path; assert Path('out/{task_id}.txt').read_text(encoding='utf-8') == 'done\\n'"
    return [sys.executable, "-c", code]


@dataclass
class Result:
    code: int
    data: dict[str, Any]
    stderr: str
    project: Path
    calls: list[tuple[str, str, str, str]] = field(default_factory=list)  # (provider, stage, task, mode)

    def implementers(self, task_id: str) -> list[str]:
        return [p for p, stage, task, _ in self.calls if stage == "implement" and task == task_id]

    @property
    def text(self) -> str:
        return json.dumps(self.data, sort_keys=True) + "\n" + self.stderr

    @property
    def stop(self) -> str:
        return str(self.data.get("stop_reason"))

    @property
    def brief(self) -> str:
        return (
            f"code={self.code} stop={self.stop} task={self.data.get('task_id')} message={self.data.get('message')!r} "
            f"stderr={self.stderr.strip()!r} calls={self.calls}"
        )

    def assert_hands_off(self) -> None:
        lowered = self.text.lower()
        for phrase in FORBIDDEN_OPERATOR_TEXT:
            assert phrase not in lowered, f"normal recovery asked the operator for {phrase!r}: {self.brief}"


class Matrix:
    def __init__(
        self,
        tmp_path: Path,
        tasks: list[str],
        plan: dict[str, dict[str, Any]] | None = None,
        implement_pool: tuple[str, ...] = PROVIDERS,
        review_pool: tuple[str, ...] = PROVIDERS,
    ):
        self.project = tmp_path / "repo"
        self.project.mkdir(parents=True)
        self.plan_path = tmp_path / "plan.json"
        self.calls_path = tmp_path / "calls.log"
        self.set_plan(plan or {})
        script = tmp_path / "fake_provider.py"
        script.write_text(FAKE_PROVIDER, encoding="utf-8")
        git = self.git = GitWorkspace(self.project)
        git.init_if_needed()
        git.run("config", "user.email", "matrix@example.invalid")
        git.run("config", "user.name", "Stabilization Matrix")
        (self.project / "README.md").write_text("matrix\n", encoding="utf-8")
        git.commit_all("base")
        git.run("branch", "integration")
        runtime = self.project / ".stagemesh"
        (runtime / "contracts").mkdir(parents=True)
        (runtime / "backlog.json").write_text(
            json.dumps(
                {
                    "objective": "matrix",
                    "tasks": [
                        {"id": t, "title": f"task {t}", "eligible": True, "state": "OPEN", "labels": [], "dependencies": []}
                        for t in tasks
                    ],
                }
            ),
            encoding="utf-8",
        )
        for task_id in tasks:
            (runtime / "contracts" / f"{task_id}.json").write_text(
                json.dumps(
                    {
                        "objective": f"write out/{task_id}.txt",
                        "allowed_files": ["out/**"],
                        "required_tests": [{"name": "output-done", "command": gate(task_id)}],
                    }
                ),
                encoding="utf-8",
            )
        command = lambda name: shlex.join([sys.executable, str(script), str(self.plan_path), name])
        (runtime / "config.json").write_text(
            json.dumps(
                {
                    "providers": {name: command(name) for name in PROVIDERS},
                    "integration_ref": "integration",
                    "routing": {"mode": "STAGED", "pools": {"IMPLEMENT": list(implement_pool), "REVIEW": list(review_pool)}},
                }
            ),
            encoding="utf-8",
        )

    def set_plan(self, plan: dict[str, dict[str, Any]]) -> None:
        self.plan_path.write_text(json.dumps(plan), encoding="utf-8")

    def store(self) -> Store:
        store = Store(self.project / ".stagemesh" / "stagemesh.sqlite3")
        store.migrate()
        return store

    def task(self, task_id: str) -> dict[str, Any]:
        store = self.store()
        try:
            row = store.get_task(task_id)
            return dict(row) if row is not None else {}
        finally:
            store.close()

    def audit(self, event_type: str) -> list[dict[str, Any]]:
        store = self.store()
        try:
            rows = store.conn.execute("SELECT payload FROM audit_events WHERE event_type=? ORDER BY rowid", (event_type,)).fetchall()
            return [json.loads(row["payload"]) for row in rows]
        finally:
            store.close()

    def integrated(self, task_id: str) -> bool:
        shown = self.git.run("show", f"integration:out/{task_id}.txt", check=False)
        return shown.returncode == 0 and shown.stdout == "done\n"

    def drain(self, max_runs: int = 6) -> Result:
        """Run `stagemesh continue` repeatedly, as an operator re-running it would, until a run stops for any reason but DONE."""
        for _ in range(max_runs):
            result = self.continue_()
            if result.stop != "DONE":
                return result
        raise AssertionError(f"queue did not drain in {max_runs} runs: {result.brief}")

    def continue_(self, *argv: str) -> Result:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli_module.main(["--project", str(self.project), "continue", "--json", *argv])
        try:
            data = json.loads(out.getvalue())
        except json.JSONDecodeError:
            data = {"unparsed_stdout": out.getvalue()}
        calls = []
        if self.calls_path.exists():
            calls = [tuple(line.split()[:4]) for line in self.calls_path.read_text(encoding="utf-8").splitlines()]
        return Result(code, data, err.getvalue(), self.project, calls)  # type: ignore[arg-type]
