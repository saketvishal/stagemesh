from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class QualificationGate:
    """One authoritative local release gate.

    CI, acceptance reports, and release readiness must consume this manifest.
    Keeping the commands here prevents a release path from silently qualifying a
    different product journey than the other two paths.
    """

    name: str
    command: tuple[str, ...]


def qualification_gates(include_acceptance: bool = True) -> tuple[QualificationGate, ...]:
    gates = [
        QualificationGate("compile", (sys.executable, "-m", "compileall", "-q", "src", "scripts", "build_backend.py")),
        QualificationGate("invariants", (sys.executable, "scripts/invariants.py")),
        QualificationGate("provider_acceptance", (sys.executable, "scripts/provider_acceptance.py")),
        QualificationGate("github_acceptance", (sys.executable, "scripts/github_acceptance.py")),
        QualificationGate("live_acceptance", (sys.executable, "scripts/live_acceptance.py", "--json")),
    ]
    if include_acceptance:
        gates.extend(
            (
                QualificationGate("acceptance", (sys.executable, "scripts/acceptance.py")),
                QualificationGate("project_local_acceptance", (sys.executable, "scripts/project_local_acceptance.py")),
                QualificationGate(
                    "development_journey_acceptance",
                    (sys.executable, "scripts/development_journey_acceptance.py"),
                ),
            )
        )
    return tuple(gates)


def qualification_commands(include_acceptance: bool = True) -> list[tuple[str, list[str]]]:
    return [(gate.name, list(gate.command)) for gate in qualification_gates(include_acceptance)]
