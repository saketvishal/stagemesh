from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stagemesh.activity import MAX_LIMIT
from stagemesh.api_server import ActivityApi, make_server, parse_query
from stagemesh.persistence import Store

TOKEN = "test-token"
BASE = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "s.sqlite3"
    store = Store(path)
    store.migrate()
    for i, status in enumerate(("OPEN", "DONE")):
        store.conn.execute(
            "INSERT INTO tasks (id, title, stage, status, source, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (f"t{i}", f"Task {i}", "IMPLEMENT", status, "local", 1, (BASE + timedelta(minutes=i)).timestamp()),
        )
    store.conn.commit()
    store.close()
    return path


@pytest.fixture
def server(db: Path):
    httpd, api = make_server(db, token=TOKEN)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", api
    httpd.shutdown()
    httpd.server_close()


def get(base: str, path: str, *, token: str | None = TOKEN, method: str = "GET", headers: dict | None = None):
    req = urllib.request.Request(base + path, method=method, headers=headers or {})
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read()), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), dict(exc.headers)


def test_valid_page_matches_contract(server):
    base, _ = server
    status, body, headers = get(base, "/api/v1/activity?state=closed&limit=10")
    assert status == 200
    assert body["contract"] == "stagemesh.activity/1"
    assert {e["kind"] for e in body["entries"]} == {"task"}
    assert body["entries"][0]["id"] == "t1" and body["entries"][0]["at"].endswith("Z")
    assert body["next_offset"] is None and body["truncated"] is False
    assert headers["Cache-Control"] == "no-store"
    assert "Access-Control-Allow-Origin" not in headers


def _direct_v2(db, target="/api/v2/activity", *, method="GET", headers=None):
    api = ActivityApi(db, TOKEN)
    api.bind(12345)
    defaults = {"Host": "127.0.0.1:12345", "Authorization": f"Bearer {TOKEN}"}
    status, raw, extra = api.handle(method, target, defaults if headers is None else headers)
    return status, json.loads(raw), extra


def test_v2_project_time_basis_and_counts_leave_v1_response_unchanged(db):
    store = Store(db)
    store.conn.execute("UPDATE tasks SET project='team/alpha' WHERE id='t0'")
    store.conn.commit()
    store.close()
    before = db.read_bytes()
    status, body, _ = _direct_v2(db, "/api/v2/activity?project=team%2Falpha&time_basis=updated&limit=1")
    assert status == 200 and body["contract"] == "stagemesh.activity/2"
    assert body["time_basis"] == "updated" and body["status_counts"]["task"]["OPEN"] == 1
    assert len(body["entries"]) == 1 and body["entries"][0]["project"] == "team/alpha"
    _, legacy, _ = _direct_v2(db, "/api/v1/activity?limit=1")
    assert legacy["contract"] == "stagemesh.activity/1"
    assert set(legacy) == {"contract", "entries", "total", "limit", "offset", "next_offset", "truncated"}
    assert "project" not in legacy["entries"][0]
    assert _direct_v2(db, "/api/v1/activity?project=team%2Falpha")[0] == 400
    assert _direct_v2(db, "/api/v1/activity?time_basis=completed")[0] == 400
    assert db.read_bytes() == before


@pytest.mark.parametrize("query", ["project=", "project=" + "x" * 201, "time_basis=finished",
    "time_basis=opened&time_basis=updated", "project=one&%70roject=two", "project=%FF", "project=%00",
    "time_basis=completed&since=2026-01-02T00:00:00Z&until=2026-01-01T00:00:00Z"])
def test_v2_invalid_filters_use_v2_error_envelope(db, query):
    status, body, _ = _direct_v2(db, "/api/v2/activity?" + query)
    assert status == 400 and body["contract"] == "stagemesh.activity/2"
    assert body["error"]["code"] == "invalid_query" and "entries" not in body


@pytest.mark.parametrize("path,method,headers,expected", [
    ("/api/v2/activity", "GET", {"Host": "127.0.0.1:12345"}, 401),
    ("/api/v2/activity", "GET", {"Host": "attacker.invalid"}, 403),
    ("/api/v2/activity", "POST", None, 405),
    ("/api/v2/nope", "GET", None, 404),
    ("/api/v2/health?unexpected=1", "GET", None, 400),
])
def test_v2_auth_and_route_errors_remain_read_only(db, path, method, headers, expected):
    before = db.read_bytes()
    status, body, extra = _direct_v2(db, path, method=method, headers=headers)
    assert status == expected and body["contract"] == "stagemesh.activity/2"
    assert extra["Cache-Control"] == "no-store" and "Access-Control-Allow-Origin" not in extra
    assert db.read_bytes() == before


def test_v2_health_and_unavailable_internal_errors(db, tmp_path, monkeypatch):
    status, body, _ = _direct_v2(db, "/api/v2/health")
    assert status == 200 and body["contract"] == "stagemesh.activity/2" and body["status"] == "ok"
    status, body, _ = _direct_v2(tmp_path / "missing.sqlite3")
    assert status == 503 and body["contract"] == "stagemesh.activity/2"
    def broken(*args):
        raise RuntimeError("private database path and secret")
    monkeypatch.setattr("stagemesh.api_server.query_activity", broken)
    status, body, _ = _direct_v2(db)
    assert status == 500 and body["contract"] == "stagemesh.activity/2"
    assert "private" not in repr(body) and "secret" not in repr(body)


