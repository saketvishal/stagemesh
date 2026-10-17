"""Real local Git delivery and gates; hosted observations and reviewer are fixtures."""
from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from autonomy_support import HUMAN, add_passing_evidence, commit, git, init_repo, seed_candidate

import stagemesh.fast_forward as ff
from stagemesh.autonomy.ci_diagnosis import Conclusion, FakeHostedCI, GateOutcome, HostedCIRun
from stagemesh.autonomy.dependencies import PRState, PullRequest
from stagemesh.concurrency import IntegrationLock
from stagemesh.contract_binding import contract_for_candidate
from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage
from stagemesh.persistence import Store
from stagemesh.run_window import WindowOptions
from stagemesh.serialized_integration import SerializedIntegrator
from stagemesh.survivor_wave import STAGING_REF, RunResult, TaskRecord, WaveHooks
from stagemesh.validation import Validator


def recovery_rig(tmp_path, monkeypatch, *, stacked=False, conflict=False):
    repo = init_repo(tmp_path / "repo")
    git(repo, "config", "user.name", HUMAN[0])
    git(repo, "config", "user.email", HUMAN[1])
    base = commit(repo, {"src/first.py": "VALUE = 1\n", ".gitignore": ".stagemesh/\n"}, "base", who=HUMAN)
    remote = tmp_path / "origin.git"
    remote.mkdir()
    git(remote, "init", "--bare", "-q")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "origin", "main")
    store = Store(repo / ".stagemesh/stagemesh.sqlite3")
    store.migrate()
    ci = FakeHostedCI()
    prs, records, originals = {}, [], []
    git(repo, "checkout", "-b", "worker")
    baseline = base
    for index, task_id in enumerate(("FIRST", "SECOND") if stacked else ("FIRST",)):
        path = f"src/{task_id.lower()}.py"
        sha = commit(repo, {path: "VALUE = 2\n"}, task_id, who=HUMAN)
        contract = {"objective": f"implement {task_id}", "allowed_files": [path],
                    "acceptance_criteria": ["value is updated"],
                    "required_tests": [{"name": "focused-value", "command": [sys.executable, "-c",
                        f"from pathlib import Path; assert 'VALUE = 2' in Path({path!r}).read_text()"]}]}
        seed_candidate(store, task_id, baseline=baseline, candidate=sha, contract=contract, stage=Stage.DONE)
        contracts = repo / ".stagemesh/contracts"
        contracts.mkdir(parents=True, exist_ok=True)
        (contracts / f"{task_id}.json").write_text(json.dumps(contract))
        add_passing_evidence(store, task_id, sha, baseline=baseline, contract=contract)
        bound = contract_for_candidate(store, task_id, sha, repo)
        store.add_evidence(task_id, sha, EvidenceKind.INTEGRATION, EvidenceStatus.PASSED, bound.evidence_payload())
        branch = f"wave/task-{task_id}"
        git(repo, "push", "origin", f"{sha}:refs/heads/{branch}")
        prs[index + 1] = PullRequest(index + 1, sha, branch, "main", mergeable=False)
        records.append(TaskRecord(task_id, title=task_id, integrated_candidate=sha,
                                  pr_url=f"https://github.com/owner/repo/pull/{index + 1}", outcome="done", ready_for_merge=True))
        originals.append(sha)
        baseline = sha
    git(repo, "update-ref", STAGING_REF, originals[-1])
    git(repo, "checkout", "main")
    git(repo, "checkout", "-b", "upstream")
    target = commit(repo, {"src/first.py" if conflict else "src/upstream.py": "UPSTREAM = 3\n"}, "upstream", who=HUMAN)
    git(repo, "push", "origin", "upstream:main")
    git(repo, "checkout", "main")
    adapter = ff.FastForwardPullRequests("owner", "repo", None, ci)
    adapter.configure_ff(repo, store, "main", ("web",), ci)
    monkeypatch.setattr(adapter, "host_policy", lambda head_ref=None: None)
    monkeypatch.setattr(adapter, "bind_remote", lambda: None)
    def observe(number):
        pr = prs[number]
        if git(remote, "rev-parse", "main") == pr.head_sha:
            pr = prs[number] = replace(pr, state=PRState.MERGED, merge_commit_sha=pr.head_sha)
        return pr
    monkeypatch.setattr(adapter, "get", observe)
    def close(number, expected, replacement):
        assert prs[number].head_sha == expected
        assert any(f"/pull/{n}" == replacement[replacement.rfind('/pull/'):] and p.head_sha != expected for n, p in prs.items())
        prs[number] = replace(prs[number], state=PRState.CLOSED)
    monkeypatch.setattr(adapter, "close_superseded", close)
    monkeypatch.setattr(ff, "GitHubHostedCI", lambda *args: ci)
    monkeypatch.setattr(ff, "FastForwardPullRequests", lambda *args: adapter)
    from stagemesh import github
    from stagemesh.autonomy import cli
    monkeypatch.setattr(github, "detect_github_repository", lambda *args: SimpleNamespace(owner="owner", repo="repo"))
    monkeypatch.setattr(cli, "_github_token", lambda: None)
    merger = ff.make_merger(store, repo, "refs/heads/main", ("web",))
    phases, publish_deadlines = [], []
    from stagemesh.git import GitWorkspace
    root_git = GitWorkspace(repo)

    def hooks_for(ref):
        def run(task_id, max_steps):
            worker = Store(repo / ".stagemesh/stagemesh.sqlite3")
            try:
                return run_owned(worker, task_id, max_steps)
            finally:
                worker.close()
        def run_owned(store, task_id, max_steps):
            sha = str(store.latest_candidate(task_id)["sha"])
            phases.append((task_id, str(store.get_task(task_id)["stage"]), sha))
            if store.get_task(task_id)["stage"] == Stage.IMPLEMENT:
                return RunResult("NO_PROGRESS", "genuine conflict needs scoped implementation")
            assert store.get_task(task_id)["stage"] == Stage.VALIDATE
            assert not store.has_evidence(task_id, sha, EvidenceKind.VALIDATION)
            assert not store.has_evidence(task_id, sha, EvidenceKind.REVIEW)
            assert Validator().validate(store, task_id, sha, repo) is EvidenceStatus.PASSED
            bound = contract_for_candidate(store, task_id, sha, repo)
            # A fake independent reviewer accepts only after the real gate passes.
            store.add_evidence(task_id, sha, EvidenceKind.REVIEW, EvidenceStatus.PASSED, {
                **bound.evidence_payload(), "review_provider": "fixture-reviewer", "implementer_provider": "codex",
                "independent_reviewer": True, "review_execution_invoked": True,
            })
            integration = SerializedIntegrator(ref, True, IntegrationLock(repo / ".stagemesh/integration.lock"))
            assert integration.integrate(store, task_id, sha, repo) is EvidenceStatus.PASSED
            store.advance_task(task_id, Stage.DONE)
            ci.runs[sha] = HostedCIRun(sha, {"web": GateOutcome("web", Conclusion.SUCCESS)})
            return RunResult("DONE")
        def publish(record):
            assert ff.landing_evidence_problem(store, record.task_id, record.integrated_candidate) is None
            number = max(prs) + 1
            branch = f"wave/task-{record.task_id}-{record.integrated_candidate[:7]}"
            git(repo, "push", "origin", f"{record.integrated_candidate}:refs/heads/{branch}")
            prs[number] = PullRequest(number, record.integrated_candidate, branch, "main", mergeable=True)
            return f"https://github.com/owner/repo/pull/{number}"
        publish.set_deadline = publish_deadlines.append
        return WaveHooks(run_task=run, advance_baseline=lambda task_id: pytest.fail("unexpected baseline discard"),
                         open_pr=publish, target_sha=lambda: git(repo, "rev-parse", ref),
                         main_sha=lambda: git(repo, "rev-parse", "main"),
                         is_ancestor=lambda old, new: root_git.run("merge-base", "--is-ancestor", old, new, check=False).returncode == 0,
                         retarget=hooks_for)
    # GitWorkspace exposes the actual exit status for ancestry checks.
    hooks = hooks_for(STAGING_REF)
    def retarget(ref):
        result = hooks_for(ref)
        result.is_ancestor = lambda old, new: root_git.run("merge-base", "--is-ancestor", old, new, check=False).returncode == 0
        result.retarget = retarget
        return result
    hooks = retarget(STAGING_REF)
    failure = {"refresh_required": True, "remote_landed": False, "base_sha": target,
               "candidate_sha": originals[0], "pr": 1, "reason": "base advanced"}
    return SimpleNamespace(repo=repo, remote=remote, store=store, adapter=adapter, ci=ci, prs=prs,
                           merger=merger, hooks=hooks, records=records, originals=originals,
                           target=target, phases=phases, failure=failure)


