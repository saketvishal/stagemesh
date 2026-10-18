"""Publication after an older merged PR, without rewriting its work."""

import json
import subprocess

import pytest

from stagemesh import survivor_wave
from stagemesh.survivor_wave import TaskRecord
from test_survivor_wave import _commit_on, _git, _main_ref, _with_origin, make_store


@pytest.mark.parametrize("local_original", ["missing", "current"])
@pytest.mark.parametrize("fallback", ["new", "open", "merged", "local_conflict", "remote_conflict", "pr_conflict"])
def test_merged_original_uses_exact_retry_without_rewriting_old_work(tmp_path, monkeypatch, local_original, fallback):
    _store, project = make_store(tmp_path, ["T-1"])
    origin = _with_origin(tmp_path, project)
    base_ref = _main_ref(project)
    base = base_ref.removeprefix("refs/heads/")
    old = _git(project, "rev-parse", base_ref)
    survivor_wave.prepare_staging_ref(project, base_ref)
    sha = _commit_on(project, survivor_wave.STAGING_REF, "reviewed replacement")
    original = "wave/task-T-1"
    retry = f"{original}-{sha[:7]}"
    _git(origin, "update-ref", f"refs/heads/{original}", old)
    if local_original == "current":
        _git(project, "branch", original, sha)
    if fallback == "local_conflict":
        _git(project, "branch", retry, old)
    if fallback == "remote_conflict":
        # The old remote work is ahead of the candidate, so even a normal push must fail.
        ahead = _commit_on(project, survivor_wave.STAGING_REF, "other preserved work")
        _git(origin, "fetch", str(project), f"{ahead}:refs/heads/{retry}")
    calls = []
    created = False
    real_run = subprocess.run

    def fake_run(argv, **kwargs):
        nonlocal created
        calls.append(list(argv))
        if list(argv[:3]) == ["gh", "pr", "list"]:
            branch = argv[argv.index("--head") + 1]
            if branch == original:
                prs = [{"url": "https://example.invalid/pr/old", "state": "MERGED", "headRefOid": old, "baseRefName": base}]
            elif fallback in {"open", "merged", "pr_conflict"} or created:
                prs = [{"url": "https://example.invalid/pr/new", "state": "MERGED" if fallback == "merged" else "OPEN",
                        "headRefOid": old if fallback == "pr_conflict" else sha, "baseRefName": base}]
            else:
                prs = []
            return subprocess.CompletedProcess(argv, 0, json.dumps(prs), "")
        if list(argv[:3]) == ["gh", "pr", "create"]:
            assert argv[argv.index("--head") + 1] == retry
            created = True
            return subprocess.CompletedProcess(argv, 0, "https://example.invalid/pr/new", "")
        assert argv[0] != "gh"
        assert "--force" not in argv and "--force-with-lease" not in argv
        return real_run(argv, **kwargs)

    monkeypatch.setattr(survivor_wave.subprocess, "run", fake_run)
    opener = survivor_wave.git_gh_pr_opener(project, base=base)
    record = TaskRecord("T-1", integrated_candidate=sha)
    if fallback.endswith("conflict"):
        with pytest.raises(RuntimeError):
            opener(record)
        assert not created
    else:
        assert opener(record) == "https://example.invalid/pr/new"
        assert opener(record) == "https://example.invalid/pr/new"
        assert sum(argv[:3] == ["gh", "pr", "create"] for argv in calls) == (fallback == "new")
        if fallback == "new":
            assert _git(origin, "rev-parse", f"refs/heads/{retry}") == sha
    assert _git(origin, "rev-parse", f"refs/heads/{original}") == old
    assert _git(project, "rev-parse", base_ref) == old
    assert _git(project, "cat-file", "-t", sha) == "commit"


@pytest.mark.parametrize("invalid", ["base", "head", "url", "ambiguous", "closed", "open"])
def test_obsolete_pr_metadata_must_be_unambiguous_and_merged(tmp_path, monkeypatch, invalid):
    _store, project = make_store(tmp_path, ["T-1"])
    sha = _git(project, "rev-parse", "HEAD")
    base = _main_ref(project).removeprefix("refs/heads/")
    pr = {"url": "https://example.invalid/pr/old", "state": "MERGED", "headRefOid": "f" * 40, "baseRefName": base}
    pr.update({"base": {"baseRefName": "other"}, "head": {"headRefOid": "invalid"}, "url": {"url": None},
               "closed": {"state": "CLOSED"}, "open": {"state": "OPEN"}}.get(invalid, {}))
    real_run = subprocess.run

    def fake_run(argv, **kwargs):
        if list(argv[:3]) == ["gh", "pr", "list"]:
            assert argv[argv.index("--head") + 1] == "wave/task-T-1"
            return subprocess.CompletedProcess(argv, 0, json.dumps([pr, pr] if invalid == "ambiguous" else [pr]), "")
        assert argv[0] != "gh" and argv[1] not in {"branch", "push", "merge", "update-ref"}
        return real_run(argv, **kwargs)

    monkeypatch.setattr(survivor_wave.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError):
        survivor_wave.git_gh_pr_opener(project, base=base)(TaskRecord("T-1", integrated_candidate=sha))
