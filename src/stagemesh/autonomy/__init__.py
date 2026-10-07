"""Founder Hands-Off supervisor: deterministic, typed, evidence-backed autonomy policy around StageMesh's lifecycle.

See docs/founder-hands-off.md for the gate, capabilities, scenarios and readiness.
"""

from __future__ import annotations

from .decisions import (
    Action,
    AutonomyDecision,
    Condition,
    Escalation,
    EscalationReason,
    TaskOutcome,
    autonomy_streak,
    decision_trace,
    record_task_outcome,
)

__all__ = [
    "Action",
    "AutonomyDecision",
    "Condition",
    "Escalation",
    "EscalationReason",
    "TaskOutcome",
    "autonomy_streak",
    "decision_trace",
    "record_task_outcome",
]
