from __future__ import annotations


SECRET_MARKERS = ("token", "secret", "password", "authorization", "apikey", "api_key")


def redact_mapping(data: dict[str, object]) -> dict[str, object]:
    redacted: dict[str, object] = {}
    for key, value in data.items():
        if any(marker in key.lower() for marker in SECRET_MARKERS):
            redacted[key] = "***REDACTED***"
        elif isinstance(value, dict):
            redacted[key] = redact_mapping(value)
        else:
            redacted[key] = value
    return redacted


def redact_text(text: str, secrets: list[str | None]) -> str:
    result = text
    for secret in secrets:
        if secret:
            result = result.replace(secret, "***REDACTED***")
    return result
