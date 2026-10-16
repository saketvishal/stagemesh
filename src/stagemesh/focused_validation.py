"""Bounded conventional test discovery; never replaces a declared gate."""

from __future__ import annotations

import json
import re
import shlex
import tomllib
from collections import defaultdict
from dataclasses import replace
from pathlib import Path, PurePosixPath

from .contracts import ChangeContract, ContractError, GateCommand
from .git import GitError, GitWorkspace

MAX_PATHS = 64
MAX_METADATA_BYTES = 65536
MAX_PROBES = 256
MARKERS = ("pytest.ini", "pyproject.toml", "setup.cfg", "package.json")
JS_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx"}
IGNORED_PARTS = {".git", ".stagemesh", "node_modules", ".venv", "__pycache__"}


def _relative(value: str) -> PurePosixPath | None:
    path = PurePosixPath(value.replace("\\", "/"))
    if (path.is_absolute() or not path.parts or any(p in {"..", "."} or ":" in p for p in path.parts)
            or any(p in IGNORED_PARTS for p in path.parts) or any(c in value for c in "*?[]\n\r\0")):
        return None
    return path


def _files(project: Path, paths: set[str], sha: str | None) -> dict[str, str]:
    if not paths or len(paths) > MAX_PROBES or sum(len(path) + 3 for path in paths) > 24000:
        return {}
    if sha:
        result = GitWorkspace(project).run("ls-tree", "--full-tree", "-z", sha, "--", *sorted(paths), encoding="utf-8")
        files = {}
        for entry in result.stdout.split("\0"):
            if not entry:
                continue
            metadata, name = entry.split("\t", 1)
            mode, kind, oid = metadata.split()
            if mode in {"100644", "100755"} and kind == "blob":
                files[name] = oid
        return files
    root = project.resolve()
    return {name: "" for name in paths if (root / name).is_file()
            and (root / name).resolve().is_relative_to(root)
            and not any((root / parent).is_symlink() for parent in PurePosixPath(name).parents)
            and not (root / name).is_symlink()}


def _read(project: Path, name: str, oid: str, sha: str | None) -> str:
    if sha:
        git = GitWorkspace(project)
        if int(git.run("cat-file", "-s", oid).stdout) > MAX_METADATA_BYTES:
            return ""
        return git.run("cat-file", "blob", oid, encoding="utf-8").stdout
    path = project / name
    return path.read_text(encoding="utf-8") if path.stat().st_size <= MAX_METADATA_BYTES else ""


def _pytest_runner(contract: ChangeContract, cwd: str) -> tuple[list[str], GateCommand | None]:
    for gate in contract.gates:
        if (gate.cwd or ".") != cwd:
            continue
        for index, arg in enumerate(gate.command):
            if arg == "-m" and list(gate.command[index + 1:index + 2]) == ["pytest"]:
                return list(gate.command[:index + 2]), gate
            if Path(arg.replace("\\", "/")).name in {"pytest", "pytest.exe"}:
                return list(gate.command[:index + 1]), gate
    return ["python", "-m", "pytest"], None


def _declares_pytest(text: str) -> bool:
    if not text:
        return False
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return False
    project = data.get("project", {})
    dependencies = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        dependencies.extend(group)
    return "pytest" in data.get("tool", {}) or any(
        isinstance(dep, str) and re.match(r"^pytest(?:$|[\s\[<>=!~;])", dep, re.IGNORECASE)
        for dep in dependencies
    )


