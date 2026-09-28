from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stagemesh.config import load_config
from stagemesh.github import GitHubClient, UrlLibGitHubTransport
from stagemesh.providers import approved_default_adapters


def main() -> int:
    config = load_config(ROOT)
    checks: list[str] = []
    if config.github.configured:
        client = GitHubClient(
            config.github.owner or "",
            config.github.repo or "",
            UrlLibGitHubTransport(config.github.token),
        )
        result = client.list_open_issues()
        checks.append(f"github: {result.status}")
        if result.status not in {"OK", "UNKNOWN", "STALE"}:
            raise AssertionError(result)
    else:
        checks.append("github: NOT_CONFIGURED")
    for adapter in approved_default_adapters():
        checks.append(f"provider:{adapter.name}: {adapter.check_capacity()}")
    print("\n".join(checks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
