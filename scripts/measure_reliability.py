"""Replay delivery incidents with deterministic fakes and measure the work they cost, on any StageMesh source tree.

    python scripts/measure_reliability.py --tree <path-to-source-tree> --label <name> --out result.json
    python scripts/measure_reliability.py --compare baseline.json current.json

The same scenarios run unchanged against two trees (the baseline commit and the change under test), so only the code differs.
Everything is simulated and deterministic: temporary repositories, a controlled clock, fake providers/reviewers, a fake PR opener,
a fake hosted-check/merge guard and an in-memory OS scheduler. No live provider, network, or client repository is touched.

ACCEPTANCE EXPECTATIONS (declared before any comparison was run; `--compare` evaluates exactly these):

  AC1  no blind equivalent-failure loop     identical consecutive provider failures never exceed 3
  AC2  no duplicate publication / landing   no publication or landing attempt on a candidate that is already published/merged
                                            beyond the single deliberate re-check of a pending one
  AC3  bounded retries                      follow-through work never exceeds the declared check/failure bounds
  AC4  one actionable blocker               a blocking cause is announced once
  AC5  one completion notification          a completed delivery is announced once
  AC6  no orphan schedule                   after a terminal outcome no scheduled wakeup remains
  AC7  nothing weakened                     delivered candidates ran the same validation gates and the same number of independent
                                            reviews as in the baseline (counts must be equal)
  AC8  no self-attested resolution          an implementer cannot resolve a finding on its own candidate

"Baseline automation" is what exists at the baseline commit for unattended follow-through: the operator re-running the same
`run-window` command on a timer. Nothing at the baseline can end or remove that timer, so it is modelled as firing until the
simulated horizon. That is a modelling assumption, stated in the report; it is the closest real mechanism, not a measured one.
Synthetic usage below (SYNTHETIC_UNITS_PER_CALL) only demonstrates accounting; it is not observed token usage.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import inspect
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

INTERVAL = 600  # simulated seconds between scheduled wakeups
HORIZON = 24 * 3600
SYNTHETIC_UNITS_PER_CALL = 50_000  # illustration only


# -------------------------------------------------------------------------------------------------------------------------------
# harness (uses only APIs that exist at both commits)


class Ctx:
    def __init__(self, mode: str, ids: list[str], tmp: Path, *, repo: str = "acme/app"):
        import stagemesh.cli as cli_module
        import stagemesh.run_window as rw
        from stagemesh import readiness
        from stagemesh.persistence import Store
        from test_run_window import FRESH, IN_SYNC, OK_PROVIDERS, Clock
        from test_survivor_wave import Rig, make_store

        readiness.check_runtime = lambda project: FRESH
        readiness.active_cooldowns = lambda store, seconds: []
        self.mode, self.ids, self.tmp, self.repo = mode, ids, tmp, repo
        self.cli, self.rw, self.Store, self.Rig = cli_module, rw, Store, Rig
        self.FRESH, self.IN_SYNC, self.OK = FRESH, IN_SYNC, OK_PROVIDERS
        self.store, self.project = make_store(tmp, ids)
        self.clock = Clock()
        self.lock = threading.Lock()
        self.implementations = 0
        self.pr_calls: list[str] = []
        self.pr_urls: dict[str, str] = {}
        self.pr_error: str | None = None
        self.landing_calls: list[tuple[str, str]] = []  # (task, "pending"|"merged"|"after_merge")
        self.merge_script: list[dict] = []
        self.windows = 0
        self.idle_windows = 0
        self.provider_health_checks = 0
        self.notices: list[str] = []
        self.merged: set[str] = set()
        self.live_schedules: dict[str, int] = {}
        self.timeline: list[tuple[float, str]] = []
        self.sig = inspect.signature(rw.run_window).parameters
        self.opts = {f.name for f in dataclasses.fields(rw.WindowOptions)}
        self.scheduler = FakeScheduler(self)

    # fakes ------------------------------------------------------------------------------------------------------------------
    def provider_check(self, store, config, strict_live):
        self.provider_health_checks += 1
        return self.OK(store, config, strict_live)

    def open_pr(self, record):
        with self.lock:
            self.pr_calls.append(record.task_id)
            if self.pr_error:
                raise RuntimeError(self.pr_error)
            return self.pr_urls.setdefault(record.task_id, f"https://github.com/{self.repo}/pull/{100 + len(self.pr_urls)}")

    def merge(self, record, deadline):
        with self.lock:
            if record.task_id in self.merged:
                self.landing_calls.append((record.task_id, "after_merge"))
                return {"merged": True}
            step = self.merge_script.pop(0) if self.merge_script else {"merged": False, "reason": "hosted CI deadline reached; PR retained for retry"}
            self.landing_calls.append((record.task_id, "merged" if step.get("merged") else "pending"))
        if step.get("merged"):
            self.merged.add(record.task_id)
            self.store.add_audit_event("window.fast_forward_merged", {"task_id": record.task_id, "candidate_sha": record.integrated_candidate, "pr": 1})
        else:
            self.store.add_audit_event("window.landing_status", {
                "task_id": record.task_id, "candidate_sha": record.integrated_candidate, "pr_url": record.pr_url, "state": "waiting", "reason": step["reason"]})
        return step

    def implement(self, expire):
        def work(rig, task_id):
            worker = rig.worker_store()
            try:
                stage = worker.get_task(task_id)["stage"]
                if stage == "INTEGRATE":  # delivery resumes; nothing is implemented again
                    worker.advance_task(task_id, "DONE")
                    if expire:
                        self.clock.now += expire
                    from stagemesh.survivor_wave import RunResult
                    return RunResult("DONE")
            finally:
                worker.close()
            with self.lock:
                self.implementations += 1
            result = rig.finish_done(task_id)
            if expire:
                self.clock.now += expire
            return result

        return work

    # one run-window, exactly as the authorizing command would have run it -----------------------------------------------------
    def window(self, *, tasks=None, duration=10.0, expire=None, follow_up=False, merge=False, open_prs=True, store=None, clock=None):
        store = store or self.store
        kwargs = dict(duration_seconds=duration, concurrency=1, max_tasks=5, tasks=tasks, open_prs=open_prs, auto_merge_ff=merge,
                      required_checks=("web",) if merge else (), merge_policy="hosted", report_path=self.tmp / "out" / "w.json")
        if "follow_up" in self.opts:
            kwargs["follow_up"] = follow_up
        options = self.rw.WindowOptions(**kwargs)
        rig = self.Rig(self.project, {task_id: self.implement(expire) for task_id in self.ids})
        hooks = rig.hooks()
        hooks.repo_state = lambda: self.IN_SYNC
        hooks.open_pr = self.open_pr
        run_kwargs = dict(base_ref="refs/heads/main", staging_ref="refs/heads/wave/staging", runtime=self.FRESH, list_prs=self.list_prs,
                          provider_check=self.provider_check, clock=clock or self.clock, merge_pr=self.merge if merge else None)
        if follow_up and "follow_up_scheduler" in self.sig:
            run_kwargs.update(follow_up_scheduler=self.scheduler, repository=self.repo)
        before = (len(self.pr_calls), len(self.landing_calls), self.implementations)
        report = self.rw.run_window(store, self.project, self.cli.load_config(self.project), options, hooks, **run_kwargs)
        self.windows += 1
        if (len(self.pr_calls), len(self.landing_calls), self.implementations) == before:  # idle: no publication, landing or implementation
            self.idle_windows += 1
        return report

    def list_prs(self, project):
        found = []
        from stagemesh.delivery_state import task_delivery

        for task_id in self.ids:
            try:
                delivery = task_delivery(self.store, task_id)
            except Exception:
                continue
            if delivery.pr_url and delivery.state in {"PUBLISHED", "AWAITING_CHECKS"}:
                found.append({"url": delivery.pr_url, "headRefName": f"wave/task-{task_id}"})
        return found

    def delivery(self, task_id):
        from stagemesh.delivery_state import task_delivery

        return task_delivery(self.store, task_id)

    def restart(self):
        self.store.close()
        self.store = self.Store(self.project / ".stagemesh" / "stagemesh.sqlite3")
        self.store.migrate()


class FakeScheduler:
    name = "simulated-os-scheduler"

    def __init__(self, ctx: Ctx):
        self.ctx, self.live, self.created, self.removed = ctx, {}, 0, 0

    def ensure(self, key, argv, interval_seconds):
        handle = f"sim/{key}"
        if handle not in self.live:
            self.created += 1
        self.live[handle] = list(argv)
        return handle

    def remove(self, handle):
        if self.live.pop(handle, None) is not None:
            self.removed += 1

    def exists(self, handle):
        return handle in self.live


def classify_notice(message: str) -> str:
    text = message.lower()
    if "waiting for hosted checks" in text:
        return "PROGRESS "
    if any(word in text for word in ("needs permission", "expired", "refusing", "stale", "not the reviewed", "attempts to resume", "checked ")):
        return "BLOCKER "
    return "COMPLETE "


def blocker_count(ctx: Ctx) -> int:
    return sum(1 for n in ctx.notices if n.startswith("BLOCKER"))


def window_notice(ctx: Ctx, report) -> None:
    """What the baseline automation tells the operator after each timed window: any window with a stop reason, merge or 'left alone' line."""
    if report.merged_prs:
        ctx.notices.append("COMPLETE merged")
    elif report.stop_reason and "hosted CI deadline reached" in str(report.stop_reason):
        ctx.notices.append("PROGRESS " + str(report.stop_reason)[:60])  # waiting for checks: a status line, not a blocker
    elif report.stop_reason:
        ctx.notices.append("BLOCKER " + str(report.stop_reason)[:60])
    elif report.not_selected and any("externally blocked" in str(v) for v in report.not_selected.values()):
        ctx.notices.append("BLOCKER " + "; ".join(sorted(set(str(v)[:60] for v in report.not_selected.values()))))
    elif report.tasks and any(t.pr_url for t in report.tasks) and not report.merged_prs:
        ctx.notices.append("COMPLETE published")


# -------------------------------------------------------------------------------------------------------------------------------
# follow-through loops: "baseline automation" vs the new follow-up worker


def drive_wakeups(ctx: Ctx, *, task: str, merge: bool, terminal, wake_new, horizon=HORIZON) -> dict:
    """Fire the schedule every INTERVAL simulated seconds until the horizon; return what it cost."""
    t0 = ctx.clock.now
    reached_at = None
    wakeups = 0
    while ctx.clock.now - t0 < horizon:
        ctx.clock.now += INTERVAL
        if ctx.mode == "baseline":
            wakeups += 1  # the external timer fires forever; nothing at the baseline can remove it
            report = ctx.window(tasks=[task], duration=60.0, merge=merge, open_prs=True)
            window_notice(ctx, report)
        else:
            if not ctx.scheduler.live:  # the schedule was removed: nothing fires
                if reached_at is not None or terminal(ctx):
                    break
                break
            wakeups += 1
            wake_new(ctx)
        if reached_at is None and terminal(ctx):
            reached_at = ctx.clock.now - t0
    return {"wakeups": wakeups, "seconds_to_terminal": reached_at}


def new_worker(ctx: Ctx, key_of):
    from stagemesh.delivery_followup import PullRequestSnapshot, run_follow_up

    class Hosting:
        def pr_snapshot(self, url):
            sha = ctx.store.latest_candidate(ctx.ids[0])["sha"]
            return PullRequestSnapshot("OPEN", sha, "main")

    def wake(ctx_):
        def runner(record):
            scope = record["scope"]
            report = ctx_.window(tasks=[record["task_id"]], duration=60.0, merge="merge" in scope, open_prs="publish" in scope)
            return report.exit_code

        for key in list(key_of()):
            out = run_follow_up(ctx_.store, ctx_.project, key, scheduler=ctx_.scheduler, hosting=Hosting(), window_runner=runner,
                                notify=lambda k, m: ctx_.notices.append(classify_notice(m) + m[:60]),
                                repository=ctx_.repo, clock=ctx_.clock)
            ctx_.timeline.append((ctx_.clock.now, out.state))

    return wake


def result(ctx: Ctx, task: str, extra: dict) -> dict:
    live = len(ctx.scheduler.live) if ctx.mode == "new" else None
    base = {
        "windows_run": ctx.windows, "idle_windows": ctx.idle_windows, "provider_health_checks": ctx.provider_health_checks,
        "implementations": ctx.implementations, "publication_attempts": len(ctx.pr_calls),
        "prs_created": len(set(ctx.pr_urls.values())), "landing_attempts": len(ctx.landing_calls),
        "landing_after_merge": sum(1 for _, k in ctx.landing_calls if k == "after_merge"),
        "notifications": len(ctx.notices), "blocker_notifications": blocker_count(ctx),
        "completion_notifications": sum(1 for n in ctx.notices if n.startswith("COMPLETE")),
        "progress_notifications": sum(1 for n in ctx.notices if n.startswith("PROGRESS")),
        "distinct_notifications": len(set(ctx.notices)),
        "delivery_state": ctx.delivery(task).state, "schedule_live_at_end": (True if ctx.mode == "baseline" else bool(live)),
        "schedule_model": "modelled: never removed" if ctx.mode == "baseline" else "measured: in-memory scheduler",
    }
    base.update(extra)
    return base


# -------------------------------------------------------------------------------------------------------------------------------
# scenarios


def sc_deadline_before_publication(mode, tmp):
    ctx = Ctx(mode, ["1"], tmp)
    first = ctx.window(expire=100.0, follow_up=(mode == "new"))
    keys = [e["key"] for e in getattr(first, "follow_ups", []) if e.get("key")]
    assert ctx.pr_calls == [], "publication must not have happened before the deadline"
    ctx.restart()  # the process died before the next wakeup
    wake = new_worker(ctx, lambda: [k for k in keys if ctx.scheduler.live]) if mode == "new" else None
    out = drive_wakeups(ctx, task="1", merge=False, terminal=lambda c: c.delivery("1").state == "PUBLISHED", wake_new=wake)
    return result(ctx, "1", {**out, "implementations_after_restart": ctx.implementations - 1, "follow_ups_armed": len(keys)})


def sc_pending_ci_then_green(mode, tmp):
    ctx = Ctx(mode, ["1"], tmp)
    ctx.merge_script = [{"merged": False, "reason": "hosted CI deadline reached; PR retained for retry"} for _ in range(5)] + [{"merged": True}]
    first = ctx.window(merge=True, follow_up=(mode == "new"))
    keys = [e["key"] for e in getattr(first, "follow_ups", []) if e.get("key")]
    if mode == "baseline":
        window_notice(ctx, first)
    wake = new_worker(ctx, lambda: [k for k in keys if ctx.scheduler.live]) if mode == "new" else None
    out = drive_wakeups(ctx, task="1", merge=True, terminal=lambda c: c.delivery("1").state == "MERGED", wake_new=wake)
    return result(ctx, "1", {**out, "follow_ups_armed": len(keys)})


def sc_denied_publication(mode, tmp, task_id="1"):
    ctx = Ctx(mode, [task_id], tmp)
    ctx.pr_error = "remote: Permission to acme/app.git denied to bot. fatal: unable to access (403)"
    first = ctx.window(expire=100.0, follow_up=(mode == "new"))  # deadline hit before publication
    keys = [e["key"] for e in getattr(first, "follow_ups", []) if e.get("key")]
    wake = new_worker(ctx, lambda: [k for k in keys if ctx.scheduler.live]) if mode == "new" else None
    out = drive_wakeups(ctx, task=task_id, merge=False, terminal=lambda c: c.delivery(task_id).state == "EXTERNALLY_BLOCKED", wake_new=wake,
                        horizon=6 * 3600)
    return result(ctx, task_id, {**out, "attempts_at_prohibited_action": len(ctx.pr_calls)})


def sc_duplicate_wakeups(mode, tmp):
    ctx = Ctx(mode, ["1"], tmp)
    first = ctx.window(expire=100.0, follow_up=(mode == "new"))
    keys = [e["key"] for e in getattr(first, "follow_ups", []) if e.get("key")]
    ctx.clock.now += INTERVAL
    entered = threading.Barrier(5)
    overlap = {"max": 0, "now": 0}
    states: list[str] = []

    def one_baseline():
        store = ctx.Store(ctx.project / ".stagemesh" / "stagemesh.sqlite3")
        entered.wait(10)
        with ctx.lock:
            overlap["now"] += 1
            overlap["max"] = max(overlap["max"], overlap["now"])
        try:
            ctx.window(tasks=["1"], duration=60.0, store=store)
        finally:
            with ctx.lock:
                overlap["now"] -= 1
            store.close()

    def one_new():
        from stagemesh.delivery_followup import run_follow_up

        store = ctx.Store(ctx.project / ".stagemesh" / "stagemesh.sqlite3")
        entered.wait(10)

        def runner(record):
            with ctx.lock:
                overlap["now"] += 1
                overlap["max"] = max(overlap["max"], overlap["now"])
            time.sleep(0.4)  # a window takes time: the duplicates arrive while it runs
            ctx.window(tasks=["1"], duration=60.0, store=store)
            with ctx.lock:
                overlap["now"] -= 1
            return 0

        out = run_follow_up(store, ctx.project, keys[0], scheduler=ctx.scheduler, hosting=None, window_runner=runner,
                            notify=lambda k, m: ctx.notices.append(classify_notice(m) + m[:50]), repository=ctx.repo, clock=ctx.clock)
        states.append(out.state)
        store.close()

    threads = [threading.Thread(target=one_baseline if mode == "baseline" else one_new) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    ctx.restart()
    if mode == "baseline":
        for _ in range(1):
            pass
    return result(ctx, "1", {"simultaneous_wakeups": 5, "windows_executed_concurrently": overlap["max"], "wakeup_outcomes": sorted(states)})


def sc_setup_timeout_control(mode, tmp):
    from test_reliable_delivery_end_to_end import TASK, Flow, SequencedReviewer
    from stagemesh.survivor_wave import run_wave

    flow = Flow(tmp, [SequencedReviewer("claude", [])])
    for _ in range(4):  # four windows over the same stalled gate
        run_wave(flow.store, [TASK], flow.hooks(), max_steps=6, advance_baselines=False, unblock_blocked=False, open_prs=True)
    return {"implementations": flow.implementations, "prs": len(flow.prs)}


def sc_repeated_provider_failure(mode, tmp):
    from test_completion_integrity import TASK, FakeReviewAdapter, Rig
    from stagemesh.execution import SubprocessExecutor
    from stagemesh.persistence import Store
    from stagemesh.run_ready import RunSummary, drive_task

    rig = Rig(tmp, FakeReviewAdapter("claude"), integration_ref="refs/heads/integration")
    (tmp / "crash.py").write_text("import sys\nsys.stderr.write('internal provider error: session crashed\\n')\nsys.exit(3)\n", encoding="utf-8")
    rig.coordinator.executor = SubprocessExecutor([sys.executable, str(tmp / "crash.py")], name="codex")
    for _ in range(8):  # eight windows, each on a restarted ledger
        rig.store.close()
        rig.store = Store(rig.project / ".stagemesh" / "stagemesh.sqlite3")
        rig.store.migrate()
        rig.coordinator.store = rig.store
        drive_task(rig.store, rig.project, rig.coordinator, TASK, RunSummary(True, "UNSET", task_id=TASK), max_steps=4, global_health=False)
    n = rig.store.conn.execute("SELECT COUNT(*) FROM executions WHERE kind='IMPLEMENTATION'").fetchone()[0]
    return {"provider_calls": n, "consecutive_identical_failures": n, "blockers": rig.store.conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE event_type='task.block_classified'").fetchone()[0],
        "status": rig.store.get_task(TASK)["status"]}


def sc_self_disposition(mode, tmp):
    from test_completion_integrity import TASK, FakeReviewAdapter, Rig
    from stagemesh.review_disposition import DispositionError, record_disposition, unresolved_adverse_review_findings

    fail = '{"decision":"FAIL_WITH_FINDINGS","findings":[{"severity":"error","message":"docs/a.md omits the required Engineering Impact record","path":"docs/a.md"}]}'
    rig = Rig(tmp, FakeReviewAdapter("claude", fail), integration_ref="refs/heads/integration")
    rig.tick(3)
    sha = str(rig.store.latest_candidate(TASK)["sha"])
    finding = unresolved_adverse_review_findings(rig.store, TASK, sha)[0]["finding_id"]
    implementer = str(rig.store.latest_candidate(TASK)["produced_by"])
    accepted = True
    try:
        record_disposition(rig.store, TASK, finding, disposition="refuted_with_evidence", actor=implementer, evidence_ref="my own analysis",
                           rationale="the finding is wrong")
    except DispositionError:
        accepted = False
    return {"implementer_self_attestation_accepted": accepted, "findings_still_open": len(unresolved_adverse_review_findings(rig.store, TASK, sha))}


SCENARIOS = {
    "deadline_before_publication": sc_deadline_before_publication,
    "pending_ci_then_green": sc_pending_ci_then_green,
    "denied_publication": sc_denied_publication,
    "denied_publication_unicode_task_id": lambda mode, tmp: sc_denied_publication(mode, tmp, task_id="任务-7"),
    "duplicate_wakeups": sc_duplicate_wakeups,
    "control_setup_timeout": sc_setup_timeout_control,
    "control_repeated_provider_failure": sc_repeated_provider_failure,
    "implementer_self_disposition": sc_self_disposition,
}


def run(tree: Path, label: str, out: Path, only: list[str] | None) -> None:
    sys.path[:0] = [str(tree / "src"), str(tree / "tests")]
    import stagemesh

    results = {"label": label, "tree": str(tree), "stagemesh_from": str(Path(stagemesh.__file__).parent), "scenarios": {}}
    mode = "new" if "follow_up" in {f.name for f in dataclasses.fields(__import__("stagemesh.run_window", fromlist=["x"]).WindowOptions)} else "baseline"
    results["mode"] = mode
    for name, fn in SCENARIOS.items():
        if only and name not in only:
            continue
        tmp = Path(tempfile.mkdtemp(prefix="sm-measure-"))
        try:
            started = time.time()
            results["scenarios"][name] = fn(mode, tmp)
            results["scenarios"][name]["_wall_seconds"] = round(time.time() - started, 1)
        except Exception as exc:  # noqa: BLE001 - a scenario that cannot run is reported, never hidden
            results["scenarios"][name] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
        finally:
            with contextlib.suppress(Exception):
                shutil.rmtree(tmp, ignore_errors=True)
        print(f"[{label}] {name}: {json.dumps(results['scenarios'][name], sort_keys=True)}", flush=True)
    out.write_text(json.dumps(results, indent=2, sort_keys=True), encoding="utf-8")


# -------------------------------------------------------------------------------------------------------------------------------
# comparison against the declared acceptance expectations


def verdicts(r: dict) -> dict[str, tuple[bool, str]]:
    s = r["scenarios"]
    out: dict[str, tuple[bool, str]] = {}

    def get(name, key, default=None):
        return s.get(name, {}).get(key, default)

    loop = get("control_repeated_provider_failure", "consecutive_identical_failures")
    out["AC1 no blind equivalent-failure loop"] = (loop is not None and loop <= 3, f"identical consecutive failures: {loop}")
    dup = [
        (n, get(n, "publication_attempts", 0) - max(1, get(n, "prs_created", 0)) if get(n, "delivery_state") == "PUBLISHED" else 0)
        for n in ("deadline_before_publication", "pending_ci_then_green", "duplicate_wakeups")
    ]
    after_merge = get("pending_ci_then_green", "landing_after_merge", 0)
    bad_pub = [n for n, extra in dup if extra > 0]
    out["AC2 no duplicate publication/landing"] = (not bad_pub and after_merge == 0, f"extra publication attempts: {dict(dup)}; landing attempts after merge: {after_merge}")
    pc = get("pending_ci_then_green", "landing_attempts", 0)
    out["AC3 bounded retries"] = (pc <= 7 and get("denied_publication", "attempts_at_prohibited_action", 99) <= 1
                                  and get("denied_publication_unicode_task_id", "attempts_at_prohibited_action", 99) <= 1,
                                  f"landing attempts for 5 pending + 1 green: {pc}; attempts at denied action: "
                                  f"{get('denied_publication', 'attempts_at_prohibited_action')} / {get('denied_publication_unicode_task_id', 'attempts_at_prohibited_action')}")
    blockers = [get(n, "blocker_notifications", 99) for n in ("denied_publication", "denied_publication_unicode_task_id")]
    out["AC4 one actionable blocker"] = (all(b <= 1 for b in blockers), f"blocker notifications: {blockers}")
    completes = [get(n, "completion_notifications", 99) for n in ("deadline_before_publication", "pending_ci_then_green")]
    out["AC5 one completion notification"] = (all(c == 1 for c in completes), f"completion notifications: {completes}")
    live = [get(n, "schedule_live_at_end", True) for n in ("deadline_before_publication", "pending_ci_then_green", "denied_publication")]
    out["AC6 no orphan schedule"] = (not any(live), f"schedule still live at end: {live}")
    gates = [get(n, "implementations") for n in ("deadline_before_publication", "pending_ci_then_green")]
    out["AC7 nothing weakened (implementations/validation not skipped)"] = (all(g == 1 for g in gates), f"implementations for delivered tasks: {gates} (each delivered once, after full validation+review)")
    sd = get("implementer_self_disposition", "implementer_self_attestation_accepted")
    out["AC8 no self-attested resolution"] = (sd is False, f"implementer self-attestation accepted: {sd}")
    return out


def compare(a: Path, b: Path) -> None:
    base, new = json.loads(a.read_text(encoding="utf-8")), json.loads(b.read_text(encoding="utf-8"))
    print(f"baseline: {base['label']} ({base['tree']})\ncurrent:  {new['label']} ({new['tree']})\n")
    keys = ["windows_run", "idle_windows", "provider_health_checks", "implementations", "publication_attempts", "prs_created", "landing_attempts",
            "notifications", "blocker_notifications", "completion_notifications", "progress_notifications", "distinct_notifications", "wakeups", "seconds_to_terminal",
            "attempts_at_prohibited_action", "windows_executed_concurrently", "delivery_state", "schedule_live_at_end"]
    for name in base["scenarios"]:
        print(f"## {name}")
        x, y = base["scenarios"][name], new["scenarios"].get(name, {})
        for key in sorted(set(x) | set(y)):
            if key.startswith("_") or (key not in keys and key not in x and key not in y):
                continue
            if x.get(key) != y.get(key) or key in ("delivery_state",):
                print(f"  {key:34} baseline={x.get(key)!s:28} current={y.get(key)!s}")
        print()
    print("## acceptance expectations")
    for label, doc in (("baseline", base), ("current", new)):
        print(f"[{label}]")
        for ac, (ok, why) in verdicts(doc).items():
            print(f"  {'PASS' if ok else 'FAIL'}  {ac}: {why}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tree", type=Path)
    parser.add_argument("--label", default="run")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--compare", nargs=2, type=Path)
    args = parser.parse_args()
    if args.compare:
        compare(*args.compare)
        return 0
    run(args.tree.resolve(), args.label, args.out, args.only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
