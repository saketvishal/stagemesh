"""Recovery authority for an unfinished validation tree, separate from parent liveness."""
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


def recovery_identity_state(execution: sqlite3.Row | Mapping[str, Any] | None, parent_state: str) -> str:
    """Keep a known live parent visible; its absence cannot prove tree completion."""
    return "UNKNOWN" if parent_state != "LIVE" and validation_tree_unresolved(execution) else parent_state
