# Examples

This directory contains configuration examples for the Build Coordinator.

All examples use generic placeholder paths. Replace `/path/to/your-repo`,
`/path/to/worktree-a`, etc. with real paths on your machine.

> **Note on agent CLIs**: The coordinator is provider-neutral. The `command`
> field in each worker definition is an operator-controlled trusted list.
> Replace `["codex", "exec", "--json"]` or any other CLI shown here with
> whatever agent executable you have installed.

---

## Examples

| Directory | Description |
|---|---|
| [single_agent/](single_agent/) | One agent handles all roles (simplest setup) |
| [staged_multi_agent/](staged_multi_agent/) | Separate agents for building and reviewing |
| [recovery_checkpoint/](recovery_checkpoint/) | How checkpoint/resume works |
| [validation_review_model/](validation_review_model/) | Independent review and result validation |

## Other files

- `build-coordinator.example.json` — coordinator config file template
- `runner-config.example.json` — legacy JSON runner config (still supported)
- `stdin_print_cli_wrapper.py` — adapter for agent CLIs that read from stdin

---

## Result-file contract

Workers write a single JSON object to the path at `BUILD_COORDINATOR_RESULT_PATH`.

### Common fields

```json
{
  "schema_version": 1,
  "execution_id": "<BUILD_COORDINATOR_EXECUTION_ID>",
  "task_id": "<BUILD_COORDINATOR_TASK_ID>",
  "role": "<BUILD_COORDINATOR_ROLE>",
  "status": "SUCCEEDED",
  "completed_at": "2024-01-01T12:00:00Z"
}
```

### BUILDER / REMEDIATION additional fields

```json
{
  "feature_sha": "abc123",
  "files_changed": ["src/main.py", "tests/test_main.py"],
  "tests": {"items": 10, "passed": 10, "failed": 0},
  "scope_expansion_required": false,
  "blockers": [],
  "commits_created": ["abc123"]
}
```

### REVIEWER additional fields

```json
{
  "reviewed_feature_sha": "abc123",
  "verdict": "GREEN",
  "findings": [],
  "required_remediation": [],
  "architecture_notes": [],
  "ready_for_integration": true
}
```

Valid verdicts: `GREEN`, `GREEN_WITH_NOTES`, `REMEDIATION_REQUIRED`.
`GREEN` and `GREEN_WITH_NOTES` are integration-eligible only when
`required_remediation` is empty and `ready_for_integration` is true.

### INTEGRATION additional fields

```json
{
  "feature_sha": "abc123",
  "reviewed_feature_sha": "abc123",
  "current_main_sha": "def456",
  "merge_base": "ghi789",
  "merge_commit_sha": "jkl012",
  "tests": {"items": 10, "passed": 10, "failed": 0},
  "push_status": "PUSHED",
  "final_main_sha": "jkl012"
}
```

## Exit-code semantics

| Exit code | Result file present? | Outcome |
|---|---|---|
| 0 | Yes, valid SUCCEEDED | Success |
| 0 | No result file | `COORDINATOR_INVARIANT_FAILURE` (fail closed) |
| 0 | Yes, but non-SUCCEEDED | `COORDINATOR_INVARIANT_FAILURE` (fail closed) |
| Non-zero | No result file | Process `FAILED` (task not auto-failed) |
| Non-zero | Yes, valid SUCCEEDED | `COORDINATOR_INVARIANT_FAILURE` (fail closed) |