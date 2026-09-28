from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReleaseArtifact:
    archive: Path
    manifest: Path


def build_release_artifact(root: Path, output_dir: Path, version: str, candidate_sha: str) -> ReleaseArtifact:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = output_dir / "stagemesh-release-manifest.json"
    archive = output_dir / f"stagemesh-{version}-{candidate_sha[:8]}.zip"
    files = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and ".git" not in path.parts
        and ".tmp-install" not in path.parts
        and "__pycache__" not in path.parts
    ]
    manifest.write_text(
        json.dumps(
            {
                "name": "stagemesh",
                "version": version,
                "candidate_sha": candidate_sha,
                "file_count": len(files),
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, path.relative_to(root).as_posix())
        zf.write(manifest, manifest.relative_to(root).as_posix() if manifest.is_relative_to(root) else manifest.name)
    return ReleaseArtifact(archive=archive, manifest=manifest)
