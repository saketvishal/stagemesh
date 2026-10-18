"""Automatic delivery follow-through, wired into the real run-window, publication and landing paths.

Two entry points, one delivery system:

* `register_pending_follow_ups`  called from `run_window` when a window ends with authorized delivery still pending (publication
  deferred by the deadline or a transient failure, or a published PR still waiting for hosted checks). It persists the exact
  identity (repository, task, candidate, PR) and the explicit authorization scope granted by that invocation, and arms ONE schedule.
* `run_follow_up`  the worker a schedule wakes. It does not implement a second publisher or merger: it re-validates the identity and
  then runs the very same narrowed `run-window` command that the authorizing window would have run (no implementation replay),
  and decides what happened from the ledger, not from the command's exit code.

Rules enforced here:

* authorization is explicit and scoped. `publish` and `merge` come only from the options of the invocation that registered the
  follow-up; passing checks or a finished task never grant a merge.
* identity is exact. A different repository, a candidate that is no longer the task's latest, or a PR whose head is not the
  recorded candidate is stale and is never acted on; the follow-up ends with one blocker.
* pending CI, deadline expiry, transient failure and permission denial are different outcomes. Pending CI keeps waiting (quietly);
  expiry and exhausted retries end with one blocker; a permission denial is recorded once and ends the follow-up, with no other
  route to the same action (no alternative branch, credential, force or merge path).
* one worker per follow-up (an OS file lock), one record per fact (atomic once-inserts), one notification per distinct fact.
* completion and cancellation remove the OS schedule through the scheduler adapter and verify it is gone. A removal that fails is
  retried by the next wakeup, which does nothing else.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .audit import record_audit
from .concurrency import IntegrationLock, IntegrationLockTimeout
from .delivery_state import (
    DELIVERY_UNBLOCKED,
    is_permission_denial,
    latest_landing_result,
    pr_url_repository,
    record_external_block,
    task_delivery,
)
from .follow_through import (
    SCOPE_MERGE,
    SCOPE_PUBLISH,
    FollowUpIdentity,
    authorize_follow_up,
    checks_made,
    complete_follow_up,
    follow_up_active,
    follow_up_outcome,
    follow_up_record,
    notify_once,
    record_check,
    record_descheduled,
    record_schedule,
    register_follow_up,
    schedule_handle,
)
from .followup_scheduler import FollowUpScheduler, SchedulerError, follow_up_command
from .persistence import Store
from .retry import RetryRegistry

DEFAULT_INTERVAL_SECONDS = 600
DEFAULT_TTL_SECONDS = 24 * 60 * 60
MAX_TRANSIENT_FAILURES = 5
REARMED = "followup.rearmed"

PENDING_STATES = frozenset({"DELIVERY_PENDING", "PUBLISHED", "AWAITING_CHECKS"})


class HostingError(RuntimeError):
    def __init__(self, message: str):
        super().__init__(message)
        self.permission = is_permission_denial(message)


@dataclass(frozen=True)
class PullRequestSnapshot:
    state: str  # OPEN, MERGED, CLOSED
    head_sha: str
    base_ref: str | None = None


class Hosting(Protocol):
    def pr_snapshot(self, pr_url: str) -> PullRequestSnapshot: ...


class GhHosting:
    """Reads a PR's state and exact head with the same `gh` the publisher uses. Read-only."""

    def __init__(self, project: Path):
        self.project = Path(project)

    def pr_snapshot(self, pr_url: str) -> PullRequestSnapshot:
        import json

        try:
            proc = subprocess.run(
                ["gh", "pr", "view", pr_url, "--json", "state,headRefOid,baseRefName"],
                cwd=self.project, capture_output=True, text=True, check=False, timeout=30,
            )
            if proc.returncode != 0:
                raise HostingError((proc.stderr or proc.stdout).strip()[-300:] or "gh pr view failed")
            data = json.loads(proc.stdout)
        except HostingError:
            raise
        except (OSError, subprocess.SubprocessError, ValueError) as exc:  # timeout, gh missing, unreadable output: transient
            raise HostingError(f"{type(exc).__name__}: {exc}"[:300]) from exc
        return PullRequestSnapshot(str(data.get("state") or ""), str(data.get("headRefOid") or ""), data.get("baseRefName"))


