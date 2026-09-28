from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class SecurityBoundaryError(ValueError):
    pass


@dataclass(frozen=True)
class WorkspaceBoundary:
    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())

    def require_inside(self, path: Path) -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise SecurityBoundaryError(f"{resolved} is outside workspace {self.root}") from exc
        return resolved
