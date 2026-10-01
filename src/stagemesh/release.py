from __future__ import annotations

import hashlib
import json
import re
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .security import WorkspaceBoundary


class ReleaseValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ReleaseArtifact:
    archive: Path
    manifest: Path
    checksums: Path


def build_release_artifact(root: Path, output_dir: Path, version: str, candidate_sha: str) -> ReleaseArtifact:
    root = root.resolve()
    version = validate_release_token(version, "version")
    candidate_sha = validate_candidate_sha(candidate_sha)
    output_dir = WorkspaceBoundary(root).require_inside(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = output_dir / "stagemesh-release-manifest.json"
    archive = output_dir / f"stagemesh-{version}-{candidate_sha[:8]}.zip"
    checksums = output_dir / "SHA256SUMS"
    files = release_files(root)
    file_entries = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        }
        for path in files
    ]
    manifest.write_text(
        json.dumps(
            {
                "name": "stagemesh",
                "version": version,
                "candidate_sha": candidate_sha,
                "file_count": len(files),
                "files": file_entries,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, path.relative_to(root).as_posix())
        zf.write(manifest, manifest.name)
    checksums.write_text(
        "\n".join(
            [
                f"{sha256_file(archive)}  {archive.name}",
                f"{sha256_file(manifest)}  {manifest.name}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return ReleaseArtifact(archive=archive, manifest=manifest, checksums=checksums)


def validate_candidate_sha(candidate_sha: str) -> str:
    if not isinstance(candidate_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{7,64}", candidate_sha):
        raise ReleaseValidationError("candidate sha must be 7-64 hexadecimal characters")
    return candidate_sha.lower()


def validate_release_token(value: str, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value):
        raise ReleaseValidationError(f"{field} must be a safe release token")
    return value


def release_files(root: Path) -> list[Path]:
    tracked = git_tracked_files(root)
    if tracked:
        return sorted(tracked)
    return sorted(
        path
        for path in root.rglob("*")
        if release_file_allowed(root, path)
    )


def git_tracked_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "-c", "safe.directory=*", "ls-files", "-z"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    files: list[Path] = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        relative = Path(raw.decode("utf-8"))
        candidate = root / relative
        if release_file_allowed(root, candidate):
            files.append(candidate)
    return files


def release_path_allowed(relative: Path) -> bool:
    blocked = {".git", ".stagemesh", ".tmp-install", "__pycache__", "dist", "build"}
    return not any(part in blocked or part.endswith(".egg-info") for part in relative.parts)


def release_file_allowed(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    if not release_path_allowed(relative):
        return False
    if path.is_symlink() or not path.is_file():
        return False
    try:
        path.resolve().relative_to(root)
    except ValueError:
        return False
    return True


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
