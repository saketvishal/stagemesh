"""Configured validation services discovery and fingerprinting."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import StageMeshConfig
from .contracts import BoundChangeContract, GateCommand, candidate_workspace, run_gate
from .e2e_evidence import MAX_ARTIFACTS, MAX_ARTIFACT_SIZE, build_artifact_reference, build_e2e_evidence
from .remediation import bounded_failure_text


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
    timeout_seconds: int = 120
    artifacts: tuple[str, ...] = ()

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
            "timeout_seconds": self.timeout_seconds,
            "artifacts": list(self.artifacts),
        }


@dataclass(frozen=True)
class ValidationServicesManifest:
    services: tuple[ConfiguredService, ...]
    fingerprint: str
    conventional_services: tuple[str, ...] = ()  # discovery origin; explicit declarations remain required even for docs-only work

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
    *, config_project: Path | None = None,
) -> ValidationServicesManifest:
    """Discover configured validation services from project configuration and conventional files."""
    root = project.resolve()
    services: list[ConfiguredService] = []
    conventional_services: list[str] = []

    # Check .stagemesh/config.json or loaded config
    config_digest = ""
    config_file = (config_project or root) / ".stagemesh" / "config.json"
    if config_file.is_file():
        try:
            raw_cfg = config_file.read_bytes()
            config_digest = hashlib.sha256(raw_cfg).hexdigest()[:16]
            cfg_json = json.loads(raw_cfg)
            declared = cfg_json.get("validation_services", [])
            if not isinstance(declared, list) or len(declared) > 16:
                raise ValidationServiceError("validation_services must be a list of at most 16 services")
            for item in declared:
                if not isinstance(item, dict):
                    raise ValidationServiceError("validation service must be an object")
                service = ConfiguredService(
                    name=item.get("name"), kind=item.get("kind", "custom"),
                    command=tuple(item["command"]) if isinstance(item.get("command"), list) else (),
                    cwd=item.get("cwd"), env_keys=tuple(item.get("env_keys", ())),
                    port=item.get("port"), url=item.get("url"),
                    timeout_seconds=item.get("timeout_seconds", 120),
                    artifacts=tuple(item.get("artifacts", ())),
                )
                _validate_service(service)
                if any(s.name == service.name for s in services):
                    raise ValidationServiceError("duplicate validation service name")
                services.append(service)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise ValidationServiceError("invalid validation service configuration: " + str(exc)) from exc

    # Discover API or backend service conventions if pyproject.toml or package.json defines them
    package_json = root / "package.json"
    if package_json.is_file():
        try:
            pkg_data = json.loads(package_json.read_bytes())
            if not isinstance(pkg_data, dict):
                raise ValidationServiceError("package.json root must be an object")
            scripts = pkg_data.get("scripts", {})
            if isinstance(scripts, dict):
                # Detect browser service / gate
                for script_name in ("test:e2e", "e2e", "e2e:ui-gate", "playwright"):
                    if script_name in scripts and not any(s.name == "browser" for s in services):
                        conventional_services.append("browser")
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
                        conventional_services.append("api")
                        services.append(
                            ConfiguredService(
                                name="api",
                                kind="api",
                                command=("npm", "run", script_name),
                                script_definition=scripts[script_name],
                            )
                        )
                        break
        except (OSError, ValueError, UnicodeError) as exc:
            raise ValidationServiceError("invalid package.json service discovery") from exc

    # Explicit npm commands also depend on the selected package script definition.
    from dataclasses import replace
    for index, service in enumerate(services):
        _validate_service(service)
        script_name = _npm_script_name(service.command)
        if script_name is None:
            continue
        package = _inside(root, service.cwd or ".") / "package.json"
        if not package.is_file():
            raise ValidationServiceError("npm script service has no package.json")
        try:
            pkg_data = json.loads(package.read_bytes())
            if not isinstance(pkg_data, dict):
                raise ValidationServiceError("package.json root must be an object")
            scripts = pkg_data.get("scripts", {})
            script = scripts.get(script_name) if isinstance(scripts, dict) else None
        except (OSError, ValueError, UnicodeError) as exc:
            raise ValidationServiceError("invalid service package metadata") from exc
        if not isinstance(script, str) or not script:
            raise ValidationServiceError(f"npm script {script_name} is not defined")
        services[index] = replace(service, script_definition=script)
    environment = {key: os.environ.get(key) for s in services for key in s.env_keys}
    config_digest += hashlib.sha256(json.dumps(environment, sort_keys=True).encode()).hexdigest()
    services_tuple = tuple(sorted(services, key=lambda s: s.name))
    fp = compute_service_fingerprint(services_tuple, config_digest=config_digest)
    return ValidationServicesManifest(services=services_tuple, fingerprint=fp, conventional_services=tuple(conventional_services))


def _npm_script_name(command: tuple[str, ...]) -> str | None:
    """The script token in `npm run <script> [-- extra]`, not only a three-item command."""
    if len(command) < 3 or command[0] != "npm" or command[1] != "run":
        return None
    for token in command[2:]:
        if token == "--":
            break
        if token and not token.startswith("-"):
            return token
    return None


def _inside(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or path.drive or ".." in path.parts or ":" in relative:
        raise ValidationServiceError("service path must stay inside the candidate checkout")
    target = (root / path).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValidationServiceError("service path escapes the candidate checkout")
    return target


def _validate_service(service: ConfiguredService) -> None:
    if not isinstance(service.name, str) or not re.fullmatch(r"[\w.-]{1,100}", service.name):
        raise ValidationServiceError("invalid validation service name")
    if not service.command or any(not isinstance(p, str) or not p or "\0" in p for p in service.command):
        raise ValidationServiceError("service command must be a nonempty argument list")
    if not isinstance(service.kind, str):
        raise ValidationServiceError("service kind must be a string")
    if type(service.timeout_seconds) is not int or not 1 <= service.timeout_seconds <= 3600:
        raise ValidationServiceError("service timeout_seconds must be between 1 and 3600")
    if service.cwd is not None:
        if not isinstance(service.cwd, str):
            raise ValidationServiceError("service cwd must be a relative path")
        _inside(Path.cwd(), service.cwd)
    if any(not isinstance(k, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) for k in service.env_keys):
        raise ValidationServiceError("service env_keys must contain environment variable names")
    if len(service.artifacts) > MAX_ARTIFACTS:
        raise ValidationServiceError("too many configured artifacts")
    for artifact in service.artifacts:
        if not isinstance(artifact, str) or not artifact:
            raise ValidationServiceError("artifact must be a relative file path")
        _inside(Path.cwd(), artifact)
        if any(p.lower() in {".git", ".stagemesh"} or p.lower().startswith(".env")
               or p.lower().endswith((".pem", ".key")) for p in Path(artifact).parts):
            raise ValidationServiceError("artifact path refers to protected material")


def run_configured_evaluations(
    project: Path, bound: BoundChangeContract, manifest: ValidationServicesManifest,
    artifact_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Run declared browser/API commands once, outside implementation, on the exact candidate.

    The coordinator owns retries and task-stage budgets. This runner never installs tools,
    starts inferred services, retries failures, or substitutes for independent review.
    """
    from .diagnosis import tool_failure_evidence

    evaluations: list[dict[str, Any]] = []
    gates: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    selected = [s for s in manifest.services if s.kind in {"browser", "api"}]
    if not selected:
        return evaluations, gates, findings
    with candidate_workspace(project, bound.candidate_sha) as checkout:
        if discover_validation_services(checkout, config_project=project).fingerprint != manifest.fingerprint:
            raise ValidationServiceError("service configuration changed from the candidate")
        for index, service in enumerate(selected):
            name = "e2e:" + service.name
            run_dir = _inside(checkout, service.cwd or ".")
            missing_env = [key for key in service.env_keys if not os.environ.get(key)]
            if missing_env or not run_dir.is_dir():
                raise ValidationServiceError("service prerequisites unavailable: " + name)
            sources = [_inside(run_dir, path) for path in service.artifacts]
            before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in sources if p.is_file()}
            result = run_gate(checkout, GateCommand(
                name, list(service.command), timeout_seconds=service.timeout_seconds, cwd=service.cwd,
            ))
            secrets = tuple(os.environ.get(key, "") for key in service.env_keys)
            stdout = bounded_failure_text(result.stdout, secrets=secrets)
            stderr = bounded_failure_text(result.stderr, secrets=secrets)
            diagnostic = tool_failure_evidence(list(result.command), service.cwd, result.returncode,
                                               stdout, stderr, timed_out=result.failure_kind == "timeout")
            failure_kind = result.failure_kind
            if result.status != "PASSED" and failure_kind is None:
                kind = diagnostic.get("kind") if diagnostic else None
                failure_kind = "environment" if kind in {"browser_startup", "service_unavailable", "missing_tool", "permission"} else "assertion"
            command = [bounded_failure_text(p, 500, secrets) for p in result.command]
            references = []
            artifact_errors = []
            for artifact_index, source in enumerate(sources):
                try:
                    # Resolve again after the command: it may have created a symlink.
                    source = _inside(run_dir, service.artifacts[artifact_index])
                    stat = source.stat()
                    if not source.is_file() or stat.st_size > MAX_ARTIFACT_SIZE:
                        raise ValidationServiceError("artifact is not a bounded regular file")
                    if before.get(source) == (stat.st_mtime_ns, stat.st_size):
                        raise ValidationServiceError("configured artifact was not refreshed")
                    target = artifact_root / str(index) / (str(artifact_index) + source.suffix)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source.open("rb") as reader, target.open("xb") as writer:
                        remaining = MAX_ARTIFACT_SIZE + 1
                        while remaining:
                            chunk = reader.read(min(65536, remaining))
                            if not chunk:
                                break
                            writer.write(chunk)
                            remaining -= len(chunk)
                        if remaining == 0:
                            raise ValidationServiceError("artifact exceeded size limit during capture")
                    references.append(build_artifact_reference(source.name, target, artifact_root))
                except (OSError, ValueError) as exc:
                    artifact_errors.append(bounded_failure_text(str(exc), 500, secrets))
            status = "PASSED" if result.status == "PASSED" and not artifact_errors else "FAILED"
            if artifact_errors:
                stderr = bounded_failure_text(stderr + "\nartifact collection failed: " + "; ".join(artifact_errors))
                failure_kind = failure_kind or "environment"
            gate = {"name": name, "status": status, "command": command, "returncode": result.returncode,
                    "failure_kind": failure_kind, "stdout": stdout, "stderr": stderr}
            gates.append(gate)
            if status != "PASSED":
                findings.append({"severity": "error", "code": "gate_failed", "message": name + " failed" + (" (artifact collection)" if artifact_errors else ""),
                                 **{key: gate[key] for key in ("command", "returncode", "failure_kind", "stdout", "stderr")}})
            evidence = build_e2e_evidence(
                bound, candidate_sha=bound.candidate_sha, service_fingerprint=manifest.fingerprint,
                kind=service.kind, command=service.command, result="PASS" if status == "PASSED" else "FAIL",
                artifacts=references, artifact_root=artifact_root,
                metadata={"gate": name, "failure_kind": failure_kind},
            ).to_dict()
            # Executed identity stays bound by the service fingerprint; persist no command credentials.
            evidence["command"] = command
            evaluations.append(evidence)
    if discover_validation_services(project).fingerprint != manifest.fingerprint:
        raise ValidationServiceError("service configuration changed during evaluation")
    return evaluations, gates, findings
