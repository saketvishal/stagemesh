# Project-local StageMesh runtime

StageMesh is owned by the project that runs it. A project does not need a global editable checkout, a global `PYTHONPATH`, or another repository's `code_checkout` to use the StageMesh runtime for that project.

Run the bootstrap from the repository root:

```bash
python scripts/bootstrap_stagemesh.py
```

The bootstrap creates `.stagemesh/tooling/venv`, installs the current StageMesh package into that project-owned environment as a normal package, writes a project shim in `.stagemesh/bin`, and installs a tiny user-level dispatcher named `stagemesh` on `PATH`.

After bootstrap, run StageMesh from the project folder with the normal command:

```bash
stagemesh continue
```

The dispatcher walks upward from the current directory and invokes the nearest project-owned `.stagemesh/bin/stagemesh` or `.stagemesh\bin\stagemesh.cmd`, so different repositories can use different local StageMesh versions.

Acceptance gates must use this installed runtime path. They must not depend on `PYTHONPATH=src`, an editable install, a developer checkout outside the project, or a stale `code_checkout` value.

## Stale runtime detection

`stagemesh doctor` warns when the repository source declares a different StageMesh version than the installed project-local package, for example after pulling newer source. The warning names the fix: rerun `python scripts/bootstrap_stagemesh.py`, which reinstalls the package and fails loudly if the installed version still differs from the source. No warning appears when the versions match or the project has no StageMesh source tree.
