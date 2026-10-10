from __future__ import annotations

import contextlib
import io
import json
import shlex
import sys
from pathlib import Path

import stagemesh.cli as cli_module


def test_provider_smoke_runs_selected_provider_in_temporary_fixture(tmp_path: Path) -> None:
    project = tmp_path / "project"
    runtime = project / ".stagemesh"
    runtime.mkdir(parents=True)
    provider = tmp_path / "provider.py"
    provider.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "sys.stdin.read()\n"
        "Path('provider-smoke.txt').write_text('after\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    command = shlex.join([sys.executable, str(provider)])
    (runtime / "config.json").write_text(
        json.dumps({"providers": {"smoke": {"command": command, "capabilities": ["IMPLEMENT"]}}}),
        encoding="utf-8",
    )
    out = io.StringIO()

    with contextlib.redirect_stdout(out):
        code = cli_module.main(["--project", str(project), "provider-smoke", "--provider", "smoke", "--json"])

    data = json.loads(out.getvalue())
    assert code == 0
    assert data["status"] == "PASS"
    assert data["provider"] == "smoke"
    assert data["execution_status"] == "SUCCEEDED"
    assert data["file_ok"] is True
