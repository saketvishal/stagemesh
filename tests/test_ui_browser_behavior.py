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
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stagemesh.api_server import UI_ASSETS, ActivityApi, make_server
from stagemesh.block_kinds import BlockKind, record_block
from stagemesh.domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus
from stagemesh.operator_doctor import ProjectDoctor
from stagemesh.operator_providers import ProviderStatusProjection
from stagemesh.persistence import Store

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "src" / "stagemesh" / "ui"
TOKEN = "behaviour-token"

DRIVER = r"""
(() => {
  const scenario = new URLSearchParams(location.search).get("scenario") || "main";
  const NativeDate = Date;
  if (scenario === "periodPresets") {
    window.Date = class FixedDate extends NativeDate {
      constructor(...args) { super(...(args.length ? args : [2026, 0, 15, 12, 0, 0])); }
      static now() { return new NativeDate(2026, 0, 15, 12, 0, 0).getTime(); }
    };
  }
  const realFetch = window.fetch.bind(window);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const control = { held: [], hold: null, fail: false, executionAuth: false, dropKey: null, log: [] };
  const release = () => { control.hold = null; control.held.splice(0).forEach((r) => r()); };
  // Test-only wrapper around the page's real fetch: it can withhold, fail or alter ACTUAL server responses.
  window.fetch = async (url, opts) => {
    const u = String(url);
    const activity = u.includes("/api/v5/activity");
    const periods = u.includes("/api/v5/periods");
    const executionDetail = u.includes("/api/v1/execution");
    if (activity || periods) control.log.push("request " + u);
    if (activity && control.fail) { control.log.push("fail " + u); throw new TypeError("network down"); }
    if (executionDetail && control.executionAuth) {
      return new Response(JSON.stringify({error: {message: "missing or invalid credential"}}), {
        status: 401, headers: {"Content-Type": "application/json"},
      });
    }
    const res = await realFetch(url, opts);
    if (control.hold && control.hold(u)) { control.log.push("held " + u); await new Promise((r) => control.held.push(r)); }
    if (!activity) return res;
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

      document.activeElement.click(); // what Enter does on a focused button: open the task detail
      await until(() => !$("task-view").hidden, "task detail");
      out.detailVisible = !$("task-view").hidden;
      $("detail-history").click();
      await until(() => rows() === 1, "drill-down");
      out.drillRows = rows();
      out.drillTask = $("task").value;
      out.loadedPagesReset = $("more").hidden;
      out.drillVisible = !$("drill").hidden;
      out.allHistories = ["source_history", "task_history", "retry_history", "evidence_history"].every((id) => $(id).checked);

      $("task").value = ""; $("task").dispatchEvent(new Event("change"));
      await until(() => rows() === 100, "filter cleared");
      out.afterClear = rows();
      out.moreVisibleAgain = !$("more").hidden;

      $("state").value = "closed"; $("state").dispatchEvent(new Event("change"));
      await until(() => rows() === 44, "closed filter");
      out.closedRows = rows();
    },

    async v4Filters() {
      await until(() => rows() === 100, "first page");
      $("auto").checked = false; $("auto").dispatchEvent(new Event("change"));
      $("project").value = "team/alpha";
      $("source").value = "github";
      $("time_basis").value = "updated";
      $("task_status").value = "OPEN";
      $("source_status").value = "CLOSED";
      $("task_event").value = "status_changed";
      $("source_history").checked = true;
      $("project").dispatchEvent(new Event("change"));
      await until(() => control.log.some((entry) => entry.includes("task_event=status_changed")), "task filters");
      out.taskUrl = control.log.filter((entry) => entry.includes("task_event=status_changed")).slice(-1)[0];
      $("task_event").value = "";
      $("retry_kind").value = "operator";
      $("retry_state").value = "FAILED";
      $("retry_kind").dispatchEvent(new Event("change"));
      await until(() => control.log.some((entry) => entry.includes("retry_kind=operator")), "retry filters");
      out.retryUrl = control.log.filter((entry) => entry.includes("retry_kind=operator")).slice(-1)[0];
      $("retry_kind").value = "";
      $("retry_state").value = "";
      $("evidence_kind").value = "REVIEW";
      $("evidence_status").value = "FAILED";
      $("evidence_kind").dispatchEvent(new Event("change"));
      await until(() => control.log.some((entry) => entry.includes("evidence_kind=REVIEW")), "evidence filters");
      out.evidenceUrl = control.log.filter((entry) => entry.includes("evidence_kind=REVIEW")).slice(-1)[0];
      out.counts = $("status-counts").textContent;
    },

    async periodPresets() {
      await until(() => rows() === 100, "first page");
      $("auto").checked = false; $("auto").dispatchEvent(new Event("change"));
      $("timezone").value = "UTC";
      $("period").value = "week"; $("period").dispatchEvent(new Event("change"));
      await until(() => control.log.some((entry) => entry.includes("/api/v5/periods")), "week request");
      const week = control.log.filter((entry) => entry.includes("/api/v5/periods")).slice(-1)[0];
      const weekQuery = new URL(week.slice("request ".length), location.href).searchParams;
      const weekStart = new NativeDate(weekQuery.get("since"));
      const weekEnd = new NativeDate(weekQuery.get("until"));
      out.weekStartDay = weekStart.getDay();
      out.weekDays = (weekEnd - weekStart) / 86400000;
      out.weekTimezone = weekQuery.get("timezone");
      $("period").value = "month"; $("period").dispatchEvent(new Event("change"));
      await until(() => control.log.filter((entry) => entry.includes("/api/v5/periods")).length >= 2, "month request");
      const month = control.log.filter((entry) => entry.includes("/api/v5/periods")).slice(-1)[0];
      const monthQuery = new URL(month.slice("request ".length), location.href).searchParams;
      const monthStart = new NativeDate(monthQuery.get("since"));
      const monthEnd = new NativeDate(monthQuery.get("until"));
      out.monthStartDay = monthStart.getDate();
      out.monthEndDay = monthEnd.getDate();
      out.monthsApart = (monthEnd.getFullYear() * 12 + monthEnd.getMonth()) - (monthStart.getFullYear() * 12 + monthStart.getMonth());
    },

    async periods() {
      await until(() => rows() === 100, "first page");
      $("auto").checked = false; $("auto").dispatchEvent(new Event("change"));
      $("period").value = "custom";
      $("since").value = "2026-05-01T00:00";
      $("until").value = "2026-05-05T00:00";
      $("timezone").value = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
      $("bucket").value = "day";
      $("period").dispatchEvent(new Event("change"));
      await until(() => document.querySelectorAll("#interval-rows tr").length === 100, "interval first page");
      out.seriesRows = document.querySelectorAll("#series-rows tr").length;
      out.intervalFirst = document.querySelectorAll("#interval-rows tr").length;
      out.periodMoreVisible = !$("period-more").hidden;
      out.summary = $("period-summary").textContent;
      out.timezone = $("timezone").value;
      $("period-more").focus(); $("period-more").click();
      await until(() => document.querySelectorAll("#interval-rows tr").length === 130, "interval second page");
      out.intervalSecond = document.querySelectorAll("#interval-rows tr").length;
      out.periodMoreHidden = $("period-more").hidden;
      out.focusAfterPeriodMore = document.activeElement === $("interval-table");
      out.stillActive = [...document.querySelectorAll("#interval-rows td")].some((cell) => cell.textContent === "Still active");
      const button = document.querySelector("#interval-rows button");
      button.click();
      await until(() => !$("task-view").hidden, "interval task detail");
      out.detailVisible = !$("task-view").hidden;
      $("detail-history").click();
      await until(() => !$("drill").hidden, "interval task drill-down");
      out.drillVisible = !$("drill").hidden;
      out.allHistories = ["source_history", "task_history", "retry_history", "evidence_history"].every((id) => $(id).checked);
    },

    async status() {
      await until(() => $("operator-summary").textContent.includes("Observed"), "operator snapshot");
      $("show-status").click();
      out.statusVisible = !$("status-view").hidden && $("activity-view").hidden;
      out.summary = $("operator-summary").textContent;
      out.attention = document.querySelectorAll("#attention-rows tr").length;
      out.pending = document.querySelectorAll("#pending-rows tr").length;
      out.active = document.querySelectorAll("#active-rows tr").length;
      out.failures = document.querySelectorAll("#failure-rows tr").length;
      out.selections = document.querySelectorAll("#selection-rows tr").length;
      out.efficiency = $("efficiency-summary").textContent;
      const task = document.querySelector("#failure-rows button");
      task.click();
      await until(
        () => document.querySelectorAll("#detail-timeline tr").length >= 5,
        "status task detail",
      );
      out.detailVisible = !$("task-view").hidden;
      out.detailTimeline = document.querySelectorAll("#detail-timeline tr").length;
      out.detailEvidence = document.querySelectorAll("#detail-evidence tr").length;
      out.detailRetries = document.querySelectorAll("#detail-retries tr").length;
      out.detailDependencies = document.querySelectorAll("#detail-dependencies tr").length;
      out.detailFocus = document.activeElement === $("close-detail");
      document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", bubbles: true}));
      await until(() => !$("status-view").hidden, "status return from task detail");
      out.detailReturnFocus = document.activeElement === task;
      const execution = document.querySelectorAll("#failure-rows button")[1];
      const activeExecution = document.querySelector("#active-rows button[title='Open recorded execution detail']");
      let holdFirstExecution = true;
      control.hold = (u) => {
        if (!u.includes("/api/v1/execution") || !holdFirstExecution) return false;
        holdFirstExecution = false;
        return true;
      };
      execution.click();
      await until(() => control.held.length === 1, "first execution detail held");
      activeExecution.click();
      await until(() => $("execution-facts").textContent.includes("IMPLEMENTATION"), "newer execution detail");
      release();
      await sleep(100);
      out.executionRaceKeptNewer = $("execution-facts").textContent.includes("IMPLEMENTATION");
      document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", bubbles: true}));
      await until(() => !$("status-view").hidden, "status return from raced execution detail");
      out.executionRaceReturnFocus = document.activeElement === activeExecution;
      execution.click();
      await until(() => document.querySelectorAll("#execution-evidence tr").length === 1, "execution detail");
      out.executionVisible = !$("execution-view").hidden;
      out.executionEvidence = document.querySelectorAll("#execution-evidence tr").length;
      out.executionFacts = $("execution-facts").textContent;
      out.executionFocus = document.activeElement === $("close-execution");
      document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", bubbles: true}));
      await until(() => !$("status-view").hidden, "status return from execution detail");
      out.executionReturnFocus = document.activeElement === execution;
      task.click();
      await until(
        () => document.querySelectorAll("#detail-timeline tr").length >= 5,
        "reopened status task detail",
      );
      $("detail-history").click();
      await until(() => !$("drill").hidden, "status task drill-down");
      out.drillVisible = !$("drill").hidden;
      out.activityVisible = !$("activity-view").hidden && $("status-view").hidden;
    },

    async executionAuth() {
      await until(() => $("operator-summary").textContent.includes("Observed"), "operator snapshot");
      $("show-status").click();
      const execution = document.querySelector("#active-rows button[title='Open recorded execution detail']");
      execution.click();
      await until(() => $("execution-facts").textContent.includes("IMPLEMENTATION"), "initial execution detail");
      const facts = $("execution-facts").textContent;
      document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", bubbles: true}));
      await until(() => !$("status-view").hidden, "status return before auth failure");
      control.executionAuth = true;
      execution.click();
      await until(() => $("execution-message").textContent.includes("stale"), "stale execution auth failure");
      out.staleVisible = !$("execution-view").hidden;
      out.staleMessage = $("execution-message").textContent;
      out.staleFactsPreserved = $("execution-facts").textContent === facts;
    },

    async objectives() {
      await until(() => document.querySelectorAll("#objective-rows tr").length === 1, "objective snapshot");
      $("show-objectives").click();
      out.viewVisible = !$("objectives-view").hidden && $("activity-view").hidden;
      out.summary = $("objectives-summary").textContent;
      out.planState = document.querySelector("#objective-rows tr").textContent;
      $("objective-search").value = "does-not-exist";
      $("objective-search").dispatchEvent(new Event("input"));
      out.filteredRows = document.querySelectorAll("#objective-rows tr").length;
      out.emptyMessage = $("objectives-message").textContent;
      $("objective-search").value = "operator objective";
      $("objective-search").dispatchEvent(new Event("input"));
      const objective = document.querySelector("#objective-rows button");
      objective.focus(); objective.click();
      await until(() => !$("objective-detail").hidden, "objective detail");
      out.childRows = document.querySelectorAll("#objective-children tr").length;
      out.detailNote = $("objective-detail-note").textContent;
      let child = document.querySelector("#objective-children button");
      child.focus();
      $("objective-refresh").click();
      await until(() => !child.isConnected, "objective refresh");
      out.childRefreshFocus = !!document.activeElement &&
        document.activeElement.dataset.objectiveChildKey === child.dataset.objectiveChildKey;
      child = document.activeElement;
      child.click();
      await until(() => !$("task-view").hidden, "objective child task detail");
      out.taskVisible = !$("task-view").hidden;
      document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", bubbles: true}));
      await until(() => !$("objectives-view").hidden, "objective return from task detail");
      out.childFocusRestored = document.activeElement === child;
      $("close-objective").click();
      out.objectiveFocusRestored = !!document.activeElement &&
        document.activeElement.dataset.objectiveKey === objective.dataset.objectiveKey;
    },

    async doctor() {
      $("show-doctor").click();
      await until(() => $("doctor-summary").textContent.includes("Verdict"), "doctor snapshot");
      out.viewVisible = !$("doctor-view").hidden && $("activity-view").hidden;
      out.summary = $("doctor-summary").textContent;
      out.attention = document.querySelectorAll("#doctor-attention tr").length;
      out.providers = document.querySelectorAll("#doctor-providers tr").length;
      out.system = $("doctor-system").textContent;
      out.policies = $("doctor-policies").textContent;
      out.providerText = $("doctor-providers").textContent;
      $("doctor-refresh").click();
      await until(() => $("doctor-summary").textContent.includes("Verdict"), "doctor refresh");
      out.refreshed = $("doctor-summary").textContent.includes("Local-only");
    },

    async providers() {
      $("show-providers").click();
      await until(() => $("providers-summary").textContent.includes("Observed"), "provider snapshot");
      out.viewVisible = !$("providers-view").hidden && $("activity-view").hidden;
      out.summary = $("providers-summary").textContent;
      out.initialRows = document.querySelectorAll("#provider-rows tr").length;
      out.initialText = $("provider-rows").textContent;
      $("provider-posture").value = "invoked";
      $("provider-posture").dispatchEvent(new Event("change"));
      out.invokedRows = document.querySelectorAll("#provider-rows tr").length;
      $("provider-search").value = "does-not-exist";
      $("provider-search").dispatchEvent(new Event("input"));
      out.emptyRows = document.querySelectorAll("#provider-rows tr").length;
      out.emptyMessage = $("providers-message").textContent;
      $("provider-search").value = "";
      $("provider-search").dispatchEvent(new Event("input"));
      $("providers-refresh").click();
      await until(() => $("providers-summary").textContent.includes("Observed"), "provider refresh");
      out.refreshed = $("providers-summary").textContent.includes("Provider calls 0");
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
    httpd, _ = make_server(
        db,
        token=TOKEN,
        static_dir=site,
        api_class=FixtureApi,
        doctor=ProjectDoctor(tmp_path),
        provider_status=ProviderStatusProjection(tmp_path),
    )
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def period_base(tmp_path: Path):
    db = tmp_path / "period-ui.sqlite3"
    store = Store(db)
    store.migrate()
    start = datetime(2026, 5, 1, 12, tzinfo=UTC)
    for i in range(130):
        task = store.upsert_task(
            f"Period task {i}", source="local", source_id=f"period-{i}", project="team/alpha"
        )
        instant = (start + timedelta(minutes=i)).timestamp()
        store.conn.execute(
            "UPDATE tasks SET stage='IMPLEMENT', status='OPEN', created_at=?, updated_at=? WHERE id=?",
            (instant, instant, task),
        )
        store.conn.execute(
            "UPDATE audit_events SET created_at=? WHERE event_type='task.created' "
            "AND json_extract(payload, '$.task_id')=?",
            (instant, task),
        )
    store.conn.commit()
    store.close()
    site = tmp_path / "period-site"
    site.mkdir()
    for name in ("app.js", "app.css"):
        shutil.copyfile(UI / name, site / name)
    index = (UI / "index.html").read_text(encoding="utf-8")
    marker = '<script src="/app.js"></script>'
    (site / "index.html").write_text(index.replace(marker, '<script src="/driver.js"></script>\n' + marker), encoding="utf-8")
    httpd, _ = make_server(
        db,
        token=TOKEN,
        static_dir=site,
        api_class=FixtureApi,
        doctor=ProjectDoctor(tmp_path),
        provider_status=ProviderStatusProjection(tmp_path),
    )
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def status_base(tmp_path: Path):
    db = tmp_path / "status-ui.sqlite3"
    store = Store(db)
    store.migrate()
    task = store.upsert_task("Status task", source="local", source_id="status-task")
    dependency = store.upsert_task("Dependency", source="local", source_id="status-dependency")
    store.add_dependency(task, dependency)
    store.add_candidate(task, "a" * 40, "codex", True)
    store.add_evidence(task, "a" * 40, EvidenceKind.REVIEW, EvidenceStatus.FAILED, {"private": "not projected"})
    store.add_audit_event(
        "task.operator_retry_requested",
        {"task_id": task, "stage": "PLAN", "candidate_sha": "a" * 40},
    )
    store.block_task(task)
    record_block(store, task, BlockKind.HUMAN_DECISION, "choose recorded behavior")
    running = store.start_execution(
        task_id=task, claim_id=None, kind=ExecutionKind.IMPLEMENTATION, actor="codex"
    )
    failed = store.start_execution(
        task_id=task, claim_id=None, kind=ExecutionKind.REVIEW, actor="claude", candidate_sha="a" * 40
    )
    store.finish_execution(failed, ExecutionStatus.FAILED, actor="claude", result="review failed")
    store.upsert_worker(
        worker_id="worker-1", provider="codex", capabilities=["IMPLEMENT"], pid=None,
        process_create_time=None, boot_id=None, executable=None, heartbeat_at=1, lease_expires_at=2,
    )
    store.add_audit_event(
        "provider.selection",
        {
            "task_id": task, "stage": "IMPLEMENT", "policy": "weighted", "order": ["codex", "claude"],
            "verdicts": [
                {"provider": "codex", "eligible": True, "reason": "eligible"},
                {"provider": "claude", "eligible": False, "reason": "not configured"},
            ],
        },
    )
    store.save_objective(
        "operator-objective",
        "Operator objective",
        {
            "id": "operator-objective",
            "title": "Operator objective",
            "tasks": [
                {"id": task, "title": "Status task"},
                {"id": dependency, "title": "Dependency", "dependencies": [task]},
                {"id": "future-child", "title": "Future child"},
            ],
        },
    )
    store.close()
    assert running
    site = tmp_path / "status-site"
    site.mkdir()
    for name in ("app.js", "app.css"):
        shutil.copyfile(UI / name, site / name)
    index = (UI / "index.html").read_text(encoding="utf-8")
    marker = '<script src="/app.js"></script>'
    (site / "index.html").write_text(
        index.replace(marker, '<script src="/driver.js"></script>\n' + marker), encoding="utf-8"
    )
    httpd, _ = make_server(
        db,
        token=TOKEN,
        static_dir=site,
        api_class=FixtureApi,
        doctor=ProjectDoctor(tmp_path),
        provider_status=ProviderStatusProjection(tmp_path),
    )
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
    assert result["detailVisible"] is True and result["drillVisible"] is True and result["allHistories"] is True
    assert result["afterClear"] == 100 and result["moreVisibleAgain"] is True
    assert result["closedRows"] == 44  # every third task is DONE


def test_v5_filter_controls_emit_bounded_encoded_queries(fixture_base, tmp_path: Path):
    result = run_scenario(fixture_base, "v4Filters", tmp_path)
    task = result["taskUrl"]
    assert "/api/v5/activity?" in task and "project=team%2Falpha" in task
    assert "source=github" in task and "time_basis=updated" in task
    assert "task_status=OPEN" in task and "source_status=CLOSED" in task
    assert "include_source_history=true" in task and "include_task_history=true" in task
    retry = result["retryUrl"]
    assert "retry_kind=operator" in retry and "retry_state=FAILED" in retry
    assert "include_retry_history=true" in retry
    assert "task_event=" not in retry
    evidence = result["evidenceUrl"]
    assert "evidence_kind=REVIEW" in evidence and "evidence_status=FAILED" in evidence
    assert "include_evidence_history=true" in evidence and "retry_kind=" not in evidence


def test_calendar_presets_emit_this_week_and_this_month_bounds(fixture_base, tmp_path: Path):
    result = run_scenario(fixture_base, "periodPresets", tmp_path)
    assert result["weekStartDay"] == 1 and result["weekDays"] == 7
    assert result["weekTimezone"] == "UTC"
    assert result["monthStartDay"] == 1 and result["monthEndDay"] == 1 and result["monthsApart"] == 1


def test_recorded_series_intervals_pagination_and_drill_down(period_base, tmp_path: Path):
    result = run_scenario(period_base, "periods", tmp_path)
    assert result["seriesRows"] == 4
    assert result["intervalFirst"] == 100 and result["periodMoreVisible"] is True
    assert result["intervalSecond"] == 130 and result["periodMoreHidden"] is True
    assert result["focusAfterPeriodMore"] is True and result["stillActive"] is True
    assert "Timezone " + result["timezone"] in result["summary"] and "history unavailable 0" in result["summary"]
    assert result["detailVisible"] is True and result["drillVisible"] is True and result["allHistories"] is True


def test_status_view_renders_bounded_operator_sections_and_links_to_history(status_base, tmp_path: Path):
    result = run_scenario(status_base, "status", tmp_path)
    assert result["statusVisible"] is True and "Bounded to 100" in result["summary"]
    assert result["attention"] >= 2 and result["pending"] == 2
    assert result["active"] == 2 and result["failures"] == 1 and result["selections"] == 1
    assert "billing cost UNKNOWN" in result["efficiency"]
    assert result["detailVisible"] is True and result["detailTimeline"] >= 5
    assert result["detailEvidence"] == 1 and result["detailRetries"] == 1 and result["detailDependencies"] == 1
    assert result["detailFocus"] is True and result["detailReturnFocus"] is True
    assert result["executionVisible"] is True and result["executionEvidence"] == 1
    assert result["executionFocus"] is True and result["executionReturnFocus"] is True
    assert result["executionRaceKeptNewer"] is True and result["executionRaceReturnFocus"] is True
    assert "REVIEW" in result["executionFacts"] and "FAILED" in result["executionFacts"]
    assert result["drillVisible"] is True and result["activityVisible"] is True


def test_execution_detail_preserves_last_good_snapshot_on_auth_failure(status_base, tmp_path: Path):
    result = run_scenario(status_base, "executionAuth", tmp_path)
    assert result["staleVisible"] is True and result["staleFactsPreserved"] is True
    assert "Could not load execution detail" in result["staleMessage"] and "stale" in result["staleMessage"]


def test_objective_view_filters_drills_into_children_and_restores_focus(status_base, tmp_path: Path):
    result = run_scenario(status_base, "objectives", tmp_path)
    assert result["viewVisible"] is True and "Bounded to 100 objectives" in result["summary"]
    assert "BLOCKED" in result["planState"]
    assert result["filteredRows"] == 0 and "No loaded objectives match" in result["emptyMessage"]
    assert result["childRows"] == 3 and "1 blocked" in result["detailNote"]
    assert result["taskVisible"] is True
    assert result["childRefreshFocus"] is True
    assert result["childFocusRestored"] is True and result["objectiveFocusRestored"] is True


def test_doctor_view_renders_local_configuration_and_provider_boundaries(status_base, tmp_path: Path):
    result = run_scenario(status_base, "doctor", tmp_path)
    assert result["viewVisible"] is True and "Local-only" in result["summary"]
    assert result["attention"] >= 1 and result["providers"] == 4
    assert "Network checkedno" in result["system"] and "Mutation performedno" in result["system"]
    assert "Unattended allowlist configuredno" in result["policies"]
    assert "not authorized" in result["providerText"] and result["refreshed"] is True


def test_provider_view_distinguishes_recorded_invocation_and_never_probes_providers(status_base, tmp_path: Path):
    result = run_scenario(status_base, "providers", tmp_path)
    assert result["viewVisible"] is True and "Provider calls 0" in result["summary"]
    assert result["initialRows"] == 4
    assert "codex" in result["initialText"] and "claude" in result["initialText"]
    assert result["invokedRows"] == 2
    assert result["emptyRows"] == 0 and "No loaded providers match" in result["emptyMessage"]
    assert result["refreshed"] is True


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

        for asset, needle in (("/", b"StageMesh operator"), ("/app.js", b"/api/v5/activity"), ("/app.css", b"[hidden]")):
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
