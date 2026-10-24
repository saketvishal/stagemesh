# ruff: noqa: PLW1510, BLE001, B023, I001, RUF100
"""Instrumented hard-timeout run: records what kill_process_tree and the identity classification actually return."""
import json
import sys
import tempfile
import time
from pathlib import Path

from stagemesh import continuous_driver as cd

out = Path(sys.argv[1])
out.parent.mkdir(parents=True, exist_ok=True)
log: list[dict] = []
t0 = time.monotonic()


def wrap(module, name):
    original = getattr(module, name)

    def inner(*args, **kwargs):
        result = original(*args, **kwargs)
        log.append({"t": round(time.monotonic() - t0, 2), "call": name, "result": repr(result)[:300]})
        return result

    setattr(module, name, inner)


for name in ("kill_process_tree", "classify_process", "process_identity", "clear_child_owner", "popen_identity", "save_child_owner"):
    if hasattr(cd, name):
        wrap(cd, name)

project = Path(tempfile.mkdtemp(prefix="sm-timeout-"))
(project / ".stagemesh").mkdir()
marker = project / "child.pid"
script = project / "tree.py"
script.write_text(
    "import pathlib, subprocess, sys, time\n"
    "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "pathlib.Path(sys.argv[1]).write_text(str(c.pid))\n"
    "time.sleep(60)\n",
    encoding="utf-8",
)
code = cd._default_runner([sys.executable, str(script), str(marker)], project, 6)
result = {
    "python": sys.executable,
    "return_code": code,
    "marker_exists": marker.is_file(),
    "owner_file_exists_after": cd.child_owner_path(project).exists(),
    "owner_file": cd.child_owner_path(project).read_text(encoding="utf-8")[:400] if cd.child_owner_path(project).exists() else None,
    "calls": log,
}
out.write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result, indent=2))
