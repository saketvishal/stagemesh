"use strict";
// Read-only client for /api/v1 (docs/operator-ui-contract.md). The token lives in memory only.
(() => {
  const params = new URLSearchParams(location.hash.slice(1));
  const token = params.get("token") || "";
  history.replaceState(null, "", location.pathname + location.search);

  const $ = (id) => document.getElementById(id);
  const COLUMNS = [
    ["at", "Time"], ["kind", "Kind"], ["state", "State"], ["task_id", "Task"], ["stage", "Stage"],
    ["status", "Status"], ["provider", "Provider"], ["title", "Title / detail"], ["id", "Id"],
  ];
  const POLL_MS = 10000;
  let rows = new Map(); // "kind:id" -> entry, de-duplicated across pages
  let nextOffset = null;
  let sort = { key: "at", dir: -1 };
  let lastGood = null;
  let timer = null;
  let generation = 0;

  function setMessage(text, level) {
    const el = $("message");
    el.textContent = text;
    el.className = level || "";
  }

  function periodRange() {
    const now = new Date();
    const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const days = (n) => new Date(midnight.getFullYear(), midnight.getMonth(), midnight.getDate() - n);
    switch ($("period").value) {
      case "today": return [midnight, null];
      case "yesterday": return [days(1), midnight];
      case "7d": return [days(6), null];
      case "30d": return [days(29), null];
      case "custom": {
        const s = $("since").value, u = $("until").value;
        return [s ? new Date(s) : null, u ? new Date(u) : null];
      }
      default: return [null, null];
    }
  }

  function queryString(offset) {
    const q = new URLSearchParams();
    for (const key of ["state", "stage"]) if ($(key).value) q.set(key, $(key).value);
    if ($("task").value.trim()) q.set("task_id", $("task").value.trim());
    if ($("provider").value.trim()) q.set("provider", $("provider").value.trim());
    const [since, until] = periodRange();
    if (since && !isNaN(since)) q.set("since", since.toISOString());
    if (until && !isNaN(until)) q.set("until", until.toISOString());
    q.set("limit", "100");
    if (offset) q.set("offset", String(offset));
    return q.toString();
  }

  async function api(path) {
    const res = await fetch(path, { headers: { Authorization: "Bearer " + token }, cache: "no-store" });
    let body = null;
    try { body = await res.json(); } catch (_) { /* non-JSON error */ }
    if (!res.ok) {
      const err = new Error(body && body.error ? body.error.message : "HTTP " + res.status);
      err.status = res.status;
      throw err;
    }
    return body;
  }

  async function load(append) {
    const mine = ++generation;
    if (!token) { setMessage("No access token. Open the URL printed by `stagemesh serve`.", "error"); return; }
    if (!append) setMessage("Loading…");
    try {
      const page = await api("/api/v1/activity?" + queryString(append ? nextOffset : 0));
      if (mine !== generation) return; // a newer request superseded this one
      if (!append) rows = new Map();
      for (const e of page.entries) rows.set(e.kind + ":" + e.id, e);
      nextOffset = page.next_offset;
      lastGood = new Date();
      render(page);
    } catch (err) {
      if (mine !== generation) return;
      const stale = lastGood ? " Showing data from " + lastGood.toLocaleTimeString() + " (stale)." : "";
      setMessage("Could not load activity: " + err.message + "." + stale, err.status === 400 ? "error" : "warn");
      if (err.status === 401 || err.status === 403) { rows = new Map(); renderRows(); }
    }
  }

  function cmp(a, b) {
    const x = a[sort.key], y = b[sort.key];
    if (x === y) return 0;
    if (x === null || x === undefined) return 1;
    if (y === null || y === undefined) return -1;
    return (x < y ? -1 : 1) * sort.dir;
  }

  function cell(tr, text, cls) {
    const td = tr.insertCell();
    td.textContent = text === null || text === undefined ? "—" : String(text);
    if (cls) td.className = cls;
    return td;
  }

  function renderRows() {
    const body = $("rows");
    body.replaceChildren();
    for (const e of [...rows.values()].sort(cmp)) {
      const tr = body.insertRow();
      cell(tr, new Date(e.at).toLocaleString()).title = e.at;
      cell(tr, e.kind);
      cell(tr, e.state, "state-" + e.state);
      const td = cell(tr, "");
      if (e.task_id) {
        const b = document.createElement("button");
        b.type = "button"; b.textContent = e.task_id; b.title = "Show only this task";
        b.addEventListener("click", () => { $("task").value = e.task_id; load(false); });
        td.replaceChildren(b);
      } else td.textContent = "—";
      cell(tr, e.stage); cell(tr, e.status); cell(tr, e.provider);
      cell(tr, e.title || [e.execution_kind, e.event_type, e.result_code, e.candidate_sha && e.candidate_sha.slice(0, 12)].filter(Boolean).join(" "));
      cell(tr, e.id);
    }
  }

  function render(page) {
    renderRows();
    $("more").hidden = nextOffset === null;
    $("count").textContent = rows.size + " loaded of " + page.total + (page.truncated ? "+" : "");
    $("freshness").textContent = "updated " + lastGood.toLocaleTimeString();
    if (page.truncated) setMessage("Older records were excluded by the retention cap; narrow the time range for a complete view.", "warn");
    else if (rows.size === 0) setMessage("No activity matches these filters.");
    else setMessage("");
  }

  function buildHead() {
    const head = $("head");
    for (const [key, label] of COLUMNS) {
      const th = document.createElement("th");
      th.scope = "col";
      th.setAttribute("aria-sort", "none");
      const b = document.createElement("button");
      b.type = "button"; b.textContent = label;
      b.addEventListener("click", () => {
        sort = { key, dir: sort.key === key ? -sort.dir : 1 };
        for (const h of head.children) h.setAttribute("aria-sort", "none");
        th.setAttribute("aria-sort", sort.dir === 1 ? "ascending" : "descending");
        renderRows();
      });
      th.append(b);
      head.append(th);
    }
    head.children[0].setAttribute("aria-sort", "descending");
  }

  function schedule() {
    clearInterval(timer);
    if ($("auto").checked) timer = setInterval(() => { if (!document.hidden) load(false); }, POLL_MS);
  }

  buildHead();
  $("filters").addEventListener("submit", (e) => e.preventDefault());
  for (const id of ["period", "since", "until", "state", "stage"]) $(id).addEventListener("change", () => {
    for (const el of document.querySelectorAll(".custom")) el.hidden = $("period").value !== "custom";
    load(false);
  });
  for (const id of ["task", "provider"]) $(id).addEventListener("change", () => load(false));
  $("refresh").addEventListener("click", () => load(false));
  $("more").addEventListener("click", () => load(true));
  $("auto").addEventListener("change", schedule);
  document.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName);
    if (e.ctrlKey || e.metaKey || e.altKey || typing) return;
    if (e.key === "/") { e.preventDefault(); $("task").focus(); }
    else if (e.key === "r") load(false);
  });
  api("/api/v1/health").then((h) => { $("version").textContent = "v" + h.version; }, () => {});
  load(false);
  schedule();
})();
