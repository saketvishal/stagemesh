"""Safe operational logging for the persistent watcher (SDD-001 section 4.7).

Watcher logs are structured JSON lines under
`{data_dir}/watcher-logs/watcher.log`. They must never carry API tokens,
authorization headers, provider command environment values, full worker
prompts, hidden reasoning, raw result payload fields named like
secret/key/token/password, or full worker stdout/stderr content. It is
acceptable to log execution IDs, task IDs, status values, event types,
redacted error classes, and counts.

Reuses `tooling.build_coordinator.execution.results.sanitize_result_mapping`
for key-based redaction of any structured `extra` payload, and adds
value-based redaction for secret-shaped strings (bearer tokens, GitHub
tokens, long opaque credential-looking strings) that key-based redaction
alone would miss inside free-text messages.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from build_coordinator.execution.results import sanitize_result_mapping

_TOKEN_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub PAT / OAuth / App / refresh token prefixes
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{10,}"),
    re.compile(r"(?i)authorization:\s*\S+"),
)
_REDACTED = "[REDACTED]"


def redact_text(message: str) -> str:
    """Value-based redaction for a free-text log message. Complements
    `sanitize_result_mapping`'s key-based redaction, which only inspects
    dict keys and cannot see a token embedded inside a sentence (e.g. a raw
    `gh` CLI stderr line)."""
    redacted = message
    for pattern in _TOKEN_PATTERNS:
        redacted = pattern.sub(_REDACTED, redacted)
    return redacted


def watcher_log_dir(data_dir: Path) -> Path:
    return data_dir / "watcher-logs"


def watcher_log_path(data_dir: Path) -> Path:
    return watcher_log_dir(data_dir) / "watcher.log"


class WatcherLogger:
    """Append-only structured JSON log writer for one watcher process."""

    def __init__(self, data_dir: Path) -> None:
        self._path = watcher_log_path(data_dir)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        event_type: str,
        *,
        repository_slug: str | None = None,
        cycle_id: str | None = None,
        result_summary: str | None = None,
        error_type: str | None = None,
        message: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        record: dict[str, Any] = {
            "timestamp": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "event_type": event_type,
        }
        if repository_slug is not None:
            record["repository_slug"] = repository_slug
        if cycle_id is not None:
            record["cycle_id"] = cycle_id
        if result_summary is not None:
            record["result_summary"] = redact_text(result_summary)
        if error_type is not None:
            record["error_type"] = error_type
        if message is not None:
            record["message"] = redact_text(message)
        if extra:
            cleaned = sanitize_result_mapping(extra)
            record["extra"] = json.loads(json.dumps(cleaned, default=str))
            record["extra"] = _redact_structure(record["extra"])
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def _redact_structure(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _redact_structure(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_redact_structure(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
