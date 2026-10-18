"""Bounded, journaled recovery of a fully owned, published landing suffix."""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

from .autonomy.dependencies import PRState
from .concurrency import IntegrationLock
from .contract_binding import contract_for_candidate
from .contracts import bound_contract_from_record
from .coordinator import Coordinator
from .domain import EvidenceKind, EvidenceStatus, Stage
from .git import GitWorkspace
from .serialized_integration import SerializedIntegrator
from .survivor_wave import STAGING_REF, TaskRecord, WaveReport, _defer_pr_delivery, is_task_pr_branch, run_wave

EVENT = "window.landing_recovery"
MAX_REFRESHES = 3


class LandingRecovery:
    def __init__(self, store, project, pulls, base_ref, required_checks, merge_callback):
        self.store, self.project, self.pulls = store, Path(project), pulls
        self.base_ref, self.required_checks, self.merge = base_ref, required_checks, merge_callback
        self.git = GitWorkspace(self.project)
        self.repository = f"{pulls.owner}/{pulls.repo}"

    def _save(self, state):
        self.store.add_audit_event(EVENT, state)

    def _policy(self):
        return {"mode": self.pulls.landing_policy, "checks": sorted(self.required_checks),
                "check_app_id": self.pulls.check_app_id}

    def _verify_generation(self, state, item=None):
        tip = self._tip(state["ref"])
        if tip == state["expected_tip"]:
            return tip
        # Integration may finish immediately before a process interruption. Only
        # that task's exact, fully evidenced commit can explain the ref advance.
        from .fast_forward import landing_evidence_problem
        if item is not None:
            latest = self.store.latest_candidate(item["task_id"])
            if latest is not None and latest["sha"] == tip:
                bound = contract_for_candidate(self.store, item["task_id"], tip, self.project)
                if (bound.baseline_sha == state["expected_tip"] and bound.digest == item["contract_hash"]
                        and bound.version == item["contract_version"]
                        and landing_evidence_problem(self.store, item["task_id"], tip) is None):
                    return tip
        raise RuntimeError("recovery staging advanced outside its journaled task chain")

    def active(self):
        for row in self.store.conn.execute(
            "SELECT payload FROM audit_events WHERE event_type=? ORDER BY created_at DESC,rowid DESC", (EVENT,),
        ):
            state = json.loads(row["payload"])
            if state.get("repository") == self.repository and state.get("base_ref") == self.base_ref:
                key = str(state.get("id", ""))
                records = state.get("records")
                def valid_sha(value):
                    return bool(re.fullmatch(r"[0-9a-f]{40}", str(value or "")))
                if (state.get("version") != 1 or not re.fullmatch(r"[0-9a-f]{12}", key)
                        or state.get("status") not in {"active", "complete"}
                        or not isinstance(records, list) or not records
                        or state.get("refreshes") not in range(1, MAX_REFRESHES + 1)
                        or state.get("ref") != f"refs/heads/wave/recovery-{key}" + (
                            f"-{state['refreshes']}" if state["refreshes"] > 1 else "")
                        or not valid_sha(state.get("expected_tip")) or not valid_sha(state.get("target"))
                        or not isinstance(state.get("original_heads"), list)
                        or len(state["original_heads"]) != len(records)
                        or not all(valid_sha(sha) for sha in state["original_heads"])
                        or state.get("original_staging") != state["original_heads"][-1]
                        or len({item.get("task_id") for item in records}) != len(records)
                        or any(not valid_sha(item.get("sha")) or item.get("phase") not in {
                            "queued", "refreshing", "working", "publishing", "published", "landed",
                        } for item in records)):
                    raise RuntimeError("landing recovery journal is unreadable; preserved work was not changed")
                return state if state["status"] != "complete" else None
        return None

    def _emit(self, task_id, message):
        emit = getattr(self.merge, "progress", None)
        if callable(emit):
            emit(f"task {task_id}: landing recovery: {message}")

    def _idle(self, deadline):
        if time.time() >= deadline:
            raise RuntimeError("landing recovery deadline reached; preserved for resume")
        if self.pulls.stop_control.blocks_admission():
            raise RuntimeError("queue admission paused; landing recovery preserved")
        if list(self.store.running_executions()) or any(self.store.has_active_claim(t["id"]) for t in self.store.tasks()):
            raise RuntimeError("landing recovery requires idle workers and no active claims")

    def _tip(self, ref):
        result = self.git.run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
        return result.stdout.strip() if result.returncode == 0 else None

    def _ancestor(self, older, newer):
        return bool(older and newer) and self.git.run("merge-base", "--is-ancestor", older, newer, check=False).returncode == 0

    def _number(self, url):
        parsed = urlparse(url or "")
        prefix = f"/{self.repository}/pull/"
        if parsed.scheme != "https" or parsed.hostname != "github.com" or not parsed.path.startswith(prefix):
            raise RuntimeError("unexpected recovery PR URL")
        value = parsed.path[len(prefix):]
        if not value.isdigit():
            raise RuntimeError("invalid recovery PR number")
        return int(value)

    def _observe(self, item, replacement=False):
        url = item.get("replacement_url") if replacement else item["pr_url"]
        sha = item.get("replacement_sha") if replacement else item["sha"]
        pr = self.pulls.get(self._number(url))
        if (pr is None or pr.head_sha != sha or pr.base_ref != self.base_ref.removeprefix("refs/heads/")
                or not is_task_pr_branch(pr.head_ref, item["task_id"], sha)):
            raise RuntimeError("recovery PR identity changed; no published head was rewritten")
        return pr

    def _external_completion(self, state):
        """Observe an independently completed suffix, never authorize its landing."""
        from .fast_forward import landing_evidence_problem

        observed = [(item, self.pulls.get(self._number(item["pr_url"]))) for item in state["records"]]
        if not all(pr is not None and pr.state is PRState.MERGED for _, pr in observed):
            for item, pr in observed:
                if pr is None or (pr.state is PRState.MERGED and item["phase"] != "landed"):
                    raise RuntimeError("partial or unknown external recovery completion; work retained")
                self._observe(item)
            return None
        base = self._tip(self.base_ref)
        if base != self.pulls.remote_tip(self.base_ref) or not self._ancestor(state["target"], base):
            raise RuntimeError("externally merged recovery requires a synchronized, descendant integration base")
        current = next((item for item in state["records"] if item["phase"] != "landed"), None)
        self._verify_generation(state, current)
        staging = self._tip(STAGING_REF)
        if staging not in {state["original_staging"], base}:
            raise RuntimeError("canonical staging changed outside recovery; external completion refused")
        records = []
        for item, pr in observed:
            task_id = item["task_id"]
            task, latest = self.store.get_task(task_id), self.store.latest_candidate(task_id)
            if not all(re.fullmatch(r"[0-9a-f]{40}", str(sha or "")) for sha in (pr.head_sha, pr.merge_commit_sha)):
                raise RuntimeError("externally merged recovery commit identity is unavailable")
            head_tree = self.git.run("rev-parse", "--verify", f"{pr.head_sha}^{{tree}}", check=False)
            merge_tree = self.git.run("rev-parse", "--verify", f"{pr.merge_commit_sha}^{{tree}}", check=False)
            if head_tree.returncode or merge_tree.returncode:
                raise RuntimeError("externally merged recovery commit trees are unavailable")
            proof = "ancestry" if self._ancestor(pr.head_sha, pr.merge_commit_sha) else None
            if proof is None and head_tree.stdout == merge_tree.stdout:
                proof = "identical_tree"
            if (pr.base_ref != self.base_ref.removeprefix("refs/heads/")
                    or not is_task_pr_branch(pr.head_ref, task_id, item["sha"])
                    or proof is None
                    or not self._ancestor(pr.merge_commit_sha, base)
                    or task is None or task["stage"] != Stage.DONE or task["status"] != "DONE"
                    or latest is None or not self._ancestor(latest["sha"], pr.head_sha)):
                raise RuntimeError("externally merged recovery identity, task ownership or reachability is unverified")
            binding = self.store.contract_binding(task_id, latest["sha"])
            if binding is None:
                raise RuntimeError("externally merged recovery has no recorded candidate contract")
            bound = bound_contract_from_record(dict(binding))
            if bound.digest != item["contract_hash"] or bound.version != item["contract_version"]:
                raise RuntimeError("requirements changed before external recovery completion")
            bases = {pair[2] for pair in state["pairs"] if pair[0] == task_id}
            if latest["sha"] == item["sha"]:
                bases.add(item["binding"]["baseline_sha"])
            if bound.baseline_sha not in bases:
                raise RuntimeError("externally merged candidate is outside the journaled recovery lineage")
            records.append({"task_id": task_id, "pr_url": item["pr_url"], "head_sha": pr.head_sha,
                            "merge_sha": pr.merge_commit_sha, "candidate_sha": latest["sha"],
                            "content_proof": proof, "head_tree": head_tree.stdout.strip(),
                            "merge_tree": merge_tree.stdout.strip(), "binding_id": binding["id"],
                            "baseline_sha": bound.baseline_sha, "contract_hash": bound.digest,
                            "contract_version": bound.version,
                            "evidence_problem": landing_evidence_problem(self.store, task_id, latest["sha"])})
        return {"base_sha": base, "staging_sha": staging, "records": records,
                "landing_authority": "external", "stagemesh_verified_landing": False}

    def _complete_external(self, state, observed, deadline):
        self._idle(deadline)
        self.pulls.deadline = deadline
        problem = self.pulls.host_policy()
        if problem:
            raise RuntimeError(problem)
        with IntegrationLock(self.project / ".stagemesh/integration.lock").hold("landing-recovery-external"):
            if self._external_completion(state) != observed:
                raise RuntimeError("external landing observation changed; recovery retained")
            if (self.git.run("symbolic-ref", "HEAD").stdout.strip() != self.base_ref
                    or self.git.run("status", "--porcelain", "--untracked-files=no").stdout.strip()):
                raise RuntimeError("checkout changed; external completion refused")
            self._preserve(state)
            root = f"refs/stagemesh/preserved/landing-{state['id']}"
            self._preserve_ref(f"{root}/external-generation", self._tip(state["ref"]))
            for index, item in enumerate(observed["records"]):
                self._preserve_ref(f"{root}/external-{index}", item["candidate_sha"])
                self._preserve_ref(f"{root}/external-head-{index}", item["head_sha"])
            if observed["staging_sha"] != observed["base_sha"]:
                self.git.run("update-ref", STAGING_REF, observed["base_sha"], observed["staging_sha"])
            state["status"], state["external_completion"] = "complete", observed
            self._save(state)
        self._emit("recovery", "previous PRs were merged externally; archived recovery without certifying their evidence")

    def preflight(self, pending_prs):
        try:
            state = self.active()
            if state is None:
                return "no active landing recovery journal"
            if state["policy"] != self._policy():
                return "landing recovery required checks, trusted app or policy changed"
            if self._external_completion(state) is not None:
                if pending_prs:
                    return "unowned open PR beside externally completed recovery"
                return None
            if not self._tip(state["ref"]) and any(item["phase"] not in {"queued", "landed"} for item in state["records"]):
                return "landing recovery staging generation is missing after work started"
            if self._tip(state["ref"]):
                current = next((item for item in state["records"] if item["phase"] != "landed"), None)
                self._verify_generation(state, current)
            known = {}
            for item in state["records"]:
                old = self._observe(item)
                if old.state is PRState.MERGED and item["phase"] != "landed":
                    return "original candidate landed outside recovery; reconciliation required"
                known[item["pr_url"]] = old.head_ref
                if item.get("replacement_url"):
                    new = self._observe(item, True)
                    known[item["replacement_url"]] = new.head_ref
            for pr in pending_prs:
                if known.get(pr.get("url")) != pr.get("headRefName"):
                    # A crash may occur after publishing a journaled exact head but
                    # before saving the returned URL. Observe, never infer its SHA.
                    matches = [item for item in state["records"] if item["phase"] == "publishing"
                               and item.get("replacement_sha") and is_task_pr_branch(
                                   str(pr.get("headRefName", "")), item["task_id"], item["replacement_sha"])]
                    if len(matches) != 1:
                        return "unowned or ambiguous PR beside landing recovery"
                    item = matches[0]
                    candidate = {**item, "replacement_url": pr["url"]}
                    observed = self._observe(candidate, True)
                    if observed.state is not PRState.OPEN:
                        return "interrupted replacement publication is not open"
                    # The normal idempotent publisher adopts this URL on resume;
                    # preflight itself never mutates the recovery journal.
            return None
        except (RuntimeError, KeyError, ValueError) as exc:
            return str(exc)

    def _sync_base(self, target, deadline):
        self._idle(deadline)
        self.pulls.deadline = deadline
        policy = self.pulls.host_policy()
        if policy:
            raise RuntimeError(policy)
        if self.pulls.remote_tip(self.base_ref) != target:
            raise RuntimeError("remote base moved during recovery admission; retry observation")
        with IntegrationLock(self.project / ".stagemesh/integration.lock", timeout_seconds=10).hold("landing-recovery-sync"):
            if self.git.run("symbolic-ref", "HEAD").stdout.strip() != self.base_ref:
                raise RuntimeError("checkout changed; landing recovery refused")
            if self.git.run("status", "--porcelain", "--untracked-files=no").stdout.strip():
                raise RuntimeError("tracked checkout changed; landing recovery refused")
            current = self._tip(self.base_ref)
            if not self._ancestor(current, target):
                raise RuntimeError("local integration branch diverged; landing recovery refused")
            self.git.run("merge", "--ff-only", target)
            if self.pulls.remote_tip(self.base_ref) != target:
                raise RuntimeError("remote base advanced during recovery synchronization")

    def _snapshot(self, record):
        from .fast_forward import landing_evidence_problem

        task_id, sha = record.task_id, record.integrated_candidate
        task = self.store.get_task(task_id)
        if (not task or task["status"] != "DONE" or task["stage"] != "DONE"
                or landing_evidence_problem(self.store, task_id, sha)):
            raise RuntimeError("recovery candidate lacks exact bound validation, independent review or integration")
        bound = contract_for_candidate(self.store, task_id, sha, self.project)
        item = {"task_id": task_id, "sha": sha, "pr_url": record.pr_url,
                "title": record.title or str(task["title"]), "phase": "queued", "runs": 0,
                "binding": dict(self.store.contract_binding(task_id, sha)),
                "contract_hash": bound.digest, "contract_version": bound.version,
                "replacement_sha": None, "replacement_url": None, "superseded": False}
        if self._observe(item).state is not PRState.OPEN:
            raise RuntimeError("only a proven-open unlanded PR may enter recovery")
        return item

    def recover(self, records, failure, deadline, hooks, options):
        try:
            with IntegrationLock(self.project / ".stagemesh/landing-recovery.lock", timeout_seconds=10).hold("landing-recovery"):
                return self._recover(records, failure, deadline, hooks, options)
        except Exception as exc:
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "waves": [], "merged_prs": [], "landing_results": []}

    def _publish_deferred_tail(self, records, stage, deadline, hooks, options, waves):
        """Account for fully evidenced publication-only work before refreshing a suffix."""
        from .fast_forward import landing_evidence_problem
        from .run_window import _landing_order

        known = {record.task_id for record in records}
        seen, deferred = set(), []
        for row in self.store.conn.execute(
            "SELECT payload FROM audit_events WHERE event_type=? ORDER BY created_at DESC,rowid DESC",
            ("task.pr_delivery_pending",),
        ):
            payload = json.loads(row["payload"])
            task_id = payload.get("task_id")
            if task_id in seen or task_id in known:
                continue
            seen.add(task_id)
            sha = payload.get("candidate_sha")
            task = self.store.get_task(task_id)
            latest = self.store.latest_candidate(task_id)
            if (not task or not latest or latest["sha"] != sha
                    or (task["stage"], task["status"]) not in {(Stage.INTEGRATE, "OPEN"), (Stage.DONE, "DONE")}
                    or not self._ancestor(sha, stage)
                    or any(self._ancestor(sha, record.integrated_candidate) for record in records)):
                continue
            if options.tasks is not None and task_id not in options.tasks:
                raise RuntimeError("deferred staging work is outside selected tasks; preserved without publication")
            if landing_evidence_problem(self.store, task_id, sha):
                raise RuntimeError("deferred staging work lacks exact successful evidence; recovery refused")
            deferred.append(TaskRecord(task_id, title=str(task["title"]), integrated_candidate=sha))
        ordered = _landing_order(self.store, [*records, *deferred], hooks)
        if not deferred or not ordered or ordered[-1].integrated_candidate != stage:
            raise RuntimeError("staging includes unaccounted work; recovery refused")
        if len(ordered) > options.max_tasks:
            raise RuntimeError("deferred publication suffix exceeds window task budget; work retained")
        pending_ids = {record.task_id for record in deferred}
        bindings = {record.task_id: dict(self.store.contract_binding(record.task_id, record.integrated_candidate)) for record in deferred}
        setter = getattr(hooks.open_pr, "set_deadline", None)
        if callable(setter):
            setter(deadline)

        def verify_identity(store, record, states):
            task = store.get_task(record.task_id)
            binding = store.contract_binding(record.task_id, record.integrated_candidate)
            if (self._tip(STAGING_REF) != stage or not task or (task["stage"], task["status"]) not in states
                    or binding is None or dict(binding) != bindings[record.task_id]
                    or landing_evidence_problem(store, record.task_id, record.integrated_candidate)):
                raise RuntimeError("deferred publication identity, state or evidence changed; work retained")

        def verify(record, states):
            self._idle(deadline)
            verify_identity(self.store, record, states)
            for original in records:
                self._snapshot(original)

        for record in ordered:
            if record.task_id not in pending_ids:
                continue
            verify(record, {(Stage.INTEGRATE, "OPEN"), (Stage.DONE, "DONE")})
            self._emit(record.task_id, "resuming verified deferred PR publication before refreshing the complete suffix")
            def publish(candidate):
                if candidate.task_id != record.task_id or candidate.integrated_candidate != record.integrated_candidate:
                    raise RuntimeError("deferred publication candidate changed; work retained")
                verify(record, {(Stage.DONE, "DONE")})
                return hooks.open_pr(candidate)

            if callable(setter):
                publish.set_deadline = setter
            if self.store.get_task(record.task_id)["status"] == "DONE":
                record.outcome, record.final_stage, record.final_status = "done", "DONE", "DONE"
                record.validation = record.review = record.integration = "PASSED"
                wave = WaveReport(tasks=[record], staging_ref=STAGING_REF)
                waves.append(wave)
                try:
                    record.pr_url = publish(record)
                except Exception:
                    _defer_pr_delivery(self.store, record, "interrupted deferred PR publication")
                    raise
                record.ready_for_merge, record.pr_state, record.landing = True, "OPEN", "pr_opened"
            else:
                def run(task_id, steps):
                    if task_id != record.task_id or steps != 1:
                        raise RuntimeError("deferred publication dispatch changed")
                    from .persistence import Store

                    worker = Store(self.store.db_path)
                    try:
                        verify_identity(worker, record, {(Stage.INTEGRATE, "OPEN")})
                    finally:
                        worker.close()
                    return hooks.run_task(task_id, steps)

                guarded = replace(hooks, run_task=run, open_pr=publish)
                wave = run_wave(self.store, [record.task_id], guarded, concurrency=1, max_steps=1,
                    advance_baselines=False, unblock_blocked=False, open_prs=True, integration_mode="pr",
                    staging_ref=STAGING_REF, deadline=deadline, ordered_publication=True, project=self.project)
                waves.append(wave)
            published = wave.tasks[0] if len(wave.tasks) == 1 else None
            if (not wave.ok or published is None or published.outcome != "done" or not published.pr_url
                    or published.integrated_candidate != record.integrated_candidate):
                detail = published.reason or published.stop_reason if published is not None else str(wave.not_selected)
                raise RuntimeError(wave.stop_reason or f"deferred publication did not retain its exact candidate: {detail}")
            ordered[ordered.index(record)] = published
        return ordered

    def _recover(self, records, failure, deadline, hooks, options):
        waves = []
        try:
            self._idle(deadline)
            if not failure.get("refresh_required") or failure.get("remote_landed") is not False:
                raise RuntimeError("unknown or partial remote landing cannot be refreshed")
            if not callable(hooks.retarget):
                raise RuntimeError("task hooks cannot safely use a recovery staging generation")
            if self.active() is not None:
                raise RuntimeError("resume the active landing recovery before admitting another suffix")
            from .run_window import _landing_order
            ordered = _landing_order(self.store, records, hooks)
            if not ordered or ordered[0].integrated_candidate != failure.get("candidate_sha"):
                raise RuntimeError("landing recovery suffix does not start at the observed failed candidate")
            target = failure.get("base_sha")
            if not re.fullmatch(r"[0-9a-f]{40}", str(target or "")):
                raise RuntimeError("landing recovery requires an exact observed base SHA")
            for record in ordered:
                self._snapshot(record)
            stage = self._tip(STAGING_REF)
            if stage != ordered[-1].integrated_candidate:
                ordered = self._publish_deferred_tail(ordered, stage, deadline, hooks, options, waves)
            items = [self._snapshot(record) for record in ordered]
            if any(self._ancestor(item["sha"], target) for item in items):
                raise RuntimeError("candidate already reachable from remote base; reconcile instead of refreshing")
            key = uuid.uuid4().hex[:12]
            state = {"version": 1, "id": key, "repository": self.repository,
                     "base_ref": self.base_ref, "policy": self._policy(),
                     "status": "active", "original_staging": stage,
                     "ref": f"refs/heads/wave/recovery-{key}", "target": target, "expected_tip": target,
                     "refreshes": 1, "pairs": [], "original_heads": [item["sha"] for item in items], "records": items}
            # The intent and original refs survive interruption before any rewrite.
            self._save(state)
            self._preserve(state)
            self._sync_base(target, deadline)
            self.git.run("update-ref", state["ref"], target, "0" * 40)
            result = self._resume(deadline, hooks, options)
            result["waves"] = [*waves, *result["waves"]]
            return result
        except Exception as exc:
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "waves": waves, "merged_prs": [], "landing_results": []}

    def _preserve(self, state):
        originals = [("staging", state["original_staging"])] + [
            (str(index), sha) for index, sha in enumerate(state["original_heads"])
        ]
        for name, sha in originals:
            ref = f"refs/stagemesh/preserved/landing-{state['id']}/{name}"
            self._preserve_ref(ref, sha)

    def _preserve_ref(self, ref, sha):
        existing = self._tip(ref)
        if existing is not None and existing != sha:
            raise RuntimeError("preserved recovery reference changed")
        if existing is None:
            self.git.run("update-ref", ref, sha, "0" * 40)

    def _next_round(self, state, item, record, failure, deadline):
        if failure.get("remote_landed") is not False or not failure.get("refresh_required"):
            raise RuntimeError("partial or unknown landing cannot be refreshed")
        if state["refreshes"] >= MAX_REFRESHES:
            raise RuntimeError("landing recovery refresh budget reached; published work retained")
        target = failure.get("base_sha")
        if not re.fullmatch(r"[0-9a-f]{40}", str(target or "")):
            raise RuntimeError("new recovery base SHA is unavailable")
        if self._ancestor(record.integrated_candidate, target):
            raise RuntimeError("replacement already reachable from remote base; reconcile instead of refreshing")
        pair = [item["task_id"], record.integrated_candidate, target]
        if pair in state["pairs"]:
            raise RuntimeError("same head/base refresh was already attempted")
        snapshot = self._snapshot(record)
        if snapshot["contract_hash"] != item["contract_hash"] or snapshot["contract_version"] != item["contract_version"]:
            raise RuntimeError("requirements changed between refresh rounds")
        self._preserve_ref(f"refs/stagemesh/preserved/landing-{state['id']}/round-{state['refreshes']}", record.integrated_candidate)
        history = item.get("history", []) + [{key: item[key] for key in ("sha", "pr_url", "binding")}]
        item.update(snapshot)
        item["history"] = history
        state["refreshes"] += 1
        state["target"] = target
        state["expected_tip"] = target
        state["ref"] = f"refs/heads/wave/recovery-{state['id']}-{state['refreshes']}"
        self._save(state)
        self._sync_base(target, deadline)
        self.git.run("update-ref", state["ref"], target, "0" * 40)
        self._prepare(state, item, deadline)

    def _ensure_generation(self, state, deadline):
        if self._tip(state["ref"]):
            return
        if any(item["phase"] not in {"queued", "landed"} for item in state["records"]):
            raise RuntimeError("recovery generation disappeared after work started")
        self._idle(deadline)
        self.pulls.deadline = deadline
        current = self.pulls.remote_tip(self.base_ref)
        if current != state["target"]:
            if state["refreshes"] >= MAX_REFRESHES:
                raise RuntimeError("landing recovery base-observation budget reached; original work retained")
            branch = self.base_ref.removeprefix("refs/heads/")
            fetched = self.pulls.bounded_git("fetch", self.pulls.remote_url,
                f"refs/heads/{branch}:refs/remotes/origin/{branch}", check=False)
            observed = self._tip(f"refs/remotes/origin/{branch}")
            if fetched.returncode or not observed or observed != self.pulls.remote_tip(self.base_ref):
                raise RuntimeError("cannot verify a stable recovery base observation")
            if not self._ancestor(state["target"], observed):
                raise RuntimeError("remote base history changed; recovery refused")
            if any(item["phase"] == "queued" and self._ancestor(item["sha"], observed) for item in state["records"]):
                raise RuntimeError("queued candidate already landed; reconcile instead of refreshing")
            state["refreshes"] += 1
            state["target"] = state["expected_tip"] = observed
            state["ref"] = f"refs/heads/wave/recovery-{state['id']}-{state['refreshes']}"
            self._save(state)
            self._emit("recovery", "base advanced before work started; using the new verified base")
        self._sync_base(state["target"], deadline)
        self.git.run("update-ref", state["ref"], state["target"], "0" * 40)

    def _prepare(self, state, item, deadline):
        self._idle(deadline)
        self._verify_generation(state, item)
        task_id, sha = item["task_id"], item["sha"]
        latest = self.store.latest_candidate(task_id)
        if item["phase"] != "queued":
            return
        task = self.store.get_task(task_id)
        if task is None or task["stage"] != Stage.DONE or task["status"] != "DONE":
            raise RuntimeError("task state changed before landing recovery; operator state retained")
        if self._observe(item).state is not PRState.OPEN:
            raise RuntimeError("original PR is no longer open; refresh refused")
        if latest is None or latest["sha"] != sha:
            raise RuntimeError("candidate changed before journaled recovery; refusing to overwrite it")
        bound = contract_for_candidate(self.store, task_id, sha, self.project)
        if bound.digest != item["contract_hash"] or bound.version != item["contract_version"]:
            raise RuntimeError("candidate contract changed during landing recovery")
        pair = [task_id, sha, self._tip(state["ref"])]
        if pair in state["pairs"]:
            raise RuntimeError("same head/base refresh already attempted; no automatic retry")
        state["pairs"].append(pair)
        item["phase"] = "refreshing"
        self._save(state)
        self._emit(task_id, "refreshing preserved candidate against the current base; new evidence required")
        self.store.add_evidence(task_id, sha, EvidenceKind.INTEGRATION, EvidenceStatus.FAILED, {
            **bound.evidence_payload(), "findings": [{"code": "integration_non_fast_forward", "severity": "error",
                "message": "published candidate is obsolete against the hosted integration target"}],
        })
        self.store.advance_task(task_id, Stage.INTEGRATE)
        integrator = SerializedIntegrator(state["ref"], True, IntegrationLock(self.project / ".stagemesh/integration.lock"))
        coordinator = Coordinator(self.store, self.project, integrator=integrator, require_independent_review=True)
        coordinator._recover_integration(task_id, sha)
        task = self.store.get_task(task_id)
        if task["status"] == "BLOCKED" or task["stage"] not in {Stage.VALIDATE, Stage.IMPLEMENT}:
            raise RuntimeError("conflict recovery could not safely queue validation or scoped remediation")
        item["phase"] = "working"
        self._save(state)

    def resume(self, deadline, hooks, options):
        try:
            with IntegrationLock(self.project / ".stagemesh/landing-recovery.lock", timeout_seconds=10).hold("landing-recovery"):
                return self._resume(deadline, hooks, options)
        except Exception as exc:
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "waves": [], "merged_prs": [], "landing_results": []}

    def _resume(self, deadline, hooks, options):
        result = {"ok": False, "reason": "", "waves": [], "merged_prs": [], "landing_results": []}
        try:
            self._idle(deadline)
            state = self.active()
            if state is None:
                raise RuntimeError("no active landing recovery")
            if state["policy"] != self._policy():
                raise RuntimeError("landing recovery policy changed")
            external = self._external_completion(state)
            if external is not None:
                self._complete_external(state, external, deadline)
                result["ok"] = True
                result["landing_results"].append({"external_completion": external, "merged": False})
                return result
            self._preserve(state)
            self._ensure_generation(state, deadline)
            for item in state["records"]:
                if item["phase"] == "landed":
                    continue
                self._prepare(state, item, deadline)
                if item["phase"] == "refreshing":
                    task = self.store.get_task(item["task_id"])
                    latest = self.store.latest_candidate(item["task_id"])
                    bound = contract_for_candidate(self.store, item["task_id"], str(latest["sha"]), self.project)
                    if (task["stage"] == Stage.VALIDATE and latest["sha"] != item["sha"]
                            and bound.digest == item["contract_hash"] and bound.version == item["contract_version"]
                            and bound.baseline_sha == state["pairs"][-1][2]):
                        item["phase"] = "working"
                        self._save(state)
                    else:
                        raise RuntimeError("refresh was interrupted before a verified new binding; ownership reconciliation required")
                while item["phase"] != "landed":
                    self._idle(deadline)
                    observed_tip = self._verify_generation(state, item)
                    if observed_tip != state["expected_tip"]:
                        state["expected_tip"] = observed_tip
                        self._save(state)
                    task_id = item["task_id"]
                    task = self.store.get_task(task_id)
                    latest = self.store.latest_candidate(task_id)
                    bound = contract_for_candidate(self.store, task_id, str(latest["sha"]), self.project)
                    if bound.digest != item["contract_hash"] or bound.version != item["contract_version"]:
                        raise RuntimeError("requirements changed during landing recovery; no automatic overwrite")
                    runner = hooks.retarget(state["ref"])
                    original_publish = runner.open_pr

                    def publish(record):
                        self._verify_generation(state, item)
                        state["expected_tip"] = record.integrated_candidate
                        item["replacement_sha"], item["phase"] = record.integrated_candidate, "publishing"
                        self._save(state)
                        url = original_publish(record)
                        item["replacement_url"], item["phase"] = url, "published"
                        self._save(state)
                        return url

                    setter = getattr(original_publish, "set_deadline", None)
                    if callable(setter):
                        publish.set_deadline = setter
                        setter(deadline)

                    runner = replace(runner, open_pr=publish)
                    if task["status"] != "DONE":
                        if item["runs"] >= options.max_continuations + 1:
                            raise RuntimeError("landing recovery task step budget reached; work retained for resume")
                        item["runs"] += 1
                        self._save(state)
                        wave = run_wave(self.store, [task_id], runner, concurrency=1,
                            max_steps=options.max_steps_per_task, advance_baselines=False, unblock_blocked=False,
                            open_prs=True, integration_mode="pr", staging_ref=state["ref"], deadline=deadline,
                            ordered_publication=True, project=self.project)
                        result["waves"].append(wave)
                        if not wave.ok:
                            raise RuntimeError(wave.stop_reason or "recovery wave failed safely")
                        record = wave.tasks[0]
                        if record.outcome != "done":
                            if record.stop_reason == "MAX_STEPS" and record.resumable:
                                continue
                            raise RuntimeError(record.reason or f"recovery task stopped at {record.stop_reason}")
                    else:
                        latest = self.store.latest_candidate(task_id)
                        if latest["sha"] == item["sha"]:
                            raise RuntimeError("original candidate evidence cannot authorize its replacement")
                        record = TaskRecord(task_id, title=item["title"], outcome="done", ready_for_merge=True,
                                            integrated_candidate=str(latest["sha"]), validation="PASSED", review="PASSED", integration="PASSED")
                        from .fast_forward import landing_evidence_problem
                        if landing_evidence_problem(self.store, task_id, record.integrated_candidate):
                            raise RuntimeError("replacement lacks fresh exact-candidate evidence")
                        record.pr_url = item.get("replacement_url") or publish(record)
                    if record.integrated_candidate != item.get("replacement_sha") or not record.pr_url:
                        raise RuntimeError("replacement publication differs from its recovery journal")
                    observed = self._observe(item, True)
                    if observed.state is PRState.CLOSED:
                        raise RuntimeError("replacement PR was closed; original PR preserved")
                    problem = self.merge.validate_records([record], deadline)
                    if problem:
                        raise RuntimeError(problem)
                    if not item["superseded"]:
                        self.pulls.task_id = task_id
                        self.pulls.close_superseded(self._number(item["pr_url"]), item["sha"], record.pr_url)
                        item["superseded"] = True
                        self._save(state)
                    merged = self.merge(record, deadline)
                    result["landing_results"].append(merged)
                    if not merged.get("merged"):
                        if merged.get("refresh_required") and merged.get("remote_landed") is False:
                            self._next_round(state, item, record, merged, deadline)
                            continue
                        raise RuntimeError(str(merged.get("reason", "replacement landing unverified")))
                    result["merged_prs"].append(merged)
                    record.pr_state, record.ready_for_merge, record.landing = "MERGED", False, "fast_forward_merged"
                    item["phase"] = "landed"
                    self._save(state)
            self._idle(deadline)
            tip = self._tip(self.base_ref)
            if self.pulls.remote_tip(self.base_ref) != tip:
                raise RuntimeError("remote moved before completing landing recovery")
            if not all(self._ancestor(item["replacement_sha"], tip) for item in state["records"]):
                raise RuntimeError("recovered suffix is not completely landed")
            with IntegrationLock(self.project / ".stagemesh/integration.lock").hold("landing-recovery-complete"):
                current = self._tip(STAGING_REF)
                if current != tip:
                    if current != state["original_staging"]:
                        raise RuntimeError("canonical staging changed outside recovery; preserved without reset")
                    self.git.run("update-ref", STAGING_REF, tip, current)
            state["status"] = "complete"
            self._save(state)
            result["ok"] = True
        except Exception as exc:
            result["reason"] = f"{type(exc).__name__}: {exc}"
            self._emit("recovery", "blocked: " + result["reason"])
        return result
