"""Fail-closed StageMesh repository quality gate.

This runner is intentionally deterministic and provider-independent. It checks
only the exact Git range supplied by CI or StageMesh validation, records the
policy hash and tool output, and treats missing tools, invalid policy, crashes,
timeouts, and non-zero results as failures.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import os
import subprocess
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stagemesh.quality_policy import QualityPolicyError, load_quality_policy  # noqa: E402


class QualityGateFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class ChangeSet:
    base: str | None
    head: str
    changed: tuple[str, ...]
    added: tuple[str, ...]


def _git(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-1200:]
        raise QualityGateFailure(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def _parse_name_status(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    changed: list[str] = []
    added: list[str] = []
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) < 2:
            raise QualityGateFailure(f"unrecognized git name-status row: {line!r}")
        status = fields[0]
        path = fields[-1].replace("\\", "/")
        changed.append(path)
        if status.startswith("A"):
            added.append(path)
    return tuple(dict.fromkeys(changed)), tuple(dict.fromkeys(added))


def _changes(mode: str, base_arg: str | None, head_arg: str | None) -> ChangeSet:
    if mode == "fast":
        changed, added = _parse_name_status(_git("diff", "--cached", "--name-status", "-M", "--diff-filter=ACMR"))
        return ChangeSet(None, _git("rev-parse", "HEAD"), changed, added)
    base = base_arg or os.environ.get("STAGEMESH_QUALITY_BASE_SHA") or os.environ.get("STAGEMESH_CI_BASE_SHA")
    head = head_arg or os.environ.get("STAGEMESH_QUALITY_HEAD_SHA") or os.environ.get("STAGEMESH_CI_HEAD_SHA") or "HEAD"
    if not base and mode == "commit":
        base = _git("merge-base", "HEAD", "refs/remotes/origin/main", check=False) or _git(
            "rev-parse", "HEAD^"
        )
    if not base or base == "0" * 40:
        raise QualityGateFailure("an exact non-zero base SHA is required for commit-bound quality checks")
    resolved_base = _git("rev-parse", "--verify", f"{base}^{{commit}}")
    resolved_head = _git("rev-parse", "--verify", f"{head}^{{commit}}")
    changed, added = _parse_name_status(
        _git("diff", "--name-status", "-M", "--diff-filter=ACMR", resolved_base, resolved_head)
    )
    return ChangeSet(resolved_base, resolved_head, changed, added)


def _python_files(paths: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(path for path in paths if path.endswith(".py") and (ROOT / path).is_file())


def _source_files(paths: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(path for path in _python_files(paths) if path.startswith(("src/", "scripts/")))


def _type_escape_findings(paths: tuple[str, ...]) -> list[str]:
    findings: list[str] = []
    for relative in _python_files(paths):
        text = (ROOT / relative).read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=relative)
        except SyntaxError as exc:
            findings.append(f"{relative}:{exc.lineno or 1}: syntax error: {exc.msg}")
            continue
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.COMMENT and "# type: ignore" in token.string:
                findings.append(
                    f"{relative}:{token.start[0]}: type: ignore requires an audited exception"
                )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "typing":
                for name in node.names:
                    if name.name in {"Any", "cast"}:
                        findings.append(
                            f"{relative}:{node.lineno}: typing.{name.name} requires an audited boundary adapter"
                        )
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id == "typing" and node.attr in {"Any", "cast"}:
                    findings.append(
                        f"{relative}:{node.lineno}: typing.{node.attr} requires an audited boundary adapter"
                    )
    return findings


def _run(command: list[str], label: str, timeout: int = 300) -> dict[str, object]:
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise QualityGateFailure(f"{label} could not complete: {exc}") from exc
    record: dict[str, object] = {
        "name": label,
        "command": command,
        "returncode": result.returncode,
        "stdout_tail": result.stdout[-4000:],
        "stderr_tail": result.stderr[-4000:],
    }
    if result.returncode != 0:
        detail = (result.stdout + "\n" + result.stderr).strip()[-4000:]
        raise QualityGateFailure(f"{label} failed (exit {result.returncode})\n{detail}")
    return record


def _static_checks(changes: ChangeSet, complexity_max: int) -> list[dict[str, object]]:
    changed = _python_files(changes.changed)
    if not changed:
        return []
    records = [
        _run(
            [
                sys.executable,
                "-m",
                "ruff",
                "check",
                "--select",
                "E4,E7,E9,F,B",
                "--output-format",
                "concise",
                *changed,
            ],
            "ruff-static",
        )
    ]
    added = _python_files(changes.added)
    if added:
        records.append(
            _run(
                [
                    sys.executable,
                    "-m",
                    "ruff",
                    "check",
                    "--select",
                    "C90",
                    "--config",
                    f"lint.mccabe.max-complexity={complexity_max}",
                    "--output-format",
                    "concise",
                    *added,
                ],
                "ruff-added-file-complexity",
            )
        )
    escapes = _type_escape_findings(changes.added)
    if escapes:
        raise QualityGateFailure("new Python code contains unapproved type escape hatches:\n" + "\n".join(escapes))
    return records


def _type_checks(changes: ChangeSet, strict_modules: tuple[str, ...]) -> list[dict[str, object]]:
    targets = tuple(
        dict.fromkeys(
            path
            for path in (*strict_modules, *_source_files(changes.added))
            if (ROOT / path).is_file()
        )
    )
    if not targets:
        raise QualityGateFailure("strict type target set is empty")
    return [
        _run(
            [
                sys.executable,
                "-m",
                "mypy",
                "--strict",
                "--follow-imports=skip",
                "--warn-unreachable",
                "--show-error-codes",
                *targets,
            ],
            "mypy-strict",
        )
    ]


def _dead_code_checks(changes: ChangeSet, confidence: int) -> list[dict[str, object]]:
    targets = _source_files(changes.added)
    if not targets:
        return []
    return [
        _run(
            [sys.executable, "-m", "vulture", *targets, "--min-confidence", str(confidence)],
            "vulture-added-source",
        )
    ]


def run(mode: str, base: str | None, head: str | None) -> dict[str, object]:
    policy = load_quality_policy(ROOT)
    if policy is None:
        raise QualityGateFailure(".stagemesh/quality-policy.json is missing")
    expected_policy = os.environ.get("STAGEMESH_QUALITY_POLICY_SHA256")
    if expected_policy and expected_policy != policy.digest:
        raise QualityGateFailure(
            "quality policy hash differs from the trusted validation plan; refusing stale or modified configuration"
        )
    changes = _changes(mode, base, head)
    records: list[dict[str, object]] = []
    selected = "static" if mode == "fast" else mode
    if selected in {"static", "ci", "commit"}:
        records.extend(_static_checks(changes, policy.complexity_max))
    if selected in {"types", "ci", "commit"}:
        records.extend(_type_checks(changes, policy.strict_modules))
    if selected in {"dead-code", "ci", "commit"}:
        records.extend(_dead_code_checks(changes, policy.dead_code_min_confidence))
    return {
        "schema": "stagemesh.quality-gate/1",
        "status": "PASS",
        "mode": mode,
        "base_sha": changes.base,
        "candidate_sha": changes.head,
        "policy_id": policy.policy_id,
        "policy_hash": policy.digest,
        "changed_files": list(changes.changed),
        "added_files": list(changes.added),
        "commands": records,
        "limits": {
            "complexity_max": policy.complexity_max,
            "dead_code_min_confidence": policy.dead_code_min_confidence,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("fast", "static", "types", "dead-code", "commit", "ci"), required=True)
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        report = run(args.mode, args.base, args.head)
    except (QualityGateFailure, QualityPolicyError) as exc:
        print(f"QUALITY_GATE_FAILED: {exc}", file=sys.stderr)
        return 1
    encoded = json.dumps(report, indent=2, sort_keys=True)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
