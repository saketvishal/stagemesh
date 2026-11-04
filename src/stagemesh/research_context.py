"""Deterministic, read-only research briefs for implementation prompts."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .contracts import (
    CONTRACT_VERSION,
    BoundChangeContract,
    ChangeContract,
    canonical_contract_json,
    scope_classification,
)

if TYPE_CHECKING:
    from .persistence import Store

MAX_RESEARCH_FILES = 80
MAX_TEST_HINTS = 40
SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".stagemesh",
    "__pycache__",
    "dist",
    "node_modules",
}

# Ignored client files can still be source/runtime inputs and must remain immutable
# during research. Only dependency, cache, and generated-output trees are excluded;
# .stagemesh is intentionally included because its config/contracts affect authority.
SNAPSHOT_IGNORED_SKIP_DIRS = (SKIP_DIRS - {".stagemesh"}) | {".tox", ".venv", "venv"}


def build_research_brief(project: Path | None, contract: ChangeContract | None) -> str:
    """Return a scoped, deterministic pre-edit research brief for a provider prompt.

    This is deliberately local and read-only. It gives coding agents a bounded map of
    the files to inspect first, without broadening the change scope.
    """
    if project is None or contract is None:
        return ""
    root = project.resolve()
    allowed = _matching_files(root, contract.allowed_files, limit=MAX_RESEARCH_FILES)
    tests = _test_hints(root, allowed, limit=MAX_TEST_HINTS)
    lines = [
        "Contextual research brief (read-only; complete before editing):",
        "- First inspect the existing code paths below and summarize the local pattern in your own scratch notes.",
        "- Use this brief to narrow context. It does not expand the change contract or authorize edits outside allowed files.",
        "- This brief is a starting map, not a read allowlist. Inspect directly relevant project context as needed, subject to project access rules; an edit outside allowed files still requires an audited scope expansion.",
    ]
    if allowed:
        lines.append("- In-scope existing files to inspect first:")
        lines.extend(f"  - {path}" for path in allowed)
    else:
        lines.append("- No existing files matched the allowed scope; inspect the nearest existing parent directories before editing.")
    if tests:
        lines.append("- Nearby tests or fixtures worth checking before coding:")
        lines.extend(f"  - {path}" for path in tests)
    lines.append("")
    return "\n".join(lines) + "\n"


def _matching_files(root: Path, patterns: tuple[str, ...], *, limit: int) -> list[str]:
    files: list[str] = []
    for path in _walk_files(root):
        rel = path.relative_to(root).as_posix()
        if any(_matches(rel, pattern) for pattern in patterns):
            files.append(rel)
            if len(files) >= limit:
                break
    return sorted(files)


def _test_hints(root: Path, allowed: list[str], *, limit: int) -> list[str]:
    if not allowed:
        return []
    stems = {Path(item).stem.lower() for item in allowed if Path(item).stem}
    hints: list[str] = []
    for path in _walk_files(root):
        rel = path.relative_to(root).as_posix()
        parts = set(path.parts)
        if "tests" not in parts and not Path(rel).name.startswith("test_"):
            continue
        name = Path(rel).stem.lower().removeprefix("test_")
        if name in stems or any(stem and stem in name for stem in stems):
            hints.append(rel)
            if len(hints) >= limit:
                break
    return sorted(hints)


def _walk_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(part in SKIP_DIRS for part in rel_parts):
            continue
        yield path


def _matches(path: str, pattern: str) -> bool:
    normalized = pattern.replace("\\", "/").strip()
    if not normalized:
        return False
    if normalized == "**":
        return True
    if fnmatch.fnmatch(path, normalized):
        return True
    if normalized.endswith("/**"):
        return path.startswith(normalized[:-3].rstrip("/") + "/")
    return path == normalized


MAX_RESEARCH_EVIDENCE_BYTES = 256 * 1024
MAX_RESEARCH_TEXT = 4096
MAX_RESEARCH_BRIEF = 32 * 1024
RESEARCH_EVIDENCE_VERSION = 2


@dataclass(frozen=True)
class ResearchFinding:
    path: str
    summary: str


@dataclass(frozen=True)
class ResearchEvidence:
    """Advisory observations, never authorization or independent review evidence.

    Identity is supplied by the caller's trusted binding; this module neither
    resolves Git refs nor attests that an inspector actually read the source.
    """

    baseline_sha: str
    contract_digest: str
    inspected_paths: tuple[str, ...]
    findings: tuple[ResearchFinding, ...]
    unresolved_scope_gaps: tuple[str, ...]
    provenance: tuple[str, ...]
    brief: str
    recommended_implementation_paths: tuple[str, ...] = ()
    no_touch_paths: tuple[str, ...] = ()
    version: int = RESEARCH_EVIDENCE_VERSION


def build_research_evidence(
    project: Path,
    bound: BoundChangeContract,
    *,
    inspected_paths: tuple[str, ...],
    findings: tuple[ResearchFinding, ...] = (),
    unresolved_scope_gaps: tuple[str, ...] = (),
    provenance: tuple[str, ...],
    recommended_implementation_paths: tuple[str, ...] = (),
    no_touch_paths: tuple[str, ...] = (),
) -> ResearchEvidence:
    """Build bounded evidence without reading file contents or changing scope.

    Recorded paths must be in the bound edit scope. This does not restrict the
    broader read-only investigation permitted by the contextual brief. Missing
    context can be described in unresolved_scope_gaps without granting access.
    """
    baseline = _research_binding(bound)
    evidence = ResearchEvidence(
        baseline_sha=baseline,
        contract_digest=bound.digest,
        inspected_paths=inspected_paths,
        findings=findings,
        unresolved_scope_gaps=unresolved_scope_gaps,
        provenance=provenance,
        brief=build_research_brief(project, bound.contract),
        recommended_implementation_paths=recommended_implementation_paths,
        no_touch_paths=no_touch_paths,
    )
    _validate_research_evidence(evidence, project, bound)
    return evidence


def write_research_evidence(
    destination: Path, evidence: ResearchEvidence, *, project: Path, bound: BoundChangeContract,
) -> None:
    """Persist to a new caller-owned file outside the source tree; never overwrite.

    Storage selection and lifecycle integration belong to the caller. No default
    location, directory creation, model dispatch, or contract mutation occurs.
    """
    _validate_research_evidence(evidence, project, bound)
    target = _research_storage_path(destination, project)
    with target.open("xb") as stream:
        stream.write(_research_bytes(evidence))


def read_research_evidence(
    source: Path, *, project: Path, bound: BoundChangeContract,
) -> ResearchEvidence:
    """Load bounded JSON and reject stale identity and invalid recorded paths."""
    _research_binding(bound)
    target = _research_storage_path(source, project)
    with target.open("rb") as stream:
        raw = stream.read(MAX_RESEARCH_EVIDENCE_BYTES + 1)
    if len(raw) > MAX_RESEARCH_EVIDENCE_BYTES:
        raise ValueError("research evidence exceeds size limit")
    try:
        payload = json.loads(raw)
        legacy_fields = {
            "version", "baseline_sha", "contract_digest", "inspected_paths", "findings",
            "unresolved_scope_gaps", "provenance", "brief",
        }
        current_fields = legacy_fields | {"recommended_implementation_paths", "no_touch_paths"}
        if not isinstance(payload, dict) or frozenset(payload) not in {frozenset(legacy_fields), frozenset(current_fields)}:
            raise ValueError("invalid research evidence fields")
        version = payload.get("version")
        if set(payload) == legacy_fields and version == 1:
            payload["recommended_implementation_paths"] = []
            payload["no_touch_paths"] = []
        elif set(payload) != current_fields or version != RESEARCH_EVIDENCE_VERSION:
            raise ValueError("research evidence fields do not match version")
        for field in (
            "inspected_paths", "findings", "unresolved_scope_gaps", "provenance",
            "recommended_implementation_paths", "no_touch_paths",
        ):
            if not isinstance(payload[field], list) or len(payload[field]) > MAX_RESEARCH_FILES:
                raise ValueError(f"invalid or oversized research {field}")
        payload["findings"] = tuple(ResearchFinding(**item) for item in payload["findings"])
        for field in (
            "inspected_paths", "unresolved_scope_gaps", "provenance",
            "recommended_implementation_paths", "no_touch_paths",
        ):
            payload[field] = tuple(payload[field])
        evidence = ResearchEvidence(**payload)
    except (TypeError, UnicodeError, RecursionError) as exc:
        raise ValueError("invalid research evidence payload") from exc
    _validate_research_evidence(evidence, project, bound)
    return evidence


def _research_binding(bound: BoundChangeContract) -> str:
    if type(bound.version) is not int or bound.version != CONTRACT_VERSION:
        raise ValueError("unsupported research contract version")
    canonical = canonical_contract_json(bound.contract)
    if (canonical != bound.canonical_json
            or hashlib.sha256(canonical.encode("utf-8")).hexdigest() != bound.digest):
        raise ValueError("research contract digest does not match binding")
    if not isinstance(bound.baseline_sha, str) or not re.fullmatch(
        r"[0-9a-f]{40}|[0-9a-f]{64}", bound.baseline_sha
    ):
        raise ValueError("research requires a full baseline SHA")
    return bound.baseline_sha


def _research_text(value: str, field: str, limit: int = MAX_RESEARCH_TEXT) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"invalid or oversized research {field}")


def _validate_research_evidence(
    evidence: ResearchEvidence,
    project: Path,
    bound: BoundChangeContract,
    *,
    require_inspected_files: bool = True,
) -> None:
    baseline = _research_binding(bound)
    if type(evidence.version) is not int or evidence.version not in (1, RESEARCH_EVIDENCE_VERSION):
        raise ValueError("unsupported research evidence version")
    if evidence.baseline_sha != baseline or evidence.contract_digest != bound.digest:
        raise ValueError("stale research identity: baseline or contract digest mismatch")
    for field in (
        "inspected_paths", "findings", "unresolved_scope_gaps", "provenance",
        "recommended_implementation_paths", "no_touch_paths",
    ):
        values = getattr(evidence, field)
        if not isinstance(values, tuple) or len(values) > MAX_RESEARCH_FILES:
            raise ValueError(f"invalid or oversized research {field}")
    if evidence.version == 1 and (evidence.recommended_implementation_paths or evidence.no_touch_paths):
        raise ValueError("legacy research evidence cannot carry implementation scope")
    if not evidence.provenance:
        raise ValueError("research provenance is required")
    for field in ("unresolved_scope_gaps", "provenance"):
        for value in getattr(evidence, field):
            _research_text(value, field)
    _research_text(evidence.brief, "brief", MAX_RESEARCH_BRIEF)
    root = project.resolve()
    for path in evidence.inspected_paths:
        if require_inspected_files:
            _research_path(root, path, bound.contract)
        else:
            _research_scope_path(root, path, bound.contract)
    if len(set(evidence.inspected_paths)) != len(evidence.inspected_paths):
        raise ValueError("duplicate inspected paths")
    for finding in evidence.findings:
        if not isinstance(finding, ResearchFinding):
            raise TypeError("invalid research finding")
        if finding.path not in evidence.inspected_paths:
            raise ValueError("finding path must be inspected")
        _research_text(finding.summary, "finding summary")
    for field in ("recommended_implementation_paths", "no_touch_paths"):
        paths = getattr(evidence, field)
        if len(set(paths)) != len(paths):
            raise ValueError(f"duplicate research {field}")
        for path in paths:
            _research_scope_path(root, path, bound.contract)
    overlap = set(evidence.recommended_implementation_paths) & set(evidence.no_touch_paths)
    if overlap:
        raise ValueError("recommended implementation paths overlap no-touch paths")
    _research_bytes(evidence)


def _research_path(root: Path, path: str, contract: ChangeContract) -> None:
    _canonical_research_path(path)
    try:
        resolved = (root / path).resolve(strict=True)
        actual = resolved.relative_to(root).as_posix()
    except (OSError, ValueError, RuntimeError) as exc:
        raise ValueError("research path must exist inside project scope") from exc
    if not resolved.is_file():
        raise ValueError("research path must be a file")
    classified = scope_classification(contract, [path, actual])
    if any(classified[key] for key in ("outside_allowed", "excluded", "forbidden", "protected")):
        raise ValueError("research path is outside contract scope")


def _canonical_research_path(path: str) -> None:
    _research_text(path, "path")
    if (any(char in path for char in "\\:*?[]")
            or any(ord(char) < 32 for char in path)
            or any(part in ("", ".", "..") or part.rstrip(" .") != part
                   for part in path.split("/"))):
        raise ValueError("research paths must be canonical relative paths")


def _research_scope_path(root: Path, path: str, contract: ChangeContract) -> None:
    """Validate a proposed file path without requiring a new file to exist yet."""
    _canonical_research_path(path)
    try:
        resolved = (root / path).resolve(strict=False)
        actual = resolved.relative_to(root).as_posix()
    except (OSError, ValueError, RuntimeError) as exc:
        raise ValueError("research scope path must remain inside project scope") from exc
    if resolved.exists() and not resolved.is_file():
        raise ValueError("research scope paths must identify files")
    classified = scope_classification(contract, [path, actual])
    if any(classified[key] for key in ("outside_allowed", "excluded", "forbidden", "protected")):
        raise ValueError("research scope path is outside contract scope")


def _research_bytes(evidence: ResearchEvidence) -> bytes:
    payload = asdict(evidence)
    if evidence.version == 1:
        payload.pop("recommended_implementation_paths", None)
        payload.pop("no_touch_paths", None)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_RESEARCH_EVIDENCE_BYTES:
        raise ValueError("research evidence exceeds size limit")
    return raw


def _research_storage_path(path: Path, project: Path) -> Path:
    target = path.resolve()
    if target.is_relative_to(project.resolve()):
        raise ValueError("research evidence storage must be outside the source project")
    return target


MAX_RESEARCH_PROMPT_FINDINGS = 20
MAX_RESEARCH_PROMPT_CHARS = 8000


class ResearchBlocked(Exception):
    """Implementation must not start; `reason` is a stable machine-readable code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def research_request(bound: BoundChangeContract, *, require_scope: bool = False) -> str:
    """Prompt for the read-only researcher; it must echo the identity it researched against."""
    scope_request = (
        "Also include recommended_implementation_paths (non-empty list of exact in-contract file paths that may change) "
        "and no_touch_paths (list of exact in-contract file paths that must not change). These scope lists narrow the contract "
        "and never expand it.\n"
        if require_scope else ""
    )
    return (
        "StageMesh research stage (read-only). Do NOT edit, create, delete or stage any file and do not run git write commands.\n"
        "Inspect the code the change contract needs and reply with ONLY one JSON object with keys: "
        "baseline_sha, contract_digest, inspected_paths (list of existing in-scope file paths), "
        "findings (list of {path, summary}; path must be in inspected_paths), "
        "unresolved_scope_gaps (list of strings; required context that is missing or out of scope), "
        "provenance (non-empty list of strings).\n" + scope_request
        + f"Use baseline_sha={bound.baseline_sha} and contract_digest={bound.digest}.\n\n"
        f"{canonical_contract_json(bound.contract)}\n"
    )


