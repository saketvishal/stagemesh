from __future__ import annotations

import tomllib
from pathlib import Path


def test_public_distribution_metadata_keeps_compatible_cli_alias() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    project = pyproject["project"]
    scripts = project["scripts"]

    assert project["name"] == "stagemesh"
    assert project["version"] == "0.2.0a1"
    assert scripts["stagemesh"] == "build_coordinator.cli:main"
    assert scripts["build-coordinator"] == "build_coordinator.cli:main"
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "build_coordinator"
    ]