def test_v2_completion_query_runs_over_real_http(server):
    base, _ = server
    # The legacy DONE task has no recorded completion event: do not invent one.
    status, body, _ = get(base, "/api/v2/activity?time_basis=completed&stage=DONE&state=closed")
    assert status == 200 and body["contract"] == "stagemesh.activity/2"
    assert body["entries"] == [] and body["status_counts"]["task"]["DONE"] == 0


def test_pagination_walks_without_overlap(server):
    base, _ = server
    _, first, _ = get(base, "/api/v1/activity?limit=1")
    assert first["next_offset"] == 1
    _, second, _ = get(base, f"/api/v1/activity?limit=1&offset={first['next_offset']}")
    assert first["entries"][0]["id"] != second["entries"][0]["id"]


def test_health_requires_credential(server):
    base, _ = server
    assert get(base, "/api/v1/health")[0] == 200
    assert get(base, "/api/v1/health", token=None)[0] == 401
    assert get(base, "/api/v1/health", token="wrong")[0] == 401


@pytest.mark.parametrize(
    "query",
    [
        "state=pending", "stage=NOPE", "limit=0", f"limit={MAX_LIMIT + 1}", "limit=abc", "limit=-1", "offset=1000",
        "since=2026-01-01T00:00:00", "since=yesterday", "bogus=1", "state=open&state=closed", "task_id=",
        "since=2026-01-02T00:00:00Z&until=2026-01-01T00:00:00Z", "%",
    ],
)
def test_invalid_queries_fail_closed(server, query):
    base, _ = server
    status, body, _ = get(base, f"/api/v1/activity?{query}")
    assert status == 400 and body["error"]["code"] == "invalid_query"
    assert "entries" not in body


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
def test_only_get_is_allowed(server, method):
    base, _ = server
    req = urllib.request.Request(base + "/api/v1/activity", method=method, headers={"Authorization": f"Bearer {TOKEN}"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 405


def test_foreign_host_and_origin_rejected(server):
    base, _ = server
    assert get(base, "/api/v1/activity", headers={"Host": "evil.example"})[0] == 403
    assert get(base, "/api/v1/activity", headers={"Origin": "http://evil.example"})[0] == 403


def test_unknown_route_and_missing_store(tmp_path, server):
    base, api = server
    assert get(base, "/api/v1/nope")[0] == 404
    api.db_path = tmp_path / "missing.sqlite3"
    status, body, _ = get(base, "/api/v1/activity")
    assert status == 503 and body["error"]["code"] == "store_unavailable"
    assert not api.db_path.exists()  # a read never creates the store


def test_reads_never_mutate_the_store(server, db):
    base, _ = server
    before = db.read_bytes()
    get(base, "/api/v1/activity")
    get(base, "/api/v1/activity?state=open")
    assert db.read_bytes() == before


def test_errors_do_not_leak_values_or_paths(server, db):
    base, _ = server
    _, body, _ = get(base, "/api/v1/activity?state=SECRETVALUE")
    assert "SECRETVALUE" not in json.dumps(body) and str(db.parent) not in json.dumps(body)


def test_token_required_and_loopback_only(db):
    with pytest.raises(ValueError):
        from stagemesh.api_server import ActivityApi

        ActivityApi(db, "")
    httpd, _ = make_server(db)
    try:
        assert httpd.server_address[0] == "127.0.0.1"
    finally:
        httpd.server_close()


def test_parse_query_accepts_offset_timestamps():
    q = parse_query("since=2026-01-01T00:00:00%2B05:30&limit=5")
    assert q.since.utcoffset() == timedelta(hours=5, minutes=30) and q.limit == 5


# --- strict query decoding over real HTTP (raw request targets, so no client normalisation) -------------------------


def raw_get(base: str, target: str, token: str = TOKEN) -> tuple[int, dict]:
    import http.client

    host, port = base.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=5)
    try:
        conn.request("GET", target, headers={"Authorization": f"Bearer {token}"})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())
    finally:
        conn.close()


