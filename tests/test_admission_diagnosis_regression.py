"""Regression tests for StageMesh issue #477:
Task admission failure, diagnosis visibility, and retry loop prevention.

Verifies:
1. Ineligible task sources preserve specific eligibility reasons upon retirement.
2. classify_block identifies source retirement as EXTERNAL_DEPENDENCY (or HUMAN_DECISION for objective roots).
3. task-doctor reports actionable diagnosis and recommendation instead of 'diagnosis: none'.
4. retry-task refuses to unblock an ineligible source or objective root whose condition persists (exit 2).
5. run_ready surfaces the specific block reason in RunReadyRefusal.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from stagemesh.block_kinds import BlockKind, classify_block
from stagemesh.cli import command_retry_task
from stagemesh.domain import TaskStatus
from stagemesh.operator_actions import task_details
from stagemesh.persistence import Store
from stagemesh.recovery import task_doctor
from stagemesh.run_ready import RunReadyRefusal, run_ready
from stagemesh.task_sources import (
    ConfiguredGitHubTaskSource,
    DiscoveredTask,
    sync_source,
)


def _store(tmp_path: Path) -> Store:
    db_path = tmp_path / ".stagemesh" / "stagemesh.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(db_path)
    store.migrate()
    return store


def test_ineligible_source_retirement_preserves_reason_and_classifies_block(tmp_path: Path):
    store = _store(tmp_path)
    try:
        task_id = store.upsert_task("P0 Objective", source="github", source_id="423")
        assert store.get_task(task_id)["status"] == TaskStatus.OPEN

        discovered = [
            DiscoveredTask(
                source="github",
                source_id="423",
                title="P0 Objective",
                eligible=False,
                eligibility_reason="missing required label(s): stagemesh:ready",
                labels=(),
            )
        ]

        synced = sync_source(store, discovered)
        assert synced == []

        task = store.get_task(task_id)
        assert task["status"] == TaskStatus.BLOCKED

        details = task_details(store, task_id)
        assert details["source_reason"] == "source no longer eligible (missing required label(s): stagemesh:ready)"

        block_info = classify_block(store, task_id)
        assert block_info is not None
        assert block_info.kind == BlockKind.EXTERNAL_DEPENDENCY
        assert "missing required label(s): stagemesh:ready" in block_info.reason
    finally:
        store.close()


def test_objective_root_retirement_classifies_as_human_decision(tmp_path: Path):
    store = _store(tmp_path)
    try:
        task_id = store.upsert_task("P0 Parent Objective", source="github", source_id="423")
        discovered = [
            DiscoveredTask(
                source="github",
                source_id="423",
                title="P0 Parent Objective",
                eligible=False,
                eligibility_reason="source issue is an objective root",
                labels=("caventra:objective",),
            )
        ]

        sync_source(store, discovered)
        assert store.get_task(task_id)["status"] == TaskStatus.BLOCKED

        block_info = classify_block(store, task_id)
        assert block_info is not None
        assert block_info.kind == BlockKind.HUMAN_DECISION
        assert "objective root" in block_info.reason
    finally:
        store.close()


def test_task_doctor_reports_actionable_block_and_recommendation(tmp_path: Path):
    store = _store(tmp_path)
    project = tmp_path
    try:
        task_id = store.upsert_task("Issue 423", source="github", source_id="423")
        discovered = [
            DiscoveredTask(
                source="github",
                source_id="423",
                title="Issue 423",
                eligible=False,
                eligibility_reason="missing required label(s): stagemesh:ready",
                labels=(),
            )
        ]
        sync_source(store, discovered)

        report = task_doctor(store, project, task_id)
        assert report["status"] == "BLOCKED"
        assert report["block"] is not None
        assert report["block"]["kind"] == "BLOCKED_EXTERNAL_DEPENDENCY"
        assert "missing required label(s): stagemesh:ready" in report["block"]["reason"]
        assert "external condition not met" in report["recommendation_reason"]
    finally:
        store.close()


def test_command_retry_task_refuses_when_source_condition_unchanged(tmp_path: Path, monkeypatch):
    store = _store(tmp_path)
    project = tmp_path
    (project / ".stagemesh").mkdir(parents=True, exist_ok=True)
    (project / ".stagemesh" / "config.json").write_text(
        json.dumps(
            {
                "task_sources": [
                    {
                        "name": "github-ready",
                        "type": "github",
                        "labels": ["stagemesh:ready"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    try:
        task_id = store.upsert_task("Issue 423", source="github", source_id="423")
        discovered = [
            DiscoveredTask(
                source="github",
                source_id="423",
                title="Issue 423",
                eligible=False,
                eligibility_reason="missing required label(s): stagemesh:ready",
                labels=(),
            )
        ]
        sync_source(store, discovered)
        assert store.get_task(task_id)["status"] == TaskStatus.BLOCKED

        # Mock source sync during retry-task so it doesn't try real network calls
        monkeypatch.setattr(
            "stagemesh.cli._sync_all_sources",
            lambda s, p, c, only_task=None: None,
        )

        args = argparse.Namespace(project=str(project), task=task_id, json=False)
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            exit_code = command_retry_task(args)

        assert exit_code == 2
        assert "retry refused" in stderr.getvalue()
        assert "missing required label(s): stagemesh:ready" in stderr.getvalue()

        # Task remains BLOCKED instead of looping to OPEN
        assert store.get_task(task_id)["status"] == TaskStatus.BLOCKED
    finally:
        store.close()


def test_run_ready_refusal_surfaces_specific_block_reason(tmp_path: Path):
    store = _store(tmp_path)
    project = tmp_path
    try:
        task_id = store.upsert_task("Issue 423", source="github", source_id="423")
        discovered = [
            DiscoveredTask(
                source="github",
                source_id="423",
                title="Issue 423",
                eligible=False,
                eligibility_reason="missing required label(s): stagemesh:ready",
                labels=(),
            )
        ]
        sync_source(store, discovered)

        summary = run_ready(store, project, make_coordinator=lambda sel: None, task_id=task_id)
        assert summary.started is False
        assert summary.stop_reason == "REFUSED:task_blocked"
        assert "missing required label(s): stagemesh:ready" in summary.message
    finally:
        store.close()
