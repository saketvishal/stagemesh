# Location-independent launcher for the Build Coordinator CLI.
#
# Resolves the repository root from THIS SCRIPT'S OWN location, never from
# the caller's current directory. Add this folder to PATH once and
# `build-coordinator <command>` then works from any directory: the repo root,
# another worktree, or an unrelated directory.
#
# Canonical workspace paths (control repo, builder/reviewer worktrees,
# coordinator database) come from the coordinator config file -- see
# tooling/build_coordinator/examples/build-coordinator.example.json --
# located via $env:BUILD_COORDINATOR_CONFIG or the default
# ~/.build-coordinator/config.json.
# This launcher only makes the CLI module importable; it does not choose workspace paths itself.
#
# IMPORTANT: `python -m` normally prepends the CURRENT WORKING DIRECTORY to
# sys.path[0]. If the caller's cwd happens to contain its own
# tooling/build_coordinator/ (e.g. another worktree), that stale
# checkout would shadow the intended controller checkout on PYTHONPATH. The
# `-P` flag (Python 3.11+) disables that automatic cwd/script-dir prepend,
# so only the repo root placed on PYTHONPATH below can supply the module --
# cwd can never win.

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path

if ($env:PYTHONPATH) {
    $env:PYTHONPATH = "$RepoRoot;$env:PYTHONPATH"
} else {
    $env:PYTHONPATH = "$RepoRoot"
}

python -P -m build_coordinator.cli @args
exit $LASTEXITCODE
