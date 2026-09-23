"""Autonomous Build Coordinator runner."""

from typing import Any

__all__ = ["BuildRunner", "RunnerCycleResult"]


def __getattr__(name: str) -> Any:
    if name in {"BuildRunner", "RunnerCycleResult"}:
        from build_coordinator.runner.orchestrator import BuildRunner, RunnerCycleResult

        return {"BuildRunner": BuildRunner, "RunnerCycleResult": RunnerCycleResult}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
