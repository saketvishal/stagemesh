"""Project compatibility smoke: can this StageMesh version operate safely on this project's profile?

Everything here is generic. It reads `.stagemesh/profile.json`, builds the contracts the profile would generate, and checks them;
the optional dry run syncs tasks into a throwaway database, so nothing in the project (contracts, store, worktrees) is written and
no implementation is ever started. A project describes itself through its profile (including the optional `smoke` probes).
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .auto_plan import AutoPlanError, profile_payload, validate_generated
from .config import StageMeshConfig
from .contracts import ContractError, canonical_contract_json, parse_contract, task_contract_path
from .persistence import Store
from .profile import (
    PROFILE_FILE,
    Profile,
    ProfileError,
    TypeDecision,
    build_contract,
    expand_gate,
    load_profile,
    resolve_type,
)
from .task_selection import SelectionRefusal, rank_batch_candidates, select_next_task

SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "cmd", "powershell", "pwsh"}
_SHELL_META = re.compile(r"[;|&<>`\n]|\$\(")
MAX_GATE_TIMEOUT_SECONDS = 3600
Sync = Callable[[Store, "str | None"], None]


@dataclass
class Check:
    name: str
    status: str  # pass | fail | warn | skip
    detail: str = ""
    items: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail, **({"items": self.items} if self.items else {})}


@dataclass
class SmokeReport:
    project: str
    profile: str | None = None
    checks: list[Check] = field(default_factory=list)
    task_types: dict[str, Any] = field(default_factory=dict)
    probes: list[dict[str, Any]] = field(default_factory=list)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    selection: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return not any(check.status == "fail" for check in self.checks)

    def add(self, name: str, status: str, detail: str = "", items: list[str] | None = None) -> Check:
        check = Check(name, status, detail, items or [])
        self.checks.append(check)
        return check

    def to_dict(self) -> dict[str, Any]:
        counts = {s: sum(1 for c in self.checks if c.status == s) for s in ("pass", "fail", "warn", "skip")}
        return {
            "ok": self.ok,
            "project": self.project,
            "profile": self.profile,
            "summary": counts,
            "checks": [c.to_dict() for c in self.checks],
            "task_types": self.task_types,
            "probes": self.probes,
            "tasks": self.tasks,
            "selection": self.selection,
        }


def gate_safety_problems(gate: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(failures, warnings) for one expanded gate: it must be an argv list that no shell will interpret."""
    failures: list[str] = []
    warnings: list[str] = []
    command = gate.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
        return ["command must be a non-empty list of non-empty strings"], warnings
    program = command[0]
    stem = PurePosixPath(program.replace("\\", "/")).name.casefold().removesuffix(".exe")
    if stem in SHELLS:
        failures.append(f"runs through a shell interpreter ({program}); name the real program and its arguments instead")
    if len(command) == 1 and re.search(r"\s", program) and not os.path.exists(program):
        failures.append(f"a single string with spaces ({program!r}) looks like a shell command line, not an argument list")
    if _SHELL_META.search(program):
        failures.append(f"program {program!r} contains shell metacharacters")
    for part in command:
        if "${" in part:
            failures.append(f"unexpanded variable in {part!r}")
    cwd = gate.get("cwd")
    if cwd is not None:
        normalized = str(cwd).replace("\\", "/")
        if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized) or ".." in PurePosixPath(normalized).parts:
            failures.append(f"cwd {cwd!r} must be relative to the checkout and stay inside it")
    timeout = gate.get("timeout_seconds")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_GATE_TIMEOUT_SECONDS:
        failures.append(f"timeout_seconds must be between 1 and {MAX_GATE_TIMEOUT_SECONDS} (got {timeout!r})")
    env = gate.get("env") or {}
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        failures.append("env must map strings to strings")
    if not failures and shutil.which(program) is None and not os.path.isfile(program):
        warnings.append(f"{program} is not on PATH or a file here (it may exist where the gates run)")
    return failures, warnings


def forbidden_pattern_problems(patterns: tuple[str, ...]) -> list[str]:
    problems: list[str] = []
    for pattern in patterns:
        normalized = pattern.replace("\\", "/")
        if normalized in {"**", "*", "**/*"}:
            problems.append(f"{pattern!r} forbids every file")
        elif normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized) or ".." in PurePosixPath(normalized).parts:
            problems.append(f"{pattern!r} must be a relative pattern inside the checkout")
    return problems


