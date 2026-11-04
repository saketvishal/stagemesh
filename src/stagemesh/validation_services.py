"""Configured validation services discovery and fingerprinting."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from .config import StageMeshConfig
from .contracts import BoundChangeContract, GateCommand, candidate_workspace, run_gate
from .e2e_evidence import (
    MAX_ARTIFACT_SIZE,
    MAX_ARTIFACTS,
    build_artifact_reference,
    build_e2e_evidence,
)
from .remediation import bounded_failure_text

MAX_EVALUATION_REQUIREMENTS = 32
MAX_EVALUATION_RESULT_BYTES = 64 * 1024


class ValidationServiceError(ValueError):
    pass


@dataclass(frozen=True)
class EvaluationRequirement:
    """One declared browser state/viewport or API assertion the evaluator must report."""

    id: str
    kind: str
    target: str
    state: str | None = None
    viewport: str | None = None
    method: str | None = None
    expected_status: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "target": self.target,
            "state": self.state,
            "viewport": self.viewport,
            "method": self.method,
            "expected_status": self.expected_status,
        }


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
    evaluator: str | None = None
    require_independent: bool = False
    requirements: tuple[EvaluationRequirement, ...] = ()
    result_manifest: str | None = None
    startup_command: tuple[str, ...] = ()
    health_command: tuple[str, ...] = ()
    startup_timeout_seconds: int = 30

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
            "evaluator": self.evaluator,
            "require_independent": self.require_independent,
            "requirements": [requirement.to_dict() for requirement in self.requirements],
            "result_manifest": self.result_manifest,
            "startup_command": list(self.startup_command),
            "health_command": list(self.health_command),
            "startup_timeout_seconds": self.startup_timeout_seconds,
        }

    @property
    def policy_fingerprint(self) -> str:
        policy = {
            "name": self.name,
            "kind": self.kind,
            "evaluator": self.evaluator,
            "require_independent": self.require_independent,
            "requirements": [requirement.to_dict() for requirement in self.requirements],
            "result_manifest": self.result_manifest,
        }
        return hashlib.sha256(
            json.dumps(policy, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


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
                    evaluator=item.get("evaluator"),
                    require_independent=item.get("require_independent", False),
                    requirements=_parse_requirements(item.get("requirements", ())),
                    result_manifest=item.get("result_manifest"),
                    startup_command=_parse_optional_command(
                        item.get("startup_command", ()), "startup_command"
                    ),
                    health_command=_parse_optional_command(
                        item.get("health_command", ()), "health_command"
                    ),
                    startup_timeout_seconds=item.get("startup_timeout_seconds", 30),
                )
                _validate_service(service)
                if any(s.name == service.name for s in services):
                    raise ValidationServiceError("duplicate validation service name")
                services.append(service)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise ValidationServiceError("invalid validation service configuration: " + str(exc)) from exc

    # Discover API or backend service conventions if pyproject.toml or package.json defines them
    package_json = _inside(root, "package.json")
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

    # Explicit npm commands also depend on package metadata, including lifecycle hooks.
    from dataclasses import replace
    for index, service in enumerate(services):
        _validate_service(service)
        script_name = _npm_script_name(service.command)
        if script_name is None:
            continue
        package = _inside(root, (Path(service.cwd or ".") / "package.json").as_posix())
        if not package.is_file():
            raise ValidationServiceError("npm script service has no package.json")
        try:
            package_bytes = package.read_bytes()
            pkg_data = json.loads(package_bytes)
            if not isinstance(pkg_data, dict):
                raise ValidationServiceError("package.json root must be an object")
            scripts = pkg_data.get("scripts", {})
            script = scripts.get(script_name) if isinstance(scripts, dict) else None
        except (OSError, ValueError, UnicodeError) as exc:
            raise ValidationServiceError("invalid service package metadata") from exc
        if not isinstance(script, str) or not script:
            raise ValidationServiceError(f"npm script {script_name} is not defined")
        services[index] = replace(service, script_definition=script)
        # npm may execute pre/post hooks and read other package settings. Bind the
        # whole selected metadata file, not only the named script's body. Keep raw
        # metadata (which may contain credentials) out of the persisted manifest.
        package_identity = json.dumps(pkg_data, sort_keys=True, separators=(",", ":")).encode("utf-8")
        config_digest += hashlib.sha256(package_identity).hexdigest()
        config_digest += _npm_configuration_digest(root, _inside(root, service.cwd or "."))
    environment = {key: os.environ.get(key) for s in services for key in s.env_keys}
    config_digest += hashlib.sha256(json.dumps(environment, sort_keys=True).encode()).hexdigest()
    services_tuple = tuple(sorted(services, key=lambda s: s.name))
    fp = compute_service_fingerprint(services_tuple, config_digest=config_digest)
    return ValidationServicesManifest(services=services_tuple, fingerprint=fp, conventional_services=tuple(conventional_services))


def _npm_script_name(command: tuple[str, ...]) -> str | None:
    """Identify supported npm script calls without guessing a selected package."""
    if not command or PureWindowsPath(command[0]).name.casefold() not in {
        "npm", "npm.cmd", "npm.exe", "npm.bat",
    }:
        return None
    # Tokens following -- belong to the script, not npm's package selection.
    end = command.index("--") if "--" in command else len(command)
    tokens = command[1:end]
    positional = [token for token in tokens if not token.startswith("-")]
    run_verbs = {"run", "run-script", "rum", "urn"}
    lifecycle = {"test": "test", "t": "test", "tst": "test", "start": "start", "stop": "stop", "restart": "restart"}
    verbs = run_verbs | lifecycle.keys()
    if not any(token in verbs for token in positional):
        return None  # Not a package-script invocation (for example npm exec).
    safe_options = {"--silent", "--if-present", "--ignore-scripts", "--foreground-scripts"}
    if positional[0] not in verbs or any(token.startswith("-") and token not in safe_options for token in tokens):
        raise ValidationServiceError("unsupported npm script options; use explicit service cwd and pass script arguments after --")
    if positional[0] in lifecycle:
        return lifecycle[positional[0]]
    if len(positional) < 2:
        raise ValidationServiceError("npm evaluation requires a named script in service cwd")
    return positional[1]


def _npm_configuration_digest(root: Path, package_dir: Path) -> str:
    """Bind local/inherited npm settings; refuse selectors we cannot attribute."""
    # Only CLI --prefix selects npm's local package. The inherited/file prefix
    # sets the global installation prefix and is common on Windows CI hosts.
    selectors = {"workspace", "workspaces", "include-workspace-root", "global",
                 "location", "userconfig", "globalconfig", "dry-run"}
    environment = {key: value for key, value in os.environ.items() if key.casefold().startswith("npm_config_")}
    if any(key[11:].casefold().replace("_", "-") in selectors for key in environment):
        raise ValidationServiceError("npm configuration selects an unsupported target or mode; use explicit service cwd without npm selectors")
    digest = hashlib.sha256(json.dumps(environment, sort_keys=True).encode("utf-8"))
    # A nested package can inherit project-local npm configuration. Never read
    # user/global config or follow a project config symlink outside the candidate.
    directory = package_dir
    while True:
        config = _inside(root, (directory / ".npmrc").relative_to(root).as_posix())
        if config.is_file():
            try:
                raw = config.read_bytes()
                lines = raw.decode("utf-8-sig").splitlines()
            except (OSError, UnicodeError) as exc:
                raise ValidationServiceError("cannot read local npm configuration") from exc
            for line in lines:
                key = line.split("=", 1)[0].strip()
                if key and not key.startswith(("#", ";")) and any(char in key for char in "\"'\\"):
                    raise ValidationServiceError("quoted or escaped local npm keys are unsupported; use explicit service cwd without npm selectors")
                match = re.match(r"\s*([^#;=\s]+)\s*(?:=|$)", line)
                if match and match[1].casefold().removesuffix("[]").replace("_", "-") in selectors:
                    raise ValidationServiceError("local npm configuration selects an unsupported target or mode; use explicit service cwd without npm selectors")
            digest.update((directory.relative_to(root).as_posix() + "\0").encode("utf-8"))
            digest.update(hashlib.sha256("\n".join(lines).encode("utf-8")).digest())
        if directory == root:
            break
        directory = directory.parent
    return digest.hexdigest()


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
    if service.evaluator is not None and (
        not isinstance(service.evaluator, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", service.evaluator)
    ):
        raise ValidationServiceError("service evaluator must be a bounded identifier")
    if not isinstance(service.require_independent, bool):
        raise ValidationServiceError("service require_independent must be a boolean")
    if service.require_independent and service.evaluator is None:
        raise ValidationServiceError("independent evaluation requires an evaluator identity")
    if len(service.requirements) > MAX_EVALUATION_REQUIREMENTS:
        raise ValidationServiceError("too many evaluation requirements")
    if len({requirement.id for requirement in service.requirements}) != len(
        service.requirements
    ):
        raise ValidationServiceError("evaluation requirement ids must be unique")
    for requirement in service.requirements:
        _validate_requirement(requirement, service.kind)
    if service.result_manifest is not None:
        if not isinstance(service.result_manifest, str) or not service.result_manifest:
            raise ValidationServiceError("result_manifest must be a relative file path")
        _inside(Path.cwd(), service.result_manifest)
        if any(
            part.casefold() in {".git", ".stagemesh"}
            or part.casefold().startswith(".env")
            or part.casefold().endswith((".pem", ".key"))
            for part in Path(service.result_manifest).parts
        ):
            raise ValidationServiceError("result_manifest refers to protected material")
    if service.requirements and service.result_manifest is None:
        raise ValidationServiceError("evaluation requirements need result_manifest")
    for label, command in (
        ("startup_command", service.startup_command),
        ("health_command", service.health_command),
    ):
        if command and any(
            not isinstance(part, str) or not part or "\0" in part for part in command
        ):
            raise ValidationServiceError(f"service {label} must be an argument list")
    if bool(service.startup_command) != bool(service.health_command):
        raise ValidationServiceError("managed startup requires both startup_command and health_command")
    if (
        type(service.startup_timeout_seconds) is not int
        or not 1 <= service.startup_timeout_seconds <= 300
    ):
        raise ValidationServiceError("startup_timeout_seconds must be between 1 and 300")


def _parse_requirements(value: object) -> tuple[EvaluationRequirement, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationServiceError("service requirements must be a list")
    if len(value) > MAX_EVALUATION_REQUIREMENTS:
        raise ValidationServiceError("too many evaluation requirements")
    parsed = []
    for item in value:
        if not isinstance(item, dict):
            raise ValidationServiceError("evaluation requirement must be an object")
        parsed.append(
            EvaluationRequirement(
                id=item.get("id"),
                kind=item.get("kind"),
                target=item.get("target"),
                state=item.get("state"),
                viewport=item.get("viewport"),
                method=item.get("method"),
                expected_status=item.get("expected_status"),
            )
        )
    return tuple(parsed)


def _parse_optional_command(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationServiceError(f"service {label} must be an argument list")
    return tuple(value)


def _validate_requirement(requirement: EvaluationRequirement, service_kind: str) -> None:
    if not isinstance(requirement.id, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,100}", requirement.id
    ):
        raise ValidationServiceError("invalid evaluation requirement id")
    if requirement.kind not in {"browser", "api"} or requirement.kind != service_kind:
        raise ValidationServiceError("evaluation requirement kind must match its service")
    if not isinstance(requirement.target, str) or not 1 <= len(requirement.target) <= 500:
        raise ValidationServiceError("evaluation requirement target must be bounded")
    if any(ord(character) < 32 for character in requirement.target):
        raise ValidationServiceError("evaluation requirement target contains controls")
    if requirement.kind == "browser":
        if not isinstance(requirement.state, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,100}", requirement.state
        ):
            raise ValidationServiceError("browser requirement needs a bounded state")
        if not isinstance(requirement.viewport, str) or not re.fullmatch(
            r"[1-9][0-9]{1,4}x[1-9][0-9]{1,4}", requirement.viewport
        ):
            raise ValidationServiceError("browser requirement needs WIDTHxHEIGHT viewport")
        if requirement.method is not None or requirement.expected_status is not None:
            raise ValidationServiceError("browser requirement cannot declare API assertions")
        return
    if requirement.state is not None or requirement.viewport is not None:
        raise ValidationServiceError("API requirement cannot declare browser state or viewport")
    if requirement.method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}:
        raise ValidationServiceError("API requirement needs an allowed HTTP method")
    if not isinstance(requirement.expected_status, int) or isinstance(
        requirement.expected_status, bool
    ) or not 100 <= requirement.expected_status <= 599:
        raise ValidationServiceError("API requirement needs an expected HTTP status")


def run_configured_evaluations(
    project: Path, bound: BoundChangeContract, manifest: ValidationServicesManifest,
    artifact_root: Path,
    *,
    implementation_provider: str | None = None,
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
        for index, service in enumerate(selected):
            if discover_validation_services(checkout, config_project=project).fingerprint != manifest.fingerprint:
                raise ValidationServiceError("service configuration changed from the candidate")
            name = "e2e:" + service.name
            run_dir = _inside(checkout, service.cwd or ".")
            missing_env = [key for key in service.env_keys if not os.environ.get(key)]
            if missing_env or not run_dir.is_dir():
                raise ValidationServiceError("service prerequisites unavailable: " + name)
            evaluator = service.evaluator
            if service.require_independent and (
                not implementation_provider
                or evaluator is None
                or evaluator.casefold() == implementation_provider.casefold()
            ):
                raise ValidationServiceError(
                    "independent evaluator is missing or matches the implementation provider"
                )
            sources = [_inside(run_dir, path) for path in service.artifacts]
            before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in sources if p.is_file()}
            policy_result = (
                _inside(run_dir, service.result_manifest)
                if service.result_manifest is not None
                else None
            )
            result_before = (
                (policy_result.stat().st_mtime_ns, policy_result.stat().st_size)
                if policy_result is not None and policy_result.is_file()
                else None
            )
            evaluation_gate = GateCommand(
                name,
                list(service.command),
                timeout_seconds=service.timeout_seconds,
                cwd=service.cwd,
                env=(("STAGEMESH_E2E_POLICY_FINGERPRINT", service.policy_fingerprint),),
            )
            if service.startup_command:
                result, lifecycle = _run_with_managed_service(
                    checkout,
                    run_dir,
                    service,
                    evaluation_gate,
                )
            else:
                result = run_gate(checkout, evaluation_gate)
                lifecycle = {
                    "managed": False,
                    "started": False,
                    "healthy": False,
                    "cleanup_confirmed": True,
                }
            if discover_validation_services(checkout, config_project=project).fingerprint != manifest.fingerprint:
                raise ValidationServiceError("service configuration changed during evaluation in the candidate checkout")
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
            policy_results: list[dict[str, str]] = []
            if service.requirements:
                try:
                    policy_results = _validate_policy_result(
                        policy_result,
                        result_before,
                        service,
                    )
                except (OSError, ValueError, ValidationServiceError) as exc:
                    artifact_errors.append(bounded_failure_text(str(exc), 500, secrets))
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
            if status != "PASSED":
                signature = _failure_signature(
                    service,
                    failure_kind,
                    result.returncode,
                    stderr,
                )
                findings.append({"severity": "error", "code": "gate_failed", "message": name + " failed" + (" (artifact or policy evidence)" if artifact_errors else ""),
                                 "failure_signature": signature,
                                 **{key: gate[key] for key in ("command", "returncode", "failure_kind", "stdout", "stderr")}})
            else:
                signature = None
            gate["failure_signature"] = signature
            gates.append(gate)
            evidence = build_e2e_evidence(
                bound, candidate_sha=bound.candidate_sha, service_fingerprint=manifest.fingerprint,
                kind=service.kind, command=service.command, result="PASS" if status == "PASSED" else "FAIL",
                artifacts=references, artifact_root=artifact_root,
                metadata={
                    "gate": name,
                    "failure_kind": failure_kind,
                    "failure_signature": signature,
                    "policy_fingerprint": service.policy_fingerprint,
                    "evaluator": service.evaluator,
                    "independent": bool(service.require_independent),
                    "requirements": policy_results,
                    "lifecycle": lifecycle,
                },
            ).to_dict()
            # Executed identity stays bound by the service fingerprint; persist no command credentials.
            evidence["command"] = command
            evaluations.append(evidence)
        if discover_validation_services(checkout, config_project=project).fingerprint != manifest.fingerprint:
            raise ValidationServiceError("service configuration changed during evaluation in the candidate checkout")
    if discover_validation_services(project).fingerprint != manifest.fingerprint:
        raise ValidationServiceError("service configuration changed during evaluation")
    return evaluations, gates, findings


def _validate_policy_result(
    source: Path | None,
    before: tuple[int, int] | None,
    service: ConfiguredService,
) -> list[dict[str, str]]:
    if source is None or not source.is_file():
        raise ValidationServiceError("evaluation result manifest was not produced")
    stat = source.stat()
    if before == (stat.st_mtime_ns, stat.st_size):
        raise ValidationServiceError("evaluation result manifest was not refreshed")
    if stat.st_size > MAX_EVALUATION_RESULT_BYTES:
        raise ValidationServiceError("evaluation result manifest exceeds size limit")
    try:
        payload = json.loads(source.read_bytes())
    except (OSError, ValueError, UnicodeError) as exc:
        raise ValidationServiceError("evaluation result manifest is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValidationServiceError("evaluation result manifest must be an object")
    if payload.get("policy_fingerprint") != service.policy_fingerprint:
        raise ValidationServiceError("evaluation result policy fingerprint mismatch")
    checks = payload.get("checks")
    if not isinstance(checks, list) or len(checks) > MAX_EVALUATION_REQUIREMENTS:
        raise ValidationServiceError("evaluation result checks are missing or excessive")
    expected = {requirement.id for requirement in service.requirements}
    observed: dict[str, str] = {}
    for check in checks:
        if not isinstance(check, dict):
            raise ValidationServiceError("evaluation result check must be an object")
        check_id, status = check.get("id"), check.get("status")
        if check_id not in expected or check_id in observed or status not in {"PASS", "FAIL"}:
            raise ValidationServiceError("evaluation result check is unknown duplicate or invalid")
        observed[check_id] = status
    if set(observed) != expected:
        raise ValidationServiceError("evaluation result omits required coverage")
    if set(observed.values()) != {"PASS"}:
        raise ValidationServiceError("evaluation result contains a failed requirement")
    return [{"id": requirement.id, "status": observed[requirement.id]} for requirement in service.requirements]


def _run_with_managed_service(
    checkout: Path,
    run_dir: Path,
    service: ConfiguredService,
    evaluation_gate: GateCommand,
):
    """Start only a declared service, require bounded health, then prove cleanup."""
    from .gate_process import GateCleanupUnconfirmed, spawn_gate_process, terminate_owned_gate

    environment = {**os.environ, "STAGEMESH_E2E_POLICY_FINGERPRINT": service.policy_fingerprint}
    environment.setdefault("PYTHONUNBUFFERED", "1")
    src = run_dir / "src"
    if src.is_dir():
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            str(src) if not existing else str(src) + os.pathsep + existing
        )
    proc, identity = spawn_gate_process(list(service.startup_command), run_dir, environment)
    lifecycle = {
        "managed": True,
        "started": True,
        "healthy": False,
        "cleanup_confirmed": False,
    }
    try:
        deadline = time.monotonic() + service.startup_timeout_seconds
        last_health = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise ValidationServiceError("managed evaluation service exited before health")
            remaining = max(1, int(deadline - time.monotonic()))
            last_health = run_gate(
                checkout,
                GateCommand(
                    service.name + ":health",
                    list(service.health_command),
                    timeout_seconds=min(5, remaining),
                    cwd=service.cwd,
                    env=(("STAGEMESH_E2E_POLICY_FINGERPRINT", service.policy_fingerprint),),
                ),
            )
            if last_health.status == "PASSED":
                lifecycle["healthy"] = True
                break
            time.sleep(0.1)
        if not lifecycle["healthy"]:
            detail = "" if last_health is None else bounded_failure_text(last_health.stderr, 500)
            raise ValidationServiceError("managed evaluation service did not become healthy" + (": " + detail if detail else ""))
        result = run_gate(checkout, evaluation_gate)
    finally:
        try:
            termination = terminate_owned_gate(proc, identity)
        except BaseException as exc:
            raise GateCleanupUnconfirmed(
                type(exc).__name__, identity, cwd=str(run_dir)
            ) from exc
        if not termination.cleanup_confirmed:
            raise GateCleanupUnconfirmed(termination.note, identity, cwd=str(run_dir))
        lifecycle["cleanup_confirmed"] = True
        try:
            proc.communicate(timeout=5)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GateCleanupUnconfirmed(
                "owned service pipes did not drain", identity, cwd=str(run_dir)
            ) from exc
    return result, lifecycle


def _failure_signature(
    service: ConfiguredService,
    failure_kind: str | None,
    returncode: int | None,
    stderr: str,
) -> str:
    payload = {
        "policy_fingerprint": service.policy_fingerprint,
        "failure_kind": failure_kind,
        "returncode": returncode,
        "stderr": " ".join(stderr.casefold().split())[:1000],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def e2e_policy_projection(payload: object) -> dict[str, Any]:
    """Allowlisted E2E policy progress for operator/API surfaces; never raw targets or output."""
    if not isinstance(payload, dict):
        return {"status": "pending", "policies": [], "failure_signatures": []}
    raw = payload.get("e2e_evaluations")
    if not isinstance(raw, list):
        return {"status": "pending", "policies": [], "failure_signatures": []}
    policies: list[dict[str, Any]] = []
    signatures: list[str] = []
    for item in raw[:16]:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        fingerprint = _bounded_digest(metadata.get("policy_fingerprint"))
        signature = _bounded_digest(metadata.get("failure_signature"))
        if signature:
            signatures.append(signature)
        evaluator = metadata.get("evaluator")
        if not isinstance(evaluator, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,100}", evaluator
        ):
            evaluator = None
        requirements = []
        raw_requirements = metadata.get("requirements")
        if isinstance(raw_requirements, list):
            for requirement in raw_requirements[:MAX_EVALUATION_REQUIREMENTS]:
                if not isinstance(requirement, dict):
                    continue
                requirement_id = requirement.get("id")
                status = requirement.get("status")
                if (
                    isinstance(requirement_id, str)
                    and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", requirement_id)
                    and status in {"PASS", "FAIL"}
                ):
                    requirements.append({"id": requirement_id, "status": status})
        lifecycle = metadata.get("lifecycle")
        lifecycle_projection = {
            key: bool(lifecycle.get(key)) if isinstance(lifecycle, dict) else False
            for key in ("managed", "started", "healthy", "cleanup_confirmed")
        }
        result = item.get("result") if item.get("result") in {"PASS", "FAIL"} else "UNKNOWN"
        policies.append(
            {
                "kind": item.get("kind") if item.get("kind") in {"browser", "api"} else "unknown",
                "result": result,
                "policy_fingerprint": fingerprint,
                "evaluator": evaluator,
                "independent": bool(metadata.get("independent")),
                "requirements": requirements,
                "lifecycle": lifecycle_projection,
                "failure_signature": signature,
            }
        )
    results = {policy["result"] for policy in policies}
    status = "failed" if "FAIL" in results else "passed" if policies and results == {"PASS"} else "pending"
    return {
        "status": status,
        "policies": policies,
        "failure_signatures": sorted(set(signatures)),
    }


def _bounded_digest(value: object) -> str | None:
    return (
        value.casefold()
        if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value)
        else None
    )
