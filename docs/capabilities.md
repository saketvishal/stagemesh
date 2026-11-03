# Project capability registry

StageMesh separates seven states that are easy to conflate:

1. **Declared**: an entry exists in `.stagemesh/capabilities.json`.
2. **Installed**: its project-relative source exists inside the isolated task checkout.
3. **Available**: the source and required runtime are installed, configuration is valid, and StageMesh has an adapter for the provider.
4. **Enabled**: the entry has `"enabled": true`.
5. **Attached**: a compatible adapter delivered it to a particular provider invocation.
6. **Loaded**: machine-readable provider evidence shows that the resource was read before the first recorded mutation.
7. **Applied**: independent behavioral evidence supports that the skill's required discipline was followed. StageMesh does not currently report this state.

`stagemesh capabilities --json` reports declared, resource-installed, runtime-installed, configuration-valid, available, enabled,
and usable states. Add `--task TASK_ID` to project that task's durable attachment/load/invocation receipts without creating or
mutating the runtime database. It never downloads or installs a capability. StageMesh has three registry adapters:
read-only project `SKILL.md` attachment, skills-only import from a project-local Claude Code plugin, and an explicitly requested,
read-only Codex stdio MCP adapter. Executable Claude plugin components and MCP requests for Claude, Grok, Agy, or custom providers
remain inventory-only and fail closed.

The registry may set `"skill_evidence_mode": "observed-load-required"`. In that opt-in mode, StageMesh enables the selected provider's
JSON event stream and accepts an implementation result or review verdict only when every selected skill for that stage was observed
being read before the first recorded mutation. Review JSONL output is projected through an allowlisted final-answer parser; an unknown
event shape cannot be treated as a verdict. The default is `attachment-required`, which proves verified prompt attachment but not
provider loading.

## Registry and opt-in

```json
{
  "format_version": 1,
  "skill_evidence_mode": "attachment-required",
  "capabilities": [
    {
      "id": "domain-review",
      "kind": "skill",
      "source": "agent-skills/domain-review/SKILL.md",
      "content_sha256": "<lowercase SHA-256 of the reviewed SKILL.md>",
      "enabled": true,
      "providers": ["codex", "claude", "grok", "agy"],
      "stages": ["IMPLEMENT", "REVIEW"],
      "permissions": ["workspace-read"]
    },
    {
      "id": "serena",
      "kind": "mcp",
      "source": ".stagemesh/mcp/serena.json",
      "enabled": false,
      "providers": ["codex"],
      "stages": ["IMPLEMENT"],
      "permissions": ["workspace-read", "subprocess"]
    },
    {
      "id": "review-kit",
      "kind": "claude-plugin",
      "source": ".claude/plugins/review-kit",
      "enabled": false,
      "providers": ["claude"],
      "stages": ["REVIEW"],
      "permissions": ["workspace-read"]
    }
  ]
}
```

Registration is not task authorization. A change contract must opt in by capability id:

```json
{
  "objective": "Update domain mapping",
  "capability_refs": ["domain-review"],
  "allowed_files": ["src/domain.py"]
}
```

The registry can be managed without hand-editing JSON. Registration accepts only an already-present, project-local source and defaults
to disabled. It never downloads a package, activates a plugin, starts an MCP server, invokes a provider, or reads ambient user-home
configuration. Preview the normalized entry and projected availability first:

For a directory containing several vendored skills, such as a reviewed copy of additional Superpowers skills, discovery shows candidates
without registering or trusting them:

```powershell
stagemesh --project C:\path\to\project capabilities discover --root agent-skills --json
stagemesh --project C:\path\to\project capabilities discover --root agent-skills `
  --registration-plan writing-plans --provider codex --provider claude `
  --stage IMPLEMENT --stage REVIEW --json
```