@dataclass(frozen=True)
class FollowUpOutcome:
    key: str
    state: str
    terminal: bool
    message: str
    notified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "state": self.state, "terminal": self.terminal, "message": self.message, "notified": self.notified}


Notifier = Callable[[str, str], None]


def repository_of(project: Path) -> str | None:
    """`owner/repo` (lower-cased) of the project's origin, or None when it is not a GitHub remote."""
    from .github import detect_github_repository

    found = detect_github_repository(Path(project), "origin")
    return None if found is None else f"{found.owner}/{found.repo}".casefold()


def generation(store: Store, task_id: str) -> int:
    """How many times an operator has explicitly re-opened delivery for this task (delivery-unblock or follow-up --rearm)."""
    total = 0
    for event_type in (DELIVERY_UNBLOCKED, REARMED):
        row = store.conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type=? AND json_extract(payload, '$.task_id') = ?", (event_type, task_id)
        ).fetchone()
        total += int(row[0])
    return total


def _identity_key(identity: FollowUpIdentity, store: Store) -> str:
    # The generation separates a follow-up that ended (blocked, expired) from the one an operator explicitly re-opened.
    n = generation(store, identity.task_id)
    base = FollowUpIdentity(identity.repository, identity.task_id, identity.candidate_sha).key()
    return base if n == 0 else f"{base}-g{n}"


# --- window side ---------------------------------------------------------------------------------------------------------------


def follow_up_argv(base_argv: Sequence[str], project: Path, task_id: str, *, duration_seconds: float) -> tuple[str, ...]:
    """The narrowed, exact command a follow-up runs: the authorizing window's own options, for this one task, bounded in time."""
    argv = list(base_argv[1:] if base_argv and base_argv[0] == "stagemesh" else base_argv)  # starts at the subcommand
    for flag in ("--tasks", "--duration", "--concurrency", "--max-tasks"):
        while flag in argv:
            index = argv.index(flag)
            del argv[index : index + 2]
    argv += ["--tasks", task_id, "--duration", f"{max(60, int(duration_seconds))}s", "--concurrency", "1", "--max-tasks", "1"]
    # The same interpreter/runtime that armed it (not whatever `stagemesh` resolves to later); --project is a global option.
    argv = [sys.executable, "-m", "stagemesh.cli", "--project", str(project), *argv]
    return tuple(argv)


class UnsafeResumeCommand(ValueError):
    pass


def validated_resume_argv(record: dict[str, Any], project: Path) -> list[str]:
    """The stored resume command, only if it is exactly the shape this module writes: this interpreter's `-m stagemesh.cli`, this
    project, the `run-window` subcommand for this one task. The ledger is local and trusted, but a scheduled job should still never
    execute anything else just because a record says so."""
    argv = [str(part) for part in (record.get("resume_argv") or [])]
    task_id = str(record.get("task_id"))
    ok = (
        len(argv) > 7
        and Path(argv[0]).name.lower().startswith("python")
        and argv[1:3] == ["-m", "stagemesh.cli"]
        and argv[3] == "--project" and Path(argv[4]).resolve() == Path(project).resolve()
        and argv[5] == "run-window"
        and "--tasks" in argv and argv[argv.index("--tasks") + 1] == task_id
        and "--follow-up" not in argv
    )
    if not ok:
        raise UnsafeResumeCommand("the stored resume command is not the narrowed run-window this follow-up was registered with")
    return argv


