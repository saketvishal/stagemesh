"""Diagnosis before remediation: why does this task keep failing, and is another implementation attempt worth spending?

Every failed VALIDATION, REVIEW or INTEGRATION evidence row is turned into a Failure (stage, finding codes, failing gates, the
messages and the output lines that matter). Failures are compared across candidates; when the newest one is the same as the one(s)
before it, the failure repeats and a further attempt will almost certainly fail the same way. Each failure is classified:

* contract_scope     - the change contract forbids or does not cover what the task needs (or its size limits are too small)
* validation_gate    - a gate cannot run or is misconfigured (missing tool, timeout, no executable gate): not a code problem
* review_finding     - the independent reviewer keeps raising the same concern
* provider_no_progress - the provider produced nothing, or the same tree again
* implementation_defect - a gate runs and fails on the code (tests, lint, types)
* integration_conflict - the integration ref moved and the candidate no longer lands cleanly

Nothing here calls a provider unless a `DiagnosisPolicy.analyst` is supplied; that optional pass is a separate, read-only provider
run that sees the facts above, never the implementation worktree.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .domain import EvidenceKind
from .git import GitError, GitWorkspace
from .persistence import Store

CONTRACT_SCOPE = "contract_scope"
VALIDATION_GATE = "validation_gate"
REVIEW_FINDING = "review_finding"
PROVIDER_NO_PROGRESS = "provider_no_progress"
IMPLEMENTATION_DEFECT = "implementation_defect"
INTEGRATION_CONFLICT = "integration_conflict"
CATEGORIES = (CONTRACT_SCOPE, VALIDATION_GATE, REVIEW_FINDING, PROVIDER_NO_PROGRESS, IMPLEMENTATION_DEFECT, INTEGRATION_CONFLICT)

DIAGNOSIS_EVENT = "task.diagnosis"
DIAGNOSIS_STOP_EVENT = "task.diagnosis_stop"
DISPATCH_MODES = ("never", "on_repeat", "every_failure")

_SCOPE_CODES = {
    "outside_allowed_files", "forbidden_file_changed", "excluded_file_changed", "protected_file_changed",
    "change_size_files_exceeded", "change_size_lines_exceeded", "public_api_changed", "empty_diff", "candidate_noise_file",
    "dependency_manifest_changed_without_gate",
}
_GATE_SETUP_CODES = {
    "planned_check_missing_command", "acceptance_criteria_without_executable_gate", "candidate_unavailable",
    "candidate_workspace_unavailable", "missing_required_bound_evidence", "invalid_contract",
}
_INTEGRATION_CODES = {
    "integration_non_fast_forward", "integration_stale_base", "integration_rebase_conflict", "integration_ref_missing",
    "integration_ref_update_failed", "integration_ref_not_updated",
}
_ENVIRONMENT_HINTS = (
    "not found", "no such file", "is not recognized", "cannot find", "timed out", "timeout", "permission denied",
    "command not found", "executable file", "winerror 2",
)
_STAGE_OF_KIND = {EvidenceKind.VALIDATION: "VALIDATE", EvidenceKind.REVIEW: "REVIEW", EvidenceKind.INTEGRATION: "INTEGRATE"}
_KEY_LINE = re.compile(r"(fail|error|assert|exception|traceback|expected|denied|not found)", re.IGNORECASE)
_SHA = re.compile(r"\b[0-9a-f]{7,40}\b")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_PATHISH = re.compile(r"(?:[A-Za-z]:)?[\\/][\w.\-\\/]*(?:tmp|temp|pytest-of-[\w]+|stagemesh-[\w\-]+)[\w.\-\\/]*", re.IGNORECASE)
_SPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Strip what changes between otherwise identical failures: shas, numbers (timings, counts), temp paths."""
    text = _PATHISH.sub("<path>", text)
    text = _SHA.sub("<sha>", text)
    text = _NUMBER.sub("#", text)
    return _SPACE.sub(" ", text).strip().casefold()


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


