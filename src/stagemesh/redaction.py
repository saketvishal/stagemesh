from __future__ import annotations


SECRET_MARKERS = ("token", "secret", "password", "authorization", "apikey", "api_key")


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
