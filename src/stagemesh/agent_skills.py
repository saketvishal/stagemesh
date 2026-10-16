"""Select a small, offline skill pack for bounded StageMesh CLI workers.

Upstream resources stay unchanged. StageMesh's adapter policy resolves their
workflow advice against the installed project's authority and worker role.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent / "skill_packs"
_SKILL_FILES = {
    "frontend-design": "frontend-design/SKILL.md",
    "systematic-debugging": "superpowers/systematic-debugging/SKILL.md",
    "test-driven-development": "superpowers/test-driven-development/SKILL.md",
    "verification-before-completion": "superpowers/verification-before-completion/SKILL.md",
}
_SUPPORT_FILES = {
    "frontend-design": ("frontend-design/LICENSE.txt",),
    "systematic-debugging": (
        "superpowers/LICENSE.txt",
        "superpowers/systematic-debugging/root-cause-tracing.md",
        "superpowers/systematic-debugging/defense-in-depth.md",
        "superpowers/systematic-debugging/condition-based-waiting.md",
        "superpowers/systematic-debugging/find-polluter.sh",
        "superpowers/systematic-debugging/condition-based-waiting-example.ts",
    ),
    "test-driven-development": (
        "superpowers/LICENSE.txt",
        "superpowers/test-driven-development/writing-good-tests.md",
    ),
    "verification-before-completion": ("superpowers/LICENSE.txt",),
}
_UI_SUFFIXES = {".astro", ".css", ".html", ".jsx", ".less", ".scss", ".svelte", ".tsx", ".vue"}
_CODE_SUFFIXES = {
    ".c",
    ".cpp",
    ".cs",
    ".go",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".mjs",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".sql",
    ".svelte",
    ".swift",
    ".ts",
    ".tsx",
    ".vue",
}
_DOC_SUFFIXES = {".md", ".mdx", ".rst", ".txt"}
_BUG_WORDS = {
    "broken",
    "bug",
    "bugfix",
    "bugs",
    "crash",
    "debug",
    "exception",
    "fail",
    "failed",
    "fails",
    "failure",
    "failures",
    "fix",
    "flaky",
    "hang",
    "regression",
    "regressions",
    "repair",
    "timeout",
}
_BEHAVIOR_WORDS = {
    "accessibility",
    "behavior",
    "crash",
    "exception",
    "failed",
    "failure",
    "focus",
    "interaction",
    "keyboard",
    "navigation",
    "regression",
    "submit",
    "timeout",
    "validation",
}
_COSMETIC_WORDS = {
    "color",
    "colours",
    "colors",
    "copy",
    "cosmetic",
    "spacing",
    "spelling",
    "typography",
    "typo",
    "wording",
}

_ADAPTER_POLICY = (
    "StageMesh skill adapter (takes precedence over upstream skill procedures):\n"
    "- The user's instructions, installed project's AGENTS.md/CLAUDE.md/GEMINI.md, "
    "project-owned specs, approved UI, and bound change contract control this task. "
    "Read the applicable project guidance and referenced specs before editing. "
    "Do not substitute StageMesh's own product specs or invent missing requirements.\n"
    "- Focused development is the default in every client repository, even without AGENTS.md or a custom profile. "
    "Start with the task, relevant symbols, direct callers and nearby tests. Use CodeGraph first if the repository is indexed; "
    "otherwise use file-scoped searches. Do not scan/read the whole repository, generated files, dependencies, backups or "
    "unrelated worktrees. Expand only to resolve a concrete dependency, failure or uncertainty.\n"
    "- Preserve the approved UI's tokens, layout, navigation, and progressive disclosure. "
    "Reveal menus and tools only when the user's role, data, or current task needs them. "
    "Redesign only the surfaces explicitly authorized by the task; a skill is not redesign permission.\n"
    "- StageMesh owns scheduling, workspaces, Git commits, independent review, and merging. "
    "Do not start additional agents, create worktrees, install global plugins, or run upstream "
    "branch/commit/merge workflows. Supporting scripts are references, not installation steps.\n"
    "- Use focused tests for changed behavior and affected callers, plus required contract gates. "
    "This overrides upstream requests for a full project suite on every task. "
    "Do not repeat successful checks with unchanged inputs; broaden only for a failure, "
    "new change, required gate, or unresolved risk. Do not add tests mirroring implementation "
    "or for simple copy/style edits. Never weaken a gate or refresh a baseline to conceal drift.\n"
    "- Report evidence with the command, outcome, and remaining validation limits. "
    "These skills guide the work; they do not prove it passed.\n"
)


class SkillPackError(RuntimeError):
    """A selected, bundled skill is unavailable or differs from its source snapshot."""


def _task_text(title: str, contract: object | None) -> str:
    fields = [title, str(getattr(contract, "objective", ""))]
    for attr in ("acceptance_criteria", "public_api", "spec_refs", "skill_refs"):
        values = getattr(contract, attr, ())
        if isinstance(values, (tuple, list)):
            fields.extend(str(value) for value in values)
    return "\n".join(fields).lower()


def _paths(contract: object | None) -> tuple[str, ...]:
    paths = getattr(contract, "allowed_files", ())
    if not isinstance(paths, (tuple, list)):
        return ()
    return tuple(
        str(path).lower().replace("\\", "/")
        for path in paths
        if str(path) not in ("*", "**", "**/*")
    )


def _documentation_only(paths: tuple[str, ...]) -> bool:
    return bool(paths) and all(
        Path(path).suffix in _DOC_SUFFIXES or path.startswith("docs/") for path in paths
    )


def is_frontend_task(title: str, contract: object | None = None) -> bool:
    paths = _paths(contract)
    if _documentation_only(paths):
        return False
    if any(Path(path).suffix in _UI_SUFFIXES for path in paths):
        return True
    # A backend under app/ or a generic "design component" is not evidence of UI work.
    if any(
        Path(path).suffix in {"", ".js", ".ts", ".mjs"}
        and re.search(r"(?:^|/)(?:frontend|components|pages|styles|ui|web)/", path)
        for path in paths
    ):
        return True
    text = _task_text(title, contract)
    words = set(re.findall(r"[a-z0-9]+", text))
    return bool(words & {"frontend", "ui", "ux", "webpage", "stylesheet", "responsive"}) or any(
        phrase in text for phrase in ("user interface", "web page", "mobile screen")
    )


def select_skills(
    title: str,
    contract: object | None = None,
    *,
    remediation: bool = False,
    stage: str = "implementation",
) -> tuple[str, ...]:
    if stage == "review":
        return ("verification-before-completion",)
    if stage != "implementation":
        raise ValueError(f"unsupported skill stage: {stage}")
    paths = _paths(contract)
    words = set(re.findall(r"[a-z0-9]+", _task_text(title, contract)))
    cosmetic = bool(words & _COSMETIC_WORDS) and not bool(words & _BEHAVIOR_WORDS)
    docs_only = _documentation_only(paths)
    skills: list[str] = []
    if is_frontend_task(title, contract):
        skills.append("frontend-design")
    if remediation or (words & _BUG_WORDS and not cosmetic and not docs_only):
        skills.append("systematic-debugging")
    code = any(
        Path(path).suffix in _CODE_SUFFIXES
        or (
            not Path(path).suffix
            and re.search(
                r"(?:^|/)(?:src|app|apps|lib|packages|backend|frontend|web|ui|components|tests)/",
                path,
            )
        )
        for path in paths
    )
    unspecified_change = not paths and bool(
        words & {"add", "build", "implement", "refactor", "fix", "repair"}
    )
    if not docs_only and not cosmetic and (code or unspecified_change):
        skills.append("test-driven-development")
    skills.append("verification-before-completion")
    return tuple(skills)


def _resolve_resources(skills: tuple[str, ...]) -> dict[str, Path]:
    root = SKILL_ROOT.resolve()
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("format_version") != 1 or not isinstance(manifest.get("resources"), dict):
            raise ValueError("unsupported manifest format")
    except (OSError, ValueError, AttributeError) as exc:
        raise SkillPackError(f"cannot load bundled skill manifest: {exc}") from exc
    resolved: dict[str, Path] = {}
    checked: set[str] = set()
    for skill in skills:
        main = _SKILL_FILES[skill]
        for relative in (main, *_SUPPORT_FILES[skill]):
            if relative in checked:
                continue
            path = (root / relative).resolve()
            try:
                if not path.is_relative_to(root):
                    raise ValueError("resource escapes installed skill pack")
                expected = manifest["resources"][relative]["git_blob_sha"]
                data = path.read_bytes()
                blob = b"blob " + str(len(data)).encode("ascii") + b"\0" + data
                actual = hashlib.sha1(blob, usedforsecurity=False).hexdigest()
                if actual != expected:
                    raise ValueError("resource differs from pinned upstream blob")
            except (OSError, KeyError, TypeError, ValueError) as exc:
                raise SkillPackError(f"unavailable skill resource {relative}: {exc}") from exc
            checked.add(relative)
        resolved[skill] = root / main
    return resolved


_PROJECT_SKILL_ROOTS = ("agent-skills", ".agents/skills", ".claude/skills")


def _resolve_project_skill(project: Path, relative: str) -> Path:
    root = project.resolve()
    candidate = (root / relative).resolve()
    try:
        inside = candidate.is_relative_to(root)
    except ValueError:
        inside = False
    if not inside or candidate.name != "SKILL.md" or not candidate.is_file():
        raise SkillPackError(
            f"unavailable project skill {relative}: path must name an existing SKILL.md inside the project"
        )
    return candidate


def _project_skills(
    title: str,
    contract: object | None,
    project: Path | None,
) -> dict[str, Path]:
    if project is None:
        return {}
    selected: dict[str, Path] = {}
    explicit = getattr(contract, "skill_refs", ())
    if isinstance(explicit, (tuple, list)):
        for relative in explicit:
            path = _resolve_project_skill(project, str(relative))
            selected[path.parent.name] = path
    if is_frontend_task(title, contract) and "genui-architect" not in selected:
        for skill_root in _PROJECT_SKILL_ROOTS:
            relative = f"{skill_root}/genui-architect/SKILL.md"
            candidate = (project / relative)
            if candidate.is_file():
                selected["genui-architect"] = _resolve_project_skill(project, relative)
                break
    return selected


def skill_prompt(
    title: str,
    contract: object | None = None,
    *,
    remediation: bool = False,
    stage: str = "implementation",
    project: Path | None = None,
) -> str:
    selected = select_skills(title, contract, remediation=remediation, stage=stage)
    files = _resolve_resources(selected)
    project_files = _project_skills(title, contract, project)
    lines = [
        "\n" + _ADAPTER_POLICY,
        (
            "Read the selected full SKILL.md files below before starting this stage. "
            "Use ordinary file-reading tools (or Python Path.read_text) if your provider has no native Skill tool. "
            "Read linked companion files when applying their technique. "
            "Apply them subject to the StageMesh adapter policy above; no network fetch is needed. "
            "Project skills guide only the bounded task and do not expand the change contract."
        ),
    ]
    if stage == "review":
        lines.append(
            "Review is read-only: do not implement fixes, run mutating checks, or refresh snapshots. Preserve the requested structured response format."
        )
    lines.extend(
        f"- Skill {name}: {json.dumps(str(path), ensure_ascii=False)}"
        for name, path in files.items()
    )
    lines.extend(
        f"- Project skill {name}: {json.dumps(str(path), ensure_ascii=False)}"
        for name, path in project_files.items()
    )
    return "\n".join(lines) + "\n\n"
