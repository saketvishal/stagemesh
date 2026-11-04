from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

SECRET_MARKERS = ("token", "secret", "password", "authorization", "apikey", "api_key", "api-key")
REDACTED = "***REDACTED***"
MAX_PROVIDER_OUTPUT_CHARS = 500


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
    if isinstance(value, str):
        return redact_output(value)
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


# Anchored so a long run of word characters is scanned once (no quadratic backtracking on captured output).
_URL_CREDENTIALS = re.compile(r"(?<![\w+.\-])(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@")
_SECRET_ASSIGNMENT = re.compile(
    r"(?P<key>(?:token|secret|password|passwd|authorization|api[_\-]?key)\w*)(?P<sep>\s*[=:]\s*)(?:Bearer\s+)?[^\s\"',;]+",
    re.IGNORECASE,
)
_STANDALONE_SECRET = re.compile(
    r"(?i)(?P<bearer>\bBearer\s+)[^\s\"',;]+|(?<![A-Za-z0-9_])(?:gh[pousr]_|sk-|xai-)[^\s\"',;]+"
)
_TRUNCATION_MARKER = "\n...[truncated]...\n"


def redact_output(text: str) -> str:
    """Scrub credentials from captured process output before it is stored or shown."""
    text = _URL_CREDENTIALS.sub(lambda match: f"{match.group('scheme')}***REDACTED***@", text)
    text = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group('key')}{match.group('sep')}{REDACTED}", text)
    return _STANDALONE_SECRET.sub(
        lambda match: f"{match.group('bearer') or ''}{REDACTED}", text
    )


def sanitize_provider_output(
    value: object, limit: int = MAX_PROVIDER_OUTPUT_CHARS
) -> str:
    """Return bounded, printable, credential-scrubbed text for untrusted provider evidence."""
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        try:
            text = str(value)
        except Exception:  # noqa: BLE001 - evidence conversion must never escape this boundary
            text = "<unprintable provider output>"
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(
        character if character in {"\n", "\t"} or character.isprintable() else "�"
        for character in text
    )
    text = redact_output(text)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= len(_TRUNCATION_MARKER):
        return text[:limit]
    remaining = limit - len(_TRUNCATION_MARKER)
    head = (remaining + 1) // 2
    tail = remaining - head
    return text[:head] + _TRUNCATION_MARKER + (text[-tail:] if tail else "")
