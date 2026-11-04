from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from stagemesh.api_server import ActivityApi, make_server
from stagemesh.operator_control import OperatorControl
from stagemesh.persistence import Store

UI = Path(__file__).resolve().parents[1] / "src" / "stagemesh" / "ui"
TOKEN = "control-browser-token"

DRIVER = r"""
(() => {
  const nativeFetch = window.fetch.bind(window);
  let dropConfirmationResponse = true;
  window.fetch = async (...args) => {
    const response = await nativeFetch(...args);
    const url = String(args[0]);
    if (dropConfirmationResponse && url.includes("/api/v1/control/confirm")) {
      dropConfirmationResponse = false;
      throw new TypeError("simulated response loss after target accepted the action");
    }
    return response;
  };
  const $ = (id) => document.getElementById(id);
  const until = async (check, label) => {
    for (let i = 0; i < 300; i += 1) {
      if (check()) return;
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    throw new Error("timeout: " + label);
  };
  (async () => {
    const result = { errors: [] };
    try {
      await until(() => !$("show-control").hidden, "control capability");
      $("show-control").click();
      $("control-task").value = "blocked-ui-task";
      $("control-plan").focus();
      $("control-form").requestSubmit($("control-plan"));
      await until(() => !$("control-plan-view").hidden, "planned action");
      result.planFocusedConfirm = document.activeElement === $("control-confirm");
      result.exactTarget = $("control-plan-facts").textContent.includes("task:blocked-ui-task");
      result.hasExpiry = $("control-plan-facts").textContent.includes("Expires");
      result.preconditions = $("control-preconditions").children.length;
      $("control-confirm").focus();
      $("control-confirm").click();
      $("control-confirm").click();
      await until(() => $("control-result").textContent.includes("Receipt reconciled"), "receipt reconciliation");
      result.reconciled = true;
      result.confirmDisabled = $("control-confirm").disabled;
      result.liveRegion = $("control-result").getAttribute("aria-live");
    } catch (error) {
      result.errors.push(String(error && error.stack || error));
    }
    const output = document.createElement("pre");
    output.id = "result";
    output.textContent = JSON.stringify(result);
    document.body.append(output);
  })();
})();
"""


def _find_browser() -> str | None:
    override = os.environ.get("STAGEMESH_TEST_BROWSER")
    if override:
        return override
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "chrome",
        "msedge",
        "microsoft-edge",
    ):
        if found := shutil.which(name):
            return found
    for path in (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ):
        if Path(path).is_file():
            return path
    return None


class ControlFixtureApi(ActivityApi):
    def _static(self, path: str):
        if path == "/driver.js":
            _, _, headers = super()._static("/app.js")
            return 200, DRIVER.encode(), headers
        return super()._static(path)


def test_keyboard_semantic_plan_confirm_and_lost_response_reconciliation(tmp_path: Path):
    browser = _find_browser()
    if browser is None:
        if os.environ.get("CI"):
            pytest.fail("a Chromium-family browser is required in CI")
        pytest.skip("no Chromium-family browser available")
    db = tmp_path / "state.sqlite3"
    store = Store(db)
    store.migrate()
    task_id = store.upsert_task("Blocked UI task", source_id="blocked-ui-task")
    store.block_task(task_id)
    store.close()
    site = tmp_path / "site"
    site.mkdir()
    for name in ("app.js", "app.css"):
        shutil.copyfile(UI / name, site / name)
    index = (UI / "index.html").read_text(encoding="utf-8")
    marker = '<script src="/app.js"></script>'
    (site / "index.html").write_text(
        index.replace(marker, '<script src="/driver.js"></script>\n' + marker),
        encoding="utf-8",
    )
    control = OperatorControl("project-ui", "0123456789abcdef")
    httpd, _api = make_server(
        db,
        token=TOKEN,
        static_dir=site,
        api_class=ControlFixtureApi,
        control=control,
    )
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    command = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={tmp_path / 'profile'}",
        "--virtual-time-budget=90000",
        "--dump-dom",
        f"{base}/#token={TOKEN}",
    ]
    if sys.platform.startswith("linux"):
        command.insert(1, "--no-sandbox")
    try:
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=150,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        match = re.search(r'<pre id="result">(.*?)</pre>', process.stdout, re.DOTALL)
        assert match, f"control driver did not finish; browser exit {process.returncode}\n{process.stderr[-800:]}"
        result = json.loads(html.unescape(match.group(1)))
        assert result["errors"] == []
        assert result["planFocusedConfirm"] and result["exactTarget"] and result["hasExpiry"]
        assert result["preconditions"] >= 5
        assert result["reconciled"] and result["confirmDisabled"]
        assert result["liveRegion"] == "assertive"
        check = Store(db)
        try:
            assert check.conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE event_type='operator.control.receipt'"
            ).fetchone()[0] == 1
        finally:
            check.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
