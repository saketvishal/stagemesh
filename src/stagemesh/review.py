from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .contract_binding import contract_for_candidate
from .contracts import ContractError, evaluate_contract
from .domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus
from .persistence import Store
from .remediation import finding_identity


@dataclass(frozen=True)
class ReviewFinding:
    identity: str
    severity: str
    message: str


INFRASTRUCTURE_FAILURE = "INFRASTRUCTURE_FAILURE"
MAX_REVIEW_EXECUTION_RESULT_CHARS = 200

# The strict final verdict vocabulary. "FAIL" is accepted as a legacy alias of FAIL_WITH_FINDINGS and is held to the same rules.
VERDICT_PASS = "PASS"
VERDICT_FAIL_WITH_FINDINGS = "FAIL_WITH_FINDINGS"
VERDICT_REVIEW_INCOMPLETE = "REVIEW_INCOMPLETE"
LEGACY_FAIL = "FAIL"
REVIEW_INCOMPLETE_RETRIES = 2  # extra attempts after an incomplete answer
MAX_RECORDED_REVIEW_RESPONSE_CHARS = 4000

# Progress chatter is not a verdict. _PRELIMINARY finds it anywhere in a short answer; findings are judged only by how they START
# (or by being a bare filler word) so a concrete defect that merely mentions "in progress" is never discarded.
_PRELIMINARY_CORE = (
    r"review\s+(is\s+)?(still\s+)?(in\s+progress|underway|ongoing|pending|started|starting)"
    r"|i\s*(will|'ll|am\s+going\s+to|'m\s+going\s+to)\b|let\s+me\s+(inspect|review|examine|check|look|take|start|begin|see|read|go)\b"
    r"|(starting|beginning|about\s+to)\b.{0,20}\breview|will\s+(now\s+)?(inspect|review|examine|check|look)\b"
    r"|now\s+(inspecting|reviewing|examining|checking)\b|stand\s*by\b|work\s+in\s+progress\b"
)
_PRELIMINARY = re.compile(r"\b(" + _PRELIMINARY_CORE + r")", re.IGNORECASE)
_PRELIMINARY_FINDING = re.compile(
    r"\s*((" + _PRELIMINARY_CORE + r")|(in\s+progress|pending|placeholder|tbd|todo|n/?a|none|unknown)\s*[.!…]*\s*$)", re.IGNORECASE
)
_MAX_PRELIMINARY_CHARS = 200


@dataclass(frozen=True)
class ReviewVerdict:
    """The classified final answer of a reviewer: PASS, FAIL_WITH_FINDINGS (with usable findings) or REVIEW_INCOMPLETE."""

    kind: str
    findings: tuple[dict[str, str], ...] = ()
    reason: str | None = None
    provider_failure: bool = False  # the provider itself reported infrastructure trouble; asking again cannot help


def looks_preliminary(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped) and len(stripped) <= _MAX_PRELIMINARY_CHARS and _PRELIMINARY.search(stripped) is not None


def _placeholder_finding(message: str) -> bool:
    stripped = message.strip()
    return len(stripped) <= _MAX_PRELIMINARY_CHARS and _PRELIMINARY_FINDING.match(stripped) is not None


def _actionable_findings(raw: object) -> tuple[dict[str, str], ...]:
    """Findings that name a concrete problem. Empty, placeholder or non-text entries are dropped, never promoted to defects."""
    if not isinstance(raw, list):
        return ()
    kept: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, dict):
            message, severity, path = item.get("message"), item.get("severity"), item.get("path")
        else:
            message, severity, path = item, None, None
        if not isinstance(message, str) or not message.strip() or _placeholder_finding(message):
            continue
        finding = {
            "message": message.strip(),
            "severity": severity.strip() if isinstance(severity, str) and severity.strip() else "error",
        }
        if isinstance(path, str) and path.strip():
            finding["path"] = path.strip()
        kept.append(finding)
    return tuple(kept)


def _decision_objects(text: str) -> list[dict[str, object]]:
    decoder = json.JSONDecoder()
    found: list[dict[str, object]] = []
    index = 0
    while True:
        index = text.find("{", index)
        if index < 0:
            return found
        try:
            parsed, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            index += 1
            continue
        if isinstance(parsed, dict) and "decision" in parsed:
            found.append(parsed)
        index += end