Discovery scans only the explicit project-relative root, never follows symlinks, and is bounded by depth, entry, candidate, and skill-size
limits. It recognizes standalone `SKILL.md` files, skills-only Claude plugin packages, and StageMesh MCP manifests using the production
validators. Invalid candidates remain invalid; discovery and registration plans perform no writes, provider/network calls, subprocess
launches, package installs, database creation, or user-home lookup. The plan requires explicit providers and lifecycle stages and still
defaults the candidate to disabled. Discovery never applies its plan; actual registration remains the separate `capabilities register`
operation below.

```powershell
stagemesh --project C:\path\to\project capabilities register domain-review `
  --kind skill `
  --source agent-skills/domain-review/SKILL.md `
  --provider codex `
  --provider claude `
  --stage IMPLEMENT `
  --stage REVIEW `
  --plan --json
```

Remove `--plan` to atomically write the disabled declaration. Add `--enable` only when registration itself should explicitly enable an
available capability. Existing ids are rejected unless the complete replacement is supplied with `--update`; unrelated entries and
`skill_evidence_mode` are preserved. Kind-specific least-privilege permissions are used unless `--permission` is supplied: skills and
skills-only Claude plugins receive `workspace-read`, while the bounded MCP adapter requires `workspace-read` plus `subprocess`.

New registration also pins the validated source content. Existing legacy declarations without a pin remain explicit as
`integrity=unpinned`; they are not reported as integrity-verified. Preview or apply a deliberate pin/refresh with:

```powershell
stagemesh --project C:\path\to\project capabilities pin domain-review --plan --json
stagemesh --project C:\path\to\project capabilities pin domain-review
```

The pin uses exact `SKILL.md` bytes, the deterministic aggregate of accepted plugin skill components, or exact MCP manifest bytes. A
matching pin reports `verified`; a changed source reports `drifted`, is not available/usable, and is rejected before provider or MCP
launch. Pin refresh is never automatic: the operator must review the changed source and run the explicit command. Audit reports compare
attachment/load/invocation evidence digests with current content, so historical evidence for older bytes cannot masquerade as current
use. Pins and digests are safe bounded identity metadata; file contents and absolute paths are not persisted.

Enable and disable remain separate explicit registry operations and also support read-only plans:

```powershell
stagemesh --project C:\path\to\project capabilities enable domain-review --plan --json
stagemesh --project C:\path\to\project capabilities enable domain-review
stagemesh --project C:\path\to\project capabilities disable domain-review
```

Enable fails unless the source, configuration, runtime command, declared provider, and StageMesh adapter are available. Disable still
works if a formerly registered source or executable has disappeared. Neither command edits a task contract, attaches the capability,
creates the runtime database, or records an invocation; task-level use still requires `capability_refs` and observable execution
evidence below.

Task opt-in can also be planned and applied without hand-editing the contract:

```powershell
stagemesh --project C:\path\to\project capabilities contract `
  --task 123 --add domain-review --plan --json
stagemesh --project C:\path\to\project capabilities contract `
  --task 123 --add domain-review
