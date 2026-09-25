"""Foreground watcher loop (SDD-001 sections 4.1, 4.6).

`run_foreground_cycle` performs exactly one iteration of the durable
watcher loop: validate repository authorization, acquire/refresh the
watcher lock, delegate work reconciliation to `BuildRunner.run_once()`
(the one and only task scheduler), and record a structured, redacted cycle
outcome. `run_foreground` repeats this until `stop_requested` is set on the
watcher record, sleeping between cycles according to `RunnerConfig.
poll_seconds` modified by any active transient-failure backoff.

This module never approves objective gates, never sets
`auto_push_allowed`, and never bypasses a human-gated push -- it only calls
the same `BuildRunner.run_once()` the foreground `caventra-build run`
command already uses.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from build_coordinator.github.controller import GitHubAutonomousController
from build_coordinator.runner.models import RunnerConfig
from build_coordinator.watcher import lock as watcher_lock
from build_coordinator.watcher.authorization import AuthorizedRepository, authorize
from build_coordinator.watcher.failure_classification import (
    DEFAULT_BACKOFF,
    BackoffPolicy,
    classify_failure,
    is_retryable,
)
from build_coordinator.watcher.labels import provision_labels
from build_coordinator.watcher.safe_logging import WatcherLogger
from build_coordinator.watcher.windows_task_scheduler import stable_task_name


@dataclass(frozen=True)
class CycleOutcome:
    task_name: str
    cycle_id: str
    ok: bool
    failure_class: str | None = None
    backoff_seconds: float | None = None


def run_foreground_cycle(
    session_factory,
    *,
    repository_slug: str,
    logger: WatcherLogger,
    runner_config: RunnerConfig | None = None,
    backoff_policy: BackoffPolicy = DEFAULT_BACKOFF,
    provision_labels_on_cycle: bool = False,
    executors: dict | None = None,
    git=None,
    github_client=None,
    instance_id: str | None = None,
) -> CycleOutcome:
    """Run exactly one watcher cycle. Safe to call repeatedly (e.g. from a
    test or a `--once` CLI flag) without duplicating claims or executions,
    since all task-scheduling work is delegated to
    `BuildRunner.run_once()`."""
    cycle_id = uuid4().hex
    with session_factory() as session:
        try:
            repo = authorize(repository_slug)
        except Exception as exc:  # RepositoryAuthorizationError, CoordinatorConfigError
            failure_class = classify_failure(exc)
            logger.log(
                "watcher.cycle_failed",
                repository_slug=repository_slug,
                cycle_id=cycle_id,
                error_type=failure_class,
                message=str(exc),
            )
            return CycleOutcome(
                task_name="",
                cycle_id=cycle_id,
                ok=False,
                failure_class=failure_class,
            )

        task_name = stable_task_name(str(repo.control_repo_root), repo.slug)
        try:
            acquisition = watcher_lock.acquire_lock(
                session,
                task_name=task_name,
                control_repo_root=str(repo.control_repo_root),
                repository_slug=repo.slug,
                instance_id=instance_id,
            )
            session.commit()
        except watcher_lock.WatcherLockHeld as exc:
            logger.log(
                "watcher.lock_held",
                repository_slug=repo.slug,
                cycle_id=cycle_id,
                message=str(exc),
                extra=exc.owner_health,
            )
            return CycleOutcome(task_name=task_name, cycle_id=cycle_id, ok=False, failure_class="POLICY_FAILURE")

        if acquisition.recovered_stale:
            logger.log(
                "watcher.lock_recovered",
                repository_slug=repo.slug,
                cycle_id=cycle_id,
                extra={
                    "current_instance_id": instance_id,
                    "recorded_owner_instance_id": acquisition.record.watcher_id,
                    "current_pid": acquisition.record.process_id,
                    "recovery_reason": acquisition.recovery_reason,
                },
            )

        if provision_labels_on_cycle and repo.labels:
            try:
                result = provision_labels(repo)
                logger.log(
                    "watcher.labels_provisioned",
                    repository_slug=repo.slug,
                    cycle_id=cycle_id,
                    extra={
                        "created": list(result.created),
                        "updated": list(result.updated),
                    },
                )
            except Exception as exc:
                failure_class = classify_failure(exc)
                _record_failure(session, task_name, failure_class, str(exc), backoff_policy, logger, repo.slug, cycle_id)
                session.commit()
                return CycleOutcome(
                    task_name=task_name,
                    cycle_id=cycle_id,
                    ok=False,
                    failure_class=failure_class,
                )

    try:
        controller = GitHubAutonomousController(
            session_factory,
            runner_config or RunnerConfig.default(),
            github_client=github_client,
            default_repo=repo.slug,
            executors=executors,
            git=git,
        )
        result = controller.run_once()
    except Exception as exc:
        failure_class = classify_failure(exc)
        with session_factory() as session:
            _record_failure(session, task_name, failure_class, str(exc), backoff_policy, logger, repo.slug, cycle_id)
            session.commit()
        return CycleOutcome(task_name=task_name, cycle_id=cycle_id, ok=False, failure_class=failure_class)

    with session_factory() as session:
        runner_result = result.runner_result
        watcher_lock.heartbeat(session, task_name)
        watcher_lock.record_cycle(
            session,
            task_name,
            summary={
                "mode": runner_result.mode,
                "recovered": len(runner_result.recovered),
                "launched": len(runner_result.launched),
                "observed": len(runner_result.observed),
                "escalations": len(runner_result.escalations),
                "github_issues_ingested": len(result.issues_ingested),
                "github_gates_approved": len(result.gates_approved),
                "github_gates_published": len(result.gates_published),
                "github_prs_created": len(result.prs_created),
                "github_statuses_synced": len(result.statuses_synced),
            },
        )
        session.commit()
    logger.log(
        "watcher.cycle_succeeded",
        repository_slug=repo.slug,
        cycle_id=cycle_id,
        result_summary=(
            f"mode={runner_result.mode} launched={len(runner_result.launched)} "
            f"observed={len(runner_result.observed)} github_issues={len(result.issues_ingested)}"
        ),
    )
    return CycleOutcome(task_name=task_name, cycle_id=cycle_id, ok=True)


def _record_failure(
    session,
    task_name: str,
    failure_class: str,
    message: str,
    backoff_policy: BackoffPolicy,
    logger: WatcherLogger,
    repository_slug: str,
    cycle_id: str,
) -> None:
    from build_coordinator.watcher.safe_logging import redact_text

    redacted = redact_text(message)
    backoff_until = None
    if is_retryable(failure_class):
        from build_coordinator.models import BuildWatcherRecord

        record = session.get(BuildWatcherRecord, task_name)
        # `record_cycle` below increments `consecutive_failure_count` for
        # this same failure -- read the pre-increment value here so
        # `attempt` matches the ordinal of the failure being recorded (1st,
        # 2nd, 3rd, ...), letting backoff climb the full exponential curve
        # across restarts instead of plateauing after two failures.
        attempt = (record.consecutive_failure_count if record is not None else 0) + 1
        delay = backoff_policy.delay_seconds(attempt)
        backoff_until = datetime.now(UTC) + timedelta(seconds=delay)
    watcher_lock.record_cycle(
        session,
        task_name,
        summary={},
        error_type=failure_class,
        error_message_redacted=redacted,
        backoff_until=backoff_until,
    )
    logger.log(
        "watcher.cycle_failed",
        repository_slug=repository_slug,
        cycle_id=cycle_id,
        error_type=failure_class,
        message=message,
    )


def next_sleep_seconds(session, task_name: str, *, default_poll_seconds: float) -> float:
    """Poll interval for the next cycle: the configured poll interval,
    or -- if a persisted backoff is still active -- the remaining backoff
    time. Backoff is persisted so a process restart does not erase it."""
    from build_coordinator.models import BuildWatcherRecord

    record = session.get(BuildWatcherRecord, task_name)
    if record is None or record.backoff_until is None:
        return default_poll_seconds
    remaining = (record.backoff_until - datetime.now(UTC)).total_seconds()
    return max(default_poll_seconds, remaining)


def run_foreground(
    session_factory,
    *,
    repository_slug: str,
    logger: WatcherLogger,
    runner_config: RunnerConfig | None = None,
    once: bool = False,
    sleep_fn=time.sleep,
    executors: dict | None = None,
    git=None,
    github_client=None,
) -> None:
    """Repeat `run_foreground_cycle` until stop is requested (or `once` is
    set, for deterministic tests / smoke checks)."""
    config = runner_config or RunnerConfig.default()
    instance_id = uuid4().hex
    while True:
        outcome = run_foreground_cycle(
            session_factory,
            repository_slug=repository_slug,
            logger=logger,
            runner_config=config,
            provision_labels_on_cycle=True,
            executors=executors,
            git=git,
            github_client=github_client,
            instance_id=instance_id,
        )
        if once:
            return
        with session_factory() as session:
            from build_coordinator.models import BuildWatcherRecord

            record = session.get(BuildWatcherRecord, outcome.task_name) if outcome.task_name else None
            if record is not None and record.stop_requested:
                watcher_lock.release_lock(session, outcome.task_name)
                session.commit()
                return
            delay = (
                next_sleep_seconds(session, outcome.task_name, default_poll_seconds=config.poll_seconds)
                if outcome.task_name
                else config.poll_seconds
            )
        sleep_fn(delay)