def parse_research_report(
    raw: str, project: Path, bound: BoundChangeContract, *, require_scope: bool = False,
) -> ResearchEvidence:
    """Validate a researcher's reply against the bound identity; unresolved scope gaps block."""
    if len(raw.encode("utf-8", "replace")) > MAX_RESEARCH_EVIDENCE_BYTES:
        raise ResearchBlocked("research_invalid", "report exceeds size limit")
    start, end = raw.find("{"), raw.rfind("}")
    try:
        payload = json.loads(raw[start:end + 1]) if 0 <= start < end else None
        legacy_fields = {
            "baseline_sha", "contract_digest", "inspected_paths", "findings",
            "unresolved_scope_gaps", "provenance",
        }
        current_fields = legacy_fields | {"recommended_implementation_paths", "no_touch_paths"}
        if not isinstance(payload, dict) or frozenset(payload) not in {frozenset(legacy_fields), frozenset(current_fields)}:
            raise ValueError("invalid research report fields")
        if require_scope and set(payload) != current_fields:
            raise ResearchBlocked("research_scope_missing", "required implementation scope is absent")
        for field in set(payload) - {"baseline_sha", "contract_digest"}:
            if not isinstance(payload[field], list):
                raise TypeError(f"invalid research {field}")
        evidence = ResearchEvidence(
            baseline_sha=payload["baseline_sha"],
            contract_digest=payload["contract_digest"],
            inspected_paths=tuple(payload["inspected_paths"]),
            findings=tuple(ResearchFinding(**item) for item in payload["findings"]),
            unresolved_scope_gaps=tuple(payload["unresolved_scope_gaps"]),
            provenance=tuple(payload["provenance"]),
            brief=build_research_brief(project, bound.contract),
            recommended_implementation_paths=tuple(payload.get("recommended_implementation_paths", ())),
            no_touch_paths=tuple(payload.get("no_touch_paths", ())),
            version=RESEARCH_EVIDENCE_VERSION if set(payload) == current_fields else 1,
        )
        _validate_research_evidence(evidence, project, bound)
        if require_scope and not evidence.recommended_implementation_paths:
            raise ResearchBlocked("research_scope_missing", "recommended implementation paths are required")
    except (ValueError, TypeError, RecursionError) as exc:
        reason = "research_stale" if "stale" in str(exc) else "research_invalid"
        raise ResearchBlocked(reason, str(exc)[:200]) from exc
    if evidence.unresolved_scope_gaps:
        raise ResearchBlocked("research_unresolved_scope", "; ".join(evidence.unresolved_scope_gaps)[:200])
    return evidence


