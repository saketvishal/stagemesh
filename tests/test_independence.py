"""StageMesh must not depend on any product or on where it happens to be checked out."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCANNED = [REPO_ROOT / "build_coordinator", REPO_ROOT / ".stagemesh", REPO_ROOT / "docs", REPO_ROOT / "examples"]
SUFFIXES = {".py", ".yaml", ".yml", ".md", ".json", ".ps1", ".cmd", ".sh", ""}
# oss_boundary.py exists precisely to name (and forbid) product coupling.
EXEMPT = {"oss_boundary.py"}
DRIVE_PATH = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\/](?!/)(?:Users|caventra|build-coordinator|smdemo|Windows)", re.IGNORECASE)


def files():
    for root in SCANNED:
        for path in root.rglob("*"):
            if (
                path.is_file()
                and "__pycache__" not in path.parts
                and ".build-coordinator" not in path.parts
                and path.suffix in SUFFIXES
                and path.name not in EXEMPT
                and "evidence" not in path.parts
            ):
                yield path


def test_no_product_name_in_shipped_code_docs_or_project_definition():
    offenders = [
        str(p.relative_to(REPO_ROOT))
        for p in files()
        if re.search(r"caventra", p.read_text(encoding="utf-8", errors="ignore"), re.IGNORECASE)
    ]
    assert not offenders, offenders


def test_no_machine_specific_absolute_paths_are_embedded():
    offenders = [
        f"{p.relative_to(REPO_ROOT)}: {m.group(0)}"
        for p in files()
        for m in DRIVE_PATH.finditer(p.read_text(encoding="utf-8", errors="ignore"))
    ]
    assert not offenders, offenders


def test_own_project_is_discoverable_from_wherever_the_repo_lives(tmp_path):
    from build_coordinator.project.backlog import load_backlog
    from build_coordinator.project.definition import find_project_root, load_project

    root = find_project_root(REPO_ROOT / "build_coordinator")
    assert root == REPO_ROOT.resolve()
    project = load_project(root)
    assert project.project_id == "stagemesh" and project.concurrency == 1
    definitions = load_backlog(project)
    ids = {d.task_id for d in definitions}
    assert {"SM-011", "SM-012", "SM-013", "SM-014", "SM-015"} <= ids
    assert project.reviewers >= 2 or not any(d.review_policy == "TWO_REVIEWERS" for d in definitions)


def test_runtime_state_is_ignored_by_git():
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".build-coordinator/" in ignored and "*.sqlite3" in ignored