def run_smoke(
    project: Path,
    config: StageMeshConfig,
    *,
    task_ids: list[str] | None = None,
    dry_run_selection: bool = False,
    sync: Sync | None = None,
) -> SmokeReport:
    project = Path(project).resolve()
    report = SmokeReport(str(project))
    profile = _load(report, project)
    if profile is None:
        return report
    report.profile = profile.name
    _check_types(report, profile)
    _check_forbidden(report, profile)
    _check_probes(report, profile)
    if task_ids or dry_run_selection:
        if sync is None:
            report.add("task sync", "fail", "no task sync function was provided")
            return report
        scratch = tempfile.TemporaryDirectory(prefix="stagemesh-smoke-")
        store = Store(Path(scratch.name) / "smoke.sqlite3")
        try:
            store.migrate()
            sync(store, None)
            report.add("task discovery", "pass", f"{len(store.tasks())} task(s) discovered into a throwaway database")
            for task_id in task_ids or []:
                _check_task(report, store, project, profile, task_id)
            if dry_run_selection:
                _dry_run(report, store, project, config)
        except Exception as exc:  # noqa: BLE001 - a broken source must be a reported failure, not a traceback
            report.add("task discovery", "fail", f"{type(exc).__name__}: {exc}")
        finally:
            store.close()
            scratch.cleanup()
    else:
        report.add("task selection", "skip", "pass --task <id> and/or --dry-run-selection to exercise real tasks")
    return report


def _load(report: SmokeReport, project: Path) -> Profile | None:
    try:
        profile = load_profile(project)
    except ProfileError as exc:
        report.add("profile loads", "fail", f"{PROFILE_FILE} is unusable: {exc}")
        return None
    if profile is None:
        report.add("profile loads", "fail", f"no {PROFILE_FILE}; auto-planning would fall back to root-level gate guessing")
        return None
    report.add("profile loads", "pass", f"profile {profile.name}: {len(profile.types)} task type(s), {len(profile.gates)} gate(s)")
    return profile


def _check_types(report: SmokeReport, profile: Profile) -> None:
    bad_contracts: list[str] = []
    unsafe: list[str] = []
    warned: list[str] = []
    seen_gates: dict[str, dict[str, Any]] = {}
    for type_id, task_type in profile.types.items():
        info: dict[str, Any] = {"level": task_type.level, "gates": [], "allowed_files": list(task_type.allowed_files)}
        try:
            payload = build_contract(profile, TypeDecision(type_id, "smoke", ()), f"smoke probe for {type_id}", "smoke-task", "")
            size = validate_generated(payload)
            parse_contract(payload)
            info["contract_chars"] = size
        except (AutoPlanError, ProfileError, ContractError, ValueError) as exc:
            bad_contracts.append(f"{type_id}: {getattr(exc, 'message', exc)}")
        for gate_id in task_type.gates:
            try:
                gate = expand_gate(profile, gate_id)
            except ProfileError as exc:
                unsafe.append(f"{type_id}/{gate_id}: {exc}")
                continue
            info["gates"].append(gate["name"])
            if gate_id not in seen_gates:
                seen_gates[gate_id] = gate
                failures, warnings = gate_safety_problems(gate)
                unsafe.extend(f"{gate['name']}: {problem}" for problem in failures)
                warned.extend(f"{gate['name']}: {problem}" for problem in warnings)
        report.task_types[type_id] = info
    report.add(
        "generated contracts parse",
        "fail" if bad_contracts else "pass",
        "every task type produces a valid explicit contract within the size limit" if not bad_contracts else "some task types produce invalid contracts",
        bad_contracts,
    )
    report.add(
        "validation gates are safe command lists",
        "fail" if unsafe else "pass",
        f"{len(seen_gates)} gate(s) are argv lists with bounded timeouts and no shell interpretation" if not unsafe else "unsafe or malformed gates",
        unsafe,
    )
    if warned:
        report.add("gate executables resolve", "warn", "some gate programs were not found on this machine", warned)
    else:
        report.add("gate executables resolve", "pass", "every gate program was found")


def _check_forbidden(report: SmokeReport, profile: Profile) -> None:
    problems = forbidden_pattern_problems(profile.forbidden_files)
    for type_id, task_type in profile.types.items():
        problems += [f"{type_id}: {p}" for p in forbidden_pattern_problems(task_type.forbidden_files)]
        clash = sorted(set(task_type.allowed_files) & set((*profile.forbidden_files, *task_type.forbidden_files)))
        problems += [f"{type_id}: {pattern!r} is both allowed and forbidden" for pattern in clash]
    if not profile.forbidden_files:
        problems.append("the profile defines no top-level forbidden_files (secrets, lockfiles, CI and similar belong there)")
    report.add(
        "forbidden-file patterns exist",
        "fail" if problems else "pass",
        f"{len(profile.forbidden_files)} profile-wide forbidden pattern(s), all well-formed" if not problems else "forbidden patterns are missing or malformed",
        problems,
    )


