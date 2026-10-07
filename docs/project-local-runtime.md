# Project-local StageMesh runtime

StageMesh is owned by the project that runs it. A project does not need a global editable checkout, a global `PYTHONPATH`, or another repository's `code_checkout` to use the StageMesh runtime for that project.

Run the bootstrap from the repository root:

```bash
python scripts/bootstrap_stagemesh.py
```

The bootstrap creates `.stagemesh/tooling/venv`, installs the current StageMesh package into that project-owned environment as a normal package, and writes a shim in `.stagemesh/bin`.

After bootstrap, use the project-owned shim:

```bash
./.stagemesh/bin/stagemesh continue
```

On Windows:

```powershell
.stagemesh\bin\stagemesh.cmd continue
```

Acceptance gates must use this installed runtime path. They must not depend on `PYTHONPATH=src`, an editable install, a developer checkout outside the project, or a stale `code_checkout` value.
