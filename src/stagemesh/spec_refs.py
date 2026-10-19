"""Framework-neutral, immutable records for read-only specification adapters."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Protocol


class SpecAdapterError(ValueError):
    """An adapter input cannot be represented without ambiguity."""


def nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SpecAdapterError(f"{label} must be a non-empty string")
    return value


def local_path(value: object) -> str:
    """Require an unambiguous project-relative path on any platform."""
    value = nonempty(value, "path").replace("\\", "/")
    if (PureWindowsPath(value).drive or value.startswith("/")
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or any(char in value for char in (":", "\x00"))):
        raise SpecAdapterError(f"unsafe project path: {value!r}")
    return PurePosixPath(value).as_posix()


def project_file(root: Path, value: str) -> Path:
    path = root / local_path(value)
    if not path.resolve().is_relative_to(root.resolve()):
        raise SpecAdapterError(f"path escapes project: {value}")
    return path


@dataclass(frozen=True)
class Requirement:
    id: str
    text: str
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class SpecOwner:
    id: str
    path: str
    status: str
    requirements: tuple[Requirement, ...] = ()
    dependencies: tuple[str, ...] = ()

    @property
    def canonical(self) -> bool:
        return self.status == "active"


@dataclass(frozen=True)
class SpecSnapshot:
    adapter: str
    adapter_version: str
    owners: tuple[SpecOwner, ...]
    contract_paths: tuple[str, ...] = ()

    def __post_init__(self):
        nonempty(self.adapter, "adapter")
        nonempty(self.adapter_version, "adapter_version")
        seen: set[str] = set()
        paths: set[str] = set()
        for path in self.contract_paths:
            local_path(path)
        for owner in self.owners:
            local_path(owner.path)
            if owner.path in paths:
                raise SpecAdapterError(f"duplicate owner path: {owner.path}")
            paths.add(owner.path)
            if not isinstance(owner.status, str) or owner.status not in {"active", "history"}:
                raise SpecAdapterError(f"unsupported owner status: {owner.status}")
            for record in (owner, *owner.requirements):
                identifier = nonempty(record.id, "id")
                if identifier in seen:
                    raise SpecAdapterError(f"duplicate id: {identifier}")
                seen.add(identifier)
                if not isinstance(record.dependencies, tuple):
                    raise SpecAdapterError("dependencies must be a tuple")
                for dependency in record.dependencies:
                    nonempty(dependency, "dependency")
            for requirement in owner.requirements:
                nonempty(requirement.text, "requirement text")


class SpecAdapter(Protocol):
    """Discover only; implementations must not rewrite specs or execute client code."""

    def discover(self, root: Path) -> SpecSnapshot: ...