@pytest.fixture(scope="module")
def unicode_server(tmp_path_factory):
    path = tmp_path_factory.mktemp("unicode") / "u.sqlite3"
    store = Store(path)
    store.migrate()
    for i, task_id in enumerate(("café-1", "100%", "plain")):
        store.conn.execute(
            "INSERT INTO tasks (id, title, stage, status, source, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (task_id, "t", "IMPLEMENT", "OPEN", "local", 1, (BASE + timedelta(minutes=i)).timestamp()),
        )
    store.conn.commit()
    store.close()
    httpd, _ = make_server(path, token=TOKEN)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.parametrize(
    "query",
    [
        "task_id=%FF",  # invalid UTF-8 must not become U+FFFD
        "task_id=%C3",  # truncated multi-byte sequence
        "task_id=%",  # dangling escape
        "task_id=%zz",  # non-hex escape
        "task_id=%2",  # short escape
        "task_id=abc%",
        "%FF=1",  # malformed escape in a parameter name
        "task_id=%00",  # control character
        "task_id=a%0Ab",
        "since=20260101T000000Z",  # ISO 8601 basic format is not RFC 3339
        "since=2026-01-01",  # date only
        "since=2026-01-01T00:00:00",  # no offset
        "since=2026-01-01t00:00:00z",  # lowercase designators
        "since=2026-01-01%2000:00:00Z",  # space instead of T
        "since=2026-01-01T00:00Z",  # no seconds
        "since=2026-01-01T00:00:00+05:30",  # unencoded '+' decodes to a space
        "since=2026-01-01T00:00:00%2B0530",  # offset without colon
        "since=2026-01-01T24:00:00Z",
        "since=2026-01-01T00:00:60Z",  # leap second not representable
        "since=2026-02-30T00:00:00Z",
        "since=2026-01-01T00:00:00%2B24:00",
        "since=2026-01-01T00:00:00.Z",
        "since=2026-01-01T00:00:00.1234567890Z",
        "since=%D9%A2%D9%A0%D9%A2%D9%A6-01-01T00:00:00Z",  # Arabic-Indic digits for the year
        "since=2026-%EF%BC%90%EF%BC%91-01T00:00:00Z",  # fullwidth digits for the month
        "since=2026-01-01T00:00:00.%D9%A5Z",  # non-ASCII fraction digit
        "since=2026-01-01T00:00:00Z%0A",  # trailing newline must not satisfy the pattern
        "limit=%D9%A5",  # non-ASCII digit in an integer
        "offset=%EF%BC%91",
        "since=",
        "task_id=a&&limit=1",  # empty segment
        "limit=1&",
        "task_id",  # no '='
        "=x",
        "task_id=a&task_id=b",
        "task_id=a&%74ask_id=b",  # repeated after decoding the name
        "provider=",
        "bogus=1",
        "café=1".encode().decode("latin-1"),  # raw non-ASCII must be percent-encoded
        "task_id=" + "a" * 3000,
    ],
)
def test_malformed_queries_are_rejected_over_http(unicode_server, query):
    status, body = raw_get(unicode_server, "/api/v1/activity?" + query) if query.isascii() else _raw_bytes(unicode_server, query)
    assert status == 400 and body["error"]["code"] == "invalid_query"
    assert "entries" not in body
    dumped = json.dumps(body)
    assert query not in dumped and "caf" not in dumped and "aaaa" not in dumped  # no echo of request values


def _raw_bytes(base: str, query: str) -> tuple[int, dict]:
    import socket

    host, port = base.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        request = f"GET /api/v1/activity?{query} HTTP/1.1\r\nHost: {host}:{port}\r\nAuthorization: Bearer {TOKEN}\r\nConnection: close\r\n\r\n"
        sock.sendall(request.encode("latin-1"))
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
    head, _, payload = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload)


def test_raw_invalid_utf8_bytes_in_target_are_rejected(unicode_server):
    status, body = _raw_bytes(unicode_server, "task_id=ÿ")
    assert status == 400 and body["error"]["code"] == "invalid_query"


@pytest.mark.parametrize(
    "query,expected",
    [
        ("task_id=caf%C3%A9-1", ["café-1"]),  # valid encoded Unicode
        ("task_id=100%25", ["100%"]),  # valid encoded percent
        ("task_id=plain&limit=5&offset=0", ["plain"]),
        ("task_id=plain&since=2026-01-01T00:00:00Z", ["plain"]),
        ("task_id=plain&since=2026-01-01T05:30:00%2B05:30", ["plain"]),  # plus offset, same instant as 00:00Z
        ("task_id=plain&since=2025-12-31T18:30:00-05:30", ["plain"]),
        ("task_id=plain&since=2026-01-01T00:00:00.000Z&until=2026-01-01T00:05:00.123456789Z", ["plain"]),
        ("task_id=plain&since=2026-01-01T00:03:00Z", []),  # task updated at 00:02 is before the window
        ("task_id=plain&until=2026-01-01T00:02:00Z", []),  # until is exclusive
    ],
)
def test_valid_encodings_and_timestamp_forms_still_work(unicode_server, query, expected):
    status, body = raw_get(unicode_server, "/api/v1/activity?" + query)
    assert status == 200
    assert [e["id"] for e in body["entries"] if e["kind"] == "task"] == expected


def test_decoded_blank_and_literal_plus_follow_form_rules(unicode_server):
    # '+' is a space in form decoding, so it can never smuggle an offset
    status, _ = raw_get(unicode_server, "/api/v1/activity?task_id=a+b")
    assert status == 200  # a task id containing a space is a valid (empty) match, not an error
    assert raw_get(unicode_server, "/api/v1/activity?task_id=")[0] == 400


def test_credential_is_checked_before_query_parsing(unicode_server):
    assert raw_get(unicode_server, "/api/v1/activity?task_id=%FF", token="wrong")[0] == 401  # auth is checked first
