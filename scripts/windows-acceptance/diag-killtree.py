# ruff: noqa: PLW1510, BLE001, B023, I001, RUF100
"""Windows diagnostic: why can a hard timeout leave a grandchild alive? Records the real process tree.

Writes JSON evidence to the directory in argv[1]. Starts only its own sleeping test processes and kills only them.
"""
import json, os, subprocess, sys, time
from pathlib import Path

out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from stagemesh.execution import popen_session_kwargs, kill_process_tree  # noqa: E402

def table():
    ps = ("Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CreationDate"
          " | ConvertTo-Json -Compress")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=60)
    return {p["ProcessId"]: p for p in json.loads(r.stdout)}

def descendants(tbl, root):
    kids = {}
    for p in tbl.values():
        kids.setdefault(p["ParentProcessId"], []).append(p["ProcessId"])
    seen, stack = [], [root]
    while stack:
        pid = stack.pop()
        for k in kids.get(pid, []):
            seen.append(k); stack.append(k)
    return seen

TREE = ("import pathlib, subprocess, sys, time\n"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "pathlib.Path(sys.argv[1]).write_text(str(c.pid))\n"
        "time.sleep(120)\n")

def experiment(name, exe, strategy):
    work = out / name; work.mkdir(exist_ok=True)
    marker = work / "grandchild.pid"; script = work / "tree.py"; script.write_text(TREE)
    wrapper = [exe, "-m", "stagemesh.driver_child", str(work), exe, str(script), str(marker)]
    env = dict(os.environ); env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    proc = subprocess.Popen(wrapper, cwd=work, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            **popen_session_kwargs())
    deadline = time.monotonic() + 60
    while not (marker.is_file() and marker.read_text().strip()):
        if time.monotonic() > deadline or proc.poll() is not None:
            return {"name": name, "error": "tree never ready"}
        time.sleep(0.05)
    gc = int(marker.read_text())
    before = table(); tree_before = descendants(before, proc.pid)
    rec = {"name": name, "exe": exe, "wrapper_pid": proc.pid, "grandchild_pid": gc,
           "tree_before": [(p, before[p]["Name"], before[p]["ParentProcessId"]) for p in tree_before if p in before],
           "grandchild_in_tree_before": gc in tree_before}
    t0 = time.monotonic()
    if strategy == "kill_process_tree":
        rec["kill_returned"] = kill_process_tree(proc)
    else:
        r = subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, text=True)
        rec["taskkill"] = {"rc": r.returncode, "out": r.stdout[-400:], "err": r.stderr[-400:]}
    rec["kill_seconds"] = round(time.monotonic() - t0, 3)
    time.sleep(2)
    after = table()
    rec["survivors"] = [(p, after[p]["Name"], after[p]["ParentProcessId"]) for p in tree_before + [proc.pid] if p in after]
    rec["grandchild_alive_after_2s"] = gc in after
    for p in rec["survivors"]:  # clean up only processes this experiment created
        subprocess.run(["taskkill", "/F", "/PID", str(p[0])], capture_output=True)
    return rec

results = {"python": sys.executable, "version": sys.version, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "experiments": []}
base = Path(sys.base_prefix) / "python.exe"
for name, exe in (("venv_python", sys.executable), ("base_python", str(base))):
    for strategy in ("kill_process_tree", "taskkill_verbose"):
        try:
            results["experiments"].append(experiment(f"{name}-{strategy}", exe, strategy))
        except Exception as exc:  # record, never crash the run
            results["experiments"].append({"name": f"{name}-{strategy}", "error": repr(exc)})
(out / "result.json").write_text(json.dumps(results, indent=2))
print(json.dumps(results, indent=2))