def discover_focused_gates(
    project: Path, paths: tuple[str, ...], contract: ChangeContract, *, candidate_sha: str | None = None,
) -> tuple[GateCommand, ...]:
    """Probe exact nearby filenames, not repository trees. Candidate probes use Git blobs, never dirty HEAD."""
    if len(paths) > MAX_PATHS:
        return ()  # Broad/unknown work retains the declared validation policy.
    sources = [p for raw in paths if (p := _relative(raw)) is not None and p.suffix in {".py", *JS_EXTENSIONS}]
    ancestors = {parent for path in sources for parent in path.parents if len(path.parts) - len(parent.parts) <= 12}
    metadata_paths = {str(parent / marker) for parent in ancestors for marker in MARKERS}
    try:
        metadata = {name: _read(project, name, oid, candidate_sha)
                    for name, oid in _files(project, metadata_paths, candidate_sha).items()}
        groups: dict[tuple[str, str], set[str]] = defaultdict(set)
        for source in sources:
            if source.suffix in JS_EXTENSIONS and any(part in {"e2e", "playwright", "cypress"} for part in source.parts):
                continue
            for root in source.parents:
                cwd = str(root)
                if source.suffix == ".py":
                    configured = (str(root / "pytest.ini") in metadata
                                  or _declares_pytest(metadata.get(str(root / "pyproject.toml"), ""))
                                  or "[tool:pytest]" in metadata.get(str(root / "setup.cfg"), ""))
                    if not configured:
                        if any(str(root / marker) in metadata for marker in ("pyproject.toml", "setup.cfg")):
                            break
                        continue
                    engine = "pytest"
                    suffixes = (f"test_{source.stem}.py", f"{source.stem}_test.py")
                else:
                    if str(root / "package.json") not in metadata:
                        continue
                    try:
                        script = json.loads(metadata.get(str(root / "package.json"), "{}")).get("scripts", {}).get("test", "")
                        words = shlex.split(script)
                    except (ValueError, AttributeError, TypeError):
                        break
                    # Unknown wrappers/options may change cwd or selection semantics. Keep existing gates instead.
                    if words not in (["vitest"], ["vitest", "run"], ["jest"]):
                        break
                    engine = "jest" if words == ["jest"] else "vitest"
                    suffixes = (f"{source.stem}.test{source.suffix}", f"{source.stem}.spec{source.suffix}")
                targets = groups[(cwd, engine)]
                relative = source.relative_to(root)
                if source.name.startswith("test_") or source.name.endswith("_test.py") or any(
                    token in source.name for token in (".test.", ".spec.")
                ):
                    targets.add(str(source))
                else:
                    tail = relative.parent
                    if tail.parts and tail.parts[0] in {"src", "app"}:
                        tail = PurePosixPath(*tail.parts[1:])
                    for directory in {source.parent, root / "tests", root / "tests/unit",
                                      root / "tests" / tail, root / "tests/unit" / tail, source.parent / "__tests__"}:
                        targets.update(str(directory / name) for name in suffixes)
                break  # Never associate a source with a different package's test runner.
        present = _files(project, {target for targets in groups.values() for target in targets}, candidate_sha)
        gates = []
        for (cwd, engine), targets in sorted(groups.items()):
            selected = sorted(str(PurePosixPath(target).relative_to(cwd)) for target in targets if target in present)
            if not selected:
                continue
            template = None
            if engine == "pytest":
                command, template = _pytest_runner(contract, cwd)
                command += ["-q", *selected]
            else:
                flags = ["--runInBand", "--runTestsByPath"] if engine == "jest" else ["--run"]
                command = ["npm", "test", "--", *flags, *selected]
                template = next((g for g in contract.gates if (g.cwd or ".") == cwd and "test" in g.command
                                 and Path(g.command[0]).stem in {"npm", "npm.cmd"}), None)
            gates.append(GateCommand(f"auto-focused:{engine}:{cwd}", command,
                                     timeout_seconds=template.timeout_seconds if template else 180,
                                     cwd=None if cwd == "." else cwd, env=template.env if template else ()))
        return tuple(gates)
    except (OSError, UnicodeError, GitError, ValueError, TypeError, AttributeError):
        return ()  # Discovery is optional; a failure never removes or passes a required gate.


def with_focused_gates(contract: ChangeContract, discovered: tuple[GateCommand, ...]) -> ChangeContract:
    """Keep required gates intact and avoid repeating already-declared exact test targets."""
    additions = []
    for gate in discovered:
        existing = [g for g in contract.gates if (g.cwd or ".") == (gate.cwd or ".")]
        named = next((g for g in contract.gates if g.name == gate.name), None)
        if named is not None:
            if named != gate:
                raise ContractError(f"declared gate conflicts with discovered focused check: {gate.name}")
            continue
        if any(_covers_suite(g, gate) for g in existing):
            continue
        targets = [arg for arg in gate.command if arg.endswith((".py", ".js", ".jsx", ".ts", ".tsx"))]
        if targets and all(any(target in _plain_pytest_targets(g.command) for g in existing) for target in targets):
            continue
        additions.append(gate)
    return replace(contract, required_tests=contract.required_tests + tuple(additions))


def _covers_suite(existing: GateCommand, focused: GateCommand) -> bool:
    """Recognize plain suite commands only; never infer coverage through shells or wrappers."""
    command = list(existing.command)
    if command and Path(command[0]).stem in {"npm", "npm.cmd"} and command[1:] in (["test"], ["run", "test"]):
        return focused.name.startswith(("auto-focused:jest:", "auto-focused:vitest:"))
    args = _pytest_arguments(command)
    if args is None:
        return False
    # Filters, custom options, and subdirectories need explicit target matching, not optimistic deduplication.
    targets = [arg for arg in focused.command if arg.endswith(".py")]
    return (focused.name.startswith("auto-focused:pytest:")
            and all(arg in {"-q", "-v", ".", "tests", "tests/"} for arg in args)
            and ("." in args or any(arg in {"tests", "tests/"} for arg in args)
                 and all(target.startswith("tests/") for target in targets)))


def _pytest_arguments(command: list[str]) -> list[str] | None:
    command = list(command)
    if "-m" in command:
        index = command.index("-m")
        if command[index + 1:index + 2] == ["pytest"]:
            return command[index + 2:]
    elif command and Path(command[0].replace("\\", "/")).stem == "pytest":
        return command[1:]
    return None


def _plain_pytest_targets(command: list[str]) -> list[str]:
    args = _pytest_arguments(command)
    if args is None or any(arg not in {"-q", "-v"} and not arg.endswith(".py") for arg in args):
        return []
    return [arg for arg in args if arg.endswith(".py")]


def focused_test_prompt(project: Path | None, contract: ChangeContract | None) -> str:
    if project is None or contract is None:
        return ""
    gates = discover_focused_gates(project, contract.allowed_files, contract)
    if not gates:
        return ""
    lines = ["\nRelevant local checks (conventional filename matches, not proof of complete coverage):"]
    for gate in gates:
        lines.append(f"- cwd {gate.cwd or '.'}: {shlex.join(gate.command)}")
    lines.append("Use affected caller tests as needed. Do not repeat unchanged passing checks. Required contract gates remain mandatory.")
    return "\n".join(lines) + "\n"
