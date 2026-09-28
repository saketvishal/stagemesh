from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .security import WorkspaceBoundary


class DemoValidationError(ValueError):
    pass


@dataclass(frozen=True)
class DemoProject:
    root: Path
    objective: Path
    readme: Path


def create_demo_project(project: Path, output: Path) -> DemoProject:
    project = Path(project).resolve()
    output = WorkspaceBoundary(project).require_inside(Path(output).resolve())
    if output.exists() and any(output.iterdir()):
        raise DemoValidationError("demo output must be empty or not exist")
    output.mkdir(parents=True, exist_ok=True)
    runtime = output / ".stagemesh"
    runtime.mkdir(parents=True, exist_ok=True)
    objective = output / "objective.json"
    objective.write_text(
        json.dumps(
            {
                "id": "demo-objective",
                "title": "StageMesh demo objective",
                "tasks": [
                    {"id": "demo-plan", "title": "Prepare demo implementation"},
                    {
                        "id": "demo-validate",
                        "title": "Validate demo implementation",
                        "dependencies": ["demo-plan"],
                    },
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    readme = output / "README.md"
    readme.write_text(
        "\n".join(
            [
                "# StageMesh Demo Project",
                "",
                "Run this demo from the StageMesh repository root:",
                "",
                "```bash",
                f"stagemesh --project {output} init",
                f"stagemesh --project {output} plan {objective}",
                f"stagemesh --project {output} continue --once",
                f"stagemesh --project {output} status --json",
                "```",
                "",
                "The objective contains two dependent synthetic tasks and uses only local state.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return DemoProject(root=output, objective=objective, readme=readme)