@pytest.mark.parametrize("stacked", [False, True])
def test_recovery_replays_only_owned_deltas_rechecks_and_lands_exact_replacements(tmp_path, monkeypatch, stacked):
    r = recovery_rig(tmp_path, monkeypatch, stacked=stacked)
    result = r.merger.recover(r.records, r.failure, time.time() + 60, r.hooks, WindowOptions())
    assert result["ok"], result["reason"]
    assert len(result["merged_prs"]) == len(r.records)
    assert [phase[1] for phase in r.phases] == ["VALIDATE"] * len(r.records)
    replacements = [str(r.store.latest_candidate(record.task_id)["sha"]) for record in r.records]
    assert all(new != old for new, old in zip(replacements, r.originals, strict=True))
    assert git(r.remote, "rev-parse", "main") == replacements[-1] == git(r.repo, "rev-parse", "main")
    assert git(r.repo, "rev-parse", STAGING_REF) == replacements[-1]
    assert (r.repo / "src/upstream.py").read_text().strip() == "UPSTREAM = 3"
    for index, old in enumerate(r.originals):
        assert git(r.remote, "rev-parse", f"wave/task-{r.records[index].task_id}") == old
        assert r.prs[index + 1].state is PRState.CLOSED
    assert r.merger.active_recovery() is None


