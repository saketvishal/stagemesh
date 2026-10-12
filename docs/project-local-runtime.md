# StageMesh runtime: independently installed

StageMesh is an installed tool, not a checkout a project points at. The runtime is a normal (non-editable) package in a standalone
environment that belongs to no project:

| Platform | Runtime |
| --- | --- |
| Windows | `%LOCALAPPDATA%\StageMesh
untimeenv` |
| POSIX | `$XDG_DATA_HOME/stagemesh/runtime/venv` (default `~/.local/share/stagemesh/runtime/venv`) |

`STAGEMESH_RUNTIME_HOME` overrides the location. A host project (for example Caventra) keeps only its own state in `.stagemesh/` and a
shim in `.stagemesh/bin` that runs the installed `stagemesh`. The shim clears `PYTHONPATH`/`PYTHONHOME`, so nothing can substitute a
source checkout. Deleting, moving or switching branches in the StageMesh checkout does not affect any project.

## Install or update

From the StageMesh checkout (the only place the source is needed):

```bash
python scripts/bootstrap_stagemesh.py --project <path-to-host-project>
```

This builds and installs the checkout as a package into the standalone runtime (a re-run is the update), writes the project shim and the
user-level `stagemesh` dispatcher, and never edits the host project's source. Other projects keep working during and after an update and
pick up the new version immediately because they share the runtime. `--providers` and `--no-queue-config` behave as before.

`--editable` keeps the old development layout (an editable install in `<project>/.stagemesh/tooling/venv`). `stagemesh doctor` reports
that as `development (editable/source checkout)`; a production project must not use it.

## Which runtime am I using?

```bash
stagemesh --version     # version, package path, editable?, source checkout?, build provenance (commit, dirty flag)
stagemesh doctor        # same block, plus `editable/development status: installed`
```

A production configuration reports `editable install: no` and `source checkout: no`. Provenance (`built from: <commit>`) is baked into the
wheel at build time.

After bootstrap, run StageMesh from the project folder with the normal command:

```bash
stagemesh continue
```

The dispatcher walks upward from the current directory and invokes the nearest project-owned `.stagemesh/bin/stagemesh` or `.stagemesh\bin\stagemesh.cmd`, so different repositories can use different local StageMesh versions.

Acceptance gates must use this installed runtime path. They must not depend on `PYTHONPATH=src`, an editable install, a developer checkout outside the project, or a stale `code_checkout` value.

## Stale runtime detection

`stagemesh doctor` warns when the repository source or configured package version declares a different StageMesh version than the installed project-local package, for example after pulling newer source. The warning names the fix: rerun `python scripts/bootstrap_stagemesh.py`, which reinstalls the package and fails loudly if the installed version still differs from the source. No warning appears when the versions match, when the project is a normal host project with its own `pyproject.toml`, or when the project has no StageMesh source tree.

