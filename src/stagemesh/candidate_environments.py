"""Explicit project-owned Python dependencies for isolated validation checkouts."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path, PurePosixPath


def _relative(raw: object) -> Path:
    if (
        not isinstance(raw, str)
        or not raw
        or "\\" in raw
        or ":" in raw
        or "\n" in raw
        or "\r" in raw
    ):
        raise ValueError("validation environment paths must be repository-relative POSIX paths")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or path.name != ".venv":
        raise ValueError(
            "validation environment paths must end in .venv and stay inside the repository"
        )
    if any(part in {".git", ".stagemesh"} for part in path.parts):
        raise ValueError("validation environments cannot target repository or runtime metadata")
    return Path(*path.parts)


def prepare_candidate_environments(project: Path, checkout: Path) -> None:
    config = project / ".stagemesh/validation-environments.json"
    if not config.exists():
        return
    data = json.loads(config.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"schema_version", "python"}:
        raise ValueError("invalid validation-environments configuration")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("unsupported validation-environments schema version")
    mapping = data["python"]
    if not isinstance(mapping, dict):
        raise TypeError("validation-environments.python must be an object")
    plans = []
    targets: set[Path] = set()
    for raw_target, raw_source in mapping.items():
        target_rel, source_rel = _relative(raw_target), _relative(raw_source)
        source = (project / source_rel).resolve()
        target = (checkout / target_rel).resolve()
        if not source.is_relative_to(project.resolve()) or not target.is_relative_to(
            checkout.resolve()
        ):
            raise ValueError("validation environment escapes its repository")
        if target in targets:
            raise ValueError("duplicate candidate environment target")
        targets.add(target)
        if target.exists():
            raise ValueError("refusing to replace an existing candidate environment")
        source_python = source / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        probe = subprocess.run(
            [
                str(source_python),
                "-I",
                "-S",
                "-c",
                "import json,sys; print(json.dumps(list(sys.version_info[:2])))",
            ],
            check=True,
            text=True,
            capture_output=True,
            timeout=30,
        )
        version = json.loads(probe.stdout)
        packages = (
            source
            / (
                "Lib/site-packages"
                if os.name == "nt"
                else f"lib/python{version[0]}.{version[1]}/site-packages"
            )
        ).resolve()
        if "\n" in str(packages) or "\r" in str(packages):
            raise ValueError("invalid dependency package path")
        if not packages.is_relative_to(source) or not packages.is_dir():
            raise ValueError(
                "validation dependency environment has an incompatible interpreter or package path"
            )
        # Dependencies may be reused only for an unchanged dependency declaration.
        for name in (
            "pyproject.toml",
            "requirements.txt",
            "requirements-dev.txt",
            "uv.lock",
            "poetry.lock",
        ):
            original, candidate = source.parent / name, checkout / source_rel.parent / name
            if original.exists() and not candidate.exists() and name in {"uv.lock", "poetry.lock"}:
                tracked = subprocess.run(
                    [
                        "git",
                        "ls-files",
                        "--error-unmatch",
                        "--",
                        (source_rel.parent / name).as_posix(),
                    ],
                    cwd=project,
                    check=False,
                    capture_output=True,
                    timeout=30,
                )
                if tracked.returncode == 1:
                    continue  # an operator-only lockfile is not a repository dependency declaration
            if original.exists() != candidate.exists() or (
                original.exists()
                and original.read_text(encoding="utf-8") != candidate.read_text(encoding="utf-8")
            ):
                raise ValueError(
                    "candidate dependencies differ; install a matching environment explicitly"
                )
        plans.append((target, packages, source_python, version))
    for target, packages, source_python, version in plans:
        # A private environment avoids junctions into the source tree. Only the
        # dependency search path is shared; source .pth files are never executed,
        # so editable installs cannot redirect imports to the operator checkout.
        subprocess.run(
            [str(source_python), "-I", "-S", "-m", "venv", "--without-pip", str(target)],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        purelib = target / (
            "Lib/site-packages"
            if os.name == "nt"
            else f"lib/python{version[0]}.{version[1]}/site-packages"
        )
        (purelib / "stagemesh_dependencies.pth").write_text(str(packages) + "\n", encoding="utf-8")
