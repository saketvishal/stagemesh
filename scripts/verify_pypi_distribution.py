"""Fail-closed verification for the exact StageMesh wheel sent to PyPI."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import venv
import zipfile
from email.parser import Parser
from pathlib import Path, PurePosixPath

NAME = "stagemesh"
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
COMMIT_RE = re.compile(r"SOURCE_COMMIT = ['\"]([0-9a-f]{40}|[0-9a-f]{64})['\"]")


class DistributionVerificationError(ValueError):
    pass


def inspect_wheel(wheel: Path, expected_tag: str) -> str:
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise DistributionVerificationError("release input must be one wheel file")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        _safe_members(names)
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        entry_names = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
        if len(metadata_names) != 1 or len(entry_names) != 1:
            raise DistributionVerificationError("wheel must contain exactly one metadata and entry-point file")
        metadata_text = archive.read(metadata_names[0]).decode("utf-8")
        headers = Parser().parsestr(metadata_text)
        version = headers.get("Version", "")
        if headers.get("Name", "").casefold() != NAME or VERSION_RE.fullmatch(version) is None:
            raise DistributionVerificationError("wheel name or stable semantic version is invalid")
        if expected_tag != f"v{version}":
            raise DistributionVerificationError(f"release tag {expected_tag!r} does not match wheel version {version!r}")
        if headers.get("Requires-Python") != ">=3.11":
            raise DistributionVerificationError("wheel must retain the supported Python floor")
        if headers.get("Description-Content-Type") != "text/markdown" or "# StageMesh vNext" not in metadata_text:
            raise DistributionVerificationError("wheel metadata must include the Markdown project description")
        license_names = [name for name in names if name.endswith(".dist-info/LICENSE")]
        if len(license_names) != 1 or "MIT License" not in archive.read(license_names[0]).decode("utf-8"):
            raise DistributionVerificationError("wheel must include the project license")
        entries = archive.read(entry_names[0]).decode("utf-8")
        if "stagemesh=stagemesh.cli:main" not in entries.replace(" ", ""):
            raise DistributionVerificationError("wheel does not install the stagemesh console command")
        if "stagemesh/_build_info.py" not in names:
            raise DistributionVerificationError("wheel is missing build provenance")
        build_info = archive.read("stagemesh/_build_info.py").decode("utf-8")
        if COMMIT_RE.search(build_info) is None or "SOURCE_DIRTY = False" not in build_info:
            raise DistributionVerificationError("wheel provenance must identify a clean Git commit")
    return version


def verify_install(wheel: Path, version: str) -> None:
    with tempfile.TemporaryDirectory(prefix="stagemesh-pypi-verify-") as raw:
        root = Path(raw)
        environment = root / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        command = environment / ("Scripts/stagemesh.exe" if os.name == "nt" else "bin/stagemesh")
        clean_env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONHOME", "PYTHONPATH"} and not key.startswith("STAGEMESH_")
        }
        _run([str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel.resolve())], root, clean_env)
        version_result = _run([str(command), "--version"], root, clean_env)
        if version not in version_result.stdout:
            raise DistributionVerificationError("installed console command reports the wrong version")
        project = root / "new-project"
        project.mkdir()
        initialized = _run(
            [str(command), "--project", str(project), "init", "--json", "--allow-incomplete"],
            project,
            clean_env,
        )
        try:
            report = json.loads(initialized.stdout)
        except json.JSONDecodeError as exc:
            raise DistributionVerificationError("installed stagemesh init did not return valid JSON") from exc
        if report.get("project") != str(project.resolve()):
            raise DistributionVerificationError("installed stagemesh did not initialize the requested new project")
        providers = report.get("provider_registry")
        if not isinstance(providers, dict) or providers.get("provider_calls") != 0:
            raise DistributionVerificationError("new-project initialization must make zero provider calls")
        if not (project / ".stagemesh/config.json").is_file() or not (project / ".stagemesh/profile.json").is_file():
            raise DistributionVerificationError("new-project initialization did not create project-local configuration")
        readiness = report.get("readiness")
        if not isinstance(readiness, dict) or readiness.get("status") != "needs_attention":
            raise DistributionVerificationError("empty-project setup must report its readiness blockers truthfully")


def verify_distribution(directory: Path, expected_tag: str, *, install: bool = True) -> Path:
    wheels = sorted(directory.glob("*.whl"))
    unexpected = [path for path in directory.iterdir() if path.is_file() and path.suffix != ".whl"]
    if len(wheels) != 1 or unexpected:
        raise DistributionVerificationError("distribution directory must contain exactly one wheel")
    version = inspect_wheel(wheels[0], expected_tag)
    if install:
        verify_install(wheels[0], version)
    return wheels[0]


def _safe_members(names: list[str]) -> None:
    if not names or len(names) != len(set(names)):
        raise DistributionVerificationError("wheel members must be non-empty and unique")
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name:
            raise DistributionVerificationError("wheel contains an unsafe member path")
        if any(part in {".git", "__pycache__", "tests"} for part in path.parts):
            raise DistributionVerificationError("wheel contains source-control, cache, or test files")


def _run(command: list[str], cwd: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            text=True,
            capture_output=True,
            timeout=180,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DistributionVerificationError(f"distribution acceptance command failed: {command[0]}") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--inspect-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        wheel = verify_distribution(args.dist.resolve(), args.tag, install=not args.inspect_only)
    except (OSError, DistributionVerificationError, zipfile.BadZipFile) as exc:
        print(f"distribution verification failed: {exc}", file=sys.stderr)
        return 2
    print(f"verified PyPI distribution: {wheel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
