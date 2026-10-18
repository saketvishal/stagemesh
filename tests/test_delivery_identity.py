"""Delivery lookup is keyed by exact task identity and bound to one repository.

Regression for the task-ID edge case: audit payloads are stored as `json.dumps` text, so an id containing a quote, backslash or
non-ASCII character is stored escaped. The earlier `payload LIKE '%"task_id": "<id>"%'` lookup never found such a task's PR,
merge or external-permission blocker, so a recorded blocker was lost (publication retried against a denied permission) and a
published task looked unpublished. SQL wildcards (`%`, `_`) in an id also widened the scan.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stagemesh.delivery_state import (
    pr_url_repository,
    record_external_block,
    record_publication,
    task_delivery,
    externally_blocked,
)
from stagemesh.domain import Stage
from stagemesh.persistence import Store

SHA = "a" * 40
OTHER = "b" * 40

# Supported identifier shapes: GitHub issue numbers, uuid-like ids, "TASK-n", local backlog ids with punctuation or non-ASCII.
TASK_IDS = [
    "101",
    "gh-12",
    "TASK-1",
    "0b2f5c1e-6c1f-4f7a-9d58-0d6f3a1c9e11",
    'say "hi"',
    "back\\slash",
    "naïve-ünïcode-任务",
    "100%_done",
    "task with spaces",
]


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "s.sqlite3")
    store.migrate()
    return store


def _task_at_candidate(store: Store, task_id: str, sha: str = SHA) -> None:
    store.upsert_task(f"title {task_id}", source_id=task_id)
    store.advance_task(task_id, Stage.DONE)
    store.add_candidate(task_id, sha, "codex", durable_handoff=True)


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_publication_and_permission_blocker_are_found_for_every_supported_id_shape(tmp_path: Path, task_id: str) -> None:
    store = _store(tmp_path)
    _task_at_candidate(store, task_id)
    record_publication(store, task_id, "https://github.com/acme/app/pull/7", SHA, "main", repository="acme/app")
    assert task_delivery(store, task_id).state == "PUBLISHED"
    assert task_delivery(store, task_id).pr_url == "https://github.com/acme/app/pull/7"

    assert record_external_block(store, task_id, SHA, "403 forbidden", "permission to merge") is True
    assert externally_blocked(store, task_id) is not None
    assert record_external_block(store, task_id, SHA, "403 forbidden", "permission to merge") is False  # recorded once
    assert task_delivery(store, task_id).state == "EXTERNALLY_BLOCKED"


def test_similar_ids_never_borrow_each_others_delivery(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for task_id in ("TASK-1", "TASK-10", "TASK_1", "task-1", "TASK-1 "[:-1] + "%"):
        _task_at_candidate(store, task_id)
    record_publication(store, "TASK-1", "https://github.com/acme/app/pull/1", SHA, "main", repository="acme/app")
    record_external_block(store, "TASK-10", SHA, "permission denied", "permission to push")
    assert task_delivery(store, "TASK-1").state == "PUBLISHED"
    assert task_delivery(store, "TASK-10").state == "EXTERNALLY_BLOCKED"
    for other in ("TASK_1", "task-1", "TASK-1%"):
        assert task_delivery(store, other).state == "INTEGRATED_LOCALLY"
        assert externally_blocked(store, other) is None


def test_publication_for_another_candidate_or_repository_is_not_associated(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _task_at_candidate(store, "T-1", OTHER)
    record_publication(store, "T-1", "https://github.com/acme/app/pull/3", SHA, "main", repository="acme/app")
    assert task_delivery(store, "T-1").state != "PUBLISHED"  # the PR is for a different candidate

    _task_at_candidate(store, "T-2")
    record_publication(store, "T-2", "https://github.com/acme/app/pull/4", SHA, "main", repository="acme/app")
    assert task_delivery(store, "T-2", repository="acme/app").state == "PUBLISHED"
    assert task_delivery(store, "T-2", repository="other/repo").state != "PUBLISHED"  # a different repository's PR never counts


def test_pr_url_repository_parses_only_pull_urls() -> None:
    assert pr_url_repository("https://github.com/Acme/App/pull/12") == "acme/app"
    assert pr_url_repository("https://github.com/acme/app/issues/12") is None
    assert pr_url_repository("not a url") is None
    assert pr_url_repository(None) is None
