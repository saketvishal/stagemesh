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

from stagemesh.api_server import UI_ASSETS, ActivityApi, make_server
from stagemesh.persistence import Store

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "src" / "stagemesh" / "ui"
TOKEN = "behaviour-token"

DRIVER = r"""
(() => {
  const scenario = new URLSearchParams(location.search).get("scenario") || "main";
  const realFetch = window.fetch.bind(window);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const control = { held: [], hold: null, fail: false, dropKey: null, log: [] };
  const release = () => { control.hold = null; control.held.splice(0).forEach((r) => r()); };
  // Test-only wrapper around the page's real fetch: it can withhold, fail or alter ACTUAL server responses.
  window.fetch = async (url, opts) => {
    const u = String(url);
    const activity = u.includes("/api/v1/activity");
    if (activity && control.fail) { control.log.push("fail " + u); throw new TypeError("network down"); }
    const res = await realFetch(url, opts);
    if (!activity) return res;
    if (control.hold && control.hold(u)) { control.log.push("held " + u); await new Promise((r) => control.held.push(r)); }
    if (control.dropKey) {
      const body = await res.json();
      body.entries = body.entries.filter((e) => e.kind + ":" + e.id !== control.dropKey);
      return new Response(JSON.stringify(body), { status: res.status, headers: { "Content-Type": "application/json" } });
    }
    return res;
  };

  const $ = (id) => document.getElementById(id);
  const rows = () => document.querySelectorAll("#rows tr").length;
  const out = { errors: [] };
  const until = async (fn, label) => {
    for (let i = 0; i < 400; i++) { if (fn()) return true; await sleep(25); }
    out.errors.push("timeout: " + label); return false;
  };

  const scenarios = {
    async main() {
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
    },

    // Load more is accepted but its second-page response is still in flight when a refresh happens.
    async pending() {
      await until(() => rows() === 100, "first page");
      control.hold = (u) => u.includes("offset=100");
      $("more").click();
      await until(() => control.held.length >= 1, "second page withheld");
      out.heldBeforeRefresh = control.held.length;
      out.rowsWhileHeld = rows();
      $("refresh").click();
      await sleep(200); // the refresh makes its own requests while the append is still withheld
      out.rowsBeforeRelease = rows();
      release();
      await until(() => rows() === 130, "130 rows after release");
      out.rows = rows();
      await sleep(500);
      out.rowsLater = rows();
      out.moreHidden = $("more").hidden;
      out.log = control.log;
    },

    // Only the first second-page request is withheld, so the refresh's own second-page request completes first and the
    // withheld (now stale) append is released afterwards. Adapted from an independent native probe.
    async pendingOnce() {
      await until(() => rows() === 100, "first page");
      $("auto").checked = false; $("auto").dispatchEvent(new Event("change"));
      let used = false;
      control.hold = (u) => { if (u.includes("offset=100") && !used) { used = true; return true; } return false; };
      $("more").focus(); $("more").click();
      await until(() => control.held.length >= 1, "second page withheld");
      $("refresh").dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await until(() => rows() === 130, "refresh completes the range itself");
      out.rowsBeforeRelease = rows();
      release();
      await sleep(300); // the stale append resolves late and must not change the result
      out.rows = rows();
      out.moreHidden = $("more").hidden;
      out.log = control.log;
    },

    // An automatic poll arrives while the accepted append is withheld.
    async pendingPoll() {
      await until(() => rows() === 100, "first page");
      control.hold = (u) => u.includes("offset=100");
      $("more").click();
      await until(() => control.held.length >= 1, "second page withheld");
      await sleep(11000); // longer than the 10 s poll interval, in virtual time
      release();
      await until(() => rows() === 130, "130 rows after release");
      out.rows = rows();
      await sleep(500);
      out.rowsLater = rows();
    },

    async failure() {
      await until(() => rows() === 100, "first page");
      $("more").click();
      await until(() => rows() === 130, "second page");
      control.fail = true;
      $("refresh").click();
      await until(() => $("message").textContent.startsWith("Could not load"), "failure message");
      out.failMessage = $("message").textContent;
      out.rowsAfterFailure = rows();
      out.moreHiddenAfterFailure = $("more").hidden;
      control.fail = false;
      $("refresh").click();
      await until(() => $("message").textContent === "", "recovered");
      out.recoveredMessage = $("message").textContent;
      out.rowsAfterRecovery = rows();
      await sleep(300);
      out.rowsFinal = rows();
    },

    // A failed Load more must not leave a phantom requested range behind.
    async failedAppend() {
      await until(() => rows() === 100, "first page");
      control.fail = true;
      $("more").click();
      await until(() => $("message").textContent.startsWith("Could not load"), "failure message");
      out.rowsAfterFailedAppend = rows();
      control.fail = false;
      $("refresh").click();
      await until(() => $("message").textContent === "", "recovered");
      out.rowsAfterRefresh = rows();
      out.moreStillOffered = !$("more").hidden;
    },

    // The focused row is absent from the next response.
    async vanish() {
      await until(() => rows() === 100, "first page");
      const btn = document.querySelector("#rows button");
      control.dropKey = btn.dataset.key;
      btn.focus();
      out.focusedBefore = document.activeElement === btn;
      $("refresh").click();
      await until(() => !btn.isConnected, "rows rebuilt");
      out.rowsAfter = rows();
      out.rowGone = !document.querySelector('[data-key="' + control.dropKey + '"]');
      out.focusIsTable = document.activeElement === $("table");
      out.activeTag = document.activeElement.tagName;
      out.tableFocusable = $("table").tabIndex === -1;
    },

    // The focused row is on the second page and is absent from the next response.
    async vanishSecondPage() {
      await until(() => rows() === 100, "first page");
      $("more").click();
      await until(() => rows() === 130, "second page");
      const btn = [...document.querySelectorAll("#rows button")].find((b) => b.dataset.key === "task:task-120");
      control.dropKey = btn.dataset.key;
      btn.focus();
      out.focusedBefore = document.activeElement === btn;
      $("refresh").click();
      await until(() => !btn.isConnected, "rows rebuilt");
      out.rowsAfter = rows();
      out.focusIsTable = document.activeElement === $("table");
    },
  };

  window.addEventListener("error", (e) => out.errors.push("page error: " + e.message));
  window.addEventListener("load", async () => {
    try { await scenarios[scenario](); } catch (e) { out.errors.push(String(e)); }
    const pre = document.createElement("pre"); pre.id = "result"; pre.textContent = JSON.stringify(out);
    document.body.append(pre);
  });
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


class FixtureApi(ActivityApi):
    """Test-only: serves the driver script. The production allowlist (UI_ASSETS) is left untouched."""

    def _static(self, path: str):
        if path == "/driver.js":
            _, _, headers = super()._static("/app.js")
            return 200, DRIVER.encode(), headers
        return super()._static(path)


@pytest.fixture
def fixture_base(tmp_path: Path):
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
    marker = '<script src="/app.js"></script>'
    assert marker in index
    # The driver must run before app.js so its fetch wrapper is in place; the production page is otherwise unchanged.
    (site / "index.html").write_text(index.replace(marker, '<script src="/driver.js"></script>\n' + marker), encoding="utf-8")
    httpd, _ = make_server(db, token=TOKEN, static_dir=site, api_class=FixtureApi)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def run_scenario(base: str, scenario: str, tmp_path: Path) -> dict:
    browser = _find_browser()
    if browser is None:
        if os.environ.get("CI"):
            pytest.fail("a Chromium-family browser is required in CI")
        pytest.skip("no Chromium-family browser available")
    command = [
        browser, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
        f"--user-data-dir={tmp_path / ('profile-' + scenario)}", "--virtual-time-budget=90000", "--dump-dom",
        f"{base}/?scenario={scenario}#token={TOKEN}",
    ]
    if sys.platform.startswith("linux"):
        command.insert(1, "--no-sandbox")
    proc = subprocess.run(command, capture_output=True, text=True, timeout=150, encoding="utf-8", errors="replace", check=False)
    match = re.search(r'<pre id="result">(.*?)</pre>', proc.stdout, re.DOTALL)
    assert match, f"driver did not finish; browser exit {proc.returncode}\n{proc.stderr[-800:]}"
    result = json.loads(html.unescape(match.group(1)))
    assert result["errors"] == [], result
    return result


def test_focus_paging_and_filters_in_a_real_browser(fixture_base, tmp_path: Path):
    result = run_scenario(fixture_base, "main", tmp_path)
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


def test_accepted_load_more_survives_a_refresh_while_its_response_is_withheld(fixture_base, tmp_path: Path):
    result = run_scenario(fixture_base, "pending", tmp_path)
    assert result["heldBeforeRefresh"] >= 1 and result["rowsWhileHeld"] == 100  # the real second page was withheld
    assert result["rows"] == 130 and result["rowsLater"] == 130 and result["moreHidden"] is True


def test_stale_withheld_append_released_after_the_refresh_completed_changes_nothing(fixture_base, tmp_path: Path):
    result = run_scenario(fixture_base, "pendingOnce", tmp_path)
    assert result["rowsBeforeRelease"] == 130 and result["rows"] == 130 and result["moreHidden"] is True
    assert sum(1 for entry in result["log"] if entry.startswith("held")) == 1


def test_focus_falls_back_to_the_table_when_a_second_page_row_disappears(fixture_base, tmp_path: Path):
    result = run_scenario(fixture_base, "vanishSecondPage", tmp_path)
    assert result["focusedBefore"] is True and result["rowsAfter"] == 129 and result["focusIsTable"] is True


def test_accepted_load_more_survives_an_automatic_poll_while_its_response_is_withheld(fixture_base, tmp_path: Path):
    result = run_scenario(fixture_base, "pendingPoll", tmp_path)
    assert result["rows"] == 130 and result["rowsLater"] == 130


def test_failed_refresh_keeps_the_last_good_range_and_recovers(fixture_base, tmp_path: Path):
    result = run_scenario(fixture_base, "failure", tmp_path)
    assert "stale" in result["failMessage"] and "network down" in result["failMessage"]
    assert result["rowsAfterFailure"] == 130 and result["moreHiddenAfterFailure"] is True
    assert result["recoveredMessage"] == "" and result["rowsAfterRecovery"] == 130 and result["rowsFinal"] == 130


def test_failed_load_more_leaves_no_phantom_range(fixture_base, tmp_path: Path):
    result = run_scenario(fixture_base, "failedAppend", tmp_path)
    assert result["rowsAfterFailedAppend"] == 100
    assert result["rowsAfterRefresh"] == 100 and result["moreStillOffered"] is True


def test_focus_falls_back_to_the_table_when_the_focused_row_disappears(fixture_base, tmp_path: Path):
    result = run_scenario(fixture_base, "vanish", tmp_path)
    assert result["focusedBefore"] is True and result["rowGone"] is True and result["rowsAfter"] == 99
    assert result["tableFocusable"] is True and result["focusIsTable"] is True, result["activeTag"]


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
        installed_ui = sorted(p.name for p in (target / "stagemesh" / "ui").iterdir())
        assert installed_ui == sorted(UI_ASSETS)  # no extra, map, dependency or vendor files shipped
        for absent in ("/driver.js", "/extra.js", "/app.js.map", "/vendor/lib.js", "/secret.txt"):
            assert get(absent)[0] == 404, absent
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
