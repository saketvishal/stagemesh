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
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .activity import ActivityEntry, ActivityPage, ActivityQuery, ActivityQueryError, query_activity

CONTRACT = "stagemesh.activity/1"
LOOPBACK = "127.0.0.1"
_PARAMS = frozenset({"state", "task_id", "stage", "provider", "since", "until", "limit", "offset"})
_INT = re.compile(r"^[0-9]{1,9}$")
_MAX_QUERY_CHARS = 2048
_STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}

Response = tuple[int, bytes, dict[str, str]]


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _json(status: int, body: Mapping[str, Any]) -> Response:
    payload = json.dumps({"contract": CONTRACT, **body}, separators=(",", ":")).encode()
    return status, payload, {"Content-Type": "application/json; charset=utf-8"}


def _error(exc: ApiError) -> Response:
    return _json(exc.status, {"error": {"code": exc.code, "message": exc.message}})


def _timestamp(name: str, value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ApiError(400, "invalid_query", f"{name} must be an RFC 3339 timestamp with an offset") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ApiError(400, "invalid_query", f"{name} must include a UTC offset")
    return parsed


def _integer(name: str, value: str) -> int:
    if not _INT.match(value):
        raise ApiError(400, "invalid_query", f"{name} must be a non-negative integer")
    return int(value)


def parse_query(raw: str) -> ActivityQuery:
    if len(raw) > _MAX_QUERY_CHARS:
        raise ApiError(400, "invalid_query", "query string is too long")
    try:
        pairs = parse_qs(raw, keep_blank_values=True, strict_parsing=bool(raw), max_num_fields=len(_PARAMS) + 1)
    except ValueError:
        raise ApiError(400, "invalid_query", "malformed query string") from None
    unknown = sorted(set(pairs) - _PARAMS)
    if unknown:
        raise ApiError(400, "invalid_query", f"unknown parameter: {unknown[0][:40]}")
    for name, values in pairs.items():
        if len(values) != 1:
            raise ApiError(400, "invalid_query", f"{name} must not be repeated")
    one = {name: values[0] for name, values in pairs.items()}
    kwargs: dict[str, Any] = {}
    for name in ("state", "task_id", "stage", "provider"):
        if name in one:
            kwargs[name] = one[name]
    for name in ("since", "until"):
        if name in one:
            kwargs[name] = _timestamp(name, one[name])
    for name in ("limit", "offset"):
        if name in one:
            kwargs[name] = _integer(name, one[name])
    return ActivityQuery(**kwargs)


def _entry(entry: ActivityEntry) -> dict[str, Any]:
    data = asdict(entry)
    data["at"] = entry.at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return data


def serialize_page(page: ActivityPage) -> dict[str, Any]:
    return {
        "entries": [_entry(entry) for entry in page.entries],
        "total": page.total,
        "limit": page.limit,
        "offset": page.offset,
        "next_offset": page.next_offset,
        "truncated": page.truncated,
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
        try:
            status, body, extra = self._route(method, target, headers)
        except ApiError as exc:
            status, body, extra = _error(exc)
        except ActivityQueryError as exc:
            status, body, extra = _error(ApiError(400, "invalid_query", str(exc)))
        except (sqlite3.Error, OSError):
            status, body, extra = _error(ApiError(503, "store_unavailable", "activity store is unavailable"))
        except Exception:  # noqa: BLE001 - never leak internals
            status, body, extra = _error(ApiError(500, "internal", "internal error"))
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
        if not path.startswith("/api/"):
            return self._static(path)  # the shell carries no data, so it needs no credential
        self._authenticate(lowered.get("authorization", ""))
        if path == "/api/v1/health":
            if parts.query:
                raise ApiError(400, "invalid_query", "health takes no parameters")
            return _json(200, {"status": "ok", "version": __version__})
        if path == "/api/v1/activity":
            query = parse_query(parts.query)
            store = self._open()
            try:
                page = query_activity(store, query)  # type: ignore[arg-type]
            finally:
                store.conn.close()
            return _json(200, serialize_page(page))
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
        if "/" in name or "\\" in name or Path(name).suffix not in _STATIC_TYPES:
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
) -> tuple[HTTPServer, ActivityApi]:
    api = ActivityApi(db_path, token or secrets.token_urlsafe(32), static_dir)
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
