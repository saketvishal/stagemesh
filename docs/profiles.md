# Project profiles

A project profile tells StageMesh how to plan and validate work in a repository whose tests cannot be guessed from root files
(monorepos, mixed stacks). It lives at `<project>/.stagemesh/profile.json`, is provider neutral (it names gates, file scopes
and labels, never agents), and is used wherever StageMesh needs a contract and none exists:

* `continue` / `run-ready` auto-planning writes `.stagemesh/contracts/<task>.json` from the profile instead of probing for
  `npm test` / `pytest` at the project root;
* task selection treats a contract-less task as selectable only if the profile can plan it;
* `task_selection` in the profile supplies label defaults when `config.json` has no `task_selection` of its own.

`stagemesh profile` validates the profile; `stagemesh profile --task <id>` shows (read-only) which task type a task resolves to
and the gates, scope and limits its contract would get. A hand-written contract in `.stagemesh/contracts/` always wins.

## Shape

```jsonc
{
  "schema_version": 1,
  "name": "myproject",
  "variables": { "PYTHON": { "win32": "C:\\proj\\.venv\\Scripts\\python.exe", "default": "python3" }, "BASE_REF": "main" },
  "env_sets": { "test_db": { "DATABASE_URL": "postgresql://.../proj_test" } },   // explicit, per-gate environments
  "forbidden_files": [".env", "**/.env", "data/**"],                             // applied to every generated contract
  "defaults": { "max_changed_files": 40, "max_diff_lines": 4000 },
  "gates": {
    "boundaries": { "name": "docs-static-boundaries", "command": ["${PYTHON}", "-m", "pytest", "-q", "tests/x.py"],
                     "cwd": "apps/api", "env": "test_db", "timeout_seconds": 300 }
  },
  "task_types": {
    "prep": { "labels": ["stagemesh:prep"], "keywords": ["readiness"], "allowed_files": ["docs/**"],
              "validation_level": "light", "gates": ["boundaries"] }
  },
  "type_selection": { "default_type": "full", "escalation_type": "full", "escalate_at_product_types": 2 },
  "task_selection": { "preferred_labels": ["stagemesh:prep"], "excluded_labels": ["stagemesh:blocked"] }
}
```

* `${VAR}` expands from `variables` (a value may be per-platform: `win32` / `posix` / `default`). `STAGEMESH_PROFILE_<VAR>`
  overrides a variable, so machine-specific paths do not need editing the file.
* Gates are argv lists run without a shell. `cwd` must stay inside the checkout; `env` names an `env_sets` entry and applies
  to that gate only. Executables are resolved on `PATH` (so `npm` finds `npm.cmd` on Windows).
* `validation_level` maps to the validation tier: `light` = docs-only, `standard` = localized code, `full` = core/schema/security
  (broad checks run). Validation planning only runs some gates per tier, so the profile is **rejected at load time** unless:
  light gate names contain `docs`, `static`, `lint` or `spell`; standard gates are not "broad"; a full type has at least one
  broad gate (a name containing `acceptance`, or a pytest run with no narrow target). Every type must also produce a contract
  that parses and fits the 10,000-character canonical limit.

## Choosing a task type

1. A label of the escalation type (for example `validation:full`) forces it.
2. Otherwise the first of these that matches anything decides: task labels, keywords in the issue **title**, keywords in the
   description. Keywords match whole words, case-insensitively.
3. One matching *product* type (any type that is not `light`) wins over prep types; two or more matching product types
   (`escalate_at_product_types`) escalate to the escalation type; only prep matches select the prep type.
4. No match at all selects `default_type`. Use `full` there: when StageMesh cannot tell what a task touches it runs the most
   validation.

The decision and its evidence are written into the contract (`profile.selection`) and returned in `--json` as
`auto_plan.profile`.

## Safety defaults

The profile's global `forbidden_files` and each type's `allowed_files`, `forbidden_files`, `protected_files`, size limits and
gates become the contract. Forbid secrets, env files, local databases and WAL/SHM files, private or uploaded user data, caches,
StageMesh runtime state and user/tool state globally; a path matching a forbidden pattern fails validation even if it is also
allowed. Remember that `**/x` does not match a root-level `x`, so list both forms.

## Compatibility smoke (`stagemesh project-smoke`)

Any project with a profile can prove the installed StageMesh can operate on it safely, without running an implementation:

```
stagemesh --project <repo> project-smoke [--task <id> ...] [--dry-run-selection] [--json]
```

It checks that the profile loads; that every task type generates a contract that parses within the size limit; that every
validation gate is an argv list (no shell interpreters, no metacharacters in the program, relative `cwd`, bounded timeout, no
unexpanded variables; a program missing from `PATH` is only a warning); that forbidden-file patterns exist and are well formed;
that declared smoke probes select their expected type; and that every task type is reachable by its labels or keywords.
`--task` shows the type (with evidence), gates, scope and limits a real task would get and parses any hand-written contract.
`--dry-run-selection` ranks the discovered tasks and previews auto-planning. Tasks are synced into a throwaway database, so
nothing in the project is written. Exit code 0 means no check failed (warnings allowed); 1 means a check failed.

A profile may declare its own probes, which need no real issue:

```json
"smoke": { "tasks": [ { "title": "Update the runbook", "labels": ["docs"], "expect_type": "prep" } ] }
```
