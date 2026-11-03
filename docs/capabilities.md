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
usable, and invocation-observation states. It never downloads or installs a capability. StageMesh has three registry adapters:
read-only project `SKILL.md` attachment, skills-only import from a project-local Claude Code plugin, and an explicitly requested,
read-only Codex stdio MCP adapter. Executable Claude plugin components and MCP requests for Claude, Grok, Agy, or custom providers
remain inventory-only and fail closed.

The registry may set `"skill_evidence_mode": "observed-load-required"`. In that opt-in mode, StageMesh enables the selected provider's
JSON event stream and accepts an implementation result only when every selected skill was observed being read before the first recorded
mutation. The default is `attachment-required`, which proves verified prompt attachment but not provider loading.

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
      "enabled": true,
      "providers": ["codex", "claude", "grok", "agy"],
      "stages": ["IMPLEMENT"],
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
      "stages": ["IMPLEMENT"],
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

Dispatch requires all of the following: the id is registered, enabled, installed, compatible with the selected provider, enabled for
the stage, and supported by a StageMesh adapter. Sources must resolve inside the task checkout; symlink and `..` escapes are rejected.
A skill may request only `workspace-read`. The first MCP adapter requires exactly `workspace-read` and `subprocess`, and accepts only a
validated stdio manifest with no environment values, credentials, HTTP transport, setup command, or ambient user MCP configuration.
Capability attachment never expands `allowed_files`, provider authorization, or the worker's sandbox.

## Skills-only Claude plugin import

A `claude-plugin` source names a project-local plugin directory. StageMesh accepts the standard
`.claude-plugin/plugin.json` metadata file and one or more `skills/<name>/SKILL.md` components. It validates path containment and bounded
skill counts and sizes, then lists the skill files in the Claude implementation prompt through the same read-only adapter used for
standalone skills.

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

The current registry-backed skill, skills-only Claude plugin, and MCP adapters are implementation-stage only.

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

With `observed-load-required`, successful normalized reads produce `capability.loaded` receipts containing only provider, execution,
capability id, content hash, and ordering. Raw event streams, prompts, file contents, and absolute paths are not persisted. Missing
evidence rejects the result and moves to the next provider without a provider cooldown. This proves an observable load, not that the
model followed the skill correctly; contract gates and independent review remain authoritative.

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