@dataclass(frozen=True)
class Failure:
    stage: str
    candidate_sha: str
    created_at: float
    category: str
    codes: tuple[str, ...]
    gates: tuple[str, ...]
    messages: tuple[str, ...]
    tokens: frozenset[str]
    excerpt: str
    paths: tuple[str, ...] = ()

    def same_as(self, other: Failure) -> bool:
        if self.stage != other.stage or self.codes != other.codes or self.gates != other.gates:
            return False
        return _jaccard(self.tokens, other.tokens) >= (0.4 if self.stage == "REVIEW" else 0.5)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "candidate_sha": self.candidate_sha,
            "category": self.category,
            "codes": list(self.codes),
            "failed_gates": list(self.gates),
            "messages": [m[:400] for m in self.messages[:6]],
            "excerpt": self.excerpt,
            **({"paths": list(self.paths[:20])} if self.paths else {}),
        }


@dataclass
class Diagnosis:
    task_id: str
    category: str
    stage: str
    repeated: bool
    repeat_count: int
    threshold: int
    candidate_sha: str | None
    summary: str
    recommendation: str
    failing_evidence: list[dict[str, Any]] = field(default_factory=list)
    comparison: list[dict[str, Any]] = field(default_factory=list)
    categories: dict[str, int] = field(default_factory=dict)
    no_progress: dict[str, Any] | None = None
    provider_analysis: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "category": self.category,
            "stage": self.stage,
            "repeated": self.repeated,
            "repeat_count": self.repeat_count,
            "threshold": self.threshold,
            "candidate_sha": self.candidate_sha,
            "summary": self.summary,
            "recommendation": self.recommendation,
            "failing_evidence": self.failing_evidence,
            "comparison": self.comparison,
            "categories": self.categories,
            "no_progress": self.no_progress,
            "provider_analysis": self.provider_analysis,
        }

    def audit_payload(self) -> dict[str, Any]:
        """The compact form stored in the audit log (full evidence stays in the evidence table)."""
        analysis = (self.provider_analysis or {}).get("text")
        return {
            "task_id": self.task_id,
            "candidate_sha": self.candidate_sha,
            "category": self.category,
            "stage": self.stage,
            "repeated": self.repeated,
            "repeat_count": self.repeat_count,
            "summary": self.summary[:600],
            "recommendation": self.recommendation[:900],
            "failed_gates": sorted({g for f in self.failing_evidence[-1:] for g in f.get("failed_gates", [])}),
            "codes": sorted({c for f in self.failing_evidence[-1:] for c in f.get("codes", [])}),
            "compared_candidates": [c["candidate_sha"][:12] for c in self.comparison],
            "provider": (self.provider_analysis or {}).get("provider"),
            "provider_analysis": str(analysis)[:1500] if analysis else None,
        }

    def format_lines(self) -> list[str]:
        lines = [
            f"diagnosis: {self.category} at {self.stage}" + (f" (repeated {self.repeat_count}x)" if self.repeated else ""),
            f"  {self.summary}",
        ]
        if len(self.comparison) > 1:
            lines.append(
                "  candidates: "
                + "; ".join(
                    f"{c['candidate_sha'][:10]} {c['stage']} {c['category']}" + (" (same as previous)" if c["same_as_previous"] else "")
                    for c in self.comparison
                )
            )
        lines.append(f"  next step: {self.recommendation}")
        if self.provider_analysis and self.provider_analysis.get("text"):
            lines.append(f"  provider analysis ({self.provider_analysis.get('provider')}): {str(self.provider_analysis['text'])[:600]}")
        return lines


@dataclass
class DiagnosisPolicy:
    repeat_threshold: int = 2  # identical failures (newest first) that count as "the same failure repeats"
    stop_on_repeat: bool = True  # block the task with the diagnosis instead of spending another implementation attempt
    dispatch: str = "every_failure"  # when an analyst is configured: never | on_repeat | every_failure
    analyst: Callable[[Diagnosis, str], dict[str, Any] | None] | None = None  # optional separate, read-only provider pass

    def __post_init__(self) -> None:
        if isinstance(self.repeat_threshold, bool) or not isinstance(self.repeat_threshold, int) or self.repeat_threshold < 2:
            raise ValueError("diagnosis repeat_threshold must be an integer >= 2")
        if self.dispatch not in DISPATCH_MODES:
            raise ValueError(f"diagnosis dispatch must be one of: {', '.join(DISPATCH_MODES)}")