def _whole_document(text: str) -> bool:
    """True when the entire answer is one JSON document, or one fenced block whose content is itself one JSON document."""
    candidates = [text]
    inner = _fenced_inner(text)
    if inner is not None:
        candidates.append(inner)
    for candidate in candidates:
        try:
            json.loads(candidate)
            return True
        except (TypeError, ValueError):
            continue
    return False


def _fenced_inner(text: str) -> str | None:
    lines = text.splitlines()
    if text.startswith("```") and len(lines) >= 2 and lines[-1].strip().startswith("```"):
        return "\n".join(lines[1:-1]).strip()
    return None


def _buried_in_chatter(text: str, verdict: dict[str, object]) -> bool:
    return bool(_PRELIMINARY.search(text.replace(json.dumps(verdict), "")) or looks_preliminary(text))


def _collect_decisions(text: str, depth: int = 0) -> list[dict[str, object]] | None:
    """Every verdict object in `text` after unwrapping fences and provider envelopes; None when it is JSON but not a verdict object."""
    inner = _fenced_inner(text.strip())
    body = inner if inner is not None else text.strip()
    try:
        document = json.loads(body)
    except (TypeError, ValueError):
        objects = _decision_objects(body)
        if objects and _buried_in_chatter(body, objects[0]):
            return None  # a verdict echoed inside progress text ("I will inspect ... {"decision":"PASS"}") is not an answer
        return objects
    if not isinstance(document, dict):
        return None
    text_field, structured = document.get("text"), document.get("structured_output")
    if isinstance(text_field, str) or isinstance(structured, dict):
        found: list[dict[str, object]] = []
        if isinstance(text_field, str):
            nested = _collect_decisions(text_field, depth + 1) if depth < 3 else None
            if nested is None:
                return None
            found.extend(nested)
        if isinstance(structured, dict):
            if isinstance(text_field, str):
                # Two carriers must agree: the text must hold exactly one verdict and it must be the structured one.
                if len(found) != 1 or json.dumps(found[0], sort_keys=True) != json.dumps(structured, sort_keys=True):
                    return found + [structured] if found else None
                return [structured]
            found.append(structured)
        return found
    return [document] if "decision" in document else []


def _incomplete(reason: str, provider: bool = False) -> ReviewVerdict:
    return ReviewVerdict(VERDICT_REVIEW_INCOMPLETE, reason=reason, provider_failure=provider)


def classify_review_response(response: object) -> ReviewVerdict:
    """Classify a reviewer's answer at the protocol boundary.

    Only a complete verdict counts. PASS needs an unambiguous PASS decision with no findings. FAIL_WITH_FINDINGS needs at least one
    actionable finding. Everything else (empty, preliminary, truncated, malformed, ambiguous, provider error) is REVIEW_INCOMPLETE:
    review infrastructure trouble, never an implementation defect.
    """
    if not isinstance(response, str) or not response.strip():
        return _incomplete("empty_review_output")
    text = response.strip()
    objects = _collect_decisions(text)
    if objects is None:  # a complete JSON document that is not a verdict object (a list, a scalar)
        return _incomplete("malformed_review_output")
    if not objects:
        return _incomplete("preliminary_review_output" if looks_preliminary(text) else "malformed_review_output")
    if len(objects) > 1:  # one final verdict only: conflicting or merely repeated verdict objects are both ambiguous
        return _incomplete("ambiguous_review_output")
    parsed = objects[0]
    decision = parsed.get("decision")
    if not isinstance(decision, str):
        return _incomplete("malformed_review_output")
    # The vocabulary is exact: case or whitespace variants (" pass ", "fail_with_findings") are not verdicts.
    if decision == INFRASTRUCTURE_FAILURE:
        return _incomplete(str(parsed.get("reason") or "review_provider_failure"), provider=True)
    if decision == VERDICT_REVIEW_INCOMPLETE:
        return _incomplete(str(parsed.get("reason") or "review_incomplete"))
    raw_findings = parsed.get("findings")
    if decision == VERDICT_PASS:
        if raw_findings not in (None, []):  # any other findings value, of any shape, contradicts a PASS
            return _incomplete("contradictory_review_verdict")
        return ReviewVerdict(VERDICT_PASS)
    if decision in {VERDICT_FAIL_WITH_FINDINGS, LEGACY_FAIL}:
        findings = _actionable_findings(raw_findings)
        if not findings:
            return _incomplete("fail_without_actionable_findings")
        return ReviewVerdict(VERDICT_FAIL_WITH_FINDINGS, findings=findings)
    return _incomplete("unknown_review_decision")


