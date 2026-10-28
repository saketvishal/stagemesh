"""Recovery authority for unfinished subprocess trees, separate from root liveness."""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from .domain import ExecutionKind, ExecutionStatus


def validation_tree_unresolved(execution: sqlite3.Row | Mapping[str, Any] | None) -> bool:
    """A RUNNING validation may own children even after its recorded parent exits.

    Kind/status are committed before gate launch, including the launch crash gap.
    Legacy and prelaunch rows also hold: neither proves a child never started.
    Actual owner completion or an explicit inspected release ends this authority.
    """
    return execution is not None and execution["kind"] == ExecutionKind.VALIDATION and execution["status"] == ExecutionStatus.RUNNING


RESEARCH_PROCESS_TREE = "research/v1"


def process_tree_scope(execution: sqlite3.Row | Mapping[str, Any] | None) -> object:
    if execution is None:
        return None
    try:
        return execution["process_tree_scope"]
    except (IndexError, KeyError):
        return None  # pre-upgrade rows/dictionary projections have no authority


def execution_tree_unresolved(execution: sqlite3.Row | Mapping[str, Any] | None) -> bool:
    """Explicit launch authority survives root death and cleanup-result changes.

    Non-null unknown scope versions hold too; automatic recovery cannot infer
    their cleanup contract. Legacy validation authority stays unchanged.
    """
    return validation_tree_unresolved(execution) or (
        execution is not None and execution["status"] == ExecutionStatus.RUNNING
        and process_tree_scope(execution) is not None
    )


def tree_cleanup_state(execution: sqlite3.Row | Mapping[str, Any] | None) -> str | None:
    if not execution_tree_unresolved(execution):
        return None
    if validation_tree_unresolved(execution):
        return "UNCONFIRMED_VALIDATION_TREE"
    return "UNCONFIRMED_RESEARCH_TREE" if process_tree_scope(execution) == RESEARCH_PROCESS_TREE else "UNCONFIRMED_PROCESS_TREE"


def tree_description(execution: sqlite3.Row | Mapping[str, Any] | None) -> str:
    if validation_tree_unresolved(execution):
        return "validation tree"
    return "research tree" if process_tree_scope(execution) == RESEARCH_PROCESS_TREE else "process tree"


def recovery_identity_state(execution: sqlite3.Row | Mapping[str, Any] | None, parent_state: str) -> str:
    """Keep a known live parent visible; its absence cannot prove tree completion."""
    return "UNKNOWN" if parent_state != "LIVE" and execution_tree_unresolved(execution) else parent_state