stagemesh --project C:\path\to\project capabilities preflight --task 123 --json
```

The plan reuses the production contract parser and registry validation, reports the exact project contract and proposed
`capability_refs`, and writes nothing. Applying uses an atomic project-local write, preserves unrelated supported contract fields, and
does not change a frozen runtime binding. When the task already has a frozen contract, the result explicitly reports
`rebind_required: true`; the separate audited `rebind-contract --plan` workflow remains the only way to replace that binding. Removing
a selected capability remains possible after its source or runtime disappears, and a semantic no-op does not rewrite the file.

The preflight is read-only. It applies the same stage filtering, provider compatibility, adapter validation, executable availability,
and independent-provider requirement used by dispatch, then reports `ready`, `partial`, or `blocked` with bounded reasons. It never
invokes a provider or MCP server, creates a database, installs a package, or records usage. A successful preflight establishes only that
a route is currently available; actual use still requires later `capability.loaded` or `capability.invoked` evidence as applicable.

The first runtime contract binding also freezes a deterministic selected-capability policy. The bounded snapshot contains only the
selected ids and their normalized kind, project-relative source, provider/stage allowlists, permissions, enabled state, and source pin;
it contains no skill/plugin contents, absolute paths, prompts, credentials, MCP arguments/results, or provider output. Implementation
and review compare the current selected declarations with that snapshot before provider launch. A relevant change, missing selected id,
corrupt binding, or legacy selected-capability task without a snapshot fails closed and reports `rebind_required`; changing an unrelated
registry entry does not block the task. An explicit audited `rebind-contract` refreshes the contract and selected policy atomically.
Preflight, task-doctor, and run-window report `not-required`, `verified`, `drifted`, `legacy-unbound`, or `invalid` with bounded frozen and
current digests. These read-only reports do not create or modify the runtime database.

The skill-load evidence policy is also an explicit project operation. Preview it before opting in:

```powershell
stagemesh --project C:\path\to\project capabilities policy observed-load-required --plan --json
stagemesh --project C:\path\to\project capabilities policy observed-load-required
```

The plan performs no writes, provider calls, network calls, package installs, or database writes. Applying a changed policy atomically
updates only `.stagemesh/capabilities.json`, preserving declarations; applying the current policy is a true no-op. The default effective
policy is `attachment-required`, including when no registry exists. Task-doctor and run-window reports include the effective policy next
to requested and observed capability counts, while an invalid or unsafe registry is reported as unavailable rather than trusted.

At each lifecycle stage StageMesh selects only requested capabilities whose registry entry includes that stage. An
implementation-only capability therefore does not break review, and a review-only capability does not grant implementation authority.
Dispatch requires all of the following for the active stage: the id is registered, enabled, installed, compatible with the selected
provider, enabled for the stage, and supported by a StageMesh adapter. Sources must resolve inside the task checkout; symlink and `..`
escapes are rejected.
A skill may request only `workspace-read`. The first MCP adapter requires exactly `workspace-read` and `subprocess`, and accepts only a
validated stdio manifest with no environment values, credentials, HTTP transport, setup command, or ambient user MCP configuration.
Capability attachment never expands `allowed_files`, provider authorization, or the worker's sandbox.

This is also how a project opts into additional Superpowers skills without expanding StageMesh's small pinned built-in pack: vendor the
reviewed `SKILL.md` inside the project, register it as `kind: skill`, declare compatible providers and stages, then reference its id
from the change contract. Registration alone does not enable or attach it.

## Skills-only Claude plugin import

A `claude-plugin` source names a project-local plugin directory. StageMesh accepts the standard
`.claude-plugin/plugin.json` metadata file and one or more `skills/<name>/SKILL.md` components. It validates path containment and bounded
skill counts and sizes, then lists the skill files in the applicable Claude implementation or review prompt through the same read-only
adapter used for standalone skills.

StageMesh does **not** pass `--plugin-dir` and does not install, enable, or execute the plugin as a Claude Code plugin. Manifests that
declare commands, agents, hooks, MCP servers, LSP servers, dependencies, settings, user configuration, experimental components, or
other non-metadata fields fail closed. Standard runtime component paths such as `agents/`, `commands/`, `hooks/`, `.mcp.json`, and
`.lsp.json`, `workflows/`, `monitors/`, `bin/`, and `settings.json` also fail closed. This is a compatibility import for
plugin-packaged skills, not general plugin runtime support.

With `observed-load-required`, one exact `SKILL.md` read from the requested plugin is required before the first recorded mutation. The
redacted `capability.loaded` receipt identifies the plugin and component name without persisting an absolute path or file contents.

## Codex stdio MCP manifest

An MCP capability's `source` is a StageMesh manifest, not a user-home client configuration:

```json
{
  "format_version": 1,
  "transport": "stdio",
  "server_name": "serena",
  "command": "serena",
  "args": [
    "start-mcp-server",
    "--project-from-cwd",
    "--context=codex",
    "--open-web-dashboard=false"
  ],
  "cwd": ".",
  "allowed_tools": [
    "find_symbol",
    "find_referencing_symbols",
    "get_symbols_overview",
    "search_for_pattern"
  ],
  "startup_timeout_seconds": 15,
  "tool_timeout_seconds": 60
}
```

The command must already be installed on the StageMesh host. StageMesh does not run `uvx`, package installation, or a network fetch to
obtain it. For each invocation StageMesh adds Codex `--ignore-user-config`, a required server definition, and an exact
`enabled_tools` allowlist. No project or user Codex configuration is modified. The example follows Serena's current installed-command
guidance while disabling its optional web dashboard.

The tool allowlist limits what Codex can discover and call, but StageMesh does not yet provide an OS-level sandbox for the MCP server
process itself. `subprocess` therefore means the operator trusts the installed server executable with the host account's process
authority. Do not enable an unreviewed server. Process-level filesystem/network confinement remains an explicit security gap.

## Audit meaning

Registry-backed standalone skills and skills-only Claude plugin imports may be scoped to `IMPLEMENT`, `REVIEW`, or both. The Codex MCP
adapter remains implementation-stage only.

Before a provider starts, StageMesh writes one `capability.attached` event per bundled, project-referenced, automatically selected, or
registry-selected skill. The event includes provider, stage, origin, project-relative source, content SHA-256, declared permission,
selection reason, `status: attached`, and `invocation: unverified`.

When a selected provider is incompatible with a contract-requested capability, StageMesh records
`capability.provider_skipped` with `provider_invoked: false` and continues through the implementation pool. It does not cool down or
penalize the provider for a compatibility mismatch.

That is deliberately not an invocation claim. Default skill transport proves that the prompt contained a readable path; it does not
prove the model opened or followed the file. The Codex MCP adapter uses its structured event stream for execution binding, allowlist
enforcement, argument/result redaction, and verified invocation receipts. Self-reported prose is not sufficient. Executable plugin
and other MCP adapters still need equivalent evidence.

With `observed-load-required`, successful normalized reads produce `capability.loaded` receipts containing only provider, stage,
capability id, content hash, and ordering (plus execution identity when the stage owns one). Raw event streams, prompts, file contents,
and absolute paths are not persisted. Missing evidence rejects the implementation result or review verdict and moves to the next
provider without a provider cooldown. This proves an observable load, not that the model followed the skill correctly; contract gates
and independent review remain authoritative.

`stagemesh capabilities --task TASK_ID --json` reads those bounded receipts through SQLite read-only mode and reports counts and the
latest status separately: `attached-unverified`, `loaded`, `invoked`, or `evidence-missing`. `stagemesh task-doctor` projects the same
registered/requested/observed distinction, and run-window JSON reports include the bounded projection for every attempted task. A
missing runtime database remains `history_available: false`;
the inspection command does not create one. `loaded` means an exact skill read was observed. `invoked` is reserved for an allowlisted
MCP tool call. Neither means that the capability was correctly applied.

A requested Codex MCP capability always requires at least one observed allowlisted tool call. StageMesh records
`capability.invoked` with the capability id, manifest hash, and tool names only. Raw arguments, results, prompts, paths, and event
streams are not persisted. Missing invocation evidence rejects the attempt without provider cooldown; an observed unallowlisted call
is a capability policy violation and the candidate is rejected.

## Not implemented

- No network or global installer, marketplace client, plugin hook runner, or arbitrary setup command.
- No provider-neutral MCP process sandbox, credential broker, HTTP/network transport, or MCP adapter outside Codex.
- No executable Claude plugin activation, marketplace installation, or translation to Codex/Grok.
- No ambient user-home discovery. Project registry state is read from the isolated checkout only.

Those boundaries keep an available plugin or server from silently becoming an authorized executable. Provider-specific adapters can
be added without changing the registry or contract model, but they must enforce declared permissions and produce independent
invocation evidence before StageMesh reports the capability as used.

Reference behavior: [Claude Code plugins](https://code.claude.com/docs/en/plugins),
[Claude plugin manifests](https://code.claude.com/docs/en/plugins-reference),
[Codex MCP configuration](https://developers.openai.com/codex/extend/mcp), and
[Serena client configuration](https://oraios.github.io/serena/02-usage/030_clients).
