from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stagemesh.activity import MAX_LIMIT
from stagemesh.api_server import make_server, parse_query
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
