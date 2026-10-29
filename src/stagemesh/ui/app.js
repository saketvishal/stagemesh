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
  const MAX_PAGES = 10; // 1000 retained rows / 100 per page
  let rows = new Map(); // "kind:id" -> entry, de-duplicated across pages
  let nextOffset = null;
  let sort = { key: "at", dir: -1 };
  let lastGood = null;
  let timer = null;
  let generation = 0;
  let loadedPages = 1;
  let wantedPages = 1;
  let inflight = false;

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

  // The operator's request is a page range (`wantedPages`), not an event. "Load more" raises it immediately, and every
  // fetch (initial, refresh, poll) re-reads pages 1..wantedPages, so an accepted Load more cannot be lost to a refresh.
  async function loadRange(quiet) {
    const mine = ++generation;
    if (!token) { setMessage("No access token. Open the URL printed by `stagemesh serve`.", "error"); return; }
    inflight = true;
    if (!quiet) setMessage("Loading…");
    const target = wantedPages;
    try {
      const fresh = new Map();
      let offset = 0;
      let page;
      let fetched = 0;
      do {
        page = await api("/api/v1/activity?" + queryString(offset));
        if (mine !== generation) return; // a newer request for the same range superseded this one
        for (const e of page.entries) fresh.set(e.kind + ":" + e.id, e);
        offset = page.next_offset;
        fetched += 1;
      } while (fetched < target && offset !== null);
      rows = fresh;
      nextOffset = page.next_offset;
      loadedPages = fetched;
      wantedPages = fetched;
      lastGood = new Date();
      inflight = false;
      render(page);
    } catch (err) {
      if (mine !== generation) return;
      inflight = false;
      wantedPages = loadedPages; // keep the last good range; a failed Load more leaves no phantom request
      const stale = lastGood ? " Showing data from " + lastGood.toLocaleTimeString() + " (stale)." : "";
      setMessage("Could not load activity: " + err.message + "." + stale, err.status === 400 ? "error" : "warn");
      if ([400, 401, 403].includes(err.status)) {
        $("auto").checked = false;
        schedule(); // terminal request/access errors require an explicit correction and polling opt-in
      }
      if (err.status === 401 || err.status === 403) { rows = new Map(); renderRows(); }
    }
  }

  const refresh = () => loadRange(false);
  const reload = () => { wantedPages = 1; loadRange(false); }; // a changed filter starts again from page one
  const loadMore = () => {
    if (nextOffset === null) return;
    wantedPages = Math.min(wantedPages + 1, MAX_PAGES);
    loadRange(false);
  };

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
    const focused = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.key : null;
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
        b.dataset.key = e.kind + ":" + e.id;
        b.addEventListener("click", () => { $("task").value = e.task_id; reload(); });
        td.replaceChildren(b);
      } else td.textContent = "—";
      cell(tr, e.stage); cell(tr, e.status); cell(tr, e.provider);
      cell(tr, e.title || [e.execution_kind, e.event_type, e.result_code, e.candidate_sha && e.candidate_sha.slice(0, 12)].filter(Boolean).join(" "));
      cell(tr, e.id);
    }
    // Rebuilding rows must not drop keyboard focus (auto-refresh runs every few seconds).
    if (focused) {
      const again = [...body.querySelectorAll("button")].find((b) => b.dataset.key === focused);
      if (again) again.focus();
      else $("table").focus(); // the focused row is gone from the new result: fall back to a predictable target
    }
  }

  function render(page) {
    renderRows();
    const moreHadFocus = document.activeElement === $("more");
    $("more").hidden = nextOffset === null;
    if (moreHadFocus && nextOffset === null) $("table").focus(); // the button vanished; keep focus in the page
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
    if ($("auto").checked) timer = setInterval(() => { if (!document.hidden && !inflight) loadRange(true); }, POLL_MS);
  }

  buildHead();
  $("filters").addEventListener("submit", (e) => e.preventDefault());
  for (const id of ["period", "since", "until", "state", "stage"]) $(id).addEventListener("change", () => {
    for (const el of document.querySelectorAll(".custom")) el.hidden = $("period").value !== "custom";
    reload();
  });
  for (const id of ["task", "provider"]) $(id).addEventListener("change", reload);
  $("refresh").addEventListener("click", refresh);
  $("more").addEventListener("click", loadMore);
  $("auto").addEventListener("change", schedule);
  document.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName);
    if (e.ctrlKey || e.metaKey || e.altKey || typing) return;
    if (e.key === "/") { e.preventDefault(); $("task").focus(); }
    else if (e.key === "r") refresh();
  });
  api("/api/v1/health").then((h) => { $("version").textContent = "v" + h.version; }, () => {});
  loadRange(false);
  schedule();
})();
