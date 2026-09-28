from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stagemesh.persistence import Store
from stagemesh.provider_acceptance import run_provider_acceptance


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="stagemesh-provider-acceptance-") as raw:
        project = Path(raw)
        store = Store(project / ".stagemesh" / "state.sqlite3")
        store.migrate()
        try:
            result = run_provider_acceptance(store, project)
        finally:
            store.close()
        print(f"provider_acceptance: {result.status}")
        print(f"chosen_provider: {result.chosen_provider}")
        print(f"capacity_failure_isolated: {result.capacity_failure_isolated}")
        return 0 if result.status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