def test_content_conflict_enters_scoped_remediation_without_forcing_a_merge(tmp_path, monkeypatch):
    r = recovery_rig(tmp_path, monkeypatch, conflict=True)
    result = r.merger.recover(r.records, r.failure, time.time() + 60, r.hooks, WindowOptions())
    assert not result["ok"]
    assert r.phases and r.phases[0][1] == "IMPLEMENT"
    assert git(r.remote, "rev-parse", "main") == r.target
    assert r.prs[1].state is PRState.OPEN
    assert git(r.repo, "rev-parse", STAGING_REF) == r.originals[-1]


@pytest.mark.parametrize("problem", ["unknown_landing", "partial_landing", "active_execution", "unaccounted_staging", "changed_pr"])
def test_recovery_refuses_unsafe_admission_before_rewriting_candidates(tmp_path, monkeypatch, problem):
    r = recovery_rig(tmp_path, monkeypatch)
    if problem == "unknown_landing":
        r.failure["remote_landed"] = None
    elif problem == "partial_landing":
        r.failure["remote_landed"] = True
    elif problem == "active_execution":
        from stagemesh.domain import ExecutionKind
        r.store.start_execution(task_id="FIRST", claim_id=None, kind=ExecutionKind.VALIDATION)
    elif problem == "unaccounted_staging":
        git(r.repo, "update-ref", STAGING_REF, r.target)
    else:
        r.prs[1] = replace(r.prs[1], head_sha=r.target)
    result = r.merger.recover(r.records, r.failure, time.time() + 30, r.hooks, WindowOptions())
    assert not result["ok"] and r.phases == []
    assert r.store.latest_candidate("FIRST")["sha"] == r.originals[0]


