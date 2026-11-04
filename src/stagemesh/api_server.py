"""Loopback, read-only HTTP API over the activity projection (docs/operator-ui-contract.md).

Standard library only. Every request opens the coordinator database read-only (`mode=ro`,
`query_only`), runs `query_activity`, and closes it, so nothing here can mutate coordinator state
and no connection outlives a request. Requests fail closed: unknown or repeated parameters,
wrong methods, bad hosts/origins and missing credentials are errors, never coerced.
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import UTC, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import unquote_to_bytes, urlsplit

from . import __version__
from .activity import ActivityEntry, ActivityPage, ActivityQuery, ActivityQueryError, query_activity
from .activity_periods import (
    ActivityPeriodError,
    ActivityPeriodQuery,
    ActivityPeriodReport,
    query_activity_periods,
)

CONTRACT = "stagemesh.activity/1"
CONTRACT_V2 = "stagemesh.activity/2"
CONTRACT_V3 = "stagemesh.activity/3"
CONTRACT_V4 = "stagemesh.activity/4"
CONTRACT_V5 = "stagemesh.activity/5"
LOOPBACK = "127.0.0.1"
_PARAMS = frozenset({"state", "task_id", "stage", "provider", "since", "until", "limit", "offset"})
_PARAMS_V2 = _PARAMS | {"project", "time_basis"}
_PARAMS_V3 = _PARAMS_V2 | {"source", "source_status", "include_source_history"}
_PARAMS_V4 = _PARAMS_V3 | {
    "task_event", "task_status", "include_task_history", "retry_kind", "retry_state",
    "include_retry_history",
}
_PARAMS_V5 = _PARAMS_V4 | {
    "evidence_kind", "evidence_status", "include_evidence_history",
}
_INT = re.compile(r"^[0-9]{1,9}$")
_MAX_QUERY_CHARS = 2048
# The complete production shell. Packaging (build_backend.UI_ASSETS) and serving both use exactly this list.
UI_ASSETS = ("index.html", "app.js", "app.css")
_STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}

Response = tuple[int, bytes, dict[str, str]]


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _json(status: int, body: Mapping[str, Any], contract: str = CONTRACT) -> Response:
    payload = json.dumps({"contract": contract, **body}, separators=(",", ":")).encode()
    return status, payload, {"Content-Type": "application/json; charset=utf-8"}


def _error(exc: ApiError, contract: str = CONTRACT) -> Response:
    return _json(exc.status, {"error": {"code": exc.code, "message": exc.message}}, contract)


# Exactly RFC 3339 `date-time`: uppercase T and Z, extended format, mandatory offset, optional fraction of 1-9 digits.
_RFC3339 = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?(Z|[+-]([0-9]{2}):([0-9]{2}))",
    re.ASCII,
)
_BAD_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_URL_SAFE = re.compile(r"^[!-~]*$")


def _invalid(message: str) -> ApiError:
    return ApiError(400, "invalid_query", message)


def _timestamp(name: str, value: str) -> datetime:
    """Parse the supported form `YYYY-MM-DDTHH:MM:SS[.f{1,9}](Z|+HH:MM|-HH:MM)`; anything else is rejected."""
    match = _RFC3339.fullmatch(value)
    if not match:
        raise _invalid(f"{name} must be an RFC 3339 timestamp such as 2026-01-31T09:30:00Z or 2026-01-31T09:30:00%2B05:30")
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    micro = int((match.group(7) or "").ljust(6, "0")[:6] or 0)
    if match.group(8) == "Z":
        offset = timedelta(0)
    else:
        off_h, off_m = int(match.group(9)), int(match.group(10))
        if off_h > 23 or off_m > 59:
            raise _invalid(f"{name} has an invalid UTC offset")
        offset = timedelta(hours=off_h, minutes=off_m) * (-1 if match.group(8)[0] == "-" else 1)
    try:
        return datetime(year, month, day, hour, minute, second, micro, tzinfo=timezone(offset))
    except ValueError:
        raise _invalid(f"{name} is not a valid calendar time") from None


def _integer(name: str, value: str) -> int:
    if not _INT.match(value):
        raise _invalid(f"{name} must be a non-negative integer")
    return int(value)


def _boolean(name: str, value: str) -> bool:
    if value not in {"true", "false"}:
        raise _invalid(f"{name} must be true or false")
    return value == "true"


def _decode(component: str) -> str:
    """Strict form decoding: malformed escapes and invalid UTF-8 are errors, never replaced."""
    if _BAD_ESCAPE.search(component):
        raise _invalid("malformed percent escape in query string")
    try:
        text = unquote_to_bytes(component.replace("+", " ")).decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise _invalid("query string is not valid UTF-8") from None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise _invalid("query string contains control characters")
    return text


def parse_query(raw: str, *, version: int = 1) -> ActivityQuery:
    allowed = (
        _PARAMS_V5
        if version == 5
        else _PARAMS_V4
        if version == 4
        else (_PARAMS_V3 if version == 3 else (_PARAMS_V2 if version == 2 else _PARAMS))
    )
    if len(raw) > _MAX_QUERY_CHARS:
        raise _invalid("query string is too long")
    if not _URL_SAFE.match(raw):
        raise _invalid("query string must be percent-encoded ASCII")
    one: dict[str, str] = {}
    if raw:
        segments = raw.split("&")
        if len(segments) > len(allowed) + 1:
            raise _invalid("too many query parameters")
        for segment in segments:
            name, sep, value = segment.partition("=")
            if not segment or not sep or not name:
                raise _invalid("malformed query string")
            name = _decode(name)
            if name not in allowed:
                raise _invalid("unknown parameter")
            if name in one:
                raise _invalid(f"{name} must not be repeated")
            one[name] = _decode(value)
    kwargs: dict[str, Any] = {}
    for name in (
        "state", "task_id", "stage", "provider", "project", "time_basis", "source", "source_status",
        "task_event", "task_status", "retry_kind", "retry_state", "evidence_kind", "evidence_status",
    ):
        if name in one:
            kwargs[name] = one[name]
    if "include_source_history" in one:
        kwargs["include_source_history"] = _boolean(
            "include_source_history", one["include_source_history"]
        )
    if "include_task_history" in one:
        kwargs["include_task_history"] = _boolean(
            "include_task_history", one["include_task_history"]
        )
    if "include_retry_history" in one:
        kwargs["include_retry_history"] = _boolean(
            "include_retry_history", one["include_retry_history"]
        )
    if "include_evidence_history" in one:
        kwargs["include_evidence_history"] = _boolean(
            "include_evidence_history", one["include_evidence_history"]
        )
    for name in ("since", "until"):
        if name in one:
            kwargs[name] = _timestamp(name, one[name])
    for name in ("limit", "offset"):
        if name in one:
            kwargs[name] = _integer(name, one[name])
    return ActivityQuery(**kwargs)


def parse_period_query(raw: str) -> ActivityPeriodQuery:
    allowed = {
        "since", "until", "bucket", "timezone", "task_id", "project", "source", "limit", "offset"
    }
    if len(raw) > _MAX_QUERY_CHARS:
        raise _invalid("query string is too long")
    if not _URL_SAFE.match(raw):
        raise _invalid("query string must be percent-encoded ASCII")
    one: dict[str, str] = {}
    for segment in raw.split("&") if raw else ():
        name, sep, value = segment.partition("=")
        if not segment or not sep or not name:
            raise _invalid("malformed query string")
        name = _decode(name)
        if name not in allowed:
            raise _invalid("unknown parameter")
        if name in one:
            raise _invalid(f"{name} must not be repeated")
        one[name] = _decode(value)
    if "since" not in one or "until" not in one:
        raise _invalid("periods requires since and until")
    kwargs: dict[str, Any] = {}
    for name in ("limit", "offset"):
        if name in one:
            kwargs[name] = _integer(name, one[name])
    return ActivityPeriodQuery(
        since=_timestamp("since", one["since"]),
        until=_timestamp("until", one["until"]),
        bucket=one.get("bucket", "day"),  # type: ignore[arg-type]
        timezone=one.get("timezone", "UTC"),
        task_id=one.get("task_id"),
        project=one.get("project"),
        source=one.get("source"),
        **kwargs,
    )


def _entry(entry: ActivityEntry, *, version: int = 1) -> dict[str, Any]:
    data = asdict(entry)
    data["at"] = entry.at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    if version == 1:
        data.pop("project")
    if version < 3:
        for name in ("source", "source_id", "source_status", "previous_source_status"):
            data.pop(name)
    if version < 4:
        for name in (
            "previous_task_status", "current_task_status", "rework_required",
            "retry_kind", "retry_state", "retry_attempt", "retry_limit",
        ):
            data.pop(name)
    if version < 5:
        for name in ("evidence_kind", "evidence_status"):
            data.pop(name)
    return data


def serialize_page(page: ActivityPage, *, version: int = 1) -> dict[str, Any]:
    body = {
        "entries": [_entry(entry, version=version) for entry in page.entries],
        "total": page.total,
        "limit": page.limit,
        "offset": page.offset,
        "next_offset": page.next_offset,
        "truncated": page.truncated,
    }
    if version >= 2:
        counts = page.status_counts
        if version < 5:
            counts = {
                name: values for name, values in counts.items()
                if name not in {"evidence", "evidence_outcome"}
            }
        if version < 4:
            counts = {
                name: values for name, values in counts.items()
                if name not in {"task_transition", "retry", "retry_outcome"}
            }
        if version == 2:
            counts = {name: values for name, values in counts.items() if name != "source"}
        body.update(time_basis=page.time_basis, status_counts=counts)
    return body


def serialize_period_report(report: ActivityPeriodReport) -> dict[str, Any]:
    def instant(value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    return {
        "since": instant(report.since),
        "until": instant(report.until),
        "bucket": report.bucket,
        "timezone": report.timezone,
        "active_intervals": [
            {
                **asdict(interval),
                "active_from": instant(interval.active_from),
                "active_until": instant(interval.active_until),
            }
            for interval in report.active_intervals
        ],
        "total_intervals": report.total_intervals,
        "limit": report.limit,
        "offset": report.offset,
        "next_offset": report.next_offset,
        "series": [
            {**asdict(bucket), "start": instant(bucket.start), "end": instant(bucket.end)}
            for bucket in report.series
        ],
        "unavailable_tasks": report.unavailable_tasks,
        "considered_tasks": report.considered_tasks,
        "truncated": report.truncated,
    }


class ActivityApi:
    """Transport-independent request handling, so tests need no sockets."""

    def __init__(self, db_path: Path, token: str, static_dir: Path | None = None):
        if not token:
            raise ValueError("an access token is required")
        self.db_path = Path(db_path)
        self.token = token
        self.static_dir = static_dir
        self.allowed_hosts: frozenset[str] = frozenset()

    def bind(self, port: int) -> None:
        hosts = {f"{LOOPBACK}:{port}", f"localhost:{port}"}
        self.allowed_hosts = frozenset(hosts)

    def handle(self, method: str, target: str, headers: Mapping[str, str]) -> Response:
        contract = CONTRACT
        try:
            if urlsplit(target).path.startswith("/api/v5/"):
                contract = CONTRACT_V5
            elif urlsplit(target).path.startswith("/api/v4/"):
                contract = CONTRACT_V4
            elif urlsplit(target).path.startswith("/api/v3/"):
                contract = CONTRACT_V3
            elif urlsplit(target).path.startswith("/api/v2/"):
                contract = CONTRACT_V2
            status, body, extra = self._route(method, target, headers)
        except ApiError as exc:
            status, body, extra = _error(exc, contract)
        except ActivityQueryError as exc:
            status, body, extra = _error(ApiError(400, "invalid_query", str(exc)), contract)
        except ActivityPeriodError as exc:
            status, body, extra = _error(ApiError(400, "invalid_query", str(exc)), contract)
        except (sqlite3.Error, OSError):
            status, body, extra = _error(ApiError(503, "store_unavailable", "activity store is unavailable"), contract)
        except Exception:  # noqa: BLE001 - never leak internals
            status, body, extra = _error(ApiError(500, "internal", "internal error"), contract)
        extra.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})
        return status, body, extra

    def _route(self, method: str, target: str, headers: Mapping[str, str]) -> Response:
        lowered = {key.lower(): value for key, value in headers.items()}
        host = lowered.get("host", "")
        if host not in self.allowed_hosts:
            raise ApiError(403, "forbidden", "host not allowed")
        origin = lowered.get("origin")
        if origin is not None and origin not in {f"http://{h}" for h in self.allowed_hosts}:
            raise ApiError(403, "forbidden", "origin not allowed")
        if method != "GET":
            raise ApiError(405, "method_not_allowed", "only GET is supported")
        if lowered.get("content-length", "0") not in {"", "0"} or "transfer-encoding" in lowered:
            raise ApiError(400, "invalid_query", "request body is not allowed")
        parts = urlsplit(target)
        path = parts.path
        version = 5 if path.startswith("/api/v5/") else (4 if path.startswith("/api/v4/") else (3 if path.startswith("/api/v3/") else (2 if path.startswith("/api/v2/") else 1)))
        contract = CONTRACT_V5 if version == 5 else (CONTRACT_V4 if version == 4 else (CONTRACT_V3 if version == 3 else (CONTRACT_V2 if version == 2 else CONTRACT)))
        if not path.startswith("/api/"):
            return self._static(path)  # the shell carries no data, so it needs no credential
        self._authenticate(lowered.get("authorization", ""))
        if path in {"/api/v1/health", "/api/v2/health", "/api/v3/health", "/api/v4/health", "/api/v5/health"}:
            if parts.query:
                raise ApiError(400, "invalid_query", "health takes no parameters")
            return _json(200, {"status": "ok", "version": __version__}, contract)
        if path in {"/api/v1/activity", "/api/v2/activity", "/api/v3/activity", "/api/v4/activity", "/api/v5/activity"}:
            query = parse_query(parts.query, version=version)
            store = self._open()
            try:
                page = query_activity(store, query)  # type: ignore[arg-type]
            finally:
                store.conn.close()
            return _json(200, serialize_page(page, version=version), contract)
        if path in {"/api/v4/periods", "/api/v5/periods"}:
            query = parse_period_query(parts.query)
            store = self._open()
            try:
                report = query_activity_periods(store, query)
            finally:
                store.conn.close()
            return _json(200, serialize_period_report(report), contract)
        raise ApiError(404, "not_found", "unknown route")

    def _authenticate(self, header: str) -> None:
        scheme, _, value = header.partition(" ")
        if scheme != "Bearer" or not hmac.compare_digest(value.encode(), self.token.encode()):
            raise ApiError(401, "unauthenticated", "missing or invalid credential")

    def _open(self) -> Any:
        if not self.db_path.is_file():
            raise ApiError(503, "store_unavailable", "activity store is unavailable")
        conn = sqlite3.connect(f"{self.db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            return SimpleNamespace(conn=conn)  # query_activity only touches `.conn`
        except BaseException:
            conn.close()
            raise

    def _static(self, path: str) -> Response:
        if self.static_dir is None:
            raise ApiError(404, "not_found", "unknown route")
        name = "index.html" if path == "/" else path.lstrip("/")
        if name not in UI_ASSETS:
            raise ApiError(404, "not_found", "unknown route")
        file = self.static_dir / name
        if not file.is_file():
            raise ApiError(404, "not_found", "unknown route")
        headers = {
            "Content-Type": _STATIC_TYPES[file.suffix],
            "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        }
        return 200, file.read_bytes(), headers


def make_server(
    db_path: Path,
    token: str | None = None,
    port: int = 0,
    static_dir: Path | None = None,
    api_class: type[ActivityApi] = ActivityApi,
) -> tuple[HTTPServer, ActivityApi]:
    api = api_class(db_path, token or secrets.token_urlsafe(32), static_dir)
    handler = _handler(api)
    server = HTTPServer((LOOPBACK, port), handler)
    api.bind(server.server_address[1])
    return server, api


def _handler(api: ActivityApi) -> Callable[..., BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        timeout = 10
        server_version = "stagemesh"
        sys_version = ""

        def _serve(self) -> None:
            status, body, headers = api.handle(self.command, self.path, dict(self.headers.items()))
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _serve

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler
