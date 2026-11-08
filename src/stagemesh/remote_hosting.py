"""Validation for an operator-selected remote-hosting security plan.

This module does not create a listener or change the loopback-only API server.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit


class RemoteHostingPlanError(ValueError):
    """Raised when a remote-hosting plan is incomplete or unsafe."""


@dataclass(frozen=True)
class RemoteHostingPlan:
    normalized: dict[str, object]
    digest: str

    def to_dict(self) -> dict[str, object]:
        return {"digest": self.digest, "plan": self.normalized, "status": "PLAN_VALIDATED"}


_TOP_LEVEL = {
    "schema",
    "deployment",
    "identity",
    "authorization",
    "secrets",
    "audit",
    "limits",
    "recovery",
}
_SECRET_SHAPED = {"secret", "token", "password", "private_key", "client_secret", "cookie"}


def validate_remote_hosting_plan(payload: Mapping[str, object]) -> RemoteHostingPlan:
    """Normalize a complete operator plan; reject implicit or secret-bearing choices."""
    if set(payload) != _TOP_LEVEL or payload.get("schema") != "stagemesh.remote-hosting-plan/1":
        raise RemoteHostingPlanError("plan fields must exactly match stagemesh.remote-hosting-plan/1")
    _reject_secret_shaped_fields(payload)

    deployment = _section(payload, "deployment", {"target", "tls_owner", "public_origin"})
    identity = _section(
        payload,
        "identity",
        {"strategy", "issuer", "client_id", "session_ttl_seconds", "browser_storage"},
    )
    authorization = _section(
        payload,
        "authorization",
        {"default_role", "read_permission", "control_permission", "project_scope_required"},
    )
    secrets = _section(payload, "secrets", {"store", "rotation_days"})
    audit = _section(payload, "audit", {"record_reads", "record_controls", "record_failures", "retention_days"})
    limits = _section(payload, "limits", {"requests_per_minute", "max_payload_bytes", "log_body_policy"})
    recovery = _section(payload, "recovery", {"backup_target", "restore_test_days", "rollback_strategy"})

    if deployment["target"] != "single_tenant_reverse_proxy":
        raise RemoteHostingPlanError("deployment target must be single_tenant_reverse_proxy in version 1")
    if deployment["tls_owner"] != "reverse_proxy":
        raise RemoteHostingPlanError("the reverse proxy must own TLS in version 1")
    _https_url(deployment["public_origin"], "public origin")
    if identity["strategy"] != "oidc_authorization_code_pkce":
        raise RemoteHostingPlanError("identity strategy must be oidc_authorization_code_pkce")
    _https_url(identity["issuer"], "OIDC issuer")
    _bounded_text(identity["client_id"], "client id", 200)
    _bounded_int(identity["session_ttl_seconds"], "session ttl", 300, 86_400)
    if identity["browser_storage"] != "http_only_secure_same_site_cookie":
        raise RemoteHostingPlanError("sessions must use a secure HttpOnly SameSite cookie")
    if authorization != {
        "default_role": "read_only",
        "read_permission": "project.read",
        "control_permission": "project.control",
        "project_scope_required": True,
    }:
        raise RemoteHostingPlanError("authorization must split read/control and require project scope")
    _bounded_text(secrets["store"], "secret store", 200)
    _bounded_int(secrets["rotation_days"], "secret rotation", 1, 365)
    if any(audit[key] is not True for key in ("record_reads", "record_controls", "record_failures")):
        raise RemoteHostingPlanError("audit must record reads, controls, and failures")
    _bounded_int(audit["retention_days"], "audit retention", 1, 3650)
    _bounded_int(limits["requests_per_minute"], "request rate", 1, 10_000)
    _bounded_int(limits["max_payload_bytes"], "payload limit", 1, 10_000_000)
    if limits["log_body_policy"] != "never":
        raise RemoteHostingPlanError("request and response bodies must not be logged")
    _bounded_text(recovery["backup_target"], "backup target", 200)
    _bounded_int(recovery["restore_test_days"], "restore test interval", 1, 365)
    if recovery["rollback_strategy"] != "versioned_atomic_rollback":
        raise RemoteHostingPlanError("rollback strategy must be versioned_atomic_rollback")

    normalized = json.loads(json.dumps(payload, sort_keys=True))
    encoded = json.dumps(normalized, separators=(",", ":"), sort_keys=True).encode()
    return RemoteHostingPlan(normalized, hashlib.sha256(encoded).hexdigest())


def _section(
    payload: Mapping[str, object], name: str, keys: set[str]
) -> Mapping[str, object]:
    value = payload.get(name)
    if not isinstance(value, Mapping) or set(value) != keys:
        raise RemoteHostingPlanError(f"{name} fields do not match the versioned schema")
    return value


def _reject_secret_shaped_fields(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in _SECRET_SHAPED:
                raise RemoteHostingPlanError("plans must reference secret stores, never contain secrets")
            _reject_secret_shaped_fields(item)
    elif isinstance(value, list):
        for item in value:
            _reject_secret_shaped_fields(item)


def _https_url(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise RemoteHostingPlanError(f"{label} must be an HTTPS URL")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise RemoteHostingPlanError(f"{label} must be an HTTPS URL without embedded credentials")
    if parsed.query or parsed.fragment:
        raise RemoteHostingPlanError(f"{label} must not contain a query or fragment")


def _bounded_text(value: object, label: str, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise RemoteHostingPlanError(f"{label} must be a bounded non-empty string")


def _bounded_int(value: object, label: str, minimum: int, maximum: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise RemoteHostingPlanError(f"{label} must be between {minimum} and {maximum}")
