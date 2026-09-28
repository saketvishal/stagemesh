from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stagemesh.github_acceptance import run_github_acceptance
from stagemesh.persistence import Store


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="stagemesh-github-acceptance-") as raw:
        store = Store(Path(raw) / "state.sqlite3")
        store.migrate()
        try:
            result = run_github_acceptance(store)
        finally:
            store.close()
    print(f"github_acceptance: {result.status}")
    print(f"discovered: {result.discovered}")
    print(f"deferred_skipped: {result.deferred_skipped}")
    print(f"outbound_status: {result.outbound_status}")
    print(f"rate_limit_status: {result.rate_limit_status}")
    return 0 if result.status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
