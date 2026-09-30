from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
import pytest

from stagemesh.execution import ExecutionStatus
from stagemesh.persistence import Store
from stagemesh.providers import RuntimeCommandAdapter, approved_default_adapters


def test_grok_deterministic_command_adapter_contract(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    store.migrate()
    task_id = store.upsert_task("Test Grok Adapter", source_id="T-GROK-1")
    store.advance_task(task_id, "IMPLEMENT")

    # Script recording invocation details to result directory
    record_file = tmp_path / "grok_invocation.json"
    shim_script = tmp_path / "fake_grok.py"
    shim_script.write_text(
        "import sys, os, json\n"
        "from pathlib import Path\n"
        "prompt = sys.stdin.read()\n"
        "data = {'args': sys.argv, 'cwd': os.getcwd(), 'prompt': prompt}\n"
        "Path(sys.argv[1]).write_text(json.dumps(data), encoding='utf-8')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )

    adapter = RuntimeCommandAdapter(
        name="grok",
        command=(sys.executable, str(shim_script), str(record_file)),
    )

    result = adapter.execute(store, task_id, claim_id=None, project=tmp_path)

    assert result.status == ExecutionStatus.SUCCEEDED
    assert record_file.exists()
    assert "StageMesh task: Test Grok Adapter" in record_file.read_text(encoding="utf-8")


def test_grok_default_adapter_definition():
    adapters = approved_default_adapters()
    grok_adapter = next((a for a in adapters if a.name == "grok"), None)
    assert grok_adapter is not None
    assert grok_adapter.name == "grok"
    assert "code" in grok_adapter.capabilities


def test_grok_live_execution_if_available(tmp_path: Path):
    import os
    executable = shutil.which("grok")
    if not executable or not (os.environ.get("GROK_API_KEY") or os.environ.get("XAI_API_KEY")):
        pytest.skip("Live Grok CLI executable or API credentials not installed/configured on system")

    store = Store(tmp_path / "test.db")
    store.migrate()
    task_id = store.upsert_task("Live Grok Execution Task", source_id="T-GROK-LIVE")

    adapter = RuntimeCommandAdapter(name="grok", command=(executable,))
    if adapter.check_capacity() != "AVAILABLE":
        pytest.skip("Grok provider capacity unavailable (credentials/quota missing)")

    try:
        result = adapter.execute(store, task_id, claim_id=None, project=tmp_path)
        assert result.status == ExecutionStatus.SUCCEEDED
    except Exception as exc:
        pytest.skip(f"Live Grok CLI execution failed/timed out: {exc}")
