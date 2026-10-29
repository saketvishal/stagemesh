# Exact-label work scopes

Use one exact, case-insensitive source label to restrict new work. A title or
substring match never qualifies. Normal source readiness, dependency, contract,
security, ownership, validation and independent-review gates still apply.

```powershell
stagemesh run-window --label release-fixes --pr-only --plan --json
stagemesh run-window --label release-fixes --pr-only --duration 30m
stagemesh continue --label release-fixes --json
```

The execution report includes a `scope_id`. Keep it to resume the same scope
after a stop or restart:

```powershell
stagemesh run-window --resume-scope <scope_id> --pr-only
stagemesh continue --resume-scope <scope_id>
stagemesh run-ready --resume-scope <scope_id> --task 123
```

The ID is stored durably in the project's audit database; it is not inferred
from the last few log messages. Window resume commands, task context and child
processes retain it. Targeted `continue --task` also retains that task's recorded
scope. A fresh, untargeted invocation without either option stays unfiltered.
The plan preview uses an in-memory database and does not create a saved scope.

Changing or clearing a saved selector is explicit and audited:

```powershell
stagemesh continue --resume-scope <scope_id> --label next-workstream
stagemesh continue --resume-scope <scope_id> --clear-label
```

These commands create a replacement scope and preserve the original. A change
is refused while any execution or claim is active: pause admissions and allow
stages to finish first. The older scope cannot admit new work after replacement.
An already-produced candidate can finish validation, review and integration
under its recorded grant, including a verified mechanical derivation. Each new
candidate still requires fresh validation and review. Remediation that starts
new implementation must satisfy current scope. Source closure, security gates
and uncertain process ownership still block; changing scope never kills a
worker, releases UNKNOWN ownership or rewrites candidate evidence.

`--task` and `--tasks` cannot bypass the selector. Label scope cannot be combined
with fuzzy `--focus`/`--sprint` or legacy `--once`. The legacy `queue-run` command
supports these scope flags but still requires its existing `--legacy-unsafe`
opt-in; this feature does not qualify that runner for production use.

Before scoped admission, all configured sources must refresh successfully.
GitHub outages, rate limits, malformed responses, missing source items and
unavailable label catalogs refuse admission rather than trusting old labels.
Scope selection never rewrites source configuration or broadens its filters.
Global housekeeping for owners proven dead remains available. No matching or
eligible tasks means zero provider work; dependencies outside the scope are
reported, never automatically admitted or relabelled.

Reports and `run-window --plan --json` include the effective label and scope ID,
matching/eligible/blocked/excluded counts, exclusion reasons, out-of-scope
dependency IDs/status and source freshness. A successful GitHub label catalog
distinguishes `unknown_label` from `known_empty`; local sources without such a
catalog report `no_verified_matches`. Objective roots are reported as
non-executable source items, not treated as an empty workstream.

## Planned children

Bind structured planner output to an operator-selected scope and a verified
GitHub parent before admitting its local children:

```powershell
stagemesh plan objective.json --resume-scope <scope_id> --source-objective 90
```

The parent must be open, carry the selected label and satisfy source exclusions
and dependency restrictions. StageMesh writes the trusted scope/root provenance
and label independently of planner-supplied labels. Child admission rechecks the
current parent authority as well as each child's ordinary gates. Planner JSON
alone cannot grant scope. Unscoped planning retains its existing behavior;
generated scoped children cannot escape through an unfiltered runner.

A confirmed objective root does not need the standard `stagemesh:ready`
implementation label to authorize planning. Ready-plus-objective conflicts remain
invalid, roots remain non-executable, and every other required/excluded source
label still applies. Ordinary implementation issues receive no such exception.

No command above enables a schedule or changes an installed runtime. Use
`--pr-only` to retain manual merge approval; saved label scope conveys no merge,
provider-account or scheduling permission.

Regression coverage: `tests/test_selection_scope.py`,
`tests/test_label_selector_admission.py` and the directly affected window,
source-publication, task-selection and objective-planning tests. These use
controlled sources/providers; they do not certify live-provider autonomy.