def _check_probes(report: SmokeReport, profile: Profile) -> None:
    """Declared probes must select their expected type; every type should also be reachable by its own labels or keywords."""
    mismatches: list[str] = []
    for probe in profile.smoke:
        decision = resolve_type(profile, probe.labels, probe.title, probe.body)
        ok = decision.type_id == probe.expect_type
        report.probes.append({"title": probe.title, "labels": list(probe.labels), "expected": probe.expect_type, "selected": decision.type_id, "ok": ok, "selection": decision.to_dict()})
        if not ok:
            mismatches.append(f"{probe.title!r}: expected {probe.expect_type}, selected {decision.type_id} ({decision.source})")
    if profile.smoke:
        report.add("smoke probes select the expected types", "fail" if mismatches else "pass", f"{len(profile.smoke)} probe(s) declared by the profile", mismatches)
    else:
        report.add("smoke probes select the expected types", "skip", "the profile declares no smoke probes (optional `smoke.tasks`)")
    unreachable = []
    for type_id, task_type in profile.types.items():
        if type_id in {profile.default_type, profile.escalation_type}:
            continue
        triggers = [(task_type.labels, "", ""), ((), task_type.keywords[0] if task_type.keywords else "", "")]
        if not any(
            resolve_type(profile, labels, title, body).type_id == type_id
            for labels, title, body in triggers
            if labels or title
        ):
            unreachable.append(type_id)
    report.add(
        "every task type is reachable",
        "warn" if unreachable else "pass",
        "each non-default type can be selected by its own labels or keywords" if not unreachable else "no label or keyword selects these types",
        unreachable,
    )


def _check_task(report: SmokeReport, store: Store, project: Path, profile: Profile, task_id: str) -> None:
    name = f"task {task_id}"
    if store.get_task(task_id) is None:
        row = store.conn.execute("SELECT id FROM tasks WHERE source_id=?", (task_id,)).fetchone()
        task_id = str(row["id"]) if row else task_id
    if store.get_task(task_id) is None:
        report.add(name, "fail", "task was not found in any configured task source")
        return
    entry: dict[str, Any] = {"task_id": task_id, "hand_written_contract": False}
    path = task_contract_path(project, task_id)
    if path is not None:
        entry["hand_written_contract"] = True
        try:
            import json

            canonical_contract_json(parse_contract(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError, ContractError) as exc:
            report.tasks.append(entry)
            report.add(name, "fail", f"hand-written contract {path.name} does not parse: {exc}")
            return
    try:
        planned = profile_payload(store, project, task_id)
        assert planned is not None
        payload, info = planned
        size = validate_generated(payload)
    except AutoPlanError as exc:
        report.tasks.append(entry)
        report.add(name, "fail", exc.message)
        return
    entry.update(
        selection=info["selection"],
        task_type=info["task_type"],
        gates=[g["name"] for g in payload["required_tests"]],
        allowed_files=payload["allowed_files"],
        forbidden_files=payload["forbidden_files"],
        max_changed_files=payload["max_changed_files"],
        max_diff_lines=payload["max_diff_lines"],
        contract_chars=size,
    )
    report.tasks.append(entry)
    selection = info["selection"]
    report.add(name, "pass", f"type {info['task_type']} via {selection['source']}; {len(entry['gates'])} gate(s); contract parses ({size} chars)")


def _dry_run(report: SmokeReport, store: Store, project: Path, config: StageMeshConfig) -> None:
    try:
        ranked, skipped, policy = rank_batch_candidates(store, project, config.task_selection, auto_plan=True)
    except Exception as exc:  # noqa: BLE001
        report.add("dry-run selection", "fail", f"{type(exc).__name__}: {exc}")
        return
    try:
        chosen = select_next_task(store, project, config.task_selection, auto_plan=True).to_dict()
    except SelectionRefusal as refusal:
        chosen = {"refused": refusal.reason, "message": refusal.message}
    report.selection = {"next": chosen, "eligible": [c.to_dict() for c in ranked], "skipped": skipped, "policy": policy}
    failures: list[str] = []
    for candidate in ranked:
        if candidate.contract == "valid":
            continue  # an existing contract; nothing to auto-plan
        try:
            planned = profile_payload(store, project, candidate.task_id)
            assert planned is not None
            validate_generated(planned[0])
        except AutoPlanError as exc:
            failures.append(f"{candidate.task_id}: {exc.message}")
    detail = f"{len(ranked)} eligible, {len(skipped)} skipped; next: " + (
        f"task {chosen['task_id']} ({chosen['mode']})" if "task_id" in chosen else str(chosen.get("message"))
    )
    report.add("dry-run selection and auto-planning", "fail" if failures else "pass", detail + " (nothing was implemented or written)", failures)
