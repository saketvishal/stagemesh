from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

SECRET_MARKERS = ("token", "secret", "password", "authorization", "apikey", "api_key", "api-key")


def redact_mapping(data: dict[str, object]) -> dict[str, object]:
    redacted: dict[str, object] = {}
    for key, value in data.items():
        if any(marker in key.lower() for marker in SECRET_MARKERS):
            redacted[key] = "***REDACTED***"
        else:
            redacted[key] = _redact_value(value)
    return redacted


def _redact_value(value: object) -> object:
    if isinstance(value, dict):
        return redact_mapping(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


def redact_text(text: str, secrets: list[str | None]) -> str:
    result = text
    for secret in secrets:
        if secret:
            result = result.replace(secret, "***REDACTED***")
    return result


def redact_url_credentials(url: str | None) -> str | None:
    if not url:
        return url
    try:
        parsed = urlsplit(url)
    except ValueError:
        return url
    if not parsed.username and not parsed.password:
        return url
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, f"***REDACTED***@{host}", parsed.path, parsed.query, parsed.fragment))


def redact_command_secrets(command: str) -> str:
    import shlex

    try:
        parts = shlex.split(command)
    except ValueError:
        return "***REDACTED***" if any(marker in command.lower() for marker in SECRET_MARKERS) else command
    redacted: list[str] = []
    redact_next = False
    for part in parts:
        lower = part.lower()
        if redact_next:
            redacted.append("***REDACTED***")
            redact_next = False
            continue
        if any(marker in lower for marker in SECRET_MARKERS):
            if "=" in part:
                key, _value = part.split("=", 1)
                redacted.append(f"{key}=***REDACTED***")
            else:
                redacted.append(part)
                redact_next = True
            continue
        redacted.append(part)
    return shlex.join(redacted)