def test_recovery_resume_uses_journal_and_rejects_unowned_pending_pr(tmp_path, monkeypatch):
    r = recovery_rig(tmp_path, monkeypatch)
    calls = []
    original = r.hooks.retarget
    def interrupted(ref):
        calls.append(ref)
        raise RuntimeError("fixture interrupted before validation")
    r.hooks.retarget = interrupted
    result = r.merger.recover(r.records, r.failure, time.time() + 60, r.hooks, WindowOptions())
    assert not result["ok"] and calls
    assert r.merger.active_recovery()
    pending = [{"url": r.records[0].pr_url, "headRefName": "wave/task-FIRST"}]
    assert r.merger.recovery_preflight(pending) is None
    assert "unowned" in r.merger.recovery_preflight(pending + [{"url": "https://github.com/owner/repo/pull/99", "headRefName": "wave/task-OTHER"}])
    r.hooks.retarget = original
    result = r.merger.resume_recovery(time.time() + 60, r.hooks, WindowOptions())
    assert result["ok"], result["reason"]


@pytest.mark.parametrize("problem", ["generation", "checks", "trusted_app"])
def test_resume_refuses_unowned_generation_or_changed_policy(tmp_path, monkeypatch, problem):
    r = recovery_rig(tmp_path, monkeypatch)
    normal = r.hooks.retarget
    r.hooks.retarget = lambda ref: (_ for _ in ()).throw(RuntimeError("fixture paused before validation"))
    result = r.merger.recover(r.records, r.failure, time.time() + 60, r.hooks, WindowOptions())
    assert not result["ok"] and r.merger.active_recovery()
    current = r.store.latest_candidate("FIRST")["sha"]
    if problem == "generation":
        state = r.merger.active_recovery()
        git(r.repo, "checkout", "-b", "unowned-generation", r.target)
        foreign = commit(r.repo, {"src/unowned.py": "UNOWNED = True"}, "unowned", who=HUMAN)
        git(r.repo, "checkout", "main")
        git(r.repo, "update-ref", state["ref"], foreign)
    elif problem == "checks":
        r.merger = ff.make_merger(r.store, r.repo, "refs/heads/main", ("web", "extra"))
    else:
        r.adapter.check_app_id = 999
    r.hooks.retarget = normal
    result = r.merger.resume_recovery(time.time() + 60, r.hooks, WindowOptions())
    assert not result["ok"]
    assert ("staging advanced" if problem == "generation" else "policy changed") in result["reason"]
    assert r.store.latest_candidate("FIRST")["sha"] == current
    assert git(r.remote, "rev-parse", "main") == r.target


def test_closed_replacement_never_closes_original_on_resume(tmp_path, monkeypatch):
    r = recovery_rig(tmp_path, monkeypatch)
    observe = r.adapter.get
    def interrupt(number):
        if number > 1:
            raise RuntimeError("fixture interrupted after replacement publication")
        return observe(number)
    monkeypatch.setattr(r.adapter, "get", interrupt)
    result = r.merger.recover(r.records, r.failure, time.time() + 60, r.hooks, WindowOptions())
    assert not result["ok"] and len(r.prs) == 2
    assert r.prs[1].state is PRState.OPEN
    monkeypatch.setattr(r.adapter, "get", observe)
    r.prs[2] = replace(r.prs[2], state=PRState.CLOSED)
    result = r.merger.resume_recovery(time.time() + 60, r.hooks, WindowOptions())
    assert not result["ok"] and "replacement PR was closed" in result["reason"]
    assert r.prs[1].state is PRState.OPEN