def register_pending_follow_ups(
    store: Store,
    project: Path,
    task_ids: Iterable[str],
    *,
    repository: str | None,
    open_prs: bool,
    merge_authorized: bool,
    merge_policy: str | None,
    required_checks: Sequence[str],
    authorized_by: str,
    window_id: str,
    base_argv: Sequence[str],
    base: str | None,
    scheduler: FollowUpScheduler | None,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    window_seconds: float = 1800,
    clock: Callable[[], float] = time.time,
) -> list[dict[str, Any]]:
    """Arm at most one follow-up per pending delivery. Returns a compact record per task for the window report."""
    entries: list[dict[str, Any]] = []
    for task_id in dict.fromkeys(task_ids):
        delivery = task_delivery(store, task_id, repository)
        if delivery.state not in PENDING_STATES or delivery.head_sha is None:
            continue
        scope = ((SCOPE_PUBLISH,) if open_prs else ()) + ((SCOPE_MERGE,) if merge_authorized else ())
        needs = SCOPE_PUBLISH if delivery.state == "DELIVERY_PENDING" else SCOPE_MERGE
        landed = latest_landing_result(store, task_id, delivery.head_sha) if needs == SCOPE_MERGE else None
        if landed is not None and not landed["retryable"]:
            entries.append({"task_id": task_id, "state": "not_followed", "reason": f"landing stopped for a reason waiting cannot fix: {landed['reason']}"})
            continue
        if needs not in scope:
            entries.append({"task_id": task_id, "state": "not_followed", "reason": f"this window did not authorize {needs}; a person must {needs}"})
            continue
        if repository is None:
            entries.append({"task_id": task_id, "state": "not_followed", "reason": "the project has no GitHub origin to bind the follow-up to"})
            continue
        identity = FollowUpIdentity(
            repository=repository, task_id=task_id, candidate_sha=delivery.head_sha, pr_url=delivery.pr_url, base=base, scope=scope,
            merge_policy=merge_policy if merge_authorized else None, required_checks=tuple(required_checks) if merge_authorized else (),
            authorized_by=authorized_by, window_id=window_id, expires_at=clock() + ttl_seconds,
            resume_argv=follow_up_argv(base_argv, project, task_id, duration_seconds=window_seconds),
            check_interval_seconds=interval_seconds, max_checks=max(1, ttl_seconds // max(60, interval_seconds)),
        )
        key = _identity_key(identity, store)
        created = register_follow_up(store, key, f"deliver task {task_id}: {delivery.state.lower()}", identity)
        if not created:
            if not follow_up_active(store, key):
                entries.append({"task_id": task_id, "key": key, "state": "ended", "reason": f"the follow-up for this delivery already ended ({follow_up_outcome(store, key)})"})
                continue
            authorize_follow_up(store, key, identity)
        entry = {"task_id": task_id, "key": key, "state": "registered" if created else "existing", "scope": list(scope), "pr_url": delivery.pr_url}
        if scheduler is None:
            entry["scheduled"] = False
            entry["reason"] = "no supported scheduler on this platform; run: stagemesh follow-up --key " + key
        else:
            try:
                handle = schedule_handle(store, key)
                if handle is None or not scheduler.exists(handle):
                    handle = scheduler.ensure(key, follow_up_command(project, key), interval_seconds)
                    record_schedule(store, key, handle, scheduler.name)
                entry["scheduled"], entry["handle"] = True, handle
            except SchedulerError as exc:
                entry["scheduled"], entry["reason"] = False, str(exc)
        entries.append(entry)
    return entries


# --- worker side ---------------------------------------------------------------------------------------------------------------


def _finish(
    store: Store, key: str, scheduler: FollowUpScheduler | None, notify: Notifier, state: str, message: str, *, success: bool
) -> FollowUpOutcome:
    """End a follow-up: record completion, remove the OS schedule (verified), announce once. Safe to repeat after a crash."""
    complete_follow_up(store, key, f"{state}: {message}"[:300])
    cleaned = _deschedule(store, key, scheduler)
    first = notify_once(store, f"{key}:final", f"{state}: {message}")
    if first:
        notify(key, message if cleaned else f"{message} (the schedule could not be removed; the next wakeup will retry)")
    return FollowUpOutcome(key, state, True, message, notified=first)


def _deschedule(store: Store, key: str, scheduler: FollowUpScheduler | None) -> bool:
    handle = schedule_handle(store, key)
    if handle is None:
        return True
    if scheduler is None:
        return False
    try:
        scheduler.remove(handle)
        if scheduler.exists(handle):
            raise SchedulerError("the schedule still exists after removal")
    except SchedulerError as exc:
        if notify_once(store, f"{key}:cleanup_failed", str(exc)):
            record_audit(store, "followup.cleanup_failed", {"key": key, "handle": handle, "error": str(exc)[:300]})
        return False
    record_descheduled(store, key, handle)
    return True


def cancel_follow_up(store: Store, key: str, scheduler: FollowUpScheduler | None, notify: Notifier, reason: str) -> FollowUpOutcome:
    if follow_up_record(store, key) is None:
        return FollowUpOutcome(key, "unknown_follow_up", True, "no such follow-up")
    return _finish(store, key, scheduler, notify, "cancelled", reason, success=False)


def run_follow_up(
    store: Store,
    project: Path,
    key: str,
    *,
    scheduler: FollowUpScheduler | None,
    hosting: Hosting | None,
    window_runner: Callable[[dict[str, Any]], int | None],
    notify: Notifier,
    repository: str | None = None,
    clock: Callable[[], float] = time.time,
    lock_timeout: float = 0.0,
) -> FollowUpOutcome:
    """One wakeup. Never raises for an ordinary delivery condition; returns what happened."""
    lock = IntegrationLock(Path(project) / ".stagemesh" / "followups" / f"{key}.lock", timeout_seconds=lock_timeout)
    try:
        with lock.hold(f"follow-up {key}"):
            return _wakeup(store, project, key, scheduler, hosting, window_runner, notify, repository, clock)
    except IntegrationLockTimeout:
        return FollowUpOutcome(key, "duplicate_worker", False, "another worker already holds this follow-up; this wakeup did nothing")


def _wakeup(
    store: Store, project: Path, key: str, scheduler: FollowUpScheduler | None, hosting: Hosting | None,
    window_runner: Callable[[dict[str, Any]], int | None], notify: Notifier, repository: str | None, clock: Callable[[], float],
) -> FollowUpOutcome:
    record = follow_up_record(store, key)
    if record is None:
        return FollowUpOutcome(key, "unknown_follow_up", True, "no such follow-up is registered")
    if not follow_up_active(store, key):
        # Completed earlier. All this wakeup may do is finish cleanup that a crash or a failed removal left undone.
        _deschedule(store, key, scheduler)
        done = follow_up_outcome(store, key) or "completed"
        if notify_once(store, f"{key}:final", done):  # the finishing run crashed before announcing: announce now, once
            notify(key, done)
        return FollowUpOutcome(key, "inactive", True, f"already finished ({done})")

    now = clock()
    task_id, candidate = str(record["task_id"]), str(record["candidate_sha"])
    scope = tuple(record.get("scope") or ())
    if record.get("expires_at") is not None and now >= float(record["expires_at"]):
        return _finish(store, key, scheduler, notify, "expired",
                       f"the delivery follow-up for task {task_id} expired before it could finish; the verified work is preserved. "
                       f"Re-arm with `stagemesh follow-up --rearm --key {key}` or rerun the window.", success=False)
    if checks_made(store, key) >= int(record.get("max_checks") or 1):
        return _finish(store, key, scheduler, notify, "exhausted",
                       f"task {task_id}: checked {checks_made(store, key)} times without finishing delivery; stopped.", success=False)
    if repository is not None and repository.casefold() != str(record["repository"]).casefold():
        return _finish(store, key, scheduler, notify, "repository_mismatch",
                       f"task {task_id}: this project's origin is {repository}, the follow-up was authorized for {record['repository']}; nothing was done.", success=False)
    task = store.get_task(task_id)
    latest = store.latest_candidate(task_id)
    if task is None or latest is None or str(latest["sha"]) != candidate:
        return _finish(store, key, scheduler, notify, "stale_candidate",
                       f"task {task_id}: the task's candidate is no longer {candidate[:12]}; the authorization applied to that exact head only.", success=False)

    registry = RetryRegistry(store)
    gate = registry.decision(f"followup:{key}", now)
    if not gate.allowed:
        return FollowUpOutcome(key, "not_due", False, "backing off after a transient failure")

    delivery = task_delivery(store, task_id, str(record["repository"]))
    early = _classify(store, key, task_id, candidate, record, delivery, scope, scheduler, notify, hosting)
    if early is not None:
        return early

    record_check(store, key, delivery.state, delivery.detail)
    failure: str | None = None
    wall_start = time.time()  # ledger timestamps are wall-clock even when the follow-up runs on an injected clock
    try:
        code = window_runner(record)
        if code not in (0, None):
            failure = f"the resume window exited with status {code}"
    except Exception as exc:  # noqa: BLE001 - any runner failure is a delivery condition to classify, not a crash
        failure = f"{type(exc).__name__}: {exc}"

    after = task_delivery(store, task_id, str(record["repository"]))
    if after.state == "MERGED":
        return _finish(store, key, scheduler, notify, "merged", f"task {task_id}: PR {after.pr_url} merged at the exact reviewed head {candidate[:12]}.", success=True)
    if after.state == "EXTERNALLY_BLOCKED":
        return _blocked(store, key, scheduler, notify, task_id, after.detail)
    if failure is not None and is_permission_denial(failure):
        record_external_block(store, task_id, candidate, failure, "permission to publish or merge this delivery")
        return _blocked(store, key, scheduler, notify, task_id, failure)
    if after.state in {"PUBLISHED", "AWAITING_CHECKS"}:
        registry.record_success(f"followup:{key}")
        if SCOPE_MERGE not in scope:  # published, and publication was all that was authorized: a person merges
            return _finish(store, key, scheduler, notify, "published_merge_not_authorized",
                           f"task {task_id}: PR {after.pr_url} is published; merging was not authorized, so a person must merge it.", success=True)
        landed = latest_landing_result(store, task_id, candidate, wall_start)
        if landed is not None and not landed["retryable"]:  # the guard reported a fact that waiting does not fix
            return _finish(store, key, scheduler, notify, "landing_blocked",
                           f"task {task_id}: the PR cannot be merged as it stands ({landed['reason'][:200]}). The PR and the verified head are "
                           "preserved; this needs a decision, so the follow-up has stopped.", success=False)
        if after.pr_url and after.pr_url != record.get("pr_url"):
            authorize_follow_up(store, key, FollowUpIdentity(**{**_identity_fields(record), "pr_url": after.pr_url}))
        text = f"task {task_id}: {after.state.replace('_', ' ').lower()} ({after.pr_url}); waiting for hosted checks."
        announced = notify_once(store, f"{key}:waiting:{after.state}:{after.pr_url}", text)
        if announced:
            notify(key, text)
        return FollowUpOutcome(key, "waiting", False, text, notified=announced)
    # Still pending after the run: a deadline, an unavailable publisher or a refused window. Bounded retry, then one blocker.
    reason = failure or f"delivery is still {after.state.lower()} after the resume window"
    decision = registry.record_failure(f"followup:{key}", reason[:190], now)
    if decision.attempts >= MAX_TRANSIENT_FAILURES:
        return _finish(store, key, scheduler, notify, "transient_exhausted",
                       f"task {task_id}: {decision.attempts} attempts to resume delivery failed ({reason[:160]}). The verified work is preserved; "
                       f"fix the cause, then `stagemesh follow-up --rearm --key {key}`.", success=False)
    return FollowUpOutcome(key, "retry", False, f"transient failure {decision.attempts}/{MAX_TRANSIENT_FAILURES}: {reason[:160]}")


def _identity_fields(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "repository": record["repository"], "task_id": record["task_id"], "candidate_sha": record["candidate_sha"], "pr_url": record.get("pr_url"),
        "base": record.get("base"), "scope": tuple(record.get("scope") or ()), "merge_policy": record.get("merge_policy"),
        "required_checks": tuple(record.get("required_checks") or ()), "authorized_by": record.get("authorized_by", ""),
        "window_id": record.get("window_id", ""), "resume_argv": tuple(record.get("resume_argv") or ()), "expires_at": record.get("expires_at"),
        "check_interval_seconds": record.get("check_interval_seconds") or DEFAULT_INTERVAL_SECONDS, "max_checks": record.get("max_checks") or 1,
    }


def _blocked(store: Store, key: str, scheduler: FollowUpScheduler | None, notify: Notifier, task_id: str, detail: str) -> FollowUpOutcome:
    return _finish(
        store, key, scheduler, notify, "externally_blocked",
        f"task {task_id}: delivery needs permission that is missing ({detail[:200]}). The work is preserved and no other route was tried; "
        f"grant access, then `stagemesh delivery-unblock --task {task_id}` and rerun the window with --follow-up.", success=False,
    )


def _classify(
    store: Store, key: str, task_id: str, candidate: str, record: dict[str, Any], delivery: Any, scope: tuple[str, ...],
    scheduler: FollowUpScheduler | None, notify: Notifier, hosting: Hosting | None,
) -> FollowUpOutcome | None:
    """Decisions that need no resume window. None means: go and resume."""
    if delivery.state == "MERGED":
        return _finish(store, key, scheduler, notify, "merged", f"task {task_id}: the exact reviewed head {candidate[:12]} is merged.", success=True)
    if delivery.state == "EXTERNALLY_BLOCKED":
        return _blocked(store, key, scheduler, notify, task_id, delivery.detail)
    if delivery.complete or delivery.state not in PENDING_STATES:
        return _finish(store, key, scheduler, notify, "nothing_to_deliver", f"task {task_id}: delivery state is {delivery.state.lower()}; nothing to follow.", success=True)
    if delivery.state == "DELIVERY_PENDING":
        if SCOPE_PUBLISH not in scope:
            return _finish(store, key, scheduler, notify, "not_authorized", f"task {task_id}: publication was not authorized for this follow-up.", success=False)
        return None
    # PUBLISHED / AWAITING_CHECKS: only a recorded merge authorization may continue. Passing checks never grant one.
    if SCOPE_MERGE not in scope:
        return _finish(store, key, scheduler, notify, "published_merge_not_authorized",
                       f"task {task_id}: PR {delivery.pr_url} is published; merging was not authorized, so a person must merge it.", success=True)
    pr_url = delivery.pr_url or record.get("pr_url")
    if not pr_url:
        return None
    if pr_url_repository(pr_url) not in (None, str(record["repository"]).casefold()):
        return _finish(store, key, scheduler, notify, "stale_pr", f"task {task_id}: PR {pr_url} belongs to another repository.", success=False)
    if hosting is None:
        return None
    try:
        snapshot = hosting.pr_snapshot(pr_url)
    except HostingError as exc:
        if exc.permission:
            record_external_block(store, task_id, candidate, str(exc), "permission to read or merge this pull request")
            return _blocked(store, key, scheduler, notify, task_id, str(exc))
        return None  # transient: the resume window re-reads it; a window failure is what counts against the retry budget
    except Exception:  # noqa: BLE001 - an unexpected hosting adapter failure is transient too, never a crash of the wakeup
        return None
    if snapshot.state == "MERGED":
        return None  # the landing guard verifies the exact head and records the merge; do not infer it here
    if snapshot.state != "OPEN":
        return _finish(store, key, scheduler, notify, "stale_pr", f"task {task_id}: PR {pr_url} is {snapshot.state.lower()}, not open.", success=False)
    if snapshot.head_sha and snapshot.head_sha != candidate:
        return _finish(store, key, scheduler, notify, "stale_pr",
                       f"task {task_id}: PR head {snapshot.head_sha[:12]} is not the reviewed candidate {candidate[:12]}; refusing to merge it.", success=False)
    return None


def rearm_follow_up(store: Store, key: str) -> str | None:
    """Explicitly re-open delivery for a finished follow-up's task (the next window with --follow-up arms a fresh generation)."""
    record = follow_up_record(store, key)
    if record is None:
        return None
    record_audit(store, REARMED, {"task_id": record["task_id"], "key": key})
    return str(record["task_id"])
