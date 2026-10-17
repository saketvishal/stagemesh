"""Explicit project-owned Python and Node dependencies for isolated validation checkouts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath


def _relative(raw: object, expected_name: str = ".venv") -> Path:
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
    if path.is_absolute() or ".." in path.parts or path.name != expected_name:
        raise ValueError(
            f"validation environment paths must end in {expected_name} and stay inside the repository"
        )
    if any(part in {".git", ".stagemesh"} for part in path.parts):
        raise ValueError("validation environments cannot target repository or runtime metadata")
    return Path(*path.parts)


def _is_link(path: Path) -> bool:
    if not os.path.lexists(path):
        return False
    return path.is_symlink() or bool(
        getattr(path.lstat(), "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _marker(source: Path, declarations: Path, names: tuple[str, ...]) -> dict[str, str]:
    digest = hashlib.sha256()
    for name in names:
        file = declarations / name
        digest.update(name.encode() + b"\0")
        digest.update(file.read_text(encoding="utf-8").encode() if file.exists() else b"<absent>")
    return {"source": str(source), "declarations": digest.hexdigest()}


def _managed(target: Path, expected: dict[str, str]) -> bool:
    marker = target / ".stagemesh-environment.json"
    try:
        return not _is_link(marker) and json.loads(marker.read_text(encoding="utf-8")) == expected
    except (OSError, ValueError):
        return False


def _write_marker(target: Path, marker: dict[str, str]) -> None:
    destination = target / ".stagemesh-environment.json"
    if _is_link(destination):
        raise ValueError("candidate environment marker must not be a link")
    fd, name = tempfile.mkstemp(prefix=".stagemesh-marker-", dir=target)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(marker, output)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _dependency_link(entry: Path, root: Path) -> Path:
    try:
        resolved = entry.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"cannot resolve dependency link: {entry}") from exc
    if not resolved.is_relative_to(root):
        raise ValueError(f"dependency link escapes its environment: {entry}")
    return resolved


def _check_node_links(target: Path) -> None:
    for directory, dirs, files in os.walk(target, followlinks=False):
        for name in [*dirs, *files]:
            entry = Path(directory) / name
            if _is_link(entry):
                _dependency_link(entry, target)
                if name in dirs:
                    dirs.remove(name)  # os.walk may otherwise follow Windows junctions


def _probe_python(python: Path) -> dict:
    result = subprocess.run(
        [str(python), "-I", "-S", "-c",
         "import json,sys; print(json.dumps({'version':list(sys.version_info[:3]),'base':sys.base_prefix}))"],
        check=True, text=True, capture_output=True, timeout=30,
    )
    return json.loads(result.stdout)


def _detach_legacy_alias(target: Path, source: Path) -> None:
    if not _is_link(target):
        return
    if target.resolve() != source:
        raise ValueError("candidate environment changed during provisioning")
    if target.is_symlink():
        target.unlink()
    else:
        target.rmdir()


def _copy_node_environment(source: Path, target: Path, marker: dict[str, str]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    # Windows readers may hold directory handles without delete sharing. Publish
    # under the workspace lease, with an exclusive directory and readiness marker
    # written last, instead of requiring a whole-directory rename.
    direct = os.name == "nt"
    if direct:
        _detach_legacy_alias(target, source)
        target.mkdir()
        temporary = target
    else:
        temporary = Path(tempfile.mkdtemp(prefix=".stagemesh-node-", dir=target.parent))
    owned_target = direct
    links: list[tuple[Path, Path, bool]] = []

    def skip_links(directory: str, names: list[str]) -> list[str]:
        skipped = []
        for name in names:
            if Path(directory) == source and name == ".stagemesh-environment.json":
                skipped.append(name)
                continue  # readiness belongs to this completed copy, never the source
            entry = Path(directory) / name
            if _is_link(entry):
                resolved = _dependency_link(entry, source)
                links.append((entry.relative_to(source), resolved.relative_to(source), resolved.is_dir()))
                skipped.append(name)
        return skipped

    try:
        shutil.copytree(source, temporary, ignore=skip_links, dirs_exist_ok=True)
        for relative, destination, is_dir in links:
            link, private = temporary / relative, temporary / destination
            if os.name == "nt" and is_dir:
                continue  # junctions store absolute paths; create them after the atomic move
            else:
                link.symlink_to(os.path.relpath(private, link.parent), target_is_directory=is_dir)
        if not direct:
            _detach_legacy_alias(target, source)
            temporary.rename(target)
            owned_target = True
        if os.name == "nt":
            import _winapi
            for relative, destination, is_dir in links:
                if is_dir:
                    _winapi.CreateJunction(str(target / destination), str(target / relative))
        _write_marker(target, marker)
    except BaseException:
        if owned_target:
            shutil.rmtree(target)
        raise
    finally:
        if temporary != target and temporary.exists():
            shutil.rmtree(temporary)


def prepare_candidate_environments(project: Path, checkout: Path) -> None:
    project, checkout = Path(project).resolve(), Path(checkout).resolve()
    config = project / ".stagemesh/validation-environments.json"
    if not config.exists():
        return
    if project == checkout:
        raise ValueError("candidate environments require an isolated checkout")
    data = json.loads(config.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("invalid validation-environments configuration")
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("unsupported validation-environments schema version")
    allowed_keys = {"schema_version", "python", "node"}
    if not (set(data) <= allowed_keys) or not (set(data) & {"python", "node"}):
        raise ValueError("invalid validation-environments configuration")

    targets: set[Path] = set()

    if "python" in data:
        mapping = data["python"]
        if not isinstance(mapping, dict):
            raise TypeError("validation-environments.python must be an object")
        plans = []
        for raw_target, raw_source in mapping.items():
            target_rel, source_rel = _relative(raw_target, ".venv"), _relative(raw_source, ".venv")
            source = (project / source_rel).resolve()
            target = checkout / target_rel
            legacy_link = _is_link(target) and target.resolve() == source
            if not source.is_relative_to(project) or not target.parent.resolve().is_relative_to(
                checkout
            ) or (not legacy_link and not target.resolve().is_relative_to(checkout)) or (_is_link(target) and not legacy_link):
                raise ValueError("validation environment escapes its repository")
            if target in targets:
                raise ValueError("duplicate candidate environment target")
            targets.add(target)
            source_python = source / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            identity = _probe_python(source_python)
            version = identity["version"]
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
            marker = _marker(source, checkout / source_rel.parent, ("pyproject.toml", "requirements.txt", "requirements-dev.txt", "uv.lock", "poetry.lock"))
            marker["interpreter"] = json.dumps(identity, sort_keys=True)
            if target.exists() and not legacy_link:
                pth = target / ("Lib/site-packages" if os.name == "nt" else f"lib/python{version[0]}.{version[1]}/site-packages") / "stagemesh_dependencies.pth"
                legacy = pth.parent.resolve().is_relative_to(target) and pth.is_file() and not _is_link(pth) and pth.read_text(encoding="utf-8") == str(packages) + "\n"
                if not legacy:
                    raise ValueError("refusing to replace an existing candidate environment")
                target_python = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                if _probe_python(target_python) != identity:
                    raise ValueError("candidate environment interpreter differs from its source")
                _write_marker(target, marker)
                continue
            plans.append((target, packages, source_python, version, marker))
        for target, packages, source_python, version, marker in plans:
            # A private environment avoids junctions into the source tree. Only the
            # dependency search path is shared; source .pth files are never executed,
            # so editable installs cannot redirect imports to the operator checkout.
            _detach_legacy_alias(target, source_python.parent.parent)
            target.mkdir(parents=True)
            try:
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
                _write_marker(target, marker)
            except BaseException:
                shutil.rmtree(target)
                raise

    if "node" in data:
        node_mapping = data["node"]
        if not isinstance(node_mapping, dict):
            raise TypeError("validation-environments.node must be an object")
        node_plans = []
        for raw_target, raw_source in node_mapping.items():
            target_rel = _relative(raw_target, "node_modules")
            source_rel = _relative(raw_source, "node_modules")
            source = (project / source_rel).resolve()
            target = checkout / target_rel
            legacy_link = _is_link(target) and target.resolve() == source
            if not source.is_relative_to(project.resolve()) or not target.parent.resolve().is_relative_to(
                checkout.resolve()
            ) or (_is_link(target) and not legacy_link):
                raise ValueError("validation environment escapes its repository")
            if target in targets:
                raise ValueError("duplicate candidate environment target")
            targets.add(target)
            if not source.is_dir():
                raise ValueError("validation node dependency environment does not exist")
            for name in ("package.json", "package-lock.json", "npm-shrinkwrap.json"):
                original, candidate = source.parent / name, checkout / target_rel.parent / name
                if original.exists() and not candidate.exists() and name in {"package-lock.json", "npm-shrinkwrap.json"}:
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
                        continue
                if original.exists() != candidate.exists() or (
                    original.exists()
                    and original.read_text(encoding="utf-8") != candidate.read_text(encoding="utf-8")
                ):
                    raise ValueError(
                        "candidate dependencies differ; install a matching environment explicitly"
                    )
            marker = _marker(source, target.parent, ("package.json", "package-lock.json", "npm-shrinkwrap.json"))
            if target.exists() and not legacy_link:
                if not _managed(target, marker):
                    raise ValueError("refusing to replace an existing candidate environment")
                _check_node_links(target)
                continue
            node_plans.append((source, target, marker))
        for source, target, marker in node_plans:
            _copy_node_environment(source, target, marker)