def test_second_refresh_round_preserves_original_identity_across_resume(tmp_path, monkeypatch):
    r = recovery_rig(tmp_path, monkeypatch)
    validate = r.merger.validate_records
    moved = []
    def advance_once(records, deadline):
        problem = validate(records, deadline)
        if not moved:
            git(r.repo, "checkout", "-b", "upstream-again", r.target)
            tip = commit(r.repo, {"src/upstream_again.py": "ALSO_UPSTREAM = 4"}, "upstream again", who=HUMAN)
            git(r.repo, "push", "origin", "upstream-again:main")
            git(r.repo, "checkout", "main")
            moved.append(tip)
        return problem
    r.merger.validate_records = advance_once
    retarget = r.hooks.retarget
    runs = []
    def interrupt_second(ref):
        hooks = retarget(ref)
        run = hooks.run_task
        def work(task_id, steps):
            runs.append(task_id)
            if len(runs) == 2:
                raise RuntimeError("fixture interrupted in second refresh round")
            return run(task_id, steps)
        return replace(hooks, run_task=work)
    r.hooks.retarget = interrupt_second
    result = r.merger.recover(r.records, r.failure, time.time() + 90, r.hooks, WindowOptions())
    assert not result["ok"] and len(runs) == 2, result["reason"]
    state = r.merger.active_recovery()
    assert state["original_heads"] == r.originals and state["refreshes"] == 2
    assert git(r.repo, "rev-parse", f"refs/stagemesh/preserved/landing-{state['id']}/0") == r.originals[0]
    r.hooks.retarget = retarget
    result = r.merger.resume_recovery(time.time() + 60, r.hooks, WindowOptions())
    assert result["ok"], result["reason"]
    assert (r.repo / "src/upstream_again.py").read_text().strip() == "ALSO_UPSTREAM = 4"


def test_incomplete_owned_prefix_is_refused_without_dropping_predecessor(tmp_path, monkeypatch):
    r = recovery_rig(tmp_path, monkeypatch, stacked=True)
    failure = {**r.failure, "candidate_sha": r.originals[-1], "pr": 2}
    result = r.merger.recover(r.records[1:], failure, time.time() + 30, r.hooks, WindowOptions())
    assert not result["ok"] and "unlanded task FIRST" in result["reason"]
    assert r.merger.active_recovery() is None
    assert git(r.repo, "rev-parse", STAGING_REF) == r.originals[-1]


def test_changed_task_state_before_refresh_is_not_unblocked(tmp_path, monkeypatch):
    r = recovery_rig(tmp_path, monkeypatch)
    manager = r.merger.recover.__self__
    sync = manager._sync_base
    def block_after_sync(target, deadline):
        sync(target, deadline)
        r.store.block_task("FIRST")
    monkeypatch.setattr(manager, "_sync_base", block_after_sync)
    result = r.merger.recover(r.records, r.failure, time.time() + 30, r.hooks, WindowOptions())
    assert not result["ok"] and "operator state retained" in result["reason"]
    assert r.store.get_task("FIRST")["status"] == "BLOCKED"


def test_recovery_refuses_already_landed_ancestor_in_later_round(tmp_path, monkeypatch):
    r = recovery_rig(tmp_path, monkeypatch)
    observe = r.adapter.get
    monkeypatch.setattr(r.adapter, "get", lambda number: (_ for _ in ()).throw(RuntimeError("fixture cut")) if number > 1 else observe(number))
    result = r.merger.recover(r.records, r.failure, time.time() + 60, r.hooks, WindowOptions())
    assert not result["ok"]
    monkeypatch.setattr(r.adapter, "get", observe)
    manager = r.merger.recover.__self__
    state = manager.active()
    item = state["records"][0]
    git(r.repo, "checkout", "-b", "external-landing", item["replacement_sha"])
    target = commit(r.repo, {"src/later.py": "LATER = 1"}, "later", who=HUMAN)
    git(r.repo, "push", "origin", "external-landing:main")
    git(r.repo, "checkout", "main")
    record = TaskRecord("FIRST", integrated_candidate=item["replacement_sha"], pr_url=item["replacement_url"])
    with pytest.raises(RuntimeError, match="already reachable"):
        manager._next_round(state, item, record, {"refresh_required": True, "remote_landed": False, "base_sha": target}, time.time() + 30)
    assert state["refreshes"] == 1


