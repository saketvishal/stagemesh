"""Configured validation services discovery and fingerprinting."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import StageMeshConfig, load_config


class ValidationServiceError(ValueError):
    pass


@dataclass(frozen=True)
class ConfiguredService:
    name: str
    kind: str  # e.g., "browser", "api", "database", "custom"
    command: tuple[str, ...]
    cwd: str | None = None
    env_keys: tuple[str, ...] = ()
    port: int | None = None
    url: str | None = None
    script_definition: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "command": list(self.command),
            "cwd": self.cwd,
            "env_keys": list(self.env_keys),
            "port": self.port,
            "url": self.url,
            "script_definition": self.script_definition,
        }


@dataclass(frozen=True)
class ValidationServicesManifest:
    services: tuple[ConfiguredService, ...]
    fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "services": [s.to_dict() for s in self.services],
            "fingerprint": self.fingerprint,
        }


def compute_service_fingerprint(
    services: tuple[ConfiguredService, ...] | list[ConfiguredService],
    config_digest: str | None = None,
) -> str:
    """Compute a deterministic hash fingerprint across configured services and environment."""
    hasher = hashlib.sha256()
    if config_digest:
        hasher.update(config_digest.encode("utf-8"))
    
    canonical_list = []
    for s in sorted(services, key=lambda x: x.name):
        canonical_list.append(s.to_dict())
    
    data = json.dumps(canonical_list, sort_keys=True, separators=(",", ":")).encode("utf-8")
    hasher.update(data)
    return hasher.hexdigest()[:32]


def discover_validation_services(
    project: Path,
    config: StageMeshConfig | None = None,
) -> ValidationServicesManifest:
    """Discover configured validation services from project configuration and conventional files."""
    root = project.resolve()
    services: list[ConfiguredService] = []

    # Check .stagemesh/config.json or loaded config
    config_digest = ""
    config_file = root / ".stagemesh" / "config.json"
    if config_file.is_file():
        try:
            raw_cfg = config_file.read_bytes()
            config_digest = hashlib.sha256(raw_cfg).hexdigest()[:16]
            cfg_json = json.loads(raw_cfg)
            if isinstance(cfg_json, dict) and "validation_services" in cfg_json:
                declared = cfg_json["validation_services"]
                if isinstance(declared, list):
                    for item in declared:
                        if isinstance(item, dict) and "name" in item and "command" in item:
                            cmd = tuple(str(c) for c in item["command"]) if isinstance(item["command"], (list, tuple)) else (str(item["command"]),)
                            services.append(
                                ConfiguredService(
                                    name=str(item["name"]),
                                    kind=str(item.get("kind", "custom")),
                                    command=cmd,
                                    cwd=item.get("cwd"),
                                    env_keys=tuple(item.get("env_keys", ())),
                                    port=item.get("port"),
                                    url=item.get("url"),
                                )
                            )
        except Exception:
            pass

    # Discover API or backend service conventions if pyproject.toml or package.json defines them
    package_json = root / "package.json"
    if package_json.is_file():
        try:
            pkg_data = json.loads(package_json.read_bytes())
            scripts = pkg_data.get("scripts", {})
            if isinstance(scripts, dict):
                # Detect browser service / gate
                for script_name in ("test:e2e", "e2e", "e2e:ui-gate", "playwright"):
                    if script_name in scripts and not any(s.name == "browser" for s in services):
                        services.append(
                            ConfiguredService(
                                name="browser",
                                kind="browser",
                                command=("npm", "run", script_name),
                                script_definition=scripts[script_name],
                            )
                        )
                        break
                # Detect api service
                for script_name in ("test:api", "api:e2e", "e2e:api"):
                    if script_name in scripts and not any(s.name == "api" for s in services):
                        services.append(
                            ConfiguredService(
                                name="api",
                                kind="api",
                                command=("npm", "run", script_name),
                                script_definition=scripts[script_name],
                            )
                        )
                        break
        except Exception:
            pass

    # If no browser / api explicitly found but pytest is configured, check if api fixtures exist
    services_tuple = tuple(sorted(services, key=lambda s: s.name))
    fp = compute_service_fingerprint(services_tuple, config_digest=config_digest)
    return ValidationServicesManifest(services=services_tuple, fingerprint=fp)
