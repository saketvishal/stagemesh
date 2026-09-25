"""`stagemesh init`: turn a git repository into a StageMesh project."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from build_coordinator.project.definition import PROJECT_DIR, PROJECT_FILE, TASKS_DIR, ProjectDefinition, ProjectError, load_project

PROJECT_TEMPLATE = """\
# StageMesh project definition (version-controlled). Runtime state lives in
# .build-coordinator/ and is never committed.
schema_version: 1
id: {project_id}
name: {name}
aliases: [{project_id}]

repository:
  main_ref: {main_ref}

execution:
  concurrency: 1              # tasks worked on in parallel, each in its own isolated worktree
  reviewers: 1
  default_review_policy: INDEPENDENT

# Agents: by default StageMesh uses every coding-agent runtime that
# `stagemesh agent setup` verified on this machine. Nothing to configure here.

# Local only by default: integrated work stays on the local {main_ref} branch.
# To deliver upstream after review and integration:
# upstream:
#   remote: origin
#   push: true
"""

TASK_TEMPLATE = """\
# One task per entry. `id` is its permanent identity.
tasks:
  - id: {prefix}-001
    title: Replace this with a short imperative title
    objective: >
      What should change and why.
    acceptance_criteria:
      - Observable condition that means the task is done.
    priority: 10
    review: INDEPENDENT
    validation:
      - python -m pytest -q      # StageMesh runs these itself; edit or remove
"""


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "project"


def init_project(path: str | Path, *, name: str | None = None, sample_task: bool = True) -> tuple[ProjectDefinition, list[str]]:
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise ProjectError(f"{root} is not a directory")
    if subprocess.run(["git", "rev-parse", "--git-dir"], cwd=root, capture_output=True).returncode != 0:
        raise ProjectError(f"{root} is not a git repository; run `git init` and make an initial commit first")
    if subprocess.run(["git", "rev-parse", "--verify", "--quiet", "HEAD"], cwd=root, capture_output=True).returncode != 0:
        raise ProjectError("the repository has no commits yet; make an initial commit first")
    main_ref = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root, capture_output=True, text=True
    ).stdout.strip() or "main"

    created: list[str] = []
    definition_dir = root / PROJECT_DIR
    project_file = definition_dir / PROJECT_FILE
    project_id = _slug(name or root.name)
    if not project_file.exists():
        (definition_dir / TASKS_DIR).mkdir(parents=True, exist_ok=True)
        project_file.write_text(
            PROJECT_TEMPLATE.format(project_id=project_id, name=name or root.name, main_ref=main_ref), encoding="utf-8"
        )
        created.append(f"{PROJECT_DIR}/{PROJECT_FILE}")
        if sample_task and not any((definition_dir / TASKS_DIR).glob("*.yaml")):
            (definition_dir / TASKS_DIR / "example.yaml.sample").write_text(
                TASK_TEMPLATE.format(prefix=project_id.upper()[:12]), encoding="utf-8"
            )
            created.append(f"{PROJECT_DIR}/{TASKS_DIR}/example.yaml.sample  (rename to *.yaml to activate)")
    gitignore = root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if ".build-coordinator/" not in existing:
        gitignore.write_text(existing + ("" if existing.endswith("\n") or not existing else "\n") + ".build-coordinator/\n", encoding="utf-8")
        created.append(".gitignore (+ .build-coordinator/)")
    return load_project(root), created
