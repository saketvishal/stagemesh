from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stagemesh.config import load_config
from stagemesh.domain import ExecutionKind, ExecutionStatus
from stagemesh.github import GitHubClient, UrlLibGitHubTransport
from stagemesh.persistence import Store
from stagemesh.providers import ProviderValidationError, adapters_from_config


def provider_execution_status(root: Path, provider: str) -> str:
    db = root / ".stagemesh" / "stagemesh.sqlite3"
    if not db.is_file():
        return "NOT_PROVEN"
    store = Store(db)
    try:
        row = store.conn.execute(
            """
            SELECT 1
            FROM executions
            WHERE lower(actor)=lower(?)
              AND kind IN (?, ?)
              AND status IN (?, ?)
            ORDER BY updated_at DESC, rowid DESC
            LIMIT 1
            """,
            (
                provider,
                ExecutionKind.IMPLEMENTATION,
                ExecutionKind.REVIEW,
                ExecutionStatus.SUCCEEDED,
                ExecutionStatus.FAILED,
            ),
        ).fetchone()
    finally:
        store.close()
    return "PROVEN" if row else "NOT_PROVEN"


def live_acceptance_checks(root: Path = ROOT) -> list[dict[str, str]]:
    config = load_config(root)
    checks: list[dict[str, str]] = []
    if config.github.configured:
        client = GitHubClient(
            config.github.owner or "",
            config.github.repo or "",
            UrlLibGitHubTransport(config.github.token),
        )
        result = client.list_open_issues()
        checks.append({"name": "github", "status": result.status})
        if result.status not in {"OK", "UNKNOWN", "STALE"}:
            raise AssertionError(result)
        checks.append({"name": "github:sync", "status": "NOT_PROVEN"})
    else:
        checks.append({"name": "github", "status": "NOT_CONFIGURED"})
        checks.append({"name": "github:sync", "status": "NOT_CONFIGURED"})
    try:
        adapters = adapters_from_config(config)
    except ProviderValidationError:
        checks.append({"name": "provider:config", "status": "INVALID"})
        return checks
    for adapter in adapters:
        checks.append({"name": f"provider:{adapter.name}", "status": adapter.check_capacity()})
        checks.append(
            {
                "name": f"provider:{adapter.name}:execution",
                "status": provider_execution_status(root, adapter.name),
            }
        )
    return checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    checks = live_acceptance_checks()
    status = "FAIL" if any(check["status"] == "INVALID" for check in checks) else "PASS"
    if args.json:
        print(json.dumps({"status": status, "checks": checks}, indent=2, sort_keys=True))
        return 0 if status == "PASS" else 1
    print("\n".join(f"{check['name']}: {check['status']}" for check in checks))
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