def parse_review_response(response: str) -> dict[str, object] | None:
    """Parse strict, fenced, provider-wrapped, or prose-wrapped JSON review output."""
    if not isinstance(response, str):
        return None
    text = response.strip()
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        parsed = _extract_json_object(text)
    if isinstance(parsed, dict) and isinstance(parsed.get("text"), str):
        return parse_review_response(parsed["text"])
    if isinstance(parsed, dict) and isinstance(parsed.get("structured_output"), dict):
        return parsed["structured_output"]
    return parsed if isinstance(parsed, dict) else None


def _extract_json_object(text: str) -> dict[str, object] | None:
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2 and lines[-1].strip().startswith("```"):
            fenced = "\n".join(lines[1:-1]).strip()
            try:
                parsed = json.loads(fenced)
            except (TypeError, json.JSONDecodeError):
                parsed = None
            if isinstance(parsed, dict):
                return parsed
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            parsed, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _review_execution_result(reason: str) -> str:
    return reason[:MAX_REVIEW_EXECUTION_RESULT_CHARS]


def _review_exception_reason(exc: BaseException) -> str:
    return _review_execution_result(f"{type(exc).__name__}: {exc}")


def _same_provider(left: str, right: str) -> bool:
    return left.strip().casefold() == right.strip().casefold()


def independent_review_verified(payload: dict[str, object]) -> bool:
    """True only when a distinct provider's review was actually executed (not a label or a fallback)."""
    implementer = payload.get("implementer_provider")
    reviewer = payload.get("review_provider")
    return bool(
        payload.get("independent_reviewer") is True
        and payload.get("review_execution_invoked") is True
        and isinstance(implementer, str)
        and isinstance(reviewer, str)
        and implementer
        and reviewer
        and not _same_provider(implementer, reviewer)
    )


class ReviewAdapter(Protocol):
    name: str

    def review(self, prompt: str) -> str:
        ...


