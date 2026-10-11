"""One read-only page answering "can I trust this checkout to run work right now, and what needs a person?".

Everything here only reads: it fetches nothing from git, removes no worktree, starts no task and runs no provider. The one network
call (listing open wave PRs through `gh`) is injectable and skippable, and its failure is reported, never raised.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .baseline import resolve_integration_ref
from .config import StageMeshConfig
from .persistence import Store
from .provider_acceptance import live_provider_readiness
from .provider_cooldown import active_cooldowns
from .providers import adapters_from_config, stage_command_summary
from .runtime_freshness import RuntimeFreshness, check_runtime
from .survivor_wave import blocked_needs_decision, git_repo_state

PrLister = Callable[[Path], list[dict[str, Any]]]


def gh_open_wave_prs(project: Path) -> list[dict[str, Any]]:
    """Open PRs whose head branch is a wave branch (`wave/...`). Raises when `gh` cannot answer."""
    proc = subprocess.run(
        ["gh", "pr", "list", "--state", "open", "--limit", "100", "--json", "number,title,headRefName,url,baseRefName"],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip()[-200:] or "gh pr list failed")
    return [pr for pr in json.loads(proc.stdout or "[]") if str(pr.get("headRefName", "")).startswith("wave/")]


def _runtime_block(project: Path, freshness: RuntimeFreshness, version_warning: str | None) -> dict[str, Any]:
    return {
        "stale": freshness.stale or bool(version_warning),
        "installed_cli": freshness.installed_cli,
        "source_cli": freshness.source_cli,
        "running_from": freshness.running_from,
        "running_from_source": freshness.running_from_source,
        "missing_from_installed": freshness.missing_from_installed,
        "version_warning": version_warning,
        "run_from_source": freshness.source_run,
        "reinstall": freshness.refresh_command or (f'python "{project / "scripts" / "bootstrap_stagemesh.py"}"' if version_warning else None),
        "message": freshness.message,
    }


def build_readiness(
    store: Store,
    project: Path,
    config: StageMeshConfig,
    *,
    version_warning: str | None = None,
    list_prs: PrLister | None = gh_open_wave_prs,
    runtime: RuntimeFreshness | None = None,
) -> dict[str, Any]:
    """The readiness report as plain data. `list_prs=None` skips the network lookup."""
    project = Path(project).resolve()
    attention: list[str] = []
    data: dict[str, Any] = {}

    freshness = runtime or check_runtime(project)
    data["runtime"] = _runtime_block(project, freshness, version_warning)
    if data["runtime"]["stale"]:
        attention.append("installed runtime is stale")

    if list_prs is None:
        data["wave_prs"] = {"available": False, "reason": "skipped (--no-network)", "prs": []}
    else:
        try:
            data["wave_prs"] = {"available": True, "prs": list_prs(project)}
            if data["wave_prs"]["prs"]:
                attention.append(f"{len(data['wave_prs']['prs'])} open wave PR(s) waiting for merge")
        except Exception as exc:  # noqa: BLE001 - an unreachable GitHub must not hide the rest of the report
            data["wave_prs"] = {"available": False, "reason": f"{type(exc).__name__}: {exc}", "prs": []}

    try:
        data["provider_stage_commands"] = {a.name: stage_command_summary(a) for a in sorted(adapters_from_config(config), key=lambda a: a.name)}
    except Exception:  # noqa: BLE001 - a broken provider config is reported by `doctor` elsewhere
        data["provider_stage_commands"] = {}
    base_ref = resolve_integration_ref(project, config.integration_ref) or ""
    census: dict[str, Any] | None = None
    if base_ref.startswith("refs/heads/"):
        try:
            census = git_repo_state(store, project, base_ref)().to_dict()
        except Exception as exc:  # noqa: BLE001
            data["census_error"] = f"{type(exc).__name__}: {exc}"
    data["main"] = None if census is None else {k: census[k] for k in ("main_ref", "main_sha", "origin_sha", "ahead", "behind", "relation")}
    data["activity"] = None if census is None else {k: census[k] for k in ("running_executions", "active_claims")}
    data["worktrees"] = (
        None
        if census is None
        else {k: census[k] for k in ("cleanable_worktrees", "done_unmerged_worktrees", "resumable_worktrees", "other_worktrees")}
    )
    if census is not None:
        if census["relation"] in ("ahead", "behind", "diverged"):
            attention.append(f"local {census['main_ref']} is {census['relation']} origin")
        if census["running_executions"] or census["active_claims"]:
            attention.append("work is in flight (running executions or active claims)")

    decisions = []
    for row in store.tasks():
        if str(row["status"]) == "BLOCKED":
            reason = blocked_needs_decision(store, str(row["id"]))
            if reason:
                decisions.append({"task_id": str(row["id"]), "title": str(row["title"]), "reason": reason})
    data["needs_decision"] = decisions
    if decisions:
        attention.append(f"{len(decisions)} task(s) need a design decision")

    cooldowns = {str(item["provider"]): item for item in active_cooldowns(store, config.provider_failure_cooldown_seconds)}
    providers = []
    try:
        names = sorted(adapter.name for adapter in adapters_from_config(config))
    except Exception:  # noqa: BLE001 - a broken provider config is reported by `doctor` elsewhere
        names = sorted(cooldowns)
    for name in names:
        ok, reason = live_provider_readiness(store, name, "code")
        cooldown = cooldowns.get(name)
        providers.append(
            {
                "provider": name,
                "live_acceptance": "passed" if ok else reason,
                "cooldown": None
                if cooldown is None
                else {"failure_class": cooldown["failure_class"], "seconds_remaining": cooldown["seconds_remaining"], "clear_command": cooldown["clear_command"]},
            }
        )
        if cooldown is not None:
            attention.append(f"provider {name} is cooling down ({cooldown['failure_class']})")
    data["providers"] = providers
    data["attention"] = attention
    data["verdict"] = "attention" if attention else "ready"
    return data


def format_readiness(data: dict[str, Any]) -> list[str]:
    runtime = data["runtime"]
    lines = [f"operational readiness: {data['verdict'].upper()}"]
    lines += [f"  needs attention: {item}" for item in data["attention"]]
    lines.append(f"runtime: {'STALE' if runtime['stale'] else 'current'}")
    lines.append(f"  installed command: {runtime['installed_cli'] or 'not found'}")
    lines.append(f"  source command:    {runtime['source_cli'] or 'not found'} (this process runs from {runtime['running_from']})")
    if runtime["missing_from_installed"]:
        lines.append(f"  missing from installed: {', '.join(runtime['missing_from_installed'])}")
    if runtime["version_warning"]:
        lines.append(f"  {runtime['version_warning']}")
    if runtime["stale"]:
        lines.append(f"  run from source now: {runtime['run_from_source']} <command> ...")
        if runtime["reinstall"]:
            lines.append(f"  reinstall (not done automatically): {runtime['reinstall']}")
    if data.get("provider_stage_commands"):
        lines.append("provider commands (one logical provider; a stage override only changes what runs for that stage):")
        for name, stages in data["provider_stage_commands"].items():
            detail = "; ".join(
                f"{stage} {info['executable']} [{info['source']}{'' if info['available'] else ', NOT FOUND'}]" for stage, info in stages.items()
            )
            lines.append(f"  {name}: {detail}")
    prs = data["wave_prs"]
    if not prs["available"]:
        lines.append(f"open wave PRs: not checked ({prs['reason']})")
    elif not prs["prs"]:
        lines.append("open wave PRs: none")
    else:
        lines.append(f"open wave PRs: {len(prs['prs'])} (merge them in order, oldest first)")
        lines += [f"  #{pr['number']} {pr['headRefName']}: {pr['title']} {pr['url']}" for pr in sorted(prs["prs"], key=lambda p: p["number"])]
    main = data["main"]
    if main is None:
        lines.append(f"local main: not checked ({data.get('census_error') or 'the integration ref is not a local branch'})")
    else:
        lines.append(
            f"local main: {main['main_ref']} {(main['main_sha'] or '-')[:7]} vs origin {(main['origin_sha'] or '-')[:7]}: "
            f"{main['relation']} (ahead {main['ahead']}, behind {main['behind']})"
        )
        lines.append(f"running executions: {data['activity']['running_executions']}; active claims: {data['activity']['active_claims']}")
        wt = data["worktrees"]
        lines.append(
            f"worktrees: cleanable {wt['cleanable_worktrees']}; done but not in main yet {wt['done_unmerged_worktrees']}; "
            f"resumable {wt['resumable_worktrees']}; other {wt['other_worktrees']} "
            "(doctor cleans nothing; preview with `stagemesh worktrees sweep --dry-run`)"
        )
    if data["needs_decision"]:
        lines.append("tasks needing a design decision:")
        lines += [f"  {item['task_id']} {item['title']}: {item['reason']}" for item in data["needs_decision"]]
    else:
        lines.append("tasks needing a design decision: none")
    for item in data["providers"]:
        cooldown = item["cooldown"]
        state = "ok" if cooldown is None else f"COOLING DOWN ({cooldown['failure_class']}, {cooldown['seconds_remaining']}s left; clear: {cooldown['clear_command']})"
        lines.append(f"provider {item['provider']}: {state}; last recorded live check: {item['live_acceptance']}")
    return lines
