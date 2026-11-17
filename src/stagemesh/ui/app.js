"use strict";
// Read-only by default; bounded control appears only when this loopback session explicitly enables it.
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
  let executionSnapshot = null, executionId = null, executionGeneration = 0;
  let executionReturnView = "activity", executionOpener = null;
  let objectivesSnapshot = null, objectiveGeneration = 0, objectiveLastGood = null;
  let objectiveInflight = false, selectedObjectiveId = null, objectiveOpener = null;
  let doctorSnapshot = null, doctorGeneration = 0, doctorLastGood = null, doctorInflight = false;
  let providersSnapshot = null, providersGeneration = 0, providersLastGood = null, providersInflight = false;
  let attentionSnapshot = null, attentionGeneration = 0, attentionLastGood = null, attentionInflight = false;
  let diagnosticsSnapshot = null, diagnosticsGeneration = 0, diagnosticsLastGood = null, diagnosticsInflight = false;
  let controlCapabilities = null, controlIntent = null, controlBusy = false;
  let projectTargets = new Map();
  let paletteOpener = null, shortcutsOpener = null;
  let notificationsEnabled = false, attentionBaselineReady = false;
  let knownAttentionKeys = new Set();

  const VIEW_COMMANDS = [
    ["Activity", "show-activity"], ["Status", "show-status"], ["Attention", "show-attention"],
    ["Diagnostics", "show-diagnostics"], ["Objectives", "show-objectives"], ["Doctor", "show-doctor"],
    ["Providers", "show-providers"], ["Control", "show-control"], ["Task detail", "show-task"],
    ["Execution detail", "show-execution"],
  ];

  function setMessage(text, level) {
    const el = $("message");
    el.textContent = text;
    el.className = level || "";
  }

  function recordedAge(value) {
    if (typeof value !== "number" || !Number.isFinite(value)) return "recorded age unavailable";
    const seconds = Math.max(0, Math.round(Date.now() / 1000 - value));
    if (seconds < 60) return "recorded " + seconds + "s ago";
    if (seconds < 3600) return "recorded " + Math.floor(seconds / 60) + "m ago";
    if (seconds < 86400) return "recorded " + Math.floor(seconds / 3600) + "h ago";
    return "recorded " + Math.floor(seconds / 86400) + "d ago";
  }

  function attentionKey(item) {
    return [item.task_id, item.kind, item.reason_code, item.recorded_at].join("|");
  }

  function observeAttentionForNotifications(projection) {
    const next = new Set(projection.items.map(attentionKey));
    if (!attentionBaselineReady) {
      knownAttentionKeys = next;
      attentionBaselineReady = true;
      return;
    }
    const fresh = [...next].filter((key) => !knownAttentionKeys.has(key)).length;
    knownAttentionKeys = next;
    if (!notificationsEnabled || fresh === 0 || !("Notification" in window)) return;
    new window.Notification("StageMesh needs attention", {
      body: fresh + " new recorded human-action condition" + (fresh === 1 ? "." : "s."),
      tag: "stagemesh-attention",
    });
  }

  async function enableNotifications() {
    const status = $("notification-status");
    if (!("Notification" in window)) {
      status.textContent = "Notifications are unavailable in this browser.";
      $("enable-notifications").disabled = true;
      return;
    }
    let permission = window.Notification.permission;
    try {
      if (permission === "default") permission = await window.Notification.requestPermission();
    } catch (_error) {
      notificationsEnabled = false;
      status.textContent = "Notifications could not be enabled.";
      return;
    }
    if (permission !== "granted") {
      notificationsEnabled = false;
      status.textContent = permission === "denied" ? "Notifications were denied." : "Notifications remain off.";
      return;
    }
    notificationsEnabled = true;
    status.textContent = "Notifications are enabled for new typed human-action conditions.";
    $("enable-notifications").textContent = "Notifications enabled";
    loadAttention(false);
  }

  function renderCommands() {
    const query = $("command-search").value.trim().toLocaleLowerCase();
    const list = $("command-list");
    list.replaceChildren();
    let shown = 0;
    for (const [label, targetId] of VIEW_COMMANDS) {
      const target = $(targetId);
      if (target.hidden || (query && !label.toLocaleLowerCase().includes(query))) continue;
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = "Open " + label;
      button.addEventListener("click", () => {
        paletteOpener = null;
        $("command-palette").close();
        target.click();
        target.focus();
      });
      const item = document.createElement("div");
      item.setAttribute("role", "listitem");
      item.append(button);
      list.append(item);
      shown += 1;
    }
    $("command-empty").hidden = shown !== 0;
  }

  function openPalette(opener) {
    if ($("command-palette").open) return;
    paletteOpener = opener || document.activeElement;
    $("command-search").value = "";
    renderCommands();
    $("command-palette").showModal();
    $("command-search").focus();
  }

  function closePalette() {
    if ($("command-palette").open) $("command-palette").close();
  }

  function openShortcuts(opener) {
    if ($("shortcut-help").open) return;
    shortcutsOpener = opener || document.activeElement;
    $("shortcut-help").showModal();
    $("close-shortcuts").focus();
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

  async function controlApi(path, options = {}) {
    const headers = {
      Authorization: "Bearer " + token,
      "X-Stagemesh-Control": controlCapabilities ? controlCapabilities.csrf_token : "",
    };
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(path, {
      method: options.method || "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      cache: "no-store",
    });
    let body = null;
    try { body = await response.json(); } catch (_) { /* non-JSON error */ }
    if (!response.ok) {
      const error = new Error(body && body.error ? body.error.message : "HTTP " + response.status);
      error.status = response.status;
      error.code = body && body.error ? body.error.code : "unknown";
      throw error;
    }
    return body;
  }

  function setObjectivesMessage(text, level = "") {
    const el = $("objectives-message");
    el.textContent = text;
    el.className = level;
  }

  function setProvidersMessage(text, level = "") {
    const el = $("providers-message");
    el.textContent = text;
    el.className = level;
  }

  async function loadObjectives(quiet) {
    if (!token) {
      setObjectivesMessage("No access token. Open the URL printed by `stagemesh serve`.", "error");
      renderObjectives();
      return;
    }
    if (objectiveInflight) return;
    const mine = ++objectiveGeneration;
    objectiveInflight = true;
    if (!quiet) setObjectivesMessage("Loading objective plans...");
    try {
      const projection = await api("/api/v1/objectives");
      if (mine !== objectiveGeneration) return;
      objectivesSnapshot = projection;
      objectiveLastGood = new Date();
      renderObjectives();
      setObjectivesMessage(
        projection.truncated
          ? "Only the newest bounded objective records are shown; older records are excluded."
          : projection.objectives.length ? "" : "No objective records are available."
        , projection.truncated ? "warn" : ""
      );
    } catch (error) {
      if (mine !== objectiveGeneration) return;
      const authorizationFailed = error.status === 401 || error.status === 403;
      if (authorizationFailed) {
        objectivesSnapshot = null;
        selectedObjectiveId = null;
        renderObjectives();
      }
      const stale = objectiveLastGood && !authorizationFailed
        ? " Showing the last objective snapshot from " + objectiveLastGood.toLocaleTimeString() + " (stale)." : "";
      setObjectivesMessage("Could not load objectives: " + error.message + "." + stale, error.status === 400 ? "error" : "warn");
    } finally {
      if (mine === objectiveGeneration) objectiveInflight = false;
    }
  }

  async function loadDoctor(quiet) {
    if (!token) {
      $("doctor-summary").textContent = "No access token. Open the URL printed by `stagemesh serve`.";
      $("doctor-summary").className = "error";
      return;
    }
    if (doctorInflight) return;
    const mine = ++doctorGeneration;
    doctorInflight = true;
    if (!quiet) {
      $("doctor-summary").textContent = "Loading safe local readiness...";
      $("doctor-summary").className = "muted";
    }
    try {
      const projection = await api("/api/v1/doctor");
      if (mine !== doctorGeneration) return;
      doctorSnapshot = projection;
      doctorLastGood = new Date();
      renderDoctor();
    } catch (error) {
      if (mine !== doctorGeneration) return;
      const authorizationFailed = error.status === 401 || error.status === 403;
      if (authorizationFailed) doctorSnapshot = null;
      renderDoctor();
      const stale = doctorLastGood && !authorizationFailed
        ? " Showing the last doctor snapshot from " + doctorLastGood.toLocaleTimeString() + " (stale)." : "";
      $("doctor-summary").textContent = "Could not load project doctor: " + error.message + "." + stale;
      $("doctor-summary").className = authorizationFailed ? "error" : "warn";
    } finally {
      if (mine === doctorGeneration) doctorInflight = false;
    }
  }

  async function loadProviders(quiet) {
    if (!token) {
      setProvidersMessage("No access token. Open the URL printed by `stagemesh serve`.", "error");
      return;
    }
    if (providersInflight) return;
    const mine = ++providersGeneration;
    providersInflight = true;
    if (!quiet) setProvidersMessage("Loading bounded provider status...");
    try {
      const projection = await api("/api/v1/providers");
      if (mine !== providersGeneration) return;
      providersSnapshot = projection;
      providersLastGood = new Date();
      renderProviders();
      setProvidersMessage(
        projection.status === "UNAVAILABLE"
          ? "Provider configuration is unavailable; no provider details were projected."
          : projection.truncated || projection.acceptance_history_truncated
            ? "Provider or acceptance history is truncated; unavailable evidence is not treated as missing proof."
            : "",
        projection.status === "UNAVAILABLE" ? "error" :
          projection.truncated || projection.acceptance_history_truncated ? "warn" : "",
      );
    } catch (error) {
      if (mine !== providersGeneration) return;
      const authorizationFailed = error.status === 401 || error.status === 403;
      if (authorizationFailed) providersSnapshot = null;
      renderProviders();
      const stale = providersLastGood && !authorizationFailed
        ? " Showing the last provider snapshot from " + providersLastGood.toLocaleTimeString() + " (stale)." : "";
      setProvidersMessage("Could not load provider status: " + error.message + "." + stale, authorizationFailed ? "error" : "warn");
    } finally {
      if (mine === providersGeneration) providersInflight = false;
    }
  }

  async function loadAttention(quiet) {
    if (!token) {
      $("attention-summary").textContent = "No access token. Open the URL printed by `stagemesh serve`.";
      $("attention-summary").className = "error";
      return;
    }
    if (attentionInflight) return;
    const mine = ++attentionGeneration;
    attentionInflight = true;
    if (!quiet) {
      $("attention-summary").textContent = "Loading recorded human actions...";
      $("attention-summary").className = "muted";
    }
    try {
      const projection = await api("/api/v1/attention");
      if (mine !== attentionGeneration) return;
      attentionSnapshot = projection;
      attentionLastGood = new Date();
      observeAttentionForNotifications(projection);
      renderAttention();
    } catch (error) {
      if (mine !== attentionGeneration) return;
      const authorizationFailed = error.status === 401 || error.status === 403;
      if (authorizationFailed) attentionSnapshot = null;
      renderAttention();
      const stale = attentionLastGood && !authorizationFailed
        ? " Showing the last human-action snapshot from " + attentionLastGood.toLocaleTimeString() + " (stale)." : "";
      $("attention-summary").textContent = "Could not load human actions: " + error.message + "." + stale;
      $("attention-summary").className = authorizationFailed ? "error" : "warn";
    } finally {
      if (mine === attentionGeneration) attentionInflight = false;
    }
  }

  async function loadDiagnostics(quiet) {
    if (!token) {
      $("diagnostics-summary").textContent = "No access token. Open the URL printed by `stagemesh serve`.";
      $("diagnostics-summary").className = "error";
      return;
    }
    if (diagnosticsInflight) return;
    const mine = ++diagnosticsGeneration;
    diagnosticsInflight = true;
    if (!quiet) {
      $("diagnostics-summary").textContent = "Loading safe execution diagnostics...";
      $("diagnostics-summary").className = "muted";
    }
    try {
      const projection = await api("/api/v1/diagnostics");
      if (mine !== diagnosticsGeneration) return;
      diagnosticsSnapshot = projection;
      diagnosticsLastGood = new Date();
      renderDiagnostics();
    } catch (error) {
      if (mine !== diagnosticsGeneration) return;
      const authorizationFailed = error.status === 401 || error.status === 403;
      if (authorizationFailed) diagnosticsSnapshot = null;
      renderDiagnostics();
      const stale = diagnosticsLastGood && !authorizationFailed
        ? " Showing the last diagnostic snapshot from " + diagnosticsLastGood.toLocaleTimeString() + " (stale)." : "";
      $("diagnostics-summary").textContent = "Could not load execution diagnostics: " + error.message + "." + stale;
      $("diagnostics-summary").className = authorizationFailed ? "error" : "warn";
    } finally {
      if (mine === diagnosticsGeneration) diagnosticsInflight = false;
    }
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

  const refresh = () => {
    if (!$("providers-view").hidden) loadProviders(false);
    else if (!$("doctor-view").hidden) loadDoctor(false);
    else if (!$("diagnostics-view").hidden) loadDiagnostics(false);
    else if (!$("attention-view").hidden) loadAttention(false);
    else if (!$("objectives-view").hidden) loadObjectives(false);
    else loadRange(false);
  };
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
    if ($("task-view").hidden) detailReturnView = visibleView();
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
      if (!stale) detailSnapshot = null;
      renderTaskDetail();
      $("detail-message").textContent = "Could not load task detail: " + error.message + "." + stale;
    }
  }

  function closeTask() {
    detailGeneration += 1;
    setView(detailReturnView);
    const opener = detailOpener;
    detailOpener = null;
    if (opener && opener.isConnected) opener.focus();
  }

  function visibleView() {
    if (!$("task-view").hidden) return "task";
    if (!$("diagnostics-view").hidden) return "diagnostics";
    if (!$("attention-view").hidden) return "attention";
    if (!$("objectives-view").hidden) return "objectives";
    if (!$("doctor-view").hidden) return "doctor";
    if (!$("providers-view").hidden) return "providers";
    if (!$("status-view").hidden) return "status";
    if (!$("control-view").hidden) return "control";
    return "activity";
  }

  async function openExecution(id, opener) {
    const mine = ++executionGeneration;
    if ($("execution-view").hidden) executionReturnView = visibleView();
    executionOpener = opener || document.activeElement;
    executionId = id;
    $("show-execution").hidden = false;
    $("show-execution").textContent = "Execution " + id.slice(0, 8);
    $("execution-heading").textContent = "Execution " + id;
    $("execution-message").textContent = "Loading execution detail...";
    setView("execution");
    try {
      const detail = await api("/api/v1/execution?execution_id=" + encodeURIComponent(id));
      if (mine !== executionGeneration) return;
      executionSnapshot = detail;
      renderExecutionDetail();
      $("close-execution").focus();
    } catch (error) {
      if (mine !== executionGeneration) return;
      const stale = executionSnapshot && executionSnapshot.execution.id === id ? " Showing the last recorded detail (stale)." : "";
      if (!stale) executionSnapshot = null;
      renderExecutionDetail();
      $("execution-message").textContent = "Could not load execution detail: " + error.message + "." + stale;
    }
  }

  function closeExecution() {
    executionGeneration += 1;
    setView(executionReturnView);
    const opener = executionOpener;
    executionOpener = null;
    if (opener && opener.isConnected) opener.focus();
  }

  function executionButton(id, label) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label || id;
    button.title = "Open recorded execution detail";
    button.dataset.key = "execution-detail:" + id;
    button.addEventListener("click", () => openExecution(id, button));
    return button;
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
      const id = cell(row, "");
      if (entry.kind === "execution") id.replaceChildren(executionButton(entry.id));
      else id.textContent = entry.id;
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
      const state = cell(row, [
        item.process_state,
        item.parent_process_state,
        item.cleanup_state,
        recordedAge(item.started_at),
        item.stale && "stale",
      ].filter(Boolean).join(" / "));
      if (item.execution_id) state.append(" ", executionButton(item.execution_id, "Inspect execution"));
      if (item.release_unknown_allowed && item.execution_id &&
          controlCapabilities && controlCapabilities.actions.includes("release_unknown_execution")) {
        const release = document.createElement("button");
        release.type = "button";
        release.textContent = "Verify dead and release";
        release.addEventListener("click", () => openUnknownRelease(item));
        state.append(" ", release);
      }
    }
    for (const item of snapshot.sections.workers) {
      const row = $("active-rows").insertRow(); cell(row, item.worker_id || "-");
      cell(row, item.capabilities.join(", ")); cell(row, item.provider); cell(row, "worker lease " + item.lease_expires_at);
    }
    for (const item of snapshot.sections.recent_failures) {
      const row = $("failure-rows").insertRow(); taskCell(row, item.task_id);
      cell(row, item.stage); cell(row, item.provider); cell(row, item.scope);
      const reason = cell(row, item.reason);
      if (item.execution_id) reason.append(" ", executionButton(item.execution_id, "Inspect execution"));
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

  function attentionRows() {
    if (!attentionSnapshot) return [];
    const search = $("attention-search").value.trim().toLocaleLowerCase();
    const kind = $("attention-kind").value;
    const mode = $("attention-sort").value;
    const result = attentionSnapshot.items.filter((item) => {
      const matchesText = !search || [item.task_id, item.title, item.detail].some(
        (value) => String(value || "").toLocaleLowerCase().includes(search)
      );
      return matchesText && (!kind || item.kind === kind);
    });
    result.sort((left, right) => {
      if (mode === "task") return left.task_id.localeCompare(right.task_id) || right.recorded_at - left.recorded_at;
      if (mode === "kind") return left.kind.localeCompare(right.kind) || right.recorded_at - left.recorded_at;
      return right.recorded_at - left.recorded_at || left.task_id.localeCompare(right.task_id);
    });
    return result;
  }

  function renderAttention() {
    const body = $("attention-items");
    body.replaceChildren();
    const snapshot = attentionSnapshot;
    if (!snapshot) {
      $("attention-count").textContent = "";
      $("attention-message").textContent = "";
      return;
    }
    const totalLabel = snapshot.queue_total_kind === "LOWER_BOUND"
      ? "at least " + snapshot.queue_total
      : snapshot.queue_total;
    $("attention-count").textContent = snapshot.queue_total_kind === "LOWER_BOUND"
      ? "(≥" + snapshot.queue_total + ")"
      : "(" + snapshot.queue_total + ")";
    $("attention-summary").textContent = [
      "Observed " + new Date(snapshot.observed_at).toLocaleString() + ".",
      totalLabel + " recorded human actions; row limit " + snapshot.queue_limit + ".",
      "Approval coverage " + snapshot.approval_coverage + ".",
      "Database writes " + snapshot.database_writes + ", external calls " + snapshot.external_calls + ".",
      snapshot.history_truncated ? "The bounded decision history is truncated; the count is a lower bound." : "The bounded decision history scan completed.",
    ].join(" ");
    $("attention-summary").className = snapshot.truncated ? "warn" : "muted";
    const items = attentionRows();
    $("attention-message").textContent = items.length ? "" : "No recorded human actions match these filters.";
    for (const item of items) {
      const row = body.insertRow();
      taskCell(row, item.task_id);
      cell(row, item.kind.replaceAll("_", " ").toLocaleLowerCase());
      cell(row, item.stage + " / " + item.task_status);
      cell(row, item.title + " — " + item.detail);
      cell(row, new Date(item.recorded_at * 1000).toLocaleString());
    }
  }

  function diagnosticRows() {
    if (!diagnosticsSnapshot) return [];
    const search = $("diagnostics-search").value.trim().toLocaleLowerCase();
    const status = $("diagnostics-status").value;
    const kind = $("diagnostics-kind").value;
    const mode = $("diagnostics-sort").value;
    const result = diagnosticsSnapshot.executions.filter((item) => {
      const matchesText = !search || [item.id, item.task.id, item.task.title, item.provider, item.outcome.code].some(
        (value) => String(value || "").toLocaleLowerCase().includes(search)
      );
      return matchesText && (!status || item.status === status) && (!kind || item.kind === kind);
    });
    result.sort((left, right) => {
      if (mode === "task") return String(left.task.id).localeCompare(String(right.task.id)) || right.timing.updated_at - left.timing.updated_at;
      if (mode === "status") return left.status.localeCompare(right.status) || right.timing.updated_at - left.timing.updated_at;
      if (mode === "duration") return (right.timing.recorded_duration_seconds ?? -1) - (left.timing.recorded_duration_seconds ?? -1);
      return (right.timing.updated_at || 0) - (left.timing.updated_at || 0) || String(left.id).localeCompare(String(right.id));
    });
    return result;
  }

  function renderDiagnostics() {
    const body = $("diagnostics-items");
    body.replaceChildren();
    const snapshot = diagnosticsSnapshot;
    if (!snapshot) {
      $("diagnostics-message").textContent = "";
      return;
    }
    $("diagnostics-summary").textContent = [
      "Observed " + new Date(snapshot.observed_at).toLocaleString() + ".",
      snapshot.executions.length + " shown of " + snapshot.execution_total + " executions; limit " + snapshot.execution_limit + ".",
      "Raw logs and free-form results " + snapshot.raw_logs.toLocaleLowerCase() + ".",
      "Database writes " + snapshot.database_writes + ", external calls " + snapshot.external_calls + ".",
      snapshot.truncated ? "Older executions are outside this bounded view." : "The execution list is complete.",
    ].join(" ");
    $("diagnostics-summary").className = snapshot.truncated ? "warn" : "muted";
    const items = diagnosticRows();
    $("diagnostics-message").textContent = items.length ? "" : "No loaded executions match these filters.";
    for (const item of items) {
      const row = body.insertRow();
      taskCell(row, item.task.id);
      const execution = cell(row, "");
      if (item.id) execution.append(executionButton(item.id, item.id.slice(0, 8)));
      else execution.textContent = "unavailable";
      cell(row, item.kind + " / " + item.status + (item.current_for_task ? " / current" : " / history"));
      cell(row, item.provider || "withheld or unavailable");
      cell(row, item.outcome.code
        ? item.outcome.code + " / " + item.outcome.classification
        : item.outcome.raw_result_available ? "free-form result withheld" : "no recorded result");
      const evidenceCount = Object.values(item.evidence.counts).reduce(
        (total, statuses) => total + Object.values(statuses).reduce((subtotal, value) => subtotal + value, 0), 0
      );
      cell(row, item.evidence.correlation.replaceAll("_", " ").toLocaleLowerCase() +
        " / " + evidenceCount + " scanned" + (item.evidence.truncated ? " / truncated" : ""));
      cell(row, [
        item.timing.updated_at == null ? "time unavailable" : new Date(item.timing.updated_at * 1000).toLocaleString(),
        item.timing.recorded_duration_seconds == null ? "duration unavailable" : item.timing.recorded_duration_seconds + " seconds",
        item.runtime.process_state,
        item.runtime.cleanup_state,
      ].filter(Boolean).join(" / "));
    }
  }

  function renderDoctorFacts(id, facts) {
    const target = $(id);
    target.replaceChildren();
    for (const [label, value] of facts) {
      const dt = document.createElement("dt"); dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = value === true ? "yes" : value === false ? "no" : value ?? "unavailable";
      target.append(dt, dd);
    }
  }

  function renderDoctor() {
    $("doctor-attention").replaceChildren();
    $("doctor-providers").replaceChildren();
    renderDoctorFacts("doctor-system", []);
    renderDoctorFacts("doctor-policies", []);
    const snapshot = doctorSnapshot;
    if (!snapshot) {
      $("doctor-summary").textContent = "Doctor data is unavailable.";
      $("doctor-summary").className = "warn";
      return;
    }
    $("doctor-summary").textContent = [
      "Verdict " + snapshot.verdict + ".",
      "Observed " + new Date(snapshot.observed_at).toLocaleString() + ".",
      "Local-only: no network or provider checks were performed.",
    ].join(" ");
    $("doctor-summary").className = snapshot.verdict === "READY" ? "muted" : snapshot.verdict === "UNAVAILABLE" ? "error" : "warn";
    const config = snapshot.configuration || {};
    renderDoctorFacts("doctor-system", [
      ["Verdict", snapshot.verdict],
      ["Configuration", config.status],
      ["Config source", config.source],
      ["Database schema", snapshot.database.schema],
      ["Runtime", snapshot.runtime.mode + " " + snapshot.runtime.version],
      ["Runtime provenance", snapshot.runtime.provenance],
      ["Workspace isolation configured", snapshot.workspace.isolation_configured],
      ["Unsafe worktree root allowed", snapshot.workspace.unsafe_root_allowed],
      ["Network checked", snapshot.network_checked],
      ["Mutation performed", snapshot.mutation_performed],
    ]);
    if (!snapshot.attention.length) {
      const row = $("doctor-attention").insertRow(); cell(row, "none"); cell(row, "READY"); cell(row, "No local posture conditions need attention.");
    }
    for (const item of snapshot.attention) {
      const row = $("doctor-attention").insertRow(); cell(row, item.severity); cell(row, item.code); cell(row, item.message);
    }
    for (const provider of snapshot.providers) {
      const row = $("doctor-providers").insertRow(); cell(row, provider.name);
      cell(row, provider.capabilities.join(", ")); cell(row, provider.command_source);
      cell(row, provider.authorized_for_unattended ? "authorized" : "not authorized");
    }
    $("doctor-provider-note").textContent = [
      "Configured and authorized do not mean available or invoked. This view makes no provider or network calls.",
      snapshot.providers.length + " shown of " + snapshot.provider_total +
        " configured provider records (limit " + snapshot.provider_limit + ").",
      snapshot.providers_truncated ? "The provider list is truncated." : "",
    ].filter(Boolean).join(" ");
    const policies = snapshot.policies;
    if (!policies) return;
    renderDoctorFacts("doctor-policies", [
      ["Routing mode", config.routing.mode],
      ["Provider selection", config.routing.selection_policy],
      ["Independent review required", config.routing.independent_review_required],
      ["Unattended allowlist configured", policies.authorization.allowlist_configured],
      ["Authorized providers", policies.authorization.authorized_provider_count],
      ["Approved spec required", policies.planning.approved_spec_required],
      ["Plan challenge required", policies.planning.challenge_required],
      ["Merge mode", policies.merging.mode],
      ["Required hosted checks", policies.merging.required_check_count],
      ["Research stage enabled", policies.research.enabled],
      ["Research scope enforced", policies.research.scope_enforced],
      ["Continuous driver configured", policies.driver.configured],
      ["Driver auto-merge", policies.driver.auto_merge],
      ["Spec governance enabled", policies.spec_governance.enabled],
      ["Automatic task selection", policies.task_selection.automatic],
    ]);
  }

  function liveProviderState(provider, stage) {
    const value = provider.live_acceptance[stage];
    return typeof value === "string" ? value : value.state;
  }

  function providerRows() {
    if (!providersSnapshot) return [];
    const search = $("provider-search").value.trim().toLocaleLowerCase();
    const capability = $("provider-capability").value;
    const posture = $("provider-posture").value;
    const result = providersSnapshot.providers.filter((provider) => {
      if (search && !provider.name.toLocaleLowerCase().includes(search)) return false;
      if (capability && !provider.capabilities.includes(capability)) return false;
      if (posture === "authorized" && !provider.authorized_for_unattended) return false;
      if (posture === "not-authorized" && provider.authorized_for_unattended) return false;
      if (posture === "discovered" && provider.executable_discovery !== "DISCOVERED") return false;
      if (posture === "not-discovered" && provider.executable_discovery !== "NOT_DISCOVERED") return false;
      if (posture === "cooling" && !provider.cooldown.active) return false;
      if (posture === "invoked" && !provider.invocation.recorded) return false;
      if (posture === "never-invoked" && provider.invocation.recorded) return false;
      return true;
    });
    const mode = $("provider-sort").value;
    result.sort((left, right) => {
      if (mode === "invocations") return right.invocation.total - left.invocation.total || left.name.localeCompare(right.name);
      if (mode === "recent") return (right.invocation.last_started_at || 0) - (left.invocation.last_started_at || 0) || left.name.localeCompare(right.name);
      return left.name.localeCompare(right.name);
    });
    return result;
  }

  function renderProviders() {
    const body = $("provider-rows");
    body.replaceChildren();
    const snapshot = providersSnapshot;
    if (!snapshot) {
      $("providers-summary").textContent = "Provider status is unavailable.";
      return;
    }
    const items = providerRows();
    const totalLabel = snapshot.provider_total_kind === "LOWER_BOUND"
      ? "at least " + snapshot.provider_total
      : snapshot.provider_total;
    $("providers-summary").textContent = [
      "Observed " + new Date(snapshot.observed_at).toLocaleString() + ".",
      items.length + " shown of " + totalLabel + " provider records; limit " + snapshot.provider_limit + ".",
      snapshot.withheld_provider_count + " unsafe provider identifiers withheld.",
      "Provider calls " + snapshot.provider_calls + ", network calls " + snapshot.network_calls +
        ", database writes " + snapshot.database_writes + ".",
      "Billing usage " + snapshot.billing_usage + ".",
      "Acceptance scan limit " + snapshot.acceptance_scan_limit +
        (snapshot.acceptance_history_truncated ? " (truncated)." : "."),
    ].join(" ");
    for (const provider of items) {
      const row = body.insertRow();
      cell(row, provider.name); cell(row, provider.command_source);
      cell(row, provider.capabilities.join(", ") || "recorded only");
      cell(row, provider.authorization_configured
        ? provider.authorized_for_unattended ? "authorized" : "not authorized"
        : "allowlist absent");
      cell(row, provider.executable_discovery.replaceAll("_", " ").toLocaleLowerCase());
      cell(row, "implement " + liveProviderState(provider, "IMPLEMENT").toLocaleLowerCase() +
        " / review " + liveProviderState(provider, "REVIEW").toLocaleLowerCase());
      cell(row, provider.cooldown.active
        ? provider.cooldown.failure_class + " (" + provider.cooldown.seconds_remaining + " seconds)"
        : "not cooling");
      cell(row, provider.invocation.recorded
        ? provider.invocation.total + " total; " + provider.invocation.running + " running; " +
          provider.invocation.succeeded + " succeeded; " + provider.invocation.failed + " failed; " +
          provider.invocation.unknown + " unknown"
        : "none recorded");
      cell(row, provider.invocation.last_started_at == null
        ? "never recorded" : new Date(provider.invocation.last_started_at * 1000).toLocaleString());
    }
    if (!items.length && snapshot.status === "AVAILABLE") {
      setProvidersMessage("No loaded providers match these filters.");
    }
  }

  function objectiveRows() {
    if (!objectivesSnapshot) return [];
    const search = $("objective-search").value.trim().toLocaleLowerCase();
    const state = $("objective-state").value;
    const mode = $("objective-sort").value;
    const result = objectivesSnapshot.objectives.filter((objective) => {
      const matchesText = !search || [objective.id, objective.title].some(
        (value) => String(value || "").toLocaleLowerCase().includes(search)
      );
      return matchesText && (!state || objective.plan_state === state);
    });
    result.sort((left, right) => {
      if (mode === "title") return String(left.title || left.id).localeCompare(String(right.title || right.id));
      if (mode === "state") return left.plan_state.localeCompare(right.plan_state) || String(left.id).localeCompare(String(right.id));
      if (mode === "progress") return (right.counts.DONE - left.counts.DONE) || (right.counts.total - left.counts.total);
      return (right.updated_at || 0) - (left.updated_at || 0) || String(left.id).localeCompare(String(right.id));
    });
    return result;
  }

  function selectObjective(objectiveId, opener) {
    selectedObjectiveId = objectiveId;
    objectiveOpener = opener || document.activeElement;
    renderObjectiveDetail();
    $("objective-detail-heading").focus();
  }

  function closeObjective() {
    const closingId = selectedObjectiveId;
    selectedObjectiveId = null;
    $("objective-detail").hidden = true;
    const opener = objectiveOpener;
    objectiveOpener = null;
    if (opener && opener.isConnected) opener.focus();
    else {
      const replacement = [...$("objective-rows").querySelectorAll("button")].find(
        (button) => button.dataset.objectiveKey === closingId
      );
      if (replacement) replacement.focus();
      else $("objectives-table").focus();
    }
  }

  function renderObjectives() {
    const body = $("objective-rows");
    const focused = document.activeElement && document.activeElement.dataset
      ? document.activeElement.dataset.objectiveKey : null;
    body.replaceChildren();
    if (!objectivesSnapshot) {
      $("objectives-summary").textContent = "Objective data has not been loaded.";
      $("objective-detail").hidden = true;
      return;
    }
    const items = objectiveRows();
    $("objectives-summary").textContent = "Observed " + new Date(objectivesSnapshot.observed_at).toLocaleString() +
      ". Bounded to " + objectivesSnapshot.detail_limit + " objectives and " + objectivesSnapshot.child_limit +
      " children per objective. " + objectivesSnapshot.total + " objective records; " + items.length + " match this view.";
    for (const objective of items) {
      const row = body.insertRow();
      const name = cell(row, "");
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = objective.title || objective.id || "Untitled objective";
      button.title = "Open bounded objective plan";
      button.dataset.objectiveKey = objective.id;
      button.addEventListener("click", () => selectObjective(objective.id, button));
      name.replaceChildren(button);
      cell(row, objective.plan_state);
      cell(row, objective.counts.recorded + " / " + objective.counts.total);
      cell(row, objective.counts.DONE);
      cell(row, objective.counts.BLOCKED);
      cell(row, objective.counts.not_recorded);
      const updated = cell(row, objective.updated_at ? new Date(objective.updated_at * 1000).toLocaleString() : "UNKNOWN");
      if (objective.updated_at) updated.title = String(objective.updated_at);
    }
    if (!items.length) setObjectivesMessage("No loaded objectives match these filters.");
    else if (!objectivesSnapshot.truncated) setObjectivesMessage("");
    if (selectedObjectiveId) renderObjectiveDetail();
    if (focused) {
      const again = [...body.querySelectorAll("button")].find((button) => button.dataset.objectiveKey === focused);
      if (again) again.focus();
      else $("objectives-table").focus();
    }
  }

  function renderObjectiveDetail() {
    const detail = $("objective-detail");
    const objective = objectivesSnapshot && objectivesSnapshot.objectives.find(
      (item) => item.id === selectedObjectiveId
    );
    if (!objective) {
      detail.hidden = true;
      return;
    }
    detail.hidden = false;
    $("objective-detail-heading").textContent = "Objective " + objective.id + ": " + (objective.title || "Untitled");
    const counts = objective.counts;
    $("objective-detail-note").textContent = "Plan state " + objective.plan_state + ". " + counts.DONE +
      " of " + counts.total + " planned children are done; " + counts.BLOCKED + " blocked; " +
      counts.not_recorded + " not yet recorded." + (objective.children_truncated ? " Child rows are truncated by the safety limit." : "");
    const body = $("objective-children");
    const focused = document.activeElement && document.activeElement.dataset
      ? document.activeElement.dataset.objectiveChildKey : null;
    body.replaceChildren();
    for (const child of objective.children) {
      const row = body.insertRow();
      const task = cell(row, "");
      if (child.recorded && child.task_id) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = child.task_id;
        button.title = "Open recorded task detail";
        button.dataset.objectiveChildKey = child.task_id;
        button.addEventListener("click", () => openTask(child.task_id, button));
        task.replaceChildren(button);
      } else task.textContent = child.task_id || "UNKNOWN";
      cell(row, child.title);
      cell(row, child.stage || "NOT_RECORDED");
      cell(row, child.status || "NOT_RECORDED");
      cell(row, child.recorded ? "Yes" : "No");
      cell(row, (child.dependencies || []).filter(Boolean).join(", ") || "-");
    }
    if (focused) {
      const again = [...body.querySelectorAll("button")].find(
        (button) => button.dataset.objectiveChildKey === focused
      );
      if (again) again.focus();
      else $("objective-children-table").focus();
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

  function renderExecutionDetail() {
    for (const id of ["execution-facts", "execution-timing", "execution-ownership", "execution-evidence"]) $(id).replaceChildren();
    const detail = executionSnapshot;
    if (!detail || detail.execution.id !== executionId) return;
    $("execution-heading").textContent = detail.execution.kind + " execution";
    $("execution-message").textContent = [
      detail.evidence_meta.correlation.replaceAll("_", " ") + ".",
      detail.evidence_meta.truncated ? "Evidence truncated at " + detail.evidence_meta.limit + " rows." : "Evidence within retained bounds.",
      "Observed " + new Date(detail.observed_at).toLocaleString() + ".",
    ].join(" ");
    const appendFacts = (target, facts) => {
      for (const [label, value] of facts) {
        const dt = document.createElement("dt"); dt.textContent = label;
        const dd = document.createElement("dd"); dd.textContent = value ?? "-";
        target.append(dt, dd);
      }
    };
    appendFacts($("execution-facts"), [
      ["Execution", detail.execution.id], ["Kind", detail.execution.kind], ["Status", detail.execution.status],
      ["Provider", detail.execution.provider], ["Current for task", detail.execution.current_for_task ? "yes" : "history"],
      ["Task", detail.task.id], ["Task stage", detail.task.stage], ["Task status", detail.task.status],
      ["Candidate", detail.candidate.sha ? detail.candidate.sha.slice(0, 12) : "-"],
      ["Outcome", detail.outcome.code || "unavailable"], ["Outcome class", detail.outcome.classification],
      ["Outcome scope", detail.outcome.scope],
    ]);
    const recordedTime = (value) => value == null ? "unavailable" : new Date(value * 1000).toLocaleString();
    appendFacts($("execution-timing"), [
      ["Started", recordedTime(detail.timing.started_at)], ["Updated", recordedTime(detail.timing.updated_at)],
      ["Terminal", recordedTime(detail.timing.terminal_at)],
      ["Recorded duration", detail.timing.recorded_duration_seconds == null ? "unavailable" : detail.timing.recorded_duration_seconds + " seconds"],
    ]);
    appendFacts($("execution-ownership"), [
      ["Claim", detail.claim && detail.claim.id], ["Worker", detail.claim && detail.claim.worker_id],
      ["Claim active", detail.claim ? (detail.claim.active ? "yes" : "released") : "unavailable"],
      ["Identity coverage", detail.runtime.identity_coverage], ["Process state", detail.runtime.process_state],
      ["Parent process", detail.runtime.parent_process_state], ["Cleanup", detail.runtime.cleanup_state || "none recorded"],
    ]);
    for (const item of detail.evidence) {
      const row = $("execution-evidence").insertRow();
      cell(row, recordedTime(item.recorded_at)); cell(row, item.kind); cell(row, item.status);
      cell(row, item.current_candidate ? "yes" : "historical");
    }
  }

  function controlKey() {
    if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {
      return "ui-" + globalThis.crypto.randomUUID();
    }
    return "ui-" + Date.now() + "-" + Math.random().toString(16).slice(2);
  }

  function setControlResult(text, level = "") {
    const result = $("control-result");
    result.textContent = text;
    result.className = level;
  }

  function updateControlTarget() {
    const action = $("control-action").value;
    const releaseAction = action === "release_unknown_execution";
    const taskAction = action === "task_retry" || releaseAction;
    $("control-task-label").hidden = !taskAction;
    $("control-task").required = taskAction;
    $("control-execution-label").hidden = !releaseAction;
    $("control-execution").required = releaseAction;
    $("control-reason-label").hidden = !releaseAction;
    $("control-reason").required = releaseAction;
    $("control-release-warning").hidden = !releaseAction;
  }

  function clearControlPlan() {
    controlIntent = null;
    $("control-plan-view").hidden = true;
    $("control-plan-facts").replaceChildren();
    $("control-preconditions").replaceChildren();
    $("control-confirm").disabled = false;
  }

  function renderControlPlan(plan) {
    const facts = [
      ["Action", plan.action.replaceAll("_", " ")],
      ["Exact target", plan.target],
      ["Idempotency key", plan.idempotency_key],
      ["Expires", new Date(plan.expires_at * 1000).toLocaleString()],
      ["Recorded-state digest", plan.state_digest.slice(0, 16) + "..."],
    ];
    if (plan.reason) facts.splice(2, 0, ["Verification reason", plan.reason]);
    const target = $("control-plan-facts");
    target.replaceChildren();
    for (const [label, value] of facts) {
      const dt = document.createElement("dt"); dt.textContent = label;
      const dd = document.createElement("dd"); dd.textContent = value;
      target.append(dt, dd);
    }
    const conditions = $("control-preconditions");
    conditions.replaceChildren();
    for (const condition of plan.preconditions || []) {
      const item = document.createElement("li"); item.textContent = condition; conditions.append(item);
    }
    $("control-plan-view").hidden = false;
    $("control-confirm").focus();
  }

  async function loadControlCapabilities() {
    if (!token) return;
    try {
      const capabilities = await api("/api/v1/control");
      if (!capabilities.enabled) return;
      controlCapabilities = capabilities;
      renderStatus();
      $("show-control").hidden = false;
      $("control-summary").textContent =
        "Control is enabled only for this loopback session and project " + capabilities.project_id +
        ". Every action requires a short-lived plan and a separate confirmation.";
    } catch (error) {
      if (error.status === 404) {
        $("control-summary").textContent =
          "Read-only session. Restart with --enable-control only when bounded operator actions are intended.";
      } else {
        $("control-summary").textContent = "Control availability could not be verified.";
      }
    }
  }

  async function loadProjectProjection() {
    if (!token) return;
    try {
      const projection = await api("/api/v1/projects");
      if (!projection.isolated || projection.aggregated) return;
      const select = $("project-switch");
      select.replaceChildren();
      projectTargets = new Map();
      for (const project of projection.projects || []) {
        const option = document.createElement("option");
        option.value = project.project_id;
        option.textContent = project.name;
        option.selected = project.current === true;
        select.append(option);
        projectTargets.set(project.project_id, project);
      }
      $("project-switch-label").hidden = projectTargets.size < 2;
      $("project-switch-note").textContent = projectTargets.size < 2 ? "" :
        "Each choice opens its own loopback server, bearer token, and isolated database.";
    } catch (_) {
      projectTargets = new Map();
      $("project-switch-label").hidden = true;
      $("project-switch-note").textContent = "";
    }
  }

  function switchProject() {
    const select = $("project-switch");
    const target = projectTargets.get(select.value);
    if (!target || target.current || !target.launch_url) return;
    select.disabled = true;
    $("project-switch-note").textContent = "Opening the selected isolated project server...";
    location.assign(target.launch_url);
  }

  async function planControl(event) {
    event.preventDefault();
    if (!controlCapabilities || controlBusy) return;
    const action = $("control-action").value;
    const taskId = $("control-task").value.trim();
    const executionId = $("control-execution").value.trim();
    const reason = $("control-reason").value.trim();
    if (["task_retry", "release_unknown_execution"].includes(action) && !taskId) {
      setControlResult("Enter the exact task id before reviewing this action.", "error");
      $("control-task").focus();
      return;
    }
    if (action === "release_unknown_execution" && !executionId) {
      setControlResult("Enter the exact execution id before reviewing release.", "error");
      $("control-execution").focus();
      return;
    }
    if (action === "release_unknown_execution" && !reason) {
      setControlResult("Describe what you checked to verify the process is dead.", "error");
      $("control-reason").focus();
      return;
    }
    controlBusy = true;
    $("control-plan").disabled = true;
    clearControlPlan();
    setControlResult("Checking current recorded state...");
    const request = { action, idempotency_key: controlKey() };
    if (["task_retry", "release_unknown_execution"].includes(action)) request.task_id = taskId;
    if (action === "release_unknown_execution") {
      request.execution_id = executionId;
      request.reason = reason;
    }
    try {
      controlIntent = await controlApi("/api/v1/control/plan", { method: "POST", body: request });
      renderControlPlan(controlIntent);
      setControlResult("Plan prepared. Review the exact target, expiry, and preconditions before confirming.");
    } catch (error) {
      setControlResult("Plan refused: " + error.message + ".", "error");
    } finally {
      controlBusy = false;
      $("control-plan").disabled = false;
    }
  }

  function receiptText(receipt, reconciled) {
    const result = receipt.result || {};
    const outcome = result.queue_state || result.status || result.outcome || receipt.status || "recorded";
    return (reconciled ? "Receipt reconciled after an uncertain response. " : "Action accepted. ") +
      receipt.action.replaceAll("_", " ") + " for " + (receipt.task_id || "this queue") +
      ": " + outcome + ". Receipt " + receipt.idempotency_key + ".";
  }

  function openUnknownRelease(item) {
    setView("control");
    $("control-action").value = "release_unknown_execution";
    $("control-task").value = item.task_id || "";
    $("control-execution").value = item.execution_id || "";
    $("control-reason").value = "";
    clearControlPlan();
    updateControlTarget();
    setControlResult(
      "Verify outside StageMesh that this process is dead, then record what you checked. No process will be killed.",
      "warn"
    );
    $("control-reason").focus();
  }

  async function confirmControl() {
    if (!controlIntent || controlBusy) return;
    const planned = controlIntent;
    controlBusy = true;
    $("control-confirm").disabled = true;
    $("control-plan").disabled = true;
    setControlResult("Confirming the exact reviewed plan...");
    let receipt = null, reconciled = false;
    try {
      receipt = await controlApi("/api/v1/control/confirm", {
        method: "POST", body: { intent: planned.intent },
      });
    } catch (error) {
      try {
        receipt = await controlApi(
          "/api/v1/control/receipt?idempotency_key=" + encodeURIComponent(planned.idempotency_key)
        );
        reconciled = true;
      } catch (_) {
        setControlResult("Confirmation refused or unrecorded: " + error.message + ".", "error");
      }
    }
    if (receipt) {
      setControlResult(receiptText(receipt, reconciled), "ok");
      controlIntent = null;
      $("control-confirm").disabled = true;
      await loadRange(false);
    }
    controlBusy = false;
    $("control-plan").disabled = false;
  }

  function setView(name) {
    $("activity-view").hidden = name !== "activity";
    $("status-view").hidden = name !== "status";
    $("attention-view").hidden = name !== "attention";
    $("diagnostics-view").hidden = name !== "diagnostics";
    $("objectives-view").hidden = name !== "objectives";
    $("doctor-view").hidden = name !== "doctor";
    $("providers-view").hidden = name !== "providers";
    $("control-view").hidden = name !== "control";
    $("task-view").hidden = name !== "task";
    $("execution-view").hidden = name !== "execution";
    $("show-activity").setAttribute("aria-pressed", String(name === "activity"));
    $("show-status").setAttribute("aria-pressed", String(name === "status"));
    $("show-attention").setAttribute("aria-pressed", String(name === "attention"));
    $("show-diagnostics").setAttribute("aria-pressed", String(name === "diagnostics"));
    $("show-objectives").setAttribute("aria-pressed", String(name === "objectives"));
    $("show-doctor").setAttribute("aria-pressed", String(name === "doctor"));
    $("show-providers").setAttribute("aria-pressed", String(name === "providers"));
    $("show-control").setAttribute("aria-pressed", String(name === "control"));
    $("show-task").setAttribute("aria-pressed", String(name === "task"));
    $("show-execution").setAttribute("aria-pressed", String(name === "execution"));
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
    if ($("auto").checked) timer = setInterval(() => {
      if (document.hidden) return;
      if (!inflight) loadRange(true);
      if (!objectiveInflight && !$("objectives-view").hidden) loadObjectives(true);
      if (!doctorInflight && !$("doctor-view").hidden) loadDoctor(true);
      if (!providersInflight && !$("providers-view").hidden) loadProviders(true);
      if (!attentionInflight && (!$("attention-view").hidden || notificationsEnabled)) loadAttention(true);
      if (!diagnosticsInflight && !$("diagnostics-view").hidden) loadDiagnostics(true);
    }, POLL_MS);
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
  $("show-attention").addEventListener("click", () => {
    setView("attention");
    loadAttention(Boolean(attentionSnapshot));
  });
  $("show-diagnostics").addEventListener("click", () => {
    setView("diagnostics");
    loadDiagnostics(Boolean(diagnosticsSnapshot));
  });
  $("show-objectives").addEventListener("click", () => {
    setView("objectives");
    loadObjectives(Boolean(objectivesSnapshot));
  });
  $("show-doctor").addEventListener("click", () => {
    setView("doctor");
    loadDoctor(Boolean(doctorSnapshot));
  });
  $("show-providers").addEventListener("click", () => {
    setView("providers");
    loadProviders(Boolean(providersSnapshot));
  });
  $("show-control").addEventListener("click", () => setView("control"));
  $("show-task").addEventListener("click", () => setView("task"));
  $("show-execution").addEventListener("click", () => setView("execution"));
  $("close-detail").addEventListener("click", closeTask);
  $("close-execution").addEventListener("click", closeExecution);
  $("detail-history").addEventListener("click", () => { if (detailTaskId) enterDrill(detailTaskId); });
  $("objective-filters").addEventListener("submit", (event) => event.preventDefault());
  $("objective-search").addEventListener("input", renderObjectives);
  $("objective-state").addEventListener("change", renderObjectives);
  $("objective-sort").addEventListener("change", renderObjectives);
  $("objective-refresh").addEventListener("click", () => loadObjectives(false));
  $("doctor-refresh").addEventListener("click", () => loadDoctor(false));
  $("provider-filters").addEventListener("submit", (event) => event.preventDefault());
  $("provider-search").addEventListener("input", renderProviders);
  $("provider-capability").addEventListener("change", renderProviders);
  $("provider-posture").addEventListener("change", renderProviders);
  $("provider-sort").addEventListener("change", renderProviders);
  $("providers-refresh").addEventListener("click", () => loadProviders(false));
  $("attention-filters").addEventListener("submit", (event) => event.preventDefault());
  $("attention-search").addEventListener("input", renderAttention);
  $("attention-kind").addEventListener("change", renderAttention);
  $("attention-sort").addEventListener("change", renderAttention);
  $("attention-refresh").addEventListener("click", () => loadAttention(false));
  $("diagnostics-filters").addEventListener("submit", (event) => event.preventDefault());
  $("diagnostics-search").addEventListener("input", renderDiagnostics);
  $("diagnostics-status").addEventListener("change", renderDiagnostics);
  $("diagnostics-kind").addEventListener("change", renderDiagnostics);
  $("diagnostics-sort").addEventListener("change", renderDiagnostics);
  $("diagnostics-refresh").addEventListener("click", () => loadDiagnostics(false));
  $("close-objective").addEventListener("click", closeObjective);
  $("auto").addEventListener("change", schedule);
  $("control-action").addEventListener("change", () => { clearControlPlan(); updateControlTarget(); });
  $("control-form").addEventListener("submit", planControl);
  $("control-confirm").addEventListener("click", confirmControl);
  $("control-cancel").addEventListener("click", () => {
    clearControlPlan();
    setControlResult("Plan cancelled. No action was submitted.");
    $("control-plan").focus();
  });
  $("project-switch").addEventListener("change", switchProject);
  $("open-palette").addEventListener("click", (event) => openPalette(event.currentTarget));
  $("close-palette").addEventListener("click", closePalette);
  $("command-search").addEventListener("input", renderCommands);
  $("command-palette-form").addEventListener("submit", (event) => event.preventDefault());
  $("command-palette").addEventListener("close", () => {
    if (paletteOpener && paletteOpener.isConnected) paletteOpener.focus();
  });
  $("open-shortcuts").addEventListener("click", (event) => openShortcuts(event.currentTarget));
  $("close-shortcuts").addEventListener("click", () => $("shortcut-help").close());
  $("shortcut-help").addEventListener("close", () => {
    if (shortcutsOpener && shortcutsOpener.isConnected) shortcutsOpener.focus();
  });
  $("enable-notifications").addEventListener("click", enableNotifications);
  document.addEventListener("keydown", (event) => {
    const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName);
    if (event.ctrlKey || event.metaKey || event.altKey || typing) return;
    if (event.key === "p") { event.preventDefault(); openPalette(document.activeElement); }
    else if (event.key === "?") { event.preventDefault(); openShortcuts(document.activeElement); }
    else if (event.key === "/") { event.preventDefault(); $("task").focus(); }
    else if (event.key === "r") refresh();
    else if (event.key === "Escape" && $("command-palette").open) closePalette();
    else if (event.key === "Escape" && $("shortcut-help").open) $("shortcut-help").close();
    else if (event.key === "Escape" && !$("execution-view").hidden) closeExecution();
    else if (event.key === "Escape" && !$("task-view").hidden) closeTask();
    else if (event.key === "Escape" && !$("objective-detail").hidden) closeObjective();
    else if (event.key === "Escape" && drillState) exitDrill();
  });
  api("/api/v5/health").then((health) => { $("version").textContent = "v" + health.version; }, () => {});
  updateControlTarget();
  loadControlCapabilities();
  loadProjectProjection();
  if (!("Notification" in window)) {
    $("notification-status").textContent = "Notifications are unavailable in this browser.";
    $("enable-notifications").disabled = true;
  }
  loadRange(false);
  loadObjectives(false);
  schedule();
})();