def research_findings_prompt(evidence: ResearchEvidence) -> str:
    """Bounded, advisory findings for the implementation prompt."""
    lines = ["Research findings (advisory, from a read-only researcher; they do not expand the change contract):"]
    lines.extend(f"- {f.path}: {f.summary}" for f in evidence.findings[:MAX_RESEARCH_PROMPT_FINDINGS])
    if evidence.recommended_implementation_paths:
        lines.append("Research-narrowed implementation paths: " + ", ".join(evidence.recommended_implementation_paths))
    if evidence.no_touch_paths:
        lines.append("Research no-touch paths: " + ", ".join(evidence.no_touch_paths))
    text = "\n".join(lines)
    if len(text) > MAX_RESEARCH_PROMPT_CHARS:
        text = text[:MAX_RESEARCH_PROMPT_CHARS] + "\n[research findings truncated]"
    return text + "\n\n"


@dataclass(frozen=True)
class ResearchScopeDecision:
    allowed: bool
    reason: str
    report_digest: str | None = None
    changed_paths: tuple[str, ...] = ()
    outside_recommendation: tuple[str, ...] = ()
    no_touch_violations: tuple[str, ...] = ()


def accepted_research_evidence(
    store: Store, task_id: str, project: Path, bound: BoundChangeContract, *, require_scope: bool = False,
) -> ResearchEvidence | None:
    """Return the newest exact, accepted research receipt; untrusted audit rows fail closed."""
    rows = store.conn.execute(
        "SELECT payload FROM audit_events WHERE event_type='research_stage' "
        "AND json_valid(payload) AND json_extract(payload, '$.task_id')=? "
        "ORDER BY created_at DESC, rowid DESC",
        (task_id,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"])
            if (
                not isinstance(payload, dict)
                or payload.get("outcome") != "accepted"
                or payload.get("baseline_sha") != bound.baseline_sha
                or payload.get("contract_digest") != bound.digest
                or (require_scope and payload.get("scope_enforced") is not True)
            ):
                continue
            report = payload.get("report")
            evidence = _persisted_research_report(report, project, bound)
            if require_scope and (
                evidence.version != RESEARCH_EVIDENCE_VERSION
                or not evidence.recommended_implementation_paths
                or evidence.unresolved_scope_gaps
            ):
                continue
            return evidence
        except (KeyError, TypeError, ValueError, RecursionError):
            continue
    return None


def _persisted_research_report(
    report: Any, project: Path, bound: BoundChangeContract,
) -> ResearchEvidence:
    if not isinstance(report, dict):
        raise TypeError("invalid persisted research report")
    legacy_fields = {
        "baseline_sha", "contract_digest", "inspected_paths", "findings",
        "unresolved_scope_gaps", "provenance", "brief", "version",
    }
    current_fields = legacy_fields | {"recommended_implementation_paths", "no_touch_paths"}
    fields = set(report)
    if fields == legacy_fields and report.get("version") == 1:
        recommended: tuple[str, ...] = ()
        no_touch: tuple[str, ...] = ()
    elif fields == current_fields and report.get("version") == RESEARCH_EVIDENCE_VERSION:
        recommended = tuple(report["recommended_implementation_paths"])
        no_touch = tuple(report["no_touch_paths"])
    else:
        raise ValueError("invalid persisted research report fields")
    evidence = ResearchEvidence(
        baseline_sha=report["baseline_sha"],
        contract_digest=report["contract_digest"],
        inspected_paths=tuple(report["inspected_paths"]),
        findings=tuple(ResearchFinding(**item) for item in report["findings"]),
        unresolved_scope_gaps=tuple(report["unresolved_scope_gaps"]),
        provenance=tuple(report["provenance"]),
        brief=report["brief"],
        recommended_implementation_paths=recommended,
        no_touch_paths=no_touch,
        version=report["version"],
    )
    _validate_research_evidence(evidence, project, bound, require_inspected_files=False)
    return evidence


def research_scope_gate(
    store: Store,
    task_id: str,
    project: Path,
    bound: BoundChangeContract,
    candidate_sha: str,
    *,
    required: bool,
) -> ResearchScopeDecision:
    """Admit an exact candidate only when its paths fit accepted, opt-in research scope."""
    if not required:
        return ResearchScopeDecision(True, "not-required")
    evidence = accepted_research_evidence(store, task_id, project, bound, require_scope=True)
    if evidence is None:
        return ResearchScopeDecision(False, "research_scope_missing")
    try:
        import subprocess

        raw = subprocess.run(
            [
                "git", "diff", "--name-only", "-z", "--no-renames",
                bound.baseline_sha, candidate_sha, "--",
            ],
            cwd=project,
            capture_output=True,
            check=True,
        ).stdout
        changed = tuple(sorted({item.decode("utf-8", "strict") for item in raw.split(b"\0") if item}))
    except (OSError, UnicodeError, subprocess.CalledProcessError):
        return ResearchScopeDecision(False, "research_scope_diff_unavailable")
    recommended = set(evidence.recommended_implementation_paths)
    no_touch = set(evidence.no_touch_paths)
    outside = tuple(path for path in changed if path not in recommended)
    forbidden = tuple(path for path in changed if path in no_touch)
    report_digest = hashlib.sha256(_research_bytes(evidence)).hexdigest()
    if forbidden:
        return ResearchScopeDecision(
            False, "research_no_touch_violation", report_digest, changed, outside, forbidden,
        )
    if outside:
        return ResearchScopeDecision(
            False, "research_scope_drift", report_digest, changed, outside, forbidden,
        )
    return ResearchScopeDecision(True, "accepted", report_digest, changed)


def source_snapshot(root: Path) -> str:
    """Digest tracked, untracked, and relevant ignored workspace content."""
    import os
    import subprocess

    def git(*args: str) -> bytes:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True).stdout

    head = git("rev-parse", "HEAD")
    status = git("status", "--porcelain=v1", "-z", "-uall")
    digest = hashlib.sha256(head + b"\0" + status)
    # HEAD plus status binds unchanged tracked files. Hash only tracked paths whose
    # working/index content differs, all ordinary untracked files, and relevant
    # ignored files so large repositories do not reread every committed blob.
    ordinary = b"".join((
        git("diff", "--name-only", "-z", "--no-ext-diff"),
        git("diff", "--cached", "--name-only", "-z", "--no-ext-diff"),
        git("ls-files", "-z", "--others", "--exclude-standard"),
    ))
    ignored = git("ls-files", "-z", "--others", "--ignored", "--exclude-standard")
    paths: dict[bytes, bool] = {
        raw: False for raw in ordinary.split(b"\0") if raw
    }
    # Lowercase `ls-files -v` tags are assume-unchanged; `S`/`s` are
    # skip-worktree. Both can hide content changes from status and diff, so hash
    # those tracked paths explicitly (including a MISSING marker for sparse files).
    for entry in git("ls-files", "-v", "-z").split(b"\0"):
        if len(entry) > 2 and entry[1:2] == b" ":
            tag, raw = entry[:1], entry[2:]
            if tag == b"S" or tag.islower():
                paths.setdefault(raw, False)
    for raw in ignored.split(b"\0"):
        if raw:
            paths.setdefault(raw, True)
    for raw, is_ignored in sorted(paths.items()):
        relative = raw.decode("utf-8", "surrogateescape")
        if is_ignored and any(part in SNAPSHOT_IGNORED_SKIP_DIRS for part in Path(relative).parts):
            continue
        target = root / relative
        digest.update(b"I" if is_ignored else b"S")
        digest.update(b"\0" + raw + b"\0")
        if target.is_symlink():
            digest.update(b"L\0" + os.fsencode(os.readlink(target)))
            continue
        if not target.is_file():
            digest.update(b"MISSING")
            continue
        digest.update(b"F\0")
        with target.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()
