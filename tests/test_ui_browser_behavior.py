"""Behavioural checks of the operator UI in a real headless Chromium-family browser, and of the installed wheel.

The page under test is the shipped index.html/app.js/app.css (byte copies) plus a driver script that acts like a keyboard user
and records DOM facts. Fixtures are throwaway SQLite files on an ephemeral loopback port; no project database is read.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

from stagemesh.api_server import make_server
from stagemesh.persistence import Store

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "src" / "stagemesh" / "ui"
TOKEN = "behaviour-token"

DRIVER = r"""
(async () => {
  const out = { errors: [] };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const $ = (id) => document.getElementById(id);
  const rows = () => document.querySelectorAll("#rows tr").length;
  const until = async (fn, label) => {
    for (let i = 0; i < 400; i++) { if (fn()) return true; await sleep(25); }
    out.errors.push("timeout: " + label); return false;
  };
  window.addEventListener("error", (e) => out.errors.push("page error: " + e.message));
  try {
    await until(() => rows() === 100, "first page");
    out.first = rows();
    out.hashRemoved = location.hash === "";
    $("more").focus(); $("more").click();
    await until(() => rows() === 130, "second page");
    out.afterMore = rows();
    out.moreHidden = $("more").hidden;
    out.focusAfterMoreVanished = document.activeElement === $("table");

    const btn = document.querySelector("#rows button");
    const key = btn.dataset.key;
    btn.focus();
    out.focusedBeforeRefresh = document.activeElement === btn;
    $("refresh").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await until(() => !btn.isConnected, "rows rebuilt");
    out.rowsRebuilt = !btn.isConnected;
    out.focusKept = !!document.activeElement && document.activeElement.dataset.key === key;
    out.rowsAfterRefresh = rows();

    document.activeElement.click(); // what Enter does on a focused button: drill into the task
    await until(() => rows() === 1, "drill-down");
    out.drillRows = rows();
    out.drillTask = $("task").value;
    out.loadedPagesReset = $("more").hidden;

    $("task").value = ""; $("task").dispatchEvent(new Event("change"));
    await until(() => rows() === 100, "filter cleared");
    out.afterClear = rows();
    out.moreVisibleAgain = !$("more").hidden;

    $("state").value = "closed"; $("state").dispatchEvent(new Event("change"));
    await until(() => rows() === 44, "closed filter");
    out.closedRows = rows();
  } catch (e) { out.errors.push(String(e)); }
  const pre = document.createElement("pre"); pre.id = "result"; pre.textContent = JSON.stringify(out);
  document.body.append(pre);
})();
"""


def _find_browser() -> str | None:
    override = os.environ.get("STAGEMESH_TEST_BROWSER")
    if override:
        return override
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome", "msedge", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    for path in (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ):
        if Path(path).is_file():
            return path
    return None


@pytest.fixture
def fixture_server(tmp_path: Path):
    db = tmp_path / "ui.sqlite3"
    store = Store(db)
    store.migrate()
    for i in range(130):
        store.conn.execute(
            "INSERT INTO tasks (id, title, stage, status, source, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (f"task-{i}", f"Demo {i}", "IMPLEMENT" if i % 3 else "DONE", "DONE" if i % 3 == 0 else "OPEN", "local", 1, 1_800_000_000 - i),
        )
    store.conn.commit()
    store.close()
    site = tmp_path / "site"
    site.mkdir()
    for name in ("app.js", "app.css"):
        shutil.copyfile(UI / name, site / name)
    index = (UI / "index.html").read_text(encoding="utf-8")
    assert "</body>" in index
    (site / "index.html").write_text(index.replace("</body>", '<script src="/driver.js"></script>\n</body>'), encoding="utf-8")
    (site / "driver.js").write_text(DRIVER, encoding="utf-8")
    httpd, _ = make_server(db, token=TOKEN, static_dir=site)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/#token={TOKEN}"
    httpd.shutdown()
    httpd.server_close()


def test_focus_paging_and_filters_in_a_real_browser(fixture_server, tmp_path: Path):
    browser = _find_browser()
    if browser is None:
        if os.environ.get("CI"):
            pytest.fail("a Chromium-family browser is required in CI")
        pytest.skip("no Chromium-family browser available")
    profile = tmp_path / "profile"
    command = [
        browser, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
        f"--user-data-dir={profile}", "--virtual-time-budget=60000", "--dump-dom", fixture_server,
    ]
    if sys.platform.startswith("linux"):
        command.insert(1, "--no-sandbox")
    proc = subprocess.run(command, capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace", check=False)
    match = re.search(r'<pre id="result">(.*?)</pre>', proc.stdout, re.DOTALL)
    assert match, f"driver did not finish; browser exit {proc.returncode}\n{proc.stderr[-800:]}"
    result = json.loads(html.unescape(match.group(1)))
    assert result["errors"] == [], result
    assert result["first"] == 100 and result["hashRemoved"] is True
    # Load more appends the second page, and the vanished button hands focus to the table
    assert result["afterMore"] == 130 and result["moreHidden"] is True and result["focusAfterMoreVanished"] is True
    # A refresh rebuilds every row yet keeps the loaded range and the focused control
    assert result["focusedBeforeRefresh"] and result["rowsRebuilt"] and result["focusKept"]
    assert result["rowsAfterRefresh"] == 130
    # Activating the focused button drills into that task and resets paging
    assert result["drillRows"] == 1 and result["drillTask"] == "task-0" and result["loadedPagesReset"] is True
    assert result["afterClear"] == 100 and result["moreVisibleAgain"] is True
    assert result["closedRows"] == 44  # every third task is DONE


def test_installed_wheel_serves_its_own_ui_and_requires_auth(tmp_path: Path):
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    sys.path.insert(0, str(ROOT))
    try:
        import build_backend

        name = build_backend.build_wheel(str(wheels))
    finally:
        sys.path.remove(str(ROOT))
    target = tmp_path / "site-packages"
    with zipfile.ZipFile(wheels / name) as wheel:
        wheel.extractall(target)  # a wheel is a zip laid out as site-packages
    project = tmp_path / "project"
    project.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
    env["PYTHONPATH"] = str(target)
    proc = subprocess.Popen(
        [
            sys.executable, "-I", "-c",
            (
                "import sys; sys.path.insert(0, sys.argv[1]); sys.argv = ['stagemesh'] + sys.argv[2:]; "
                "import stagemesh, stagemesh.cli as c; print(stagemesh.__file__, flush=True); sys.exit(c.main())"
            ),
            str(target), "--project", str(project), "serve", "--json",
        ],
        cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        module_path = proc.stdout.readline().strip()
        assert Path(module_path).resolve().is_relative_to(target.resolve()), module_path  # not the source checkout
        info = json.loads(proc.stdout.readline())
        base = f"http://127.0.0.1:{info['port']}"

        def get(path: str, token: str | None = None):
            req = urllib.request.Request(base + path)
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read()

        for asset, needle in (("/", b"StageMesh activity"), ("/app.js", b"/api/v1/activity"), ("/app.css", b"[hidden]")):
            status, body = get(asset)
            assert status == 200 and needle in body, asset
            assert body == (UI / ("index.html" if asset == "/" else asset.lstrip("/"))).read_bytes(), asset
        assert get("/api/v1/health")[0] == 401
        status, body = get("/api/v1/health", info["token"])
        assert status == 200 and json.loads(body)["status"] == "ok"
        status, body = get("/api/v1/activity", info["token"])
        assert status == 503 and not list(project.rglob("*.sqlite3"))  # no store: unavailable, and nothing created
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()
