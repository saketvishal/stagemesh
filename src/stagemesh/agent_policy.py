"""Provider-neutral agent coding policy loading, prompt injection and checks."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .contracts import ChangeContract
from .git import GitWorkspace

POLICY_PATH = ".stagemesh/agent-policy.json"
POLICY_SCHEMA = "stagemesh.agent-coding-policy/1"
CONSTRAINTS = (
    "no_unrequested_abstractions",
    "no_speculative_code",
    "no_filler_comments",
    "no_unsolicited_documentation_churn",
    "no_policy_evasion",
)
_EXCEPTION_FOR = {
    "allow_unrequested_abstraction": "no_unrequested_abstractions",
    "allow_speculative_code": "no_speculative_code",
    "allow_filler_comments": "no_filler_comments",
    "allow_documentation_churn": "no_unsolicited_documentation_churn",
    "allow_policy_change": "no_policy_evasion",
}
_BUILTIN: dict[str, object] = {
    "schema": POLICY_SCHEMA,
    "policy_id": "stagemesh-built-in-coding-policy",
    "approved_by": "stagemesh-release",
    "constraints": {name: True for name in CONSTRAINTS},
}
_ABSTRACTION_NAME = re.compile(r"(?:^|/)(?:helpers?|utils?|framework|wrappers?|base)(?:[._/-]|$)", re.IGNORECASE)
_ABSTRACTION_CLASS = re.compile(r"^\+\s*class\s+\w*(?:Wrapper|Factory|Manager|Framework)\b")
_SPECULATIVE = re.compile(r"^\+.*\b(?:TODO|FIXME|future[- ]feature|placeholder for future|extension point)\b", re.IGNORECASE)
_FILLER_COMMENT = re.compile(r"^\+\s*(?:#|//)\s*(?:This (?:function|method|code)|Here we|Now we|Simply )", re.IGNORECASE)
_DOC_WORDS = frozenset({"doc", "docs", "documentation", "readme", "guide", "markdown"})


class AgentPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class EffectiveAgentPolicy:
    policy_id: str
    approved_by: str
    constraints: tuple[tuple[str, bool], ...]
    exceptions: tuple[str, ...]
    exception_owner: str | None
    source: str
    digest: str

    def enabled(self, name: str) -> bool:
        return dict(self.constraints)[name]

    def payload(self) -> dict[str, object]:
        return {
            "schema": POLICY_SCHEMA,
            "policy_id": self.policy_id,
            "approved_by": self.approved_by,
            "constraints": dict(self.constraints),
            "exceptions": list(self.exceptions),
            "exception_owner": self.exception_owner,
            "source": self.source,
        }


def load_effective_agent_policy(
    project: Path, contract: ChangeContract, baseline_sha: str | None
) -> EffectiveAgentPolicy:
    raw, source = _policy_at_baseline(project, baseline_sha)
    payload = _validated_payload(raw)
    raw_constraints = payload["constraints"]
    if not isinstance(raw_constraints, dict):
        raise AgentPolicyError("agent policy constraints must be an object")
    constraints = {str(name): bool(value) for name, value in raw_constraints.items()}
    for exception in contract.agent_policy_exceptions:
        constraint = _EXCEPTION_FOR.get(exception)
        if constraint is None:
            raise AgentPolicyError(f"unknown task policy exception: {exception}")
        constraints[constraint] = False
    if contract.agent_policy_exceptions and not contract.agent_policy_owner:
        raise AgentPolicyError("task policy exceptions require an independent owner")
    effective = {
        "schema": POLICY_SCHEMA,
        "policy_id": payload["policy_id"],
        "approved_by": payload["approved_by"],
        "constraints": constraints,
        "exceptions": list(contract.agent_policy_exceptions),
        "exception_owner": contract.agent_policy_owner,
        "source": source,
    }
    canonical = json.dumps(effective, sort_keys=True, separators=(",", ":"))
    return EffectiveAgentPolicy(
        policy_id=str(payload["policy_id"]),
        approved_by=str(payload["approved_by"]),
        constraints=tuple((name, bool(constraints[name])) for name in CONSTRAINTS),
        exceptions=contract.agent_policy_exceptions,
        exception_owner=contract.agent_policy_owner,
        source=source,
        digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def agent_policy_prompt(policy: EffectiveAgentPolicy) -> str:
    enabled = dict(policy.constraints)
    rules = []
    if enabled["no_unrequested_abstractions"]:
        rules.append("Do not add wrappers, helpers, base classes, frameworks, or architectural layers the task does not require.")
    if enabled["no_speculative_code"]:
        rules.append("Do not add future features, hypothetical branches, unused extension points, or behavior outside acceptance criteria.")
    if enabled["no_filler_comments"]:
        rules.append("Do not add comments or docstrings that merely narrate obvious code; keep comments for non-obvious obligations.")
    if enabled["no_unsolicited_documentation_churn"]:
        rules.append("Do not change README or broad documentation unless the task or a changed public contract requires it.")
    if enabled["no_policy_evasion"]:
        rules.append("Do not weaken checks, suppress warnings, add unnecessary casts, or edit policy solely to make a gate pass.")
    lines = [
        "Agent coding policy (authoritative for this task):",
        f"- Policy: {policy.policy_id}",
        f"- Policy hash: {policy.digest}",
        *[f"- {rule}" for rule in rules],
    ]
    if policy.exceptions:
        lines.append(f"- Owner-approved exceptions ({policy.exception_owner}): {', '.join(policy.exceptions)}")
    return "\n".join(lines) + "\n\n"


def agent_policy_findings(
    project: Path,
    baseline_sha: str,
    candidate_sha: str,
    contract: ChangeContract,
    changed_paths: list[str],
    entries: list[dict[str, object]],
) -> tuple[EffectiveAgentPolicy, list[dict[str, str]]]:
    policy = load_effective_agent_policy(project, contract, baseline_sha)
    findings: list[dict[str, str]] = []
    if policy.enabled("no_policy_evasion") and POLICY_PATH in changed_paths:
        findings.append(
            {
                "severity": "error",
                "code": "agent_policy_tampered",
                "path": POLICY_PATH,
                "message": "candidate changes the authoritative agent policy without a frozen owner-approved exception",
            }
        )
    task_text = " ".join((contract.objective, *contract.acceptance_criteria)).casefold()
    task_words = set(re.findall(r"[a-z0-9]+", task_text))
    added_paths: list[str] = []
    for entry in entries:
        paths = entry.get("paths")
        if str(entry.get("status", "")).startswith(("A", "C")) and isinstance(paths, list) and paths:
            added_paths.append(str(paths[-1]))
    diff = GitWorkspace(project).run(
        "diff", "--unified=0", "--no-ext-diff", baseline_sha, candidate_sha, encoding="utf-8"
    ).stdout[:200_000]
    if policy.enabled("no_unrequested_abstractions") and not ({"abstraction", "framework", "wrapper"} & task_words):
        suspicious = [path for path in added_paths if _ABSTRACTION_NAME.search(path)]
        if suspicious or any(_ABSTRACTION_CLASS.search(line) for line in diff.splitlines()):
            findings.append(
                {
                    "severity": "review",
                    "code": "agent_policy_abstraction_review",
                    "path": suspicious[0] if suspicious else "",
                    "message": "candidate adds an abstraction signal not stated in the task; independent review must justify it",
                }
            )
    if policy.enabled("no_speculative_code") and any(_SPECULATIVE.search(line) for line in diff.splitlines()):
        findings.append(
            {
                "severity": "review",
                "code": "agent_policy_speculative_review",
                "path": "",
                "message": "candidate adds a future/speculative marker; independent review must connect it to acceptance criteria",
            }
        )
    if policy.enabled("no_filler_comments") and any(_FILLER_COMMENT.search(line) for line in diff.splitlines()):
        findings.append(
            {
                "severity": "review",
                "code": "agent_policy_comment_review",
                "path": "",
                "message": "candidate adds a likely narration-only comment; independent review must justify or remove it",
            }
        )
    changed_docs = [path for path in changed_paths if path.casefold().endswith((".md", ".mdx", ".rst"))]
    explicit_docs = any("doc" in pattern.casefold() or "readme" in pattern.casefold() for pattern in contract.allowed_files)
    if (
        policy.enabled("no_unsolicited_documentation_churn")
        and changed_docs
        and not explicit_docs
        and not (_DOC_WORDS & task_words)
    ):
        findings.append(
            {
                "severity": "review",
                "code": "agent_policy_documentation_review",
                "path": changed_docs[0],
                "message": "candidate changes documentation not stated in the task; independent review must justify it",
            }
        )
    return policy, findings


def _policy_at_baseline(project: Path, baseline_sha: str | None) -> tuple[dict[str, object], str]:
    if baseline_sha:
        result = GitWorkspace(project).run("show", f"{baseline_sha}:{POLICY_PATH}", check=False, encoding="utf-8")
        if result.returncode == 0:
            try:
                parsed = json.loads(result.stdout)
                if not isinstance(parsed, dict):
                    raise AgentPolicyError("baseline agent policy must be a JSON object")
                return {str(key): value for key, value in parsed.items()}, f"baseline:{baseline_sha}:{POLICY_PATH}"
            except ValueError as exc:
                raise AgentPolicyError(f"baseline agent policy is not valid JSON: {exc}") from exc
    return dict(_BUILTIN), "built-in"


def _validated_payload(payload: dict[str, object]) -> dict[str, object]:
    if not isinstance(payload, dict) or payload.get("schema") != POLICY_SCHEMA:
        raise AgentPolicyError(f"agent policy schema must be {POLICY_SCHEMA}")
    policy_id = payload.get("policy_id")
    approved_by = payload.get("approved_by")
    constraints = payload.get("constraints")
    if not isinstance(policy_id, str) or not policy_id.strip():
        raise AgentPolicyError("agent policy_id must be a non-empty string")
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise AgentPolicyError("agent approved_by must be a non-empty string")
    if not isinstance(constraints, dict):
        raise AgentPolicyError("agent policy constraints must be an object")
    unknown = sorted(set(constraints) - set(CONSTRAINTS))
    missing = sorted(set(CONSTRAINTS) - set(constraints))
    if unknown or missing:
        raise AgentPolicyError("agent policy constraints mismatch; unknown=" + ",".join(unknown) + " missing=" + ",".join(missing))
    if any(type(constraints[name]) is not bool for name in CONSTRAINTS):
        raise AgentPolicyError("agent policy constraint values must be booleans")
    return payload
