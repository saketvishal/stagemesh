from __future__ import annotations

import re
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from stagemesh.api_server import UI_ASSETS, make_server
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


CANARIES = (
    "extra.js", "extra.css", "canary.css", "notes.html", "app.js.map", "secret.txt", "dependency.js", "driver.js",
    "vendor/lib.js", "node_modules/private.js",
)


def _canary_site(tmp_path: Path) -> Path:
    site = tmp_path / "canary-site"
    site.mkdir()
    for name in UI_ASSETS:
        (site / name).write_bytes((UI / name).read_bytes())
    for name in CANARIES:
        (site / name).parent.mkdir(parents=True, exist_ok=True)
        (site / name).write_text("PRIVATE-TEST-CANARY " + name, encoding="utf-8")
    return site


def test_production_ui_directory_contains_exactly_the_named_assets():
    assert sorted(p.name for p in UI.iterdir() if p.name != "__pycache__") == sorted(UI_ASSETS)
    import importlib.util

    spec = importlib.util.spec_from_file_location("ui_assets_build_backend", Path(__file__).resolve().parents[1] / "build_backend.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert tuple(module.UI_ASSETS) == tuple(UI_ASSETS)


def test_server_serves_only_the_named_assets_even_when_other_files_exist(tmp_path: Path):
    site = _canary_site(tmp_path)
    store = Store(tmp_path / "c.sqlite3")
    store.migrate()
    store.close()
    httpd, _ = make_server(tmp_path / "c.sqlite3", token="tok", static_dir=site)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        for name in UI_ASSETS:
            assert fetch(f"{base}/{name}")[0] == 200, name
        assert fetch(f"{base}/")[0] == 200
        for name in CANARIES:
            status, body, _ = fetch(f"{base}/{name}")
            assert status == 404 and b"CANARY" not in body, name
            assert fetch(f"{base}/{name}", {"Authorization": "Bearer tok"})[0] == 404, name  # auth does not unlock them
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_wheel_build_excludes_files_that_are_not_declared_assets(tmp_path: Path):
    import importlib.util
    import shutil
    import zipfile

    root = Path(__file__).resolve().parents[1]
    tree = tmp_path / "tree"
    shutil.copytree(root / "src", tree / "src", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copyfile(root / "build_backend.py", tree / "build_backend.py")
    for name in CANARIES:
        target = tree / "src" / "stagemesh" / "ui" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("CANARY-" + name, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("canary_build_backend", tree / "build_backend.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out = tmp_path / "out"
    out.mkdir()
    wheel_name = module.build_wheel(str(out))
    with zipfile.ZipFile(out / wheel_name) as wheel:
        names = set(wheel.namelist())
        record = wheel.read(next(n for n in names if n.endswith("RECORD"))).decode()
    shipped = sorted(n for n in names if n.startswith("stagemesh/ui/"))
    assert shipped == sorted(f"stagemesh/ui/{name}" for name in UI_ASSETS)
    assert not [n for n in names if "CANARY" in wheel_text(wheel_name, out, n)]
    assert "extra.js" not in record and "secret.txt" not in record


def wheel_text(wheel_name: str, out: Path, member: str) -> str:
    import zipfile

    with zipfile.ZipFile(out / wheel_name) as wheel:
        return wheel.read(member).decode("utf-8", errors="ignore")


def test_build_fails_if_a_declared_asset_is_missing(tmp_path: Path):
    import importlib.util
    import shutil

    root = Path(__file__).resolve().parents[1]
    tree = tmp_path / "tree"
    shutil.copytree(root / "src", tree / "src", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copyfile(root / "build_backend.py", tree / "build_backend.py")
    (tree / "src" / "stagemesh" / "ui" / "app.css").unlink()
    spec = importlib.util.spec_from_file_location("missing_build_backend", tree / "build_backend.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "out").mkdir()
    with pytest.raises(FileNotFoundError, match="app.css"):
        module.build_wheel(str(tmp_path / "out"))


def test_ci_runs_the_ui_asset_and_browser_modules_on_both_platforms():
    workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    windows, _, linux = workflow.partition("\n  linux:")
    for module in ("test_ui_assets.py", "test_ui_browser_behavior.py"):
        assert module in windows and module in linux, module
