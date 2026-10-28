from __future__ import annotations

import re
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from stagemesh.api_server import make_server
from stagemesh.persistence import Store

UI = Path(__file__).resolve().parents[1] / "src" / "stagemesh" / "ui"


@pytest.fixture
def base(tmp_path: Path):
    store = Store(tmp_path / "s.sqlite3")
    store.migrate()
    store.close()
    httpd, _ = make_server(tmp_path / "s.sqlite3", token="tok", static_dir=UI)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def fetch(url: str, headers: dict | None = None):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=5) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


@pytest.mark.parametrize("path,kind", [("/", "text/html"), ("/app.js", "text/javascript"), ("/app.css", "text/css")])
def test_shell_is_served_with_strict_headers_and_no_credential(base, path, kind):
    status, body, headers = fetch(base + path)
    assert status == 200 and headers["Content-Type"].startswith(kind) and body
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "path", ["/../cli.py", "/..%2Fcli.py", "/%2e%2e/cli.py", "/app.py", "/sub/app.js", "/missing.js", "/\\app.js"]
)
def test_only_known_asset_names_are_served(base, path):
    assert fetch(base + path)[0] == 404


def test_data_routes_still_require_credential_when_ui_enabled(base):
    assert fetch(base + "/api/v1/activity")[0] == 401
    assert fetch(base + "/api/v1/activity", {"Authorization": "Bearer tok"})[0] == 200


def test_foreign_host_cannot_load_shell(base):
    assert fetch(base + "/", {"Host": "evil.example"})[0] == 403


def test_ui_source_is_csp_safe_and_never_injects_html():
    html = (UI / "index.html").read_text(encoding="utf-8")
    js = (UI / "app.js").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)", html) and "style=" not in html and "onclick" not in html
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "localStorage", "sessionStorage"):
        assert banned not in js, banned
    assert "history.replaceState" in js and "location.hash" in js  # token read once, then removed from the address bar
    assert "Authorization" in js and "/api/v1/activity" in js


def test_ui_covers_contract_states_and_filters():
    html = (UI / "index.html").read_text(encoding="utf-8")
    js = (UI / "app.js").read_text(encoding="utf-8")
    for needle in ("stale", "truncated", "No activity matches", "Could not load", "Loading"):
        assert needle in js, needle
    for stage in ("PLAN", "IMPLEMENT", "VALIDATE", "REVIEW", "INTEGRATE", "DONE"):
        assert f"<option>{stage}</option>" in html
    for marker in ('role="status"', "<caption", 'lang="en"'):
        assert marker in html, marker
    assert 'aria-live="polite"' in html


def test_built_wheel_ships_the_ui_shell(tmp_path: Path):
    import sys
    import zipfile

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    try:
        import build_backend

        name = build_backend.build_wheel(str(tmp_path))
    finally:
        sys.path.remove(str(root))
    with zipfile.ZipFile(tmp_path / name) as wheel:
        names = set(wheel.namelist())
        record = wheel.read(next(n for n in names if n.endswith("RECORD"))).decode()
    for asset in ("index.html", "app.js", "app.css"):
        assert f"stagemesh/ui/{asset}" in names, asset
        assert f"stagemesh/ui/{asset}," in record, asset
    assert not any("__pycache__" in n and "/ui/" in n for n in names)


def test_ui_preserves_focus_and_loaded_range_on_refresh():
    js = (UI / "app.js").read_text(encoding="utf-8")
    assert "dataset.key" in js and ".focus()" in js  # focused row control restored after re-render
    assert "loadedPages" in js and "const refresh = () => load(false, loadedPages)" in js
    assert 'tabindex="-1"' in (UI / "index.html").read_text(encoding="utf-8")