def _loads(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _gate_output(finding: dict[str, Any]) -> str:
    return f"{finding.get('stdout') or ''}\n{finding.get('stderr') or ''}"


def _key_lines(text: str, limit: int = 12) -> list[str]:
    lines = [normalize(line) for line in text.splitlines() if line.strip() and _KEY_LINE.search(line)]
    return list(dict.fromkeys(lines))[:limit]


def _classify(stage: str, codes: set[str], gate_findings: list[dict[str, Any]]) -> str:
    if stage == EvidenceKind.INTEGRATION and codes & _INTEGRATION_CODES and not codes & _SCOPE_CODES:
        return INTEGRATION_CONFLICT
    if codes & _SCOPE_CODES:
        return CONTRACT_SCOPE
    if codes & _GATE_SETUP_CODES:
        return VALIDATION_GATE
    if stage == EvidenceKind.INTEGRATION:
        return INTEGRATION_CONFLICT
    if "gate_failed" in codes:
        for finding in gate_findings:
            output = _gate_output(finding).casefold()
            if finding.get("returncode") in (None, 126, 127) or any(hint in output for hint in _ENVIRONMENT_HINTS):
                return VALIDATION_GATE
        return IMPLEMENTATION_DEFECT
    if stage == EvidenceKind.REVIEW:
        return REVIEW_FINDING
    return IMPLEMENTATION_DEFECT


def _last_reset(store: Store, task_id: str) -> float:
    """When an operator last gave the task a fresh budget (retry-task); failures before that are history, not a repeat."""
    for row in store.conn.execute("SELECT payload, created_at FROM audit_events WHERE event_type='task.unblocked' ORDER BY created_at DESC, rowid DESC LIMIT 50"):
        if _loads(row["payload"]).get("task_id") == task_id:
            return float(row["created_at"])
    return 0.0


def _failures(store: Store, task_id: str) -> list[Failure]:
    rows = store.conn.execute(
        "SELECT candidate_sha, kind, payload, created_at FROM evidence WHERE task_id=? AND status='FAILED' "
        "AND kind IN (?, ?, ?) AND created_at>? ORDER BY created_at, rowid",
        (task_id, EvidenceKind.VALIDATION, EvidenceKind.REVIEW, EvidenceKind.INTEGRATION, _last_reset(store, task_id)),
    ).fetchall()
    failures: list[Failure] = []
    for row in rows:
        payload = _loads(row["payload"])
        kind = str(row["kind"])
        stage = _STAGE_OF_KIND[kind]
        sha = str(row["candidate_sha"])
        findings = [f for f in payload.get("findings", []) if isinstance(f, dict)]
        codes = {str(f["code"]) for f in findings if f.get("code")}
        gate_findings = [f for f in findings if f.get("code") == "gate_failed"]
        table = [str(r["message"]) for r in store.conn.execute("SELECT message FROM findings WHERE task_id=? AND candidate_sha=?", (task_id, sha))]
        messages = list(dict.fromkeys([str(f.get("message", "")) for f in findings if f.get("message")] + table))
        gates = tuple(sorted({str(g.get("name")) for g in payload.get("gates", []) if isinstance(g, dict) and g.get("status") not in (None, "PASSED")}))
        if not gates:
            gates = tuple(sorted({str(f.get("message", "")).removesuffix(" failed") for f in gate_findings}))
        output_lines: list[str] = []
        for finding in gate_findings:
            output_lines += _key_lines(_gate_output(finding))
        if kind == EvidenceKind.REVIEW:
            tokens = frozenset(w for m in messages for w in re.findall(r"[a-z_]{4,}", normalize(m)))
        else:
            tokens = frozenset(output_lines) or frozenset(normalize(m) for m in messages)
        excerpt = "\n".join(
            line for finding in gate_findings[:2] for line in _gate_output(finding).strip().splitlines()[-6:]
        )[-700:]
        paths = tuple(dict.fromkeys(str(f["path"]) for f in findings if f.get("path")))
        failures.append(
            Failure(stage, sha, float(row["created_at"]), _classify(kind, codes, gate_findings), tuple(sorted(codes)), gates, tuple(messages), tokens, excerpt, paths)
        )
    return failures


def _no_progress(store: Store, task_id: str, since: float, project: Path | None, threshold: int) -> dict[str, Any] | None:
    """Implementation attempts after the last failed evidence that produced no candidate, or the same tree again."""
    rows = store.conn.execute(
        "SELECT event_type, payload, created_at FROM audit_events WHERE created_at>=? AND event_type IN (?, ?, ?) ORDER BY created_at, rowid",
        (since, "task.implementation_unsuccessful", "task.capacity_failure", "candidate.produced"),
    ).fetchall()
    attempts: list[str] = []
    for row in rows:
        payload = _loads(row["payload"])
        if payload.get("task_id") != task_id:
            continue
        if row["event_type"] == "candidate.produced":
            attempts.clear()
        else:
            attempts.append(str(payload.get("reason") or payload.get("result_status") or "implementation_failed"))
    identical = _identical_trees(store, task_id, project)
    if not attempts and identical is None:
        return None
    repeated = bool(identical) or (len(attempts) >= threshold and len(set(attempts[-threshold:])) == 1)
    return {"attempts": len(attempts), "reasons": attempts[-5:], "identical_trees": identical, "repeated": repeated}


def _identical_trees(store: Store, task_id: str, project: Path | None) -> list[str] | None:
    if project is None:
        return None
    shas = [str(r["sha"]) for r in store.conn.execute("SELECT sha FROM candidates WHERE task_id=? ORDER BY created_at DESC, rowid DESC LIMIT 2", (task_id,))]
    if len(shas) < 2:
        return None
    git = GitWorkspace(project)
    try:
        trees = [git.run("rev-parse", f"{sha}^{{tree}}").stdout.strip() for sha in shas]
    except (GitError, OSError):
        return None
    return [s[:12] for s in shas] if trees[0] == trees[1] else None


_RECOMMENDATIONS = {
    CONTRACT_SCOPE: (
        "The contract, not the code, is what stops this task{paths}. Widen allowed_files / size limits (or remove the "
        "forbidden/protected entry) in .stagemesh/contracts/{task}.json if the change is legitimate, or split the task; then "
        "`stagemesh retry-task --task {task}`. Another implementation attempt will hit the same wall."
    ),
    VALIDATION_GATE: (
        "A validation gate cannot run or is misconfigured ({gates}); that is an environment or contract problem, not a code "
        "defect. Fix the gate command/tooling or the contract's required_tests, then `stagemesh retry-task --task {task}`."
    ),
    REVIEW_FINDING: (
        "The independent reviewer keeps raising the same concern. Read the finding below and either address it yourself, correct "
        "the task's acceptance criteria if the reviewer is wrong, or add the missing guidance to the task, then "
        "`stagemesh retry-task --task {task}`."
    ),
    IMPLEMENTATION_DEFECT: (
        "Gate {gates} fails on the code the same way after each attempt. Read the output excerpt, fix or clarify the "
        "requirement (a failing test may be wrong), then `stagemesh retry-task --task {task}`."
    ),
    PROVIDER_NO_PROGRESS: (
        "The provider is not producing a usable change (nothing committed, or the same tree again). Check provider "
        "availability/credentials and that the task text is actionable, try a different provider (`--provider`), then retry."
    ),
    INTEGRATION_CONFLICT: (
        "The integration ref moved and the candidate no longer lands cleanly. Rebase or re-implement on the current "
        "{ref_hint}, then `stagemesh retry-task --task {task}`."
    ),
}


def diagnose(store: Store, task_id: str, project: Path | None = None, threshold: int = 2) -> Diagnosis | None:
    """The diagnosis for a task's failures so far, or None when it has no failed evidence and no no-progress attempts."""
    failures = _failures(store, task_id)
    since = failures[-1].created_at if failures else _last_reset(store, task_id)
    no_progress = _no_progress(store, task_id, since, project, threshold)
    if not failures and no_progress is None:
        return None
    comparison: list[dict[str, Any]] = []
    previous_by_stage: dict[str, Failure] = {}
    for failure in failures:
        before = previous_by_stage.get(failure.stage)
        comparison.append(
            {
                "candidate_sha": failure.candidate_sha,
                "stage": failure.stage,
                "category": failure.category,
                "failed_gates": list(failure.gates),
                "codes": list(failure.codes),
                "same_as_previous": bool(before and failure.same_as(before)),
            }
        )
        previous_by_stage[failure.stage] = failure
    categories: dict[str, int] = {}
    for failure in failures:
        categories[failure.category] = categories.get(failure.category, 0) + 1

    latest = failures[-1] if failures else None
    repeat_count = 0
    if latest is not None:
        repeat_count = 1
        for earlier in reversed([f for f in failures[:-1] if f.stage == latest.stage]):
            if not latest.same_as(earlier):
                break
            repeat_count += 1
    stalled = no_progress is not None and (latest is None or no_progress["attempts"] > 0 or no_progress["identical_trees"])
    if stalled and no_progress is not None and (no_progress["repeated"] or latest is None):
        category = PROVIDER_NO_PROGRESS
        stage = "IMPLEMENT"
        repeated = bool(no_progress["repeated"])
        repeat_count = max(repeat_count, threshold if repeated else 1)
        if no_progress["identical_trees"]:
            summary = f"the last two candidates ({', '.join(no_progress['identical_trees'])}) have identical trees: the provider changed nothing between attempts"
        else:
            summary = f"{no_progress['attempts']} implementation attempt(s) since the last failed check produced no candidate ({', '.join(dict.fromkeys(no_progress['reasons']))})"
    else:
        assert latest is not None
        category, stage = latest.category, latest.stage
        repeated = repeat_count >= threshold
        what = ", ".join(latest.gates) or ", ".join(latest.codes) or "reviewer findings"
        summary = f"{stage} failed on candidate {latest.candidate_sha[:10]}: {what}"
        if repeated:
            summary += f"; the same failure occurred on {repeat_count} consecutive candidates"
        elif len(failures) > 1:
            summary += f"; {len(failures)} failed checks so far, not identical to the previous one"
        if latest.messages:
            summary += f". First finding: {latest.messages[0][:240]}"
    focus = latest
    template = _RECOMMENDATIONS[category]
    recommendation = template.format(
        task=task_id,
        gates=", ".join(focus.gates) if focus and focus.gates else "the failing gate",
        paths=(f" ({', '.join(focus.paths[:5])})" if focus and focus.paths else ""),
        ref_hint="integration ref",
    )
    if category == REVIEW_FINDING and focus and focus.messages:
        recommendation += " Finding: " + focus.messages[0][:300]
    return Diagnosis(
        task_id=task_id,
        category=category,
        stage=stage,
        repeated=repeated,
        repeat_count=repeat_count,
        threshold=threshold,
        candidate_sha=latest.candidate_sha if latest else None,
        summary=summary,
        recommendation=recommendation,
        failing_evidence=[f.to_dict() for f in failures[-max(threshold, 3):]],
        comparison=comparison[-6:],
        categories=categories,
        no_progress=no_progress,
    )


def analysis_prompt(diagnosis: Diagnosis) -> str:
    facts = json.dumps(
        {k: v for k, v in diagnosis.to_dict().items() if k not in {"provider_analysis"}}, indent=2, sort_keys=True
    )[:6000]
    return (
        "You are a diagnostic reviewer. Do NOT modify any file. Below are the recorded facts about repeated failures of an "
        "automated coding task. In the checked-out candidate, find the root cause.\n\n"
        f"{facts}\n\n"
        "Answer with one JSON object only: "
        '{"root_cause": "...", "category": "contract_scope|validation_gate|review_finding|provider_no_progress|implementation_defect|integration_conflict", '
        '"next_step": "the single most useful action for the operator"}.'
    )


def parse_analysis(response: str, provider: str) -> dict[str, Any] | None:
    """The analyst's answer, or None when the provider failed (infrastructure/capacity) or answered nothing usable."""
    parsed = _loads(response)
    if parsed.get("decision"):  # review-style error envelopes: INFRASTRUCTURE_FAILURE / a mutation FAIL
        return None
    if not response.strip():
        return None
    if parsed:
        text = "; ".join(str(parsed[k]) for k in ("root_cause", "next_step") if parsed.get(k)) or response.strip()
        return {"provider": provider, "text": text[:1500], "category": parsed.get("category")}
    return {"provider": provider, "text": response.strip()[:1500], "category": None}


def make_adapter_analyst(adapter: Any, project: Path) -> Callable[[Diagnosis, str], dict[str, Any] | None]:
    """An analyst backed by a provider adapter's read-only candidate run (the same mechanism independent review uses)."""

    def analyst(diagnosis: Diagnosis, candidate_sha: str) -> dict[str, Any] | None:
        return parse_analysis(adapter.review_candidate(analysis_prompt(diagnosis), project, candidate_sha), adapter.name)

    return analyst
