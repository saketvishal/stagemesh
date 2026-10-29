"""Durable, explicit run scope. Fresh runs never inherit a previous run's label.

Audit records are append-only and queried by identity, not a bounded recent-event
window. A changed scope supersedes new admissions by the old run; an already
produced, exactly recorded candidate may finish verification under its old grant.
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .audit import record_audit
from .auto_plan import cached_state, task_labels
from .domain import Stage
from .task_sources import LocalBacklogSource, sync_source, task_sources_from_config


class ScopeError(ValueError):
    pass


def _events(store, kind):
    for row in store.conn.execute(
        "SELECT id, payload FROM audit_events WHERE event_type=? ORDER BY created_at DESC, rowid DESC", (kind,)
    ):
        yield str(row["id"]), json.loads(row["payload"])


@dataclass(frozen=True)
class Scope:
    id: str
    label: str | None

    def to_dict(self):
        return {"scope_id": self.id, "label": self.label}


def load_scope(store, scope_id):
    row = store.conn.execute("SELECT payload FROM audit_events WHERE id=? AND event_type='selection.scope'", (scope_id,)).fetchone()
    if row is None:
        raise ScopeError(f"unknown saved scope {scope_id!r}; nothing started")
    value = json.loads(row["payload"])
    return Scope(scope_id, value["label"])


def task_scope(store, task_id):
    for _, value in _events(store, "selection.task"):
        if value["task_id"] == task_id:
            return load_scope(store, value["scope_id"])
    return None


def resolve_scope(store, *, label=None, resume=None, clear=False, task_id=None, preview=False):
    if clear and (label is not None or not resume):
        raise ScopeError("--clear-label requires --resume-scope and cannot be combined with --label")
    if label is not None and not label.strip():
        raise ScopeError("--label must not be empty")
    prior = load_scope(store, resume) if resume else (task_scope(store, task_id) if task_id else None)
    label = label.strip() if label is not None else None
    if prior and label is None and not clear:
        return prior
    if prior and label is not None and prior.label and label.casefold() == prior.label.casefold():
        return prior
    if not prior and label is None:
        return None
    if prior and not resume:
        raise ScopeError(f"task belongs to scope {prior.id}; use --resume-scope {prior.id} to change or clear its label")
    # Changes are explicit and preserve every historical scope/candidate. Refuse
    # while a stage owns work; never kill a worker to change its selector.
    payload = {"label": label, "supersedes": prior.id if prior else None, "action": "clear" if clear else "change" if prior else "create"}
    if preview:
        return Scope("preview", label)
    with store.conn:
        if prior:
            # Serialize a scope transition with implementation claims. No active
            # owner can appear between the active check and supersession event.
            store.conn.execute("UPDATE audit_events SET created_at=created_at WHERE id=?", (prior.id,))
            if not current(store, prior):
                raise ScopeError("scope was already changed; resume its replacement")
            if next(iter(store.running_executions()), None) or store.conn.execute("SELECT 1 FROM claims WHERE active=1 LIMIT 1").fetchone():
                raise ScopeError("scope change refused while work is active; pause admission and let stages finish first")
        return Scope(record_audit(store, "selection.scope", payload), label)


def current(store, scope):
    return not any(value.get("supersedes") == scope.id for _, value in _events(store, "selection.scope"))


def drains_candidate(store, task, scope):
    if task["stage"] not in (Stage.VALIDATE, Stage.REVIEW, Stage.INTEGRATE, Stage.DONE):
        return False
    candidate = store.latest_candidate(task["id"])
    if candidate is None:
        return False
    granted = {value.get("candidate_sha") for _, value in _events(store, "selection.candidate")
        if value.get("scope_id") == scope.id and value.get("task_id") == task["id"]}
    if candidate["sha"] in granted:
        return True
    # Reuse the independently validated mechanical provenance chain. This
    # conveys scope only: the replacement still needs its own validation/review.
    from .review_identity import implementation_identity
    identity = implementation_identity(store, task["id"], candidate["sha"])
    origins = identity.get("origins", []) if identity else []
    return bool(origins) and all(origin["candidate_sha"] in granted for origin in origins)


def task_problem(store, task):
    scope = getattr(store, "selection_scope", None)
    if scope is None:
        return None
    if not getattr(store, "selection_fresh", False):
        return "source sync unavailable or stale; scoped admission refused"
    if task is None:
        return "no such task"
    child = cached_state(store, task).get("selection_scope")
    if child is not None:
        if not isinstance(child, dict) or child.get("scope_id") != scope.id:
            return "objective child belongs to a different or invalid saved scope"
        trusted = any(value.get("scope_id") == scope.id and value.get("source_objective") == child.get("source_objective")
            and task["source"] == "local-backlog" and task["source_id"] in value.get("children", [])
            for _, value in _events(store, "selection.children"))
        if not trusted:
            return "objective child lacks operator-bound scope provenance"
        problem = parent_problem(store, scope, str(child.get("source_objective")), drain=drains_candidate(store, task, scope))
        if problem:
            return problem
    if drains_candidate(store, task, scope):
        return None  # normal Scheduler/source/security gates still apply
    if not current(store, scope):
        return "saved scope was explicitly changed; resume the replacement scope"
    catalog = getattr(store, "selection_catalog", None)
    if scope.label and catalog is not None and scope.label.casefold() not in catalog:
        return "unknown label in verified GitHub catalog"
    if (task["source"], task["source_id"]) not in store.selection_seen:
        return "task absent from verified source scan; generated children require explicit source admission"
    if scope.label and scope.label.casefold() not in {x.casefold() for x in task_labels(store, task)}:
        return f"outside label selector {scope.label!r}"
    return None


def refresh(store):
    callback = getattr(store, "refresh_selection_scope", None)
    if callback:
        callback()


def attach(store, project: Path, config, scope):
    if scope is None:
        return
    store.selection_scope = scope
    store.selection_config = config
    store.selection_fresh = False
    store.selection_seen = set()
    store.selection_catalog = None

    def sync():
        store.selection_fresh = False
        discovered = []
        try:
            sources = [LocalBacklogSource(project / ".stagemesh/backlog.json"), *task_sources_from_config(config)]
            for source in sources:
                tasks = source.discover()
                if getattr(source, "last_status", "OK") != "OK":
                    raise ScopeError(f"source sync failed: {source.name} ({source.last_status}); cached labels cannot authorize work")
                if hasattr(source, "source") and scope.label:
                    store.selection_catalog = label_catalog(source.source)
                discovered.extend(tasks)
            # Publish only after every configured source was read successfully.
            sync_source(store, discovered, preserve_existing_dependencies=True)
            store.selection_seen = {(task.source, task.source_id) for task in discovered}
            store.selection_discovered = discovered
            store.selection_fresh = True
        except Exception as exc:
            raise ScopeError(f"scoped source refresh refused: {exc}") from exc

    store.refresh_selection_scope = sync


def authorize_children(store, scope, source_objective, children):
    if not scope or not scope.label or not current(store, scope):
        raise ScopeError("objective planning requires a current saved label scope")
    problem = parent_problem(store, scope, source_objective)
    if problem:
        raise ScopeError(problem)
    provenance = {**scope.to_dict(), "source_objective": source_objective}
    record_audit(store, "selection.children", {**provenance, "children": list(children)})
    return provenance


def parent_problem(store, scope, source_objective, *, drain=False):
    parent = store.source_state("github", source_objective)
    labels = {str(x).casefold() for x in parent.get("labels", [])}
    if (not store.selection_fresh or ("github", source_objective) not in store.selection_seen
            or parent.get("state") != "OPEN" or "admission_pending" in parent or parent.get("validation_errors")):
        return "objective parent source is closed, stale, invalid or unavailable"
    if labels & {"stagemesh:deferred", "stagemesh:blocked", "status:blocked", "status:remediating"}:
        return "objective parent is blocked or deferred"
    config = store.selection_config
    if labels & {x.casefold() for x in config.task_selection.excluded_labels}:
        return "objective parent excluded by task selection policy"
    for source in config.task_sources:
        if source.kind == "github" and (not {x.casefold() for x in source.labels}.issubset(labels)
                or labels & {x.casefold() for x in source.excluded_labels}):
            return "objective parent excluded by configured source policy"
    if not parent.get("eligible") and parent.get("eligibility_reason") != "source objective root":
        return "objective parent not eligible for scoped planning"
    if not drain and scope.label.casefold() not in labels:
        return "objective parent no longer carries the active label"
    for dependency in parent.get("dependencies", []):
        row = store.conn.execute("SELECT stage FROM tasks WHERE source='github' AND source_id=?", (str(dependency),)).fetchone()
        if row is None or row["stage"] != Stage.DONE:
            return f"objective parent dependency {dependency} is unresolved; scope is not expanded"
    return None


def label_catalog(source):
    """A successful complete catalog distinguishes unused labels from unknown ones.

    A private-repository 404, rate limit or malformed/truncated response is an
    unavailable authority, never proof that a label does not exist.
    """
    labels = set()
    for page in range(1, 101):
        request = urllib.request.Request(
            f"https://api.github.com/repos/{source.owner}/{source.repo}/labels?per_page=100&page={page}",
            headers={"Accept": "application/vnd.github+json", **({"Authorization": f"Bearer {source.token}"} if source.token else {})},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            batch = json.loads(response.read().decode("utf-8"))
        if not isinstance(batch, list) or any(not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in batch):
            raise ScopeError("invalid GitHub label catalog")
        labels.update(item["name"].casefold() for item in batch)
        if len(batch) < 100:
            return labels
    raise ScopeError("GitHub label catalog truncated; label identity unverified")


def record_task(store, task_id):
    scope = getattr(store, "selection_scope", None)
    if scope and scope.id != "preview":
        previous = task_scope(store, task_id)
        if previous != scope:
            record_audit(store, "selection.task", {**scope.to_dict(), "task_id": task_id})
        task = store.get_task(task_id)
        candidate = store.latest_candidate(task_id)
        if (task is not None and task["stage"] in (Stage.VALIDATE, Stage.REVIEW, Stage.INTEGRATE)
                and candidate is not None and not drains_candidate(store, task, scope)):
            record_candidate(store, task_id, candidate["sha"])


def record_candidate(store, task_id, sha):
    scope = getattr(store, "selection_scope", None)
    if scope:
        record_audit(store, "selection.candidate", {**scope.to_dict(), "task_id": task_id, "candidate_sha": sha})


def preview(store, project, policy):
    from .task_selection import rank_batch_candidates
    scope = getattr(store, "selection_scope", None)
    if scope is None:
        return None
    ranked, skipped, _ = rank_batch_candidates(store, project, policy, auto_plan=True)
    rows = list(store.tasks())
    matching = [r for r in rows if not task_problem(store, r)]
    eligible = {r.task_id for r in ranked}
    materialized = {(r["source"], r["source_id"]) for r in rows}
    unmaterialized = [r for r in getattr(store, "selection_discovered", []) if (r.source, r.source_id) not in materialized]
    matching_unmaterialized = [r for r in unmaterialized if not scope.label or scope.label.casefold() in {x.casefold() for x in r.labels}]
    skipped.extend({"task_id": r.source_id, "source": r.source,
        "reason": store.source_state(r.source, r.source_id).get("eligibility_reason") or r.eligibility_reason or "source-only objective/informational item; not executable"}
        for r in matching_unmaterialized)
    blockers = []
    for row in matching:
        for dep in store.conn.execute("SELECT depends_on_task_id FROM task_dependencies WHERE task_id=?", (row["id"],)):
            parent = store.get_task(dep[0])
            if parent and task_problem(store, parent):
                blockers.append({"task_id": row["id"], "dependency": dep[0], "status": parent["status"], "stage": parent["stage"]})
    catalog = store.selection_catalog
    label_state = ("unfiltered" if not scope.label else
                   "unknown_label" if catalog is not None and scope.label.casefold() not in catalog else
                   "known_empty" if catalog is not None and not matching and not matching_unmaterialized else
                   "observed" if matching or matching_unmaterialized else "no_verified_matches")
    return {**scope.to_dict(), "matching": len(matching) + len(matching_unmaterialized), "eligible": len(eligible),
            "blocked": sum(r["id"] not in eligible and r["stage"] != Stage.DONE for r in matching) + len(matching_unmaterialized),
            "excluded": len(rows) - len(matching) + len(unmaterialized) - len(matching_unmaterialized), "exclusions": skipped, "out_of_scope_dependencies": blockers,
            "label_state": label_state, "source_fresh": store.selection_fresh}