def test_round_transition_with_landed_predecessor_can_resume_missing_generation(tmp_path, monkeypatch):
    r = recovery_rig(tmp_path, monkeypatch, stacked=True)
    manager = r.merger.recover.__self__
    validate, sync = r.merger.validate_records, manager._sync_base
    moved = []
    def advance_second(records, deadline):
        problem = validate(records, deadline)
        if records[0].task_id == "SECOND" and not moved:
            git(r.repo, "checkout", "-b", "new-upstream", "main")
            tip = commit(r.repo, {"src/later.py": "LATER = 1"}, "new upstream", who=HUMAN)
            git(r.repo, "push", "origin", "new-upstream:main")
            git(r.repo, "checkout", "main")
            moved.append(tip)
        return problem
    def interrupt_sync(target, deadline):
        if manager.active()["refreshes"] == 2:
            raise RuntimeError("fixture cut after new-round intent")
        sync(target, deadline)
    r.merger.validate_records = advance_second
    monkeypatch.setattr(manager, "_sync_base", interrupt_sync)
    result = r.merger.recover(r.records, r.failure, time.time() + 90, r.hooks, WindowOptions())
    assert not result["ok"] and "new-round intent" in result["reason"], result["reason"]
    state = manager.active()
    assert state["records"][0]["phase"] == "landed"
    assert manager._tip(state["ref"]) is None
    pending = [{"url": state["records"][1]["pr_url"], "headRefName": r.prs[max(r.prs)].head_ref}]
    assert manager.preflight(pending) is None
    monkeypatch.setattr(manager, "_sync_base", sync)
    result = r.merger.resume_recovery(time.time() + 90, r.hooks, WindowOptions())
    assert result["ok"], result["reason"]


def test_preserved_round_ref_write_is_idempotent_but_cannot_be_overwritten(tmp_path, monkeypatch):
    r = recovery_rig(tmp_path, monkeypatch)
    manager = r.merger.recover.__self__
    ref = "refs/stagemesh/preserved/round-proof"
    manager._preserve_ref(ref, r.originals[0])
    manager._preserve_ref(ref, r.originals[0])
    with pytest.raises(RuntimeError, match="reference changed"):
        manager._preserve_ref(ref, r.target)
    assert git(r.repo, "rev-parse", ref) == r.originals[0]


def test_base_advancing_before_initial_generation_is_reobserved_on_resume(tmp_path, monkeypatch):
    r = recovery_rig(tmp_path, monkeypatch)
    manager = r.merger.recover.__self__
    sync = manager._sync_base
    advanced = []
    def move_before_sync(target, deadline):
        if not advanced:
            git(r.repo, "checkout", "-b", "before-generation", target)
            tip = commit(r.repo, {"src/before_generation.py": "LATER = 1"}, "later base", who=HUMAN)
            git(r.repo, "push", "origin", "before-generation:main")
            git(r.repo, "checkout", "main")
            advanced.append(tip)
        sync(target, deadline)
    monkeypatch.setattr(manager, "_sync_base", move_before_sync)
    result = r.merger.recover(r.records, r.failure, time.time() + 60, r.hooks, WindowOptions())
    assert not result["ok"] and r.merger.active_recovery() and not r.phases
    assert manager._tip(manager.active()["ref"]) is None
    monkeypatch.setattr(manager, "_sync_base", sync)
    assert manager.preflight([{"url": r.records[0].pr_url, "headRefName": "wave/task-FIRST"}]) is None
    result = r.merger.resume_recovery(time.time() + 60, r.hooks, WindowOptions())
    assert result["ok"], result["reason"]
    assert (r.repo / "src/before_generation.py").read_text().strip() == "LATER = 1"
