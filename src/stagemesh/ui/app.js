"use strict";
// Read-only client for activity V5 and operator V1 (docs/operator-ui-contract.md). The token lives in memory only.
(() => {
  const params = new URLSearchParams(location.hash.slice(1));
  const token = params.get("token") || "";
  history.replaceState(null, "", location.pathname + location.search);
  const $ = (id) => document.getElementById(id);
  const COLUMNS = [
    ["at", "Time"], ["kind", "Kind"], ["state", "State"], ["task_id", "Task"], ["stage", "Stage"],
    ["current_task_status", "Task status"], ["source_status", "Source status"],
    ["provider", "Provider"], ["title", "Title / detail"], ["id", "Id"],
  ];
  const POLL_MS = 10000;
  const MAX_PAGES = 10;
  let rows = new Map(), intervalRows = new Map(), statusSnapshot = null;
  let nextOffset = null, periodNextOffset = null;
  let sort = { key: "at", dir: -1 };
  let lastGood = null, timer = null, generation = 0;
  let loadedPages = 1, wantedPages = 1, periodLoadedPages = 1, periodWantedPages = 1;
  let inflight = false, drillState = null;
  let detailSnapshot = null, detailTaskId = null, detailGeneration = 0;
  let detailReturnView = "activity", detailOpener = null;

  function setMessage(text, level) {
    const el = $("message");
    el.textContent = text;
    el.className = level || "";
  }

  function periodRange() {
    const now = new Date();
    const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const days = (n) => new Date(midnight.getFullYear(), midnight.getMonth(), midnight.getDate() + n);
    switch ($("period").value) {
      case "today": return [midnight, days(1)];
      case "yesterday": return [days(-1), midnight];
      case "7d": return [days(-6), days(1)];
      case "30d": return [days(-29), days(1)];
      case "week": {
        const mondayOffset = (midnight.getDay() + 6) % 7;
        return [days(-mondayOffset), days(7 - mondayOffset)];
      }
      case "month": return [
        new Date(midnight.getFullYear(), midnight.getMonth(), 1),
        new Date(midnight.getFullYear(), midnight.getMonth() + 1, 1),
      ];
      case "custom": {
        const since = $("since").value, until = $("until").value;
        return [since ? new Date(since) : null, until ? new Date(until) : null];
      }
      default: return [null, null];
    }
  }

  function addSharedFilters(query) {
    if ($("task").value.trim()) query.set("task_id", $("task").value.trim());
    if ($("project").value.trim()) query.set("project", $("project").value.trim());
    if ($("source").value.trim()) query.set("source", $("source").value.trim());
  }

  function queryString(offset) {
    const query = new URLSearchParams();
    for (const key of [
      "state", "stage", "time_basis", "source_status", "task_status", "task_event", "retry_kind", "retry_state",
      "evidence_kind", "evidence_status",
    ]) if ($(key).value) query.set(key, $(key).value);
    addSharedFilters(query);
    if ($("provider").value.trim()) query.set("provider", $("provider").value.trim());
    if ($("source_history").checked) query.set("include_source_history", "true");
    if ($("task_history").checked || $("task_event").value) query.set("include_task_history", "true");
    if ($("retry_history").checked || $("retry_kind").value || $("retry_state").value) query.set("include_retry_history", "true");
    if ($("evidence_history").checked || $("evidence_kind").value || $("evidence_status").value) query.set("include_evidence_history", "true");
    const [since, until] = periodRange();
    if (since && !isNaN(since)) query.set("since", since.toISOString());
    if (until && !isNaN(until)) query.set("until", until.toISOString());
    query.set("limit", "100");
    if (offset) query.set("offset", String(offset));
    return query.toString();
  }

  function periodQueryString(offset) {
    const [since, until] = periodRange();
    if (!since || !until || isNaN(since) || isNaN(until)) return null;
    const query = new URLSearchParams({
      since: since.toISOString(), until: until.toISOString(),
      bucket: $("bucket").value, timezone: $("timezone").value.trim(), limit: "100",
    });
    addSharedFilters(query);
    if (offset) query.set("offset", String(offset));
    return query.toString();
  }

  async function api(path) {
    const response = await fetch(path, { headers: { Authorization: "Bearer " + token }, cache: "no-store" });
    let body = null;
    try { body = await response.json(); } catch (_) { /* non-JSON error */ }
    if (!response.ok) {
      const error = new Error(body && body.error ? body.error.message : "HTTP " + response.status);
      error.status = response.status;
      throw error;
    }
    return body;
  }

  async function fetchActivity(target, mine) {
    const fresh = new Map();
    let offset = 0, page, fetched = 0;
    do {
      page = await api("/api/v5/activity?" + queryString(offset));
      if (mine !== generation) return null;
      for (const entry of page.entries) fresh.set(entry.kind + ":" + entry.id, entry);
      offset = page.next_offset;
      fetched += 1;
    } while (fetched < target && offset !== null);
    return { fresh, page, fetched };
  }

  async function fetchPeriods(target, mine) {
    if (periodQueryString(0) === null) return { disabled: true, fresh: new Map(), report: null, fetched: 0 };
    const fresh = new Map();
    let offset = 0, report, fetched = 0;
    do {
      report = await api("/api/v5/periods?" + periodQueryString(offset));
      if (mine !== generation) return null;
      for (const interval of report.active_intervals) fresh.set(interval.task_id + ":" + interval.active_from, interval);
      offset = report.next_offset;
      fetched += 1;
    } while (fetched < target && offset !== null);
    return { disabled: false, fresh, report, fetched };
  }

  async function loadRange(quiet) {
    const mine = ++generation;
    if (!token) { setMessage("No access token. Open the URL printed by `stagemesh serve`.", "error"); return; }
    inflight = true;
    if (!quiet) setMessage("Loading...");
    const activityTarget = wantedPages, periodTarget = periodWantedPages;
    try {
      const [activity, periods, status] = await Promise.all([
        fetchActivity(activityTarget, mine), fetchPeriods(periodTarget, mine), api("/api/v1/operator"),
      ]);
      if (mine !== generation || activity === null || periods === null) return;
      rows = activity.fresh;
      nextOffset = activity.page.next_offset;
      loadedPages = activity.fetched;
      wantedPages = activity.fetched;
      intervalRows = periods.fresh;
      periodNextOffset = periods.report ? periods.report.next_offset : null;
      periodLoadedPages = periods.fetched || 1;
      periodWantedPages = periodLoadedPages;
      statusSnapshot = status;
      lastGood = new Date();
      render(activity.page, periods);
    } catch (error) {
      if (mine !== generation) return;
      wantedPages = loadedPages;
      periodWantedPages = periodLoadedPages;
      const stale = lastGood ? " Showing data from " + lastGood.toLocaleTimeString() + " (stale)." : "";
      setMessage("Could not load activity, periods, or status: " + error.message + "." + stale, error.status === 400 ? "error" : "warn");
      if ([400, 401, 403].includes(error.status)) {
        $("auto").checked = false;
        schedule();
      }
      if (error.status === 401 || error.status === 403) {
        rows = new Map(); intervalRows = new Map(); statusSnapshot = null;
        renderRows(); renderIntervals(); renderStatus();
      }
    } finally {
      if (mine === generation) inflight = false;
    }
  }

  const refresh = () => loadRange(false);
  const reload = () => { wantedPages = 1; periodWantedPages = 1; loadRange(false); };
  const loadMore = () => {
    if (nextOffset === null) return;
    wantedPages = Math.min(wantedPages + 1, MAX_PAGES);
    loadRange(false);
  };
  const loadMorePeriods = () => {
    if (periodNextOffset === null) return;
    periodWantedPages = Math.min(periodWantedPages + 1, MAX_PAGES);
    loadRange(false);
  };

  function cmp(a, b) {
    const x = a[sort.key], y = b[sort.key];
    if (x === y) return 0;
    if (x === null || x === undefined) return 1;
    if (y === null || y === undefined) return -1;
    return (x < y ? -1 : 1) * sort.dir;
  }

  function cell(row, text, cls) {
    const td = row.insertCell();
    td.textContent = text === null || text === undefined ? "-" : String(text);
    if (cls) td.className = cls;
    return td;
  }

  function enterDrill(taskId) {
    if (!drillState) {
      drillState = {};
      for (const id of [
        "task", "provider", "task_event", "retry_kind", "retry_state", "evidence_kind", "evidence_status",
        "source_history", "task_history", "retry_history", "evidence_history",
      ]) drillState[id] = $(id).type === "checkbox" ? $(id).checked : $(id).value;
    }
    $("task").value = taskId;
    $("provider").value = "";
    for (const id of ["task_event", "retry_kind", "retry_state", "evidence_kind", "evidence_status"]) $(id).value = "";
    for (const id of ["source_history", "task_history", "retry_history", "evidence_history"]) $(id).checked = true;
    $("drill-task").textContent = taskId;
    $("drill").hidden = false;
    setView("activity");
    reload();
  }

  async function openTask(taskId, opener) {
    const mine = ++detailGeneration;
    detailReturnView = $("status-view").hidden ? "activity" : "status";
    detailOpener = opener || document.activeElement;
    detailTaskId = taskId;
    $("show-task").hidden = false;
    $("show-task").textContent = "Task " + taskId;
    $("detail-heading").textContent = "Task " + taskId;
    $("detail-message").textContent = "Loading task detail...";
    setView("task");
    try {
      const detail = await api("/api/v1/task?task_id=" + encodeURIComponent(taskId));
      if (mine !== detailGeneration) return;
      detailSnapshot = detail;
      renderTaskDetail();
      $("close-detail").focus();
    } catch (error) {
      if (mine !== detailGeneration) return;
      const stale = detailSnapshot && detailSnapshot.task.id === taskId ? " Showing the last recorded detail (stale)." : "";
      $("detail-message").textContent = "Could not load task detail: " + error.message + "." + stale;
      if (!stale) detailSnapshot = null;
      renderTaskDetail();
    }
  }

  function closeTask() {
    detailGeneration += 1;
    setView(detailReturnView);
    const opener = detailOpener;
    detailOpener = null;
    if (opener && opener.isConnected) opener.focus();
  }

  function exitDrill() {
    if (!drillState) return;
    for (const [id, value] of Object.entries(drillState)) {
      if ($(id).type === "checkbox") $(id).checked = value;
      else $(id).value = value;
    }
    drillState = null;
    $("drill").hidden = true;
    reload();
  }

  function renderRows() {
    const body = $("rows");
    const focused = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.key : null;
    body.replaceChildren();
    for (const entry of [...rows.values()].sort(cmp)) {
      const row = body.insertRow();
      cell(row, new Date(entry.at).toLocaleString()).title = entry.at;
      cell(row, entry.kind);
      cell(row, entry.state, "state-" + entry.state);
      const task = cell(row, "");
      if (entry.task_id) {
        const button = document.createElement("button");
        button.type = "button"; button.textContent = entry.task_id; button.title = "Open recorded task history";
        button.dataset.key = entry.kind + ":" + entry.id;
        button.addEventListener("click", () => openTask(entry.task_id, button));
        task.replaceChildren(button);
      } else task.textContent = "-";
      cell(row, entry.stage); cell(row, entry.current_task_status || entry.status);
      cell(row, entry.source_status); cell(row, entry.provider);
      cell(row, entry.title || [
        entry.execution_kind, entry.event_type, entry.result_code, entry.retry_kind, entry.retry_state,
        entry.evidence_kind, entry.evidence_status,
        entry.rework_required && "rework required",
        entry.previous_task_status && (entry.previous_task_status + " -> " + entry.status),
        entry.previous_source_status && (entry.previous_source_status + " -> " + entry.source_status),
        entry.candidate_sha && entry.candidate_sha.slice(0, 12),
      ].filter(Boolean).join(" "));
      cell(row, entry.id);
    }
    if (focused) {
      const again = [...body.querySelectorAll("button")].find((button) => button.dataset.key === focused);
      if (again) again.focus();
      else $("table").focus();
    }
  }

  function renderSeries(report) {
    const body = $("series-rows");
    body.replaceChildren();
    if (!report) return;
    for (const bucket of report.series) {
      const row = body.insertRow();
      const range = cell(row, new Date(bucket.start).toLocaleString() + " - " + new Date(bucket.end).toLocaleString());
      range.title = bucket.start + " / " + bucket.end;
      cell(row, bucket.opened_occurrences); cell(row, bucket.opened_tasks);
      cell(row, bucket.completed_occurrences); cell(row, bucket.completed_tasks);
    }
  }

  function renderIntervals() {
    const body = $("interval-rows");
    const focused = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.intervalKey : null;
    body.replaceChildren();
    for (const interval of intervalRows.values()) {
      const row = body.insertRow();
      const task = cell(row, "");
      const button = document.createElement("button");
      button.type = "button"; button.textContent = interval.task_id; button.title = "Open recorded task history";
      button.dataset.intervalKey = interval.task_id + ":" + interval.active_from;
      button.addEventListener("click", () => openTask(interval.task_id, button));
      task.replaceChildren(button);
      const start = cell(row, new Date(interval.active_from).toLocaleString()); start.title = interval.active_from;
      const end = cell(row, interval.active_until ? new Date(interval.active_until).toLocaleString() : "Still active");
      if (interval.active_until) end.title = interval.active_until;
      cell(row, interval.current_status); cell(row, interval.stage); cell(row, interval.project); cell(row, interval.source);
    }
    if (focused) {
      const again = [...body.querySelectorAll("button")].find((button) => button.dataset.intervalKey === focused);
      if (again) again.focus();
      else $("interval-table").focus();
    }
  }

  function renderPeriods(periods) {
    if (periods.disabled) {
      $("period-results").hidden = true;
      $("period-note").hidden = false;
      $("period-note").textContent = "Choose a bounded period to view recorded intervals and opened/completed series.";
      return;
    }
    const report = periods.report;
    $("period-note").hidden = true;
    $("period-results").hidden = false;
    renderSeries(report);
    renderIntervals();
    const focused = document.activeElement === $("period-more");
    $("period-more").hidden = periodNextOffset === null;
    if (focused && periodNextOffset === null) $("interval-table").focus();
    $("period-count").textContent = intervalRows.size + " loaded of " + report.total_intervals;
    $("period-summary").textContent = [
      "Timezone " + report.timezone, "bucket " + report.bucket,
      "considered tasks " + report.considered_tasks, "history unavailable " + report.unavailable_tasks,
      report.truncated ? "task scan truncated" : "task scan complete",
    ].join(" | ");
  }

  function taskCell(row, taskId) {
    const target = cell(row, "");
    if (!taskId) { target.textContent = "-"; return target; }
    const button = document.createElement("button");
    button.type = "button"; button.textContent = taskId; button.title = "Open recorded task history";
    button.addEventListener("click", () => openTask(taskId, button));
    target.replaceChildren(button);
    return target;
  }

  function renderStatus() {
    const snapshot = statusSnapshot;
    for (const id of ["operator-metrics", "attention-rows", "pending-rows", "active-rows", "failure-rows", "selection-rows"]) {
      $(id).replaceChildren();
    }
    if (!snapshot) {
      $("operator-summary").textContent = "Status unavailable.";
      $("efficiency-summary").textContent = "";
      return;
    }
    $("operator-summary").textContent = [
      snapshot.summary === "ok" ? "No recorded attention conditions." : "Recorded conditions need attention.",
      "Observed " + new Date(snapshot.observed_at).toLocaleString() + ".",
      "Bounded to " + snapshot.detail_limit + " detail rows per section.",
    ].join(" ");
    const metricLabels = {
      task_count: "Tasks", open_task_count: "Open", eligible_open_task_count: "Eligible open",
      blocked_task_count: "Blocked", running_count: "Running", done_count: "Done",
      current_failed_execution_count: "Current failures", unknown_execution_count: "Unknown executions",
      worker_count: "Workers", recorded_pending_decision_count: "Recorded pending decisions",
    };
    for (const [key, label] of Object.entries(metricLabels)) {
      const dt = document.createElement("dt"); dt.textContent = label;
      const dd = document.createElement("dd"); dd.textContent = snapshot.metrics[key] ?? "-";
      $("operator-metrics").append(dt, dd);
    }
    for (const item of snapshot.sections.attention) {
      const row = $("attention-rows").insertRow(); cell(row, item.status); cell(row, item.kind); cell(row, item.count);
    }
    for (const item of snapshot.sections.pending_decisions) {
      const row = $("pending-rows").insertRow(); taskCell(row, item.task_id);
      cell(row, item.kind); cell(row, item.status); cell(row, item.detail);
    }
    for (const item of snapshot.sections.active_executions) {
      const row = $("active-rows").insertRow(); taskCell(row, item.task_id);
      cell(row, item.stage); cell(row, item.provider);
      cell(row, [item.process_state, item.parent_process_state, item.cleanup_state, item.stale && "stale"].filter(Boolean).join(" / "));
    }
    for (const item of snapshot.sections.workers) {
      const row = $("active-rows").insertRow(); cell(row, item.worker_id || "-");
      cell(row, item.capabilities.join(", ")); cell(row, item.provider); cell(row, "worker lease " + item.lease_expires_at);
    }
    for (const item of snapshot.sections.recent_failures) {
      const row = $("failure-rows").insertRow(); taskCell(row, item.task_id);
      cell(row, item.stage); cell(row, item.provider); cell(row, item.scope); cell(row, item.reason);
    }
    const efficiency = snapshot.sections.efficiency[0] || {};
    const usage = efficiency.usage || {};
    $("efficiency-summary").textContent = [
      efficiency.scope, "provider attempts " + (efficiency.provider_attempts ?? "unknown"),
      "failed " + (efficiency.failed_provider_attempts ?? "unknown"),
      "checks " + (efficiency.checks_executed ?? "unknown") + " run / " + (efficiency.checks_reused ?? "unknown") + " reused",
      usage.calls_reporting ? "provider-reported input " + usage.input + ", output " + usage.output : "provider usage unknown",
      "billing cost " + efficiency.billing_cost,
    ].join(" | ");
    for (const item of snapshot.sections.provider_selections) {
      const row = $("selection-rows").insertRow(); taskCell(row, item.task_id);
      cell(row, item.stage); cell(row, item.policy); cell(row, item.order.join(" -> "));
      cell(row, item.verdicts.map((v) => v.provider + ": " + (v.eligible === true ? "eligible" : v.eligible === false ? "ineligible" : "unknown")).join(", "));
    }
  }

  function renderTaskDetail() {
    for (const id of [
      "detail-facts", "detail-dependencies", "detail-decisions", "detail-evidence",
      "detail-retries", "detail-timeline",
    ]) $(id).replaceChildren();
    const detail = detailSnapshot;
    if (!detail || detail.task.id !== detailTaskId) return;
    $("detail-heading").textContent = detail.task.title || ("Task " + detail.task.id);
    $("detail-message").textContent = [
      detail.history.note,
      detail.history.truncated ? "Timeline truncated at " + detail.history.limit + " rows." : "Timeline within retained bounds.",
      "Observed " + new Date(detail.observed_at).toLocaleString() + ".",
    ].join(" ");
    const facts = [
      ["Task", detail.task.id], ["Stage", detail.task.stage], ["Status", detail.task.status],
      ["Project", detail.task.project || "-"], ["Source", [detail.source.kind, detail.source.id, detail.source.status].filter(Boolean).join(" / ") || "-"],
      ["Candidate", detail.candidate && detail.candidate.sha ? detail.candidate.sha.slice(0, 12) : "-"],
    ];
    for (const [label, value] of facts) {
      const dt = document.createElement("dt"); dt.textContent = label;
      const dd = document.createElement("dd"); dd.textContent = value;
      $("detail-facts").append(dt, dd);
    }
    for (const item of detail.dependencies) {
      const row = $("detail-dependencies").insertRow(); taskCell(row, item.task_id);
      cell(row, item.stage); cell(row, item.status);
    }
    for (const item of detail.pending_decisions) {
      const row = $("detail-decisions").insertRow(); cell(row, item.kind); cell(row, item.status);
      cell(row, item.candidate_sha ? item.candidate_sha.slice(0, 12) : "-"); cell(row, item.detail);
    }
    for (const item of detail.evidence) {
      const row = $("detail-evidence").insertRow();
      cell(row, new Date(item.at).toLocaleString()); cell(row, item.evidence_kind);
      cell(row, item.evidence_status); cell(row, item.candidate_sha ? item.candidate_sha.slice(0, 12) : "-");
    }
    for (const item of detail.retries) {
      const row = $("detail-retries").insertRow();
      cell(row, new Date(item.at).toLocaleString()); cell(row, item.retry_kind || "rework");
      cell(row, item.retry_state || "REWORK_REQUIRED");
      cell(row, item.retry_attempt ? item.retry_attempt + " of " + (item.retry_limit || "?") : "-");
      cell(row, item.candidate_sha ? item.candidate_sha.slice(0, 12) : "-");
    }
    for (const item of detail.timeline) {
      const row = $("detail-timeline").insertRow();
      const at = cell(row, new Date(item.at).toLocaleString()); at.title = item.at;
      cell(row, item.kind); cell(row, item.stage); cell(row, item.status || item.state); cell(row, item.provider);
      cell(row, item.title || [
        item.event_type, item.result_code, item.previous_task_status && (item.previous_task_status + " -> " + item.current_task_status),
        item.previous_source_status && (item.previous_source_status + " -> " + item.source_status),
        item.evidence_kind, item.evidence_status, item.retry_kind, item.retry_state,
      ].filter(Boolean).join(" "));
    }
  }

  function setView(name) {
    $("activity-view").hidden = name !== "activity";
    $("status-view").hidden = name !== "status";
    $("task-view").hidden = name !== "task";
    $("show-activity").setAttribute("aria-pressed", String(name === "activity"));
    $("show-status").setAttribute("aria-pressed", String(name === "status"));
    $("show-task").setAttribute("aria-pressed", String(name === "task"));
  }

  function render(page, periods) {
    renderRows();
    const moreHadFocus = document.activeElement === $("more");
    $("more").hidden = nextOffset === null;
    if (moreHadFocus && nextOffset === null) $("table").focus();
    $("count").textContent = rows.size + " loaded of " + page.total + (page.truncated ? "+" : "");
    const taskCounts = (page.status_counts || {}).task || {};
    $("status-counts").textContent = ["OPEN", "CLAIMED", "BLOCKED", "DONE"]
      .map((key) => key + " " + (taskCounts[key] || 0)).join(" | ");
    $("freshness").textContent = "updated " + lastGood.toLocaleTimeString();
    renderPeriods(periods);
    renderStatus();
    if (page.truncated) setMessage("Older records were excluded by the retention cap; narrow the time range for a complete view.", "warn");
    else if (rows.size === 0) setMessage("No activity matches these filters.");
    else setMessage("");
  }

  function buildHead() {
    const head = $("head");
    for (const [key, label] of COLUMNS) {
      const th = document.createElement("th");
      th.scope = "col"; th.setAttribute("aria-sort", "none");
      const button = document.createElement("button");
      button.type = "button"; button.textContent = label;
      button.addEventListener("click", () => {
        sort = { key, dir: sort.key === key ? -sort.dir : 1 };
        for (const item of head.children) item.setAttribute("aria-sort", "none");
        th.setAttribute("aria-sort", sort.dir === 1 ? "ascending" : "descending");
        renderRows();
      });
      th.append(button); head.append(th);
    }
    head.children[0].setAttribute("aria-sort", "descending");
  }

  function schedule() {
    clearInterval(timer);
    if ($("auto").checked) timer = setInterval(() => { if (!document.hidden && !inflight) loadRange(true); }, POLL_MS);
  }

  buildHead();
  $("timezone").value = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  $("filters").addEventListener("submit", (event) => event.preventDefault());
  for (const id of [
    "period", "since", "until", "bucket", "timezone", "state", "stage", "time_basis", "source_status", "task_status",
    "task_event", "retry_kind", "retry_state", "evidence_kind", "evidence_status",
    "source_history", "task_history", "retry_history", "evidence_history",
  ]) $(id).addEventListener("change", () => {
    for (const el of document.querySelectorAll(".custom")) el.hidden = $("period").value !== "custom";
    reload();
  });
  for (const id of ["provider", "project", "source"]) $(id).addEventListener("change", reload);
  $("task").addEventListener("change", () => {
    if (drillState && !$("task").value.trim()) exitDrill();
    else reload();
  });
  $("exit-drill").addEventListener("click", exitDrill);
  $("refresh").addEventListener("click", refresh);
  $("more").addEventListener("click", loadMore);
  $("period-more").addEventListener("click", loadMorePeriods);
  $("show-activity").addEventListener("click", () => setView("activity"));
  $("show-status").addEventListener("click", () => setView("status"));
  $("show-task").addEventListener("click", () => setView("task"));
  $("close-detail").addEventListener("click", closeTask);
  $("detail-history").addEventListener("click", () => { if (detailTaskId) enterDrill(detailTaskId); });
  $("auto").addEventListener("change", schedule);
  document.addEventListener("keydown", (event) => {
    const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName);
    if (event.ctrlKey || event.metaKey || event.altKey || typing) return;
    if (event.key === "/") { event.preventDefault(); $("task").focus(); }
    else if (event.key === "r") refresh();
    else if (event.key === "Escape" && !$("task-view").hidden) closeTask();
    else if (event.key === "Escape" && drillState) exitDrill();
  });
  api("/api/v5/health").then((health) => { $("version").textContent = "v" + health.version; }, () => {});
  loadRange(false);
  schedule();
})();