class Reviewer:
    def __init__(
        self,
        fail_capacity: bool = False,
        findings: list[ReviewFinding] | None = None,
        provider_name: str = "builtin-deterministic-fallback",
        adapter: ReviewAdapter | None = None,
        require_independent: bool = False,
        review_pool: object | None = None,
        max_incomplete_retries: int = REVIEW_INCOMPLETE_RETRIES,
    ):
        self.max_incomplete_retries = max(0, max_incomplete_retries)
        self.review_pool = review_pool
        self.require_independent = require_independent
        self.fail_capacity = fail_capacity
        self.findings = findings or []
        self.provider_name = provider_name
        self.adapter = adapter

    def review(self, store: Store, task_id: str, candidate_sha: str, project: Path) -> EvidenceStatus:
        execution_id = store.start_execution(
            task_id=task_id,
            claim_id=None,
            kind=ExecutionKind.REVIEW,
            candidate_sha=candidate_sha,
            actor=self.provider_name,
        )
        reviewer_provider = self.provider_name

        def finish_running(status: ExecutionStatus, *, actor: str | None = None, result: str | None = None) -> None:
            row = store.conn.execute("SELECT status FROM executions WHERE id=?", (execution_id,)).fetchone()
            if row is not None and row["status"] == ExecutionStatus.RUNNING:
                store.finish_execution(
                    execution_id,
                    status,
                    candidate_sha,
                    actor=actor or reviewer_provider,
                    result=_review_execution_result(result) if result else None,
                )

        try:
            if self.fail_capacity:
                store.add_evidence(task_id, candidate_sha, EvidenceKind.REVIEW, EvidenceStatus.CAPACITY, {"provider": "fake"})
                finish_running(ExecutionStatus.FAILED, result="capacity")
                return EvidenceStatus.CAPACITY
            produced = store.conn.execute(
                "SELECT produced_by FROM candidates WHERE task_id=? AND sha=?", (task_id, candidate_sha)
            ).fetchone()
            implementer = str(produced["produced_by"]) if produced is not None else None
            adapter = self.adapter
            considered: list[dict[str, object]] = []
            if self.review_pool is not None:
                # Dynamic selection: first eligible provider that is not the implementer, with automatic fallback.
                adapter, verdicts = self.review_pool.review_adapter(store, task_id, candidate_sha, implementer)
                considered = [v.to_dict() for v in verdicts]
            adapter_name = getattr(adapter, "name", None)
            reviewer_provider = adapter_name or self.provider_name
            same_provider = bool(implementer and _same_provider(reviewer_provider, implementer))
            independent = bool(adapter is not None and reviewer_provider and implementer and not same_provider)
            findings = list(self.findings)
            infrastructure_failure: str | None = None
            review_payload: dict[str, object] = {
                "review_provider": reviewer_provider,
                "implementer_provider": implementer,
                "independent_reviewer": independent,
                "deterministic_contract_gate": True,
                "review_execution_provider": adapter_name,
                "review_execution_invoked": False,
                "independent_review_required": self.require_independent,
            }
            if considered:
                review_payload["review_providers_considered"] = considered
            if same_provider and not self.require_independent:
                findings.append(
                    ReviewFinding(
                        finding_identity(candidate_sha, "review provider must differ from implementer"),
                        "error",
                        "review provider must differ from implementer",
                    )
                )

            try:
                bound = contract_for_candidate(store, task_id, candidate_sha, project)
                contract = bound.contract
                evaluation = evaluate_contract(
                    project,
                    candidate_sha,
                    contract,
                    baseline_sha=bound.baseline_sha,
                    run_gates=False,
                )
                review_payload.update(
                    {
                        **bound.evidence_payload(),
                        "objective": contract.objective,
                        "changed_files": list(evaluation.changed_files),
                        "findings": list(evaluation.findings),
                    }
                )
                findings.extend(
                    ReviewFinding(
                        identity=finding_identity(candidate_sha, item["message"], item.get("path")),
                        severity=str(item.get("severity", "error")),
                        message=str(item["message"]),
                    )
                    for item in evaluation.findings
                )
                if adapter is not None and not findings and not (same_provider and self.require_independent):
                    review_payload["review_execution_invoked"] = True
                    prompt = (
                        f"Review candidate {candidate_sha} for task {task_id} under contract {bound.digest} (version {bound.version}).\n"
                        f"Objective: {contract.objective}\n"
                        "Inspect the candidate, then reply with ONE final JSON verdict and nothing else (no progress text): "
                        "{\"decision\":\"PASS\"}, or "
                        "{\"decision\":\"FAIL_WITH_FINDINGS\",\"findings\":[{\"severity\":\"error\",\"message\":\"concrete defect\","
                        "\"path\":\"file\"}]} with at least one concrete finding, or "
                        "{\"decision\":\"REVIEW_INCOMPLETE\",\"reason\":\"why no verdict was reached\"}."
                    )
                    candidate_review = getattr(adapter, "review_candidate", None)
                    verdict = ReviewVerdict(VERDICT_REVIEW_INCOMPLETE, reason="review_not_attempted")
                    for attempt in range(1 + self.max_incomplete_retries):
                        if callable(candidate_review):
                            response = candidate_review(prompt, project, candidate_sha)
                        else:
                            response = adapter.review(prompt)
                        final_provider = getattr(adapter, "name", reviewer_provider)
                        if final_provider != reviewer_provider:  # a fallback reviewer produced the answer
                            reviewer_provider = final_provider
                            review_payload["review_provider"] = final_provider
                            review_payload["review_execution_provider"] = final_provider
                            same_provider = bool(implementer and _same_provider(final_provider, implementer))
                            independent = bool(final_provider and implementer and not same_provider)
                            review_payload["independent_reviewer"] = independent
                        review_payload["review_response"] = str(response)[:MAX_RECORDED_REVIEW_RESPONSE_CHARS]
                        review_payload["review_attempts"] = attempt + 1
                        verdict = classify_review_response(response)
                        if verdict.kind != VERDICT_REVIEW_INCOMPLETE or verdict.provider_failure:
                            break  # a provider outage was already walked through every eligible reviewer
                    review_payload["review_verdict"] = verdict.kind
                    if self.require_independent and not independent:
                        # The answering provider is (or fell back to) the implementer: its verdict, PASS or FAIL, is void.
                        review_payload["review_verdict"] = VERDICT_REVIEW_INCOMPLETE
                        infrastructure_failure = (
                            "review_provider_same_as_implementer"
                            if same_provider
                            else "implementer_unknown"
                            if not implementer
                            else "independent_review_unavailable"
                        )
                    elif verdict.kind == VERDICT_REVIEW_INCOMPLETE:
                        infrastructure_failure = verdict.reason or "review_incomplete"
                    elif verdict.kind == VERDICT_FAIL_WITH_FINDINGS:
                        for item in verdict.findings:
                            findings.append(
                                ReviewFinding(
                                    finding_identity(candidate_sha, item["message"], item.get("path")), item["severity"], item["message"]
                                )
                            )
            except ContractError as exc:
                findings.append(
                    ReviewFinding(
                        finding_identity(candidate_sha, str(exc)),
                        "error",
                        f"invalid change contract: {exc}",
                    )
                )
                review_payload["findings"] = [{"code": "invalid_contract", "message": str(exc)}]
            except TimeoutError as exc:
                infrastructure_failure = _review_exception_reason(exc) or "review_timeout"
                review_payload["review_exception"] = infrastructure_failure
            except Exception as exc:  # noqa: BLE001 - review infrastructure failures must not strand RUNNING executions.
                infrastructure_failure = _review_exception_reason(exc)
                review_payload["review_exception"] = infrastructure_failure

            if not findings and infrastructure_failure is None and self.require_independent and not independent:
                infrastructure_failure = (
                    "review_provider_same_as_implementer"
                    if same_provider
                    else "implementer_unknown"
                    if not implementer
                    else "independent_review_unavailable"
                )
            if infrastructure_failure is not None and not findings:
                # Not a code defect: no findings, so no implementation remediation; the task stays in REVIEW.
                review_payload["review_infrastructure_failure"] = infrastructure_failure
                providers_text = "; ".join(f"{c['provider']}: {c['reason']}" for c in considered)
                store.add_evidence(task_id, candidate_sha, EvidenceKind.REVIEW, EvidenceStatus.CAPACITY, review_payload)
                store.add_audit_event(
                    "review.infrastructure_failure",
                    {
                        "task_id": task_id,
                        "candidate_sha": candidate_sha,
                        "reason": infrastructure_failure,
                        **({"providers": providers_text} if providers_text else {}),
                    },
                )
                finish_running(ExecutionStatus.FAILED, actor=reviewer_provider, result=infrastructure_failure)
                return EvidenceStatus.CAPACITY
            if findings:
                for finding in findings:
                    identity = finding.identity or finding_identity(candidate_sha, finding.message)
                    store.upsert_finding(identity, task_id, candidate_sha, finding.severity, finding.message)
                store.add_evidence(
                    task_id,
                    candidate_sha,
                    EvidenceKind.REVIEW,
                    EvidenceStatus.FAILED,
                    {**review_payload, "finding_count": len(findings)},
                )
                finish_running(ExecutionStatus.FAILED, actor=reviewer_provider, result="findings")
                return EvidenceStatus.FAILED
            store.add_evidence(task_id, candidate_sha, EvidenceKind.REVIEW, EvidenceStatus.PASSED, review_payload)
            finish_running(ExecutionStatus.SUCCEEDED, actor=reviewer_provider)
            return EvidenceStatus.PASSED
        except BaseException as exc:
            finish_running(ExecutionStatus.FAILED, actor=reviewer_provider, result=_review_exception_reason(exc))
            raise
