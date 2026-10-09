"""Historical attribution correction: wrong identities are removed, content and legitimate contributors are preserved."""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location("attribution_tool", Path(__file__).resolve().parents[1] / "scripts" / "attribution_tool.py")
tool = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules["attribution_tool"] = tool  # dataclasses resolve their module through sys.modules
_SPEC.loader.exec_module(tool)

OWNER = ("Vishal Singh", "20689561+saketvishal@users.noreply.github.com")
BAD = "12345678+saketvishal@users.noreply.github.com"
GLUED = "Vishal Singh 12345678+saketvishal@users.noreply.github.com"
HUMAN = ("Ada Lovelace", "ada@example.org")



def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    import os

    merged = {**os.environ, "GIT_CONFIG_GLOBAL": str(repo.parent / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1", **(env or {})}
    return subprocess.run(["git", *args], cwd=repo, text=True, encoding="utf-8", capture_output=True, check=True, env=merged).stdout.strip()


def _commit(repo: Path, name: str, email: str, message: str, filename: str, content: str, when: str) -> str:
    (repo / filename).write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    env = {
        "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
        "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when,
    }
    _git(repo, "commit", "-q", "-m", message, env=env)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def history(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "gitconfig").write_text("", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    shas = {
        "base": _commit(repo, *OWNER, "base", "base.txt", "base\n", "2026-01-01T10:00:00+00:00"),
        "wrong_id": _commit(repo, "Vishal Singh", BAD, "Add feature\n\nCo-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>", "feature.txt", "feature\n", "2026-01-02T10:00:00+00:00"),
        "human": _commit(repo, *HUMAN, "Human fix\n\nCo-authored-by: Grace Hopper <grace@example.org>", "human.txt", "by a human\n", "2026-01-03T10:00:00+00:00"),
        "glued": _commit(repo, "Vishal Singh", GLUED, "Glued\n\nCo-authored-by: Vishal Singh <" + GLUED + ">", "glued.txt", "glued\n", "2026-01-04T10:00:00+00:00"),
        "clean": _commit(repo, *OWNER, "Clean", "clean.txt", "clean\n", "2026-01-05T10:00:00+00:00"),
    }
    return repo, shas


def test_audit_finds_every_wrong_identity_and_trailer(history: tuple[Path, dict[str, str]]) -> None:
    repo, shas = history
    report = tool.audit(repo, ["main"])
    flagged = {finding.sha for finding in report.offences}
    assert flagged == {shas["wrong_id"], shas["glued"]}
    kinds = {finding.kind for finding in report.offences}
    assert kinds == {"placeholder-author", "placeholder-committer", "coauthor-trailer"}
    assert tool.main(["audit", "--repo", str(repo), "main"]) == 1


def test_correction_removes_wrong_attribution_but_preserves_content_and_legitimate_contributors(history: tuple[Path, dict[str, str]], tmp_path: Path) -> None:
    repo, shas = history
    work = tmp_path / "work"
    commit_map = tool.rewrite(repo, work, OWNER[0], OWNER[1], "saketvishal", ["refs/heads/main"])

    mapping = dict(line.split() for line in commit_map.read_text(encoding="utf-8").splitlines()[1:])
    old = _git(repo, "rev-list", "--reverse", "main").split()
    new = _git(work, "rev-list", "--reverse", "main").split()
    assert len(old) == len(new) == 5  # nothing dropped, nothing added

    for before, after in zip(old, new, strict=True):
        assert mapping[before] == after
        assert _git(repo, "rev-parse", f"{before}^{{tree}}") == _git(work, "rev-parse", f"{after}^{{tree}}")  # byte-identical content
        fields = "%an|%ad|%cd|%s"
        assert _git(repo, "log", "-1", f"--format={fields}", "--date=iso-strict", before) == _git(work, "log", "-1", f"--format={fields}", "--date=iso-strict", after)

    assert new[0] == old[0]  # history before the first offence is untouched, byte for byte

    assert tool.audit(work, ["main"]).ok
    assert _git(work, "log", "main", "--format=%ae%n%ce%n%B").lower().count("12345678") == 0
    assert "anthropic" not in _git(work, "log", "main", "--format=%B").lower()

    by_subject = {_git(work, "log", "-1", "--format=%s", sha): sha for sha in new}
    wrong = by_subject["Add feature"]
    assert _git(work, "log", "-1", "--format=%an <%ae> | %cn <%ce>", wrong) == f"{OWNER[0]} <{OWNER[1]}> | {OWNER[0]} <{OWNER[1]}>"
    human = by_subject["Human fix"]
    assert _git(work, "log", "-1", "--format=%an <%ae>", human) == f"{HUMAN[0]} <{HUMAN[1]}>"  # a genuine contributor is not re-attributed
    assert "Co-authored-by: Grace Hopper <grace@example.org>" in _git(work, "log", "-1", "--format=%B", human)  # nor is their trailer touched
    assert _git(work, "log", "-1", "--format=%B", by_subject["Glued"]).strip() == "Glued"

    # the source repository was only read
    assert _git(repo, "rev-parse", "main") == shas["clean"]


def test_every_requested_ref_is_rewritten_not_only_the_last(history: tuple[Path, dict[str, str]], tmp_path: Path) -> None:
    repo, shas = history
    _git(repo, "checkout", "-q", "-b", "topic", shas["base"])
    topic = _commit(
        repo, "Vishal Singh", BAD, "Topic work\n\nCo-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>", "topic.txt", "topic\n", "2026-01-06T10:00:00+00:00"
    )
    _git(repo, "checkout", "-q", "main")
    work = tmp_path / "work"

    commit_map = tool.rewrite(repo, work, OWNER[0], OWNER[1], "saketvishal", ["refs/heads/main", "refs/heads/topic"])

    mapping = dict(line.split() for line in commit_map.read_text(encoding="utf-8").splitlines()[1:])
    assert mapping[topic] == _git(work, "rev-parse", "topic") != topic
    assert tool.audit(work, ["main", "topic"]).ok


def test_untouched_signed_commits_keep_their_exact_hash_and_signature(history: tuple[Path, dict[str, str]], tmp_path: Path) -> None:
    repo, shas = history
    tree = _git(repo, "rev-parse", f"{shas['base']}^{{tree}}")
    raw = (
        f"tree {tree}\nparent {shas['base']}\nauthor {OWNER[0]} <{OWNER[1]}> 1767268800 +0000\ncommitter GitHub <noreply@github.com> 1767268800 +0000\n"
        "gpgsig -----BEGIN PGP SIGNATURE-----\n \n abcdef\n -----END PGP SIGNATURE-----\n\nSigned merge, nothing wrong with it\n"
    )
    import os

    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(repo.parent / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1"}
    signed = subprocess.run(["git", "hash-object", "--literally", "-t", "commit", "-w", "--stdin"], cwd=repo, input=raw.encode(), capture_output=True, check=True, env=env).stdout.decode().strip()
    _git(repo, "update-ref", "refs/heads/signed", signed)
    work = tmp_path / "work"

    commit_map = tool.rewrite(repo, work, OWNER[0], OWNER[1], "saketvishal", ["refs/heads/main", "refs/heads/signed"])

    mapping = dict(line.split() for line in commit_map.read_text(encoding="utf-8").splitlines()[1:])
    assert mapping[signed] == signed == _git(work, "rev-parse", "signed")  # same object, signature intact
    assert "gpgsig" in _git(work, "cat-file", "commit", signed)
    assert mapping[shas["wrong_id"]] != shas["wrong_id"]  # while the offender on the other branch was corrected
