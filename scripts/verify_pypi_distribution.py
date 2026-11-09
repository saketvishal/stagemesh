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
        if os.name == "nt":
            # Exercise a realistic deep checkout/venv path while leaving room for standard venv files.
            root = root / ("deep-project-environment-" + "x" * 72)
            root.mkdir()
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
        skills_result = _run([str(command), "skills", "--json"], root, clean_env)
        try:
            skills = json.loads(skills_result.stdout)
        except json.JSONDecodeError as exc:
            raise DistributionVerificationError("installed bundled-skill inventory did not return valid JSON") from exc
        inventory = skills.get("skills") if isinstance(skills, dict) else None
        if not isinstance(inventory, list) or len(inventory) != 5 or any(row.get("integrity") != "verified" for row in inventory):
            raise DistributionVerificationError("installed bundled skills are missing or failed integrity verification")
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

        upgrade_project = root / "legacy-project"
        upgrade_runtime = upgrade_project / ".stagemesh"
        upgrade_runtime.mkdir(parents=True)
        legacy_commands = {
            "codex": "codex exec --sandbox workspace-write",
            "claude": (
                "claude -p --permission-mode acceptEdits --permission-prompts none "
                "--allowedTools 'Edit,Write,MultiEdit,Bash(git status*),Bash(git diff*),"
                "Bash(git log*),Bash(git show*),Bash(git rev-parse*),Bash(python *),"
                "Bash(pytest *),Bash(ruff *)'"
            ),
            "grok": (
                "grok --permission-mode acceptEdits --always-approve --allow Edit --allow Write "
                "--allow MultiEdit --allow 'Bash(git status*)' --allow 'Bash(git diff*)' "
                "--allow 'Bash(git log*)' --allow 'Bash(git show*)' "
                "--allow 'Bash(git rev-parse*)' --allow 'Bash(python *)' "
                "--allow 'Bash(pytest *)' --allow 'Bash(ruff *)'"
            ),
            "agy": "agy --mode accept-edits",
        }
        legacy_config = {
            "providers": {
                **{
                    name: {"command": provider_command, "capabilities": ["IMPLEMENT", "REVIEW"]}
                    for name, provider_command in legacy_commands.items()
                },
                "custom": {"command": "custom-agent --browser-policy user-owned", "capabilities": ["REVIEW"]},
            },
            "authorization": {"unattended_providers": ["codex", "claude", "grok", "agy"]},
        }
        config_path = upgrade_runtime / "config.json"
        config_path.write_text(json.dumps(legacy_config, indent=2) + "\n", encoding="utf-8")
        upgraded = _run(
            [str(command), "--project", str(upgrade_project), "init", "--json", "--allow-incomplete"],
            upgrade_project,
            clean_env,
        )
        try:
            upgrade_report = json.loads(upgraded.stdout)
            upgraded_config = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise DistributionVerificationError("legacy-project initialization did not return valid JSON") from exc
        registry = upgrade_report.get("provider_registry")
        if not isinstance(registry, dict) or registry.get("hardened") != ["agy", "claude", "codex", "grok"]:
            raise DistributionVerificationError("installed package did not report the complete legacy provider migration")
        upgraded_providers = upgraded_config.get("providers")
        if not isinstance(upgraded_providers, dict) or any(
            upgraded_providers.get(name, {}).get("command") == old_command
            for name, old_command in legacy_commands.items()
        ):
            raise DistributionVerificationError("installed package retained an unsafe StageMesh-generated provider default")
        if upgraded_providers.get("custom") != legacy_config["providers"]["custom"]:
            raise DistributionVerificationError("legacy provider migration changed a custom provider")
        if upgraded_config.get("authorization") != legacy_config["authorization"]:
            raise DistributionVerificationError("legacy provider migration changed provider authorization")


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
