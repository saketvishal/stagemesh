"""User-level (per machine, per user) StageMesh state: home directory and the
agent runtimes that `stagemesh agent setup` verified.

Only discovery metadata lives here. No credentials are read or stored: agent
CLIs keep their own sessions, and readiness is established by asking them to do
real headless work."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from build_coordinator.agents.profiles import DISABLED, PROFILES, READY, RuntimeStatus, discover_runtimes

HOME_ENV = "STAGEMESH_HOME"
STALE_AFTER_SECONDS = 7 * 24 * 3600


def home() -> Path:
    configured = os.getenv(HOME_ENV)
    return Path(configured).expanduser() if configured else Path.home() / ".build-coordinator"


def agents_path() -> Path:
    return home() / "agents.json"


def load_agents() -> dict[str, Any]:
    path = agents_path()
    if not path.is_file():
        return {"runtimes": {}, "disabled": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {"runtimes": {}, "disabled": []}
    data.setdefault("runtimes", {})
    data.setdefault("disabled", [])
    return data


def save_agents(data: dict[str, Any]) -> None:
    path = agents_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def setup_agents(*, live: bool = True, only: list[str] | None = None) -> list[RuntimeStatus]:
    """Probe runtimes for real and remember the outcome."""
    statuses = discover_runtimes(live=live, only=only)
    data = load_agents()
    for status in statuses:
        data["runtimes"][status.runtime_id] = status.as_dict()
    save_agents(data)
    return statuses


def set_enabled(runtime_id: str, enabled: bool) -> None:
    if runtime_id not in PROFILES:
        raise ValueError(f"unknown runtime {runtime_id!r}; known: {', '.join(PROFILES)}")
    data = load_agents()
    disabled = set(data["disabled"])
    (disabled.discard if enabled else disabled.add)(runtime_id)
    data["disabled"] = sorted(disabled)
    save_agents(data)


def known_statuses() -> list[dict[str, Any]]:
    """Saved setup results, with disabled runtimes and staleness marked."""
    data = load_agents()
    rows = []
    for runtime_id, row in data["runtimes"].items():
        row = dict(row)
        if runtime_id in data["disabled"]:
            row["state"] = DISABLED
        row["age_seconds"] = int(time.time() - row.get("checked_at", 0))
        rows.append(row)
    return rows


def ready_runtime_ids() -> list[str]:
    return [
        row["runtime_id"]
        for row in known_statuses()
        if row["state"] == READY and row["age_seconds"] < STALE_AFTER_SECONDS and row["runtime_id"] in PROFILES
    ]


def worker_command(runtime_id: str) -> list[str]:
    return [sys.executable, "-m", "build_coordinator.agents.wrapper", "--runtime", runtime_id]


def runtime_template(runtime_id: str, main_ref: str, *, timeout_seconds: int = 3600, model: str | None = None) -> dict[str, Any]:
    profile = PROFILES[runtime_id]
    return {
        "name": runtime_id,
        "provider": profile.provider,
        "runtime": runtime_id,
        "adapter": "subprocess",
        "command": worker_command(runtime_id),
        "capabilities": list(profile.capabilities),
        "timeout_seconds": timeout_seconds,
        "model": model,
        "env": {"STAGEMESH_MAIN_REF": main_ref, "STAGEMESH_AGENT_TIMEOUT": str(timeout_seconds - 300)},
    }
